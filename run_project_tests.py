#!/usr/bin/env python3

# Copyright 2012-2016 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import typing
import itertools
import os
import subprocess
import shutil
import sys
import signal
from io import StringIO
from ast import literal_eval
from enum import Enum
import tempfile
from pathlib import Path, PurePath
from mesonbuild import build
from mesonbuild import environment
from mesonbuild import compilers
from mesonbuild import mesonlib
from mesonbuild import mlog
from mesonbuild import mtest
from mesonbuild.mesonlib import MachineChoice, stringlistify, Popen_safe
from mesonbuild.coredata import backendlist
import argparse
import json
import xml.etree.ElementTree as ET
import time
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, CancelledError
import re
from run_tests import get_fake_options, run_configure, get_meson_script
from run_tests import get_backend_commands, get_backend_args_for_dir, Backend
from run_tests import ensure_backend_detects_changes
from run_tests import guess_backend


class BuildStep(Enum):
    configure = 1
    build = 2
    test = 3
    install = 4
    clean = 5
    validate = 6


class TestResult:
    def __init__(self, msg, step, stdo, stde, mlog, conftime=0, buildtime=0, testtime=0):
        self.msg = msg
        self.step = step
        self.stdo = stdo
        self.stde = stde
        self.mlog = mlog
        self.conftime = conftime
        self.buildtime = buildtime
        self.testtime = testtime


class AutoDeletedDir:
    def __init__(self, d):
        self.dir = d

    def __enter__(self):
        os.makedirs(self.dir, exist_ok=True)
        return self.dir

    def __exit__(self, _type, value, traceback):
        # We don't use tempfile.TemporaryDirectory, but wrap the
        # deletion in the AutoDeletedDir class because
        # it fails on Windows due antivirus programs
        # holding files open.
        mesonlib.windows_proof_rmtree(self.dir)

failing_logs = []
print_debug = 'MESON_PRINT_TEST_OUTPUT' in os.environ
under_ci = 'CI' in os.environ
do_debug = under_ci or print_debug
no_meson_log_msg = 'No meson-log.txt found.'

system_compiler = None

class StopException(Exception):
    def __init__(self):
        super().__init__('Stopped by user')

stop = False
def stop_handler(signal, frame):
    global stop
    stop = True
signal.signal(signal.SIGINT, stop_handler)
signal.signal(signal.SIGTERM, stop_handler)

def setup_commands(optbackend):
    global do_debug, backend, backend_flags
    global compile_commands, clean_commands, test_commands, install_commands, uninstall_commands
    backend, backend_flags = guess_backend(optbackend, shutil.which('msbuild'))
    compile_commands, clean_commands, test_commands, install_commands, \
        uninstall_commands = get_backend_commands(backend, do_debug)

def get_relative_files_list_from_dir(fromdir: Path) -> typing.List[Path]:
    return [file.relative_to(fromdir) for file in fromdir.rglob('*') if file.is_file()]

def platform_fix_name(fname: str, compiler, env) -> str:
    # canonicalize compiler
    if (compiler in {'clang-cl', 'intel-cl'} or
       (env.machines.host.is_windows() and compiler == 'pgi')):
        canonical_compiler = 'msvc'
    else:
        canonical_compiler = compiler

    if '?lib' in fname:
        if env.machines.host.is_windows() and canonical_compiler == 'msvc':
            fname = re.sub(r'lib/\?lib(.*)\.', r'bin/\1.', fname)
            fname = re.sub(r'/\?lib/', r'/bin/', fname)
        elif env.machines.host.is_windows():
            fname = re.sub(r'lib/\?lib(.*)\.', r'bin/lib\1.', fname)
            fname = re.sub(r'\?lib(.*)\.dll$', r'lib\1.dll', fname)
            fname = re.sub(r'/\?lib/', r'/bin/', fname)
        elif env.machines.host.is_cygwin():
            fname = re.sub(r'lib/\?lib(.*)\.so$', r'bin/cyg\1.dll', fname)
            fname = re.sub(r'lib/\?lib(.*)\.', r'bin/cyg\1.', fname)
            fname = re.sub(r'\?lib(.*)\.dll$', r'cyg\1.dll', fname)
            fname = re.sub(r'/\?lib/', r'/bin/', fname)
        else:
            fname = re.sub(r'\?lib', 'lib', fname)

    if fname.endswith('?exe'):
        fname = fname[:-4]
        if env.machines.host.is_windows() or env.machines.host.is_cygwin():
            return fname + '.exe'

    if fname.startswith('?msvc:'):
        fname = fname[6:]
        if canonical_compiler != 'msvc':
            return None

    if fname.startswith('?gcc:'):
        fname = fname[5:]
        if canonical_compiler == 'msvc':
            return None

    if fname.startswith('?cygwin:'):
        fname = fname[8:]
        if not env.machines.host.is_cygwin():
            return None

    if fname.startswith('?!cygwin:'):
        fname = fname[9:]
        if env.machines.host.is_cygwin():
            return None

    if fname.endswith('?so'):
        if env.machines.host.is_windows() and canonical_compiler == 'msvc':
            fname = re.sub(r'lib/([^/]*)\?so$', r'bin/\1.dll', fname)
            fname = re.sub(r'/(?:lib|)([^/]*?)\?so$', r'/\1.dll', fname)
            return fname
        elif env.machines.host.is_windows():
            fname = re.sub(r'lib/([^/]*)\?so$', r'bin/\1.dll', fname)
            fname = re.sub(r'/([^/]*?)\?so$', r'/\1.dll', fname)
            return fname
        elif env.machines.host.is_cygwin():
            fname = re.sub(r'lib/([^/]*)\?so$', r'bin/\1.dll', fname)
            fname = re.sub(r'/lib([^/]*?)\?so$', r'/cyg\1.dll', fname)
            fname = re.sub(r'/([^/]*?)\?so$', r'/\1.dll', fname)
            return fname
        elif env.machines.host.is_darwin():
            return fname[:-3] + '.dylib'
        else:
            return fname[:-3] + '.so'

    if fname.endswith('?implib') or fname.endswith('?implibempty'):
        if env.machines.host.is_windows() and canonical_compiler == 'msvc':
            # only MSVC doesn't generate empty implibs
            if fname.endswith('?implibempty') and compiler == 'msvc':
                return None
            return re.sub(r'/(?:lib|)([^/]*?)\?implib(?:empty|)$', r'/\1.lib', fname)
        elif env.machines.host.is_windows() or env.machines.host.is_cygwin():
            return re.sub(r'\?implib(?:empty|)$', r'.dll.a', fname)
        else:
            return None

    return fname

def validate_install(srcdir: str, installdir: Path, compiler, env) -> str:
    # List of installed files
    info_file = Path(srcdir) / 'installed_files.txt'
    installdir = Path(installdir)
    # If this exists, the test does not install any other files
    noinst_file = Path('usr/no-installed-files')
    expected = {}  # type: typing.Dict[Path, bool]
    ret_msg = ''
    # Generate list of expected files
    if (installdir / noinst_file).is_file():
        expected[noinst_file] = False
    elif info_file.is_file():
        with info_file.open() as f:
            for line in f:
                line = platform_fix_name(line.strip(), compiler, env)
                if line:
                    expected[Path(line)] = False
    # Check if expected files were found
    for fname in expected:
        file_path = installdir / fname
        if file_path.is_file() or file_path.is_symlink():
            expected[fname] = True
    for (fname, found) in expected.items():
        if not found:
            ret_msg += 'Expected file {} missing.\n'.format(fname)
    # Check if there are any unexpected files
    found = get_relative_files_list_from_dir(installdir)
    for fname in found:
        if fname not in expected:
            ret_msg += 'Extra file {} found.\n'.format(fname)
    if ret_msg != '':
        ret_msg += '\nInstall dir contents:\n'
        for i in found:
            ret_msg += '  - {}'.format(i)
    return ret_msg

def log_text_file(logfile, testdir, stdo, stde):
    global stop, executor, futures
    logfile.write('%s\nstdout\n\n---\n' % testdir.as_posix())
    logfile.write(stdo)
    logfile.write('\n\n---\n\nstderr\n\n---\n')
    logfile.write(stde)
    logfile.write('\n\n---\n\n')
    if print_debug:
        try:
            print(stdo)
        except UnicodeError:
            sanitized_out = stdo.encode('ascii', errors='replace').decode()
            print(sanitized_out)
        try:
            print(stde, file=sys.stderr)
        except UnicodeError:
            sanitized_err = stde.encode('ascii', errors='replace').decode()
            print(sanitized_err, file=sys.stderr)
    if stop:
        print("Aborting..")
        for f in futures:
            f[2].cancel()
        executor.shutdown()
        raise StopException()


def bold(text):
    return mlog.bold(text).get_text(mlog.colorize_console)


def green(text):
    return mlog.green(text).get_text(mlog.colorize_console)


def red(text):
    return mlog.red(text).get_text(mlog.colorize_console)


def yellow(text):
    return mlog.yellow(text).get_text(mlog.colorize_console)


def run_test_inprocess(testdir):
    old_stdout = sys.stdout
    sys.stdout = mystdout = StringIO()
    old_stderr = sys.stderr
    sys.stderr = mystderr = StringIO()
    old_cwd = os.getcwd()
    os.chdir(testdir)
    test_log_fname = Path('meson-logs', 'testlog.txt')
    try:
        returncode_test = mtest.run_with_args(['--no-rebuild'])
        if test_log_fname.exists():
            test_log = test_log_fname.open(errors='ignore').read()
        else:
            test_log = ''
        returncode_benchmark = mtest.run_with_args(['--no-rebuild', '--benchmark', '--logbase', 'benchmarklog'])
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        os.chdir(old_cwd)
    return max(returncode_test, returncode_benchmark), mystdout.getvalue(), mystderr.getvalue(), test_log

def parse_test_args(testdir):
    args = []
    try:
        with open(os.path.join(testdir, 'test_args.txt'), 'r') as f:
            content = f.read()
            try:
                args = literal_eval(content)
            except Exception:
                raise Exception('Malformed test_args file.')
            args = stringlistify(args)
    except FileNotFoundError:
        pass
    return args

# Build directory name must be the same so Ccache works over
# consecutive invocations.
def create_deterministic_builddir(src_dir):
    import hashlib
    rel_dirname = 'b ' + hashlib.sha256(src_dir.encode(errors='ignore')).hexdigest()[0:10]
    os.mkdir(rel_dirname)
    abs_pathname = os.path.join(os.getcwd(), rel_dirname)
    return abs_pathname

def run_test(skipped, testdir, extra_args, compiler, backend, flags, commands, should_fail):
    if skipped:
        return None
    with AutoDeletedDir(create_deterministic_builddir(testdir)) as build_dir:
        with AutoDeletedDir(tempfile.mkdtemp(prefix='i ', dir=os.getcwd())) as install_dir:
            try:
                return _run_test(testdir, build_dir, install_dir, extra_args, compiler, backend, flags, commands, should_fail)
            finally:
                mlog.shutdown() # Close the log file because otherwise Windows wets itself.

def pass_prefix_to_test(dirname):
    if '39 prefix absolute' in dirname:
        return False
    return True

def pass_libdir_to_test(dirname):
    if '8 install' in dirname:
        return False
    if '38 libdir must be inside prefix' in dirname:
        return False
    if '195 install_mode' in dirname:
        return False
    return True

def _run_test(testdir, test_build_dir, install_dir, extra_args, compiler, backend, flags, commands, should_fail):
    compile_commands, clean_commands, install_commands, uninstall_commands = commands
    test_args = parse_test_args(testdir)
    gen_start = time.time()
    setup_env = None
    # Configure in-process
    if pass_prefix_to_test(testdir):
        gen_args = ['--prefix', '/usr']
    else:
        gen_args = []
    if pass_libdir_to_test(testdir):
        gen_args += ['--libdir', 'lib']
    gen_args += [testdir, test_build_dir] + flags + test_args + extra_args
    nativefile = os.path.join(testdir, 'nativefile.ini')
    if os.path.exists(nativefile):
        gen_args.extend(['--native-file', nativefile])
    crossfile = os.path.join(testdir, 'crossfile.ini')
    if os.path.exists(crossfile):
        gen_args.extend(['--cross-file', crossfile])
    setup_env_file = os.path.join(testdir, 'setup_env.json')
    if os.path.exists(setup_env_file):
        setup_env = os.environ.copy()
        with open(setup_env_file, 'r') as fp:
            data = json.load(fp)
            for key, val in data.items():
                val = val.replace('@ROOT@', os.path.abspath(testdir))
                setup_env[key] = val
    (returncode, stdo, stde) = run_configure(gen_args, env=setup_env)
    try:
        logfile = Path(test_build_dir, 'meson-logs', 'meson-log.txt')
        mesonlog = logfile.open(errors='ignore', encoding='utf-8').read()
    except Exception:
        mesonlog = no_meson_log_msg
    gen_time = time.time() - gen_start
    if should_fail == 'meson':
        if returncode == 1:
            return TestResult('', BuildStep.configure, stdo, stde, mesonlog, gen_time)
        elif returncode != 0:
            return TestResult('Test exited with unexpected status {}'.format(returncode), BuildStep.configure, stdo, stde, mesonlog, gen_time)
        else:
            return TestResult('Test that should have failed succeeded', BuildStep.configure, stdo, stde, mesonlog, gen_time)
    if returncode != 0:
        return TestResult('Generating the build system failed.', BuildStep.configure, stdo, stde, mesonlog, gen_time)
    builddata = build.load(test_build_dir)
    # Touch the meson.build file to force a regenerate so we can test that
    # regeneration works before a build is run.
    ensure_backend_detects_changes(backend)
    os.utime(os.path.join(testdir, 'meson.build'))
    # Build with subprocess
    dir_args = get_backend_args_for_dir(backend, test_build_dir)
    build_start = time.time()
    pc, o, e = Popen_safe(compile_commands + dir_args, cwd=test_build_dir)
    build_time = time.time() - build_start
    stdo += o
    stde += e
    if should_fail == 'build':
        if pc.returncode != 0:
            return TestResult('', BuildStep.build, stdo, stde, mesonlog, gen_time)
        return TestResult('Test that should have failed to build succeeded', BuildStep.build, stdo, stde, mesonlog, gen_time)
    if pc.returncode != 0:
        return TestResult('Compiling source code failed.', BuildStep.build, stdo, stde, mesonlog, gen_time, build_time)
    # Touch the meson.build file to force a regenerate so we can test that
    # regeneration works after a build is complete.
    ensure_backend_detects_changes(backend)
    os.utime(os.path.join(testdir, 'meson.build'))
    test_start = time.time()
    # Test in-process
    (returncode, tstdo, tstde, test_log) = run_test_inprocess(test_build_dir)
    test_time = time.time() - test_start
    stdo += tstdo
    stde += tstde
    mesonlog += test_log
    if should_fail == 'test':
        if returncode != 0:
            return TestResult('', BuildStep.test, stdo, stde, mesonlog, gen_time)
        return TestResult('Test that should have failed to run unit tests succeeded', BuildStep.test, stdo, stde, mesonlog, gen_time)
    if returncode != 0:
        return TestResult('Running unit tests failed.', BuildStep.test, stdo, stde, mesonlog, gen_time, build_time, test_time)
    # Do installation, if the backend supports it
    if install_commands:
        env = os.environ.copy()
        env['DESTDIR'] = install_dir
        # Install with subprocess
        pi, o, e = Popen_safe(install_commands, cwd=test_build_dir, env=env)
        stdo += o
        stde += e
        if pi.returncode != 0:
            return TestResult('Running install failed.', BuildStep.install, stdo, stde, mesonlog, gen_time, build_time, test_time)
    # Clean with subprocess
    env = os.environ.copy()
    pi, o, e = Popen_safe(clean_commands + dir_args, cwd=test_build_dir, env=env)
    stdo += o
    stde += e
    if pi.returncode != 0:
        return TestResult('Running clean failed.', BuildStep.clean, stdo, stde, mesonlog, gen_time, build_time, test_time)
    if not install_commands:
        return TestResult('', BuildStep.install, '', '', mesonlog, gen_time, build_time, test_time)
    return TestResult(validate_install(testdir, install_dir, compiler, builddata.environment),
                      BuildStep.validate, stdo, stde, mesonlog, gen_time, build_time, test_time)

def gather_tests(testdir: Path) -> typing.List[Path]:
    test_names = [t.name for t in testdir.glob('*') if t.is_dir()]
    test_names = [t for t in test_names if not t.startswith('.')] # Filter non-tests files (dot files, etc)
    test_nums = [(int(t.split()[0]), t) for t in test_names]
    test_nums.sort()
    tests = [testdir / t[1] for t in test_nums]
    return tests

def have_d_compiler():
    if shutil.which("ldc2"):
        return True
    elif shutil.which("ldc"):
        return True
    elif shutil.which("gdc"):
        return True
    elif shutil.which("dmd"):
        # The Windows installer sometimes produces a DMD install
        # that exists but segfaults every time the compiler is run.
        # Don't know why. Don't know how to fix. Skip in this case.
        cp = subprocess.run(['dmd', '--version'],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
        if cp.stdout == b'':
            return False
        return True
    return False

def have_objc_compiler():
    with AutoDeletedDir(tempfile.mkdtemp(prefix='b ', dir='.')) as build_dir:
        env = environment.Environment(None, build_dir, get_fake_options('/'))
        try:
            objc_comp = env.detect_objc_compiler(MachineChoice.HOST)
        except mesonlib.MesonException:
            return False
        if not objc_comp:
            return False
        env.coredata.process_new_compiler('objc', objc_comp, env)
        try:
            objc_comp.sanity_check(env.get_scratch_dir(), env)
        except mesonlib.MesonException:
            return False
    return True

def have_objcpp_compiler():
    with AutoDeletedDir(tempfile.mkdtemp(prefix='b ', dir='.')) as build_dir:
        env = environment.Environment(None, build_dir, get_fake_options('/'))
        try:
            objcpp_comp = env.detect_objcpp_compiler(MachineChoice.HOST)
        except mesonlib.MesonException:
            return False
        if not objcpp_comp:
            return False
        env.coredata.process_new_compiler('objcpp', objcpp_comp, env)
        try:
            objcpp_comp.sanity_check(env.get_scratch_dir(), env)
        except mesonlib.MesonException:
            return False
    return True

def have_java():
    if shutil.which('javac') and shutil.which('java'):
        return True
    return False

def skippable(suite, test):
    if not under_ci:
        return True

    if not suite.endswith('frameworks'):
        return True

    # gtk-doc test may be skipped, pending upstream fixes for spaces in
    # filenames landing in the distro used for CI
    if test.endswith('10 gtk-doc'):
        return True

    # NetCDF is not in the CI image
    if test.endswith('netcdf'):
        return True

    # MSVC doesn't link with GFortran
    if test.endswith('14 fortran links c'):
        return True

    # Blocks are not supported on all compilers
    if test.endswith('29 blocks'):
        return True

    # No frameworks test should be skipped on linux CI, as we expect all
    # prerequisites to be installed
    if mesonlib.is_linux():
        return False

    # Boost test should only be skipped for windows CI build matrix entries
    # which don't define BOOST_ROOT
    if test.endswith('1 boost'):
        if mesonlib.is_windows():
            return 'BOOST_ROOT' not in os.environ
        return False

    # Qt is provided on macOS by Homebrew
    if test.endswith('4 qt') and mesonlib.is_osx():
        return False

    # Other framework tests are allowed to be skipped on other platforms
    return True

def skip_csharp(backend):
    if backend is not Backend.ninja:
        return True
    if not shutil.which('resgen'):
        return True
    if shutil.which('mcs'):
        return False
    if shutil.which('csc'):
        # Only support VS2017 for now. Earlier versions fail
        # under CI in mysterious ways.
        try:
            stdo = subprocess.check_output(['csc', '/version'])
        except subprocess.CalledProcessError:
            return True
        # Having incrementing version numbers would be too easy.
        # Microsoft reset the versioning back to 1.0 (from 4.x)
        # when they got the Roslyn based compiler. Thus there
        # is NO WAY to reliably do version number comparisons.
        # Only support the version that ships with VS2017.
        return not stdo.startswith(b'2.')
    return True

# In Azure some setups have a broken rustc that will error out
# on all compilation attempts.

def has_broken_rustc() -> bool:
    dirname = 'brokenrusttest'
    if os.path.exists(dirname):
        mesonlib.windows_proof_rmtree(dirname)
    os.mkdir(dirname)
    open(dirname + '/sanity.rs', 'w').write('''fn main() {
}
''')
    pc = subprocess.run(['rustc', '-o', 'sanity.exe', 'sanity.rs'],
                        cwd=dirname,
                        stdout = subprocess.DEVNULL,
                        stderr = subprocess.DEVNULL)
    mesonlib.windows_proof_rmtree(dirname)
    return pc.returncode != 0

def should_skip_rust() -> bool:
    if not shutil.which('rustc'):
        return True
    if backend is not Backend.ninja:
        return True
    if mesonlib.is_windows():
        if has_broken_rustc():
            return True
    return False

def detect_tests_to_run(only: typing.List[str]) -> typing.List[typing.Tuple[str, typing.List[Path], bool]]:
    """
    Parameters
    ----------
    only: list of str, optional
        specify names of tests to run

    Returns
    -------
    gathered_tests: list of tuple of str, list of pathlib.Path, bool
        tests to run
    """

    skip_fortran = not(shutil.which('gfortran') or shutil.which('flang') or
                       shutil.which('pgfortran') or shutil.which('ifort'))

    # Name, subdirectory, skip condition.
    all_tests = [
        ('cmake', 'cmake', not shutil.which('cmake') or (os.environ.get('compiler') == 'msvc2015' and under_ci)),
        ('common', 'common', False),
        ('warning-meson', 'warning', False),
        ('failing-meson', 'failing', False),
        ('failing-build', 'failing build', False),
        ('failing-test',  'failing test', False),
        ('kconfig', 'kconfig', False),

        ('platform-osx', 'osx', not mesonlib.is_osx()),
        ('platform-windows', 'windows', not mesonlib.is_windows() and not mesonlib.is_cygwin()),
        ('platform-linux', 'linuxlike', mesonlib.is_osx() or mesonlib.is_windows()),

        ('java', 'java', backend is not Backend.ninja or mesonlib.is_osx() or not have_java()),
        ('C#', 'csharp', skip_csharp(backend)),
        ('vala', 'vala', backend is not Backend.ninja or not shutil.which('valac')),
        ('rust', 'rust', should_skip_rust()),
        ('d', 'd', backend is not Backend.ninja or not have_d_compiler()),
        ('objective c', 'objc', backend not in (Backend.ninja, Backend.xcode) or not have_objc_compiler()),
        ('objective c++', 'objcpp', backend not in (Backend.ninja, Backend.xcode) or not have_objcpp_compiler()),
        ('fortran', 'fortran', skip_fortran or backend != Backend.ninja),
        ('swift', 'swift', backend not in (Backend.ninja, Backend.xcode) or not shutil.which('swiftc')),
        ('cuda', 'cuda', backend not in (Backend.ninja, Backend.xcode) or not shutil.which('nvcc')),
        ('python3', 'python3', backend is not Backend.ninja),
        ('python', 'python', backend is not Backend.ninja),
        ('fpga', 'fpga', shutil.which('yosys') is None),
        ('frameworks', 'frameworks', False),
        ('nasm', 'nasm', False),
        ('wasm', 'wasm', shutil.which('emcc') is None or backend is not Backend.ninja),
    ]

    if only:
        names = [t[0] for t in all_tests]
        ind = [names.index(o) for o in only]
        all_tests = [all_tests[i] for i in ind]
    gathered_tests = [(name, gather_tests(Path('test cases', subdir)), skip) for name, subdir, skip in all_tests]
    return gathered_tests

def run_tests(all_tests, log_name_base, failfast: bool, extra_args):
    global logfile
    txtname = log_name_base + '.txt'
    with open(txtname, 'w', encoding='utf-8', errors='ignore') as lf:
        logfile = lf
        return _run_tests(all_tests, log_name_base, failfast, extra_args)

def _run_tests(all_tests, log_name_base, failfast: bool, extra_args):
    global stop, executor, futures, system_compiler
    xmlname = log_name_base + '.xml'
    junit_root = ET.Element('testsuites')
    conf_time = 0
    build_time = 0
    test_time = 0
    passing_tests = 0
    failing_tests = 0
    skipped_tests = 0
    commands = (compile_commands, clean_commands, install_commands, uninstall_commands)

    try:
        # This fails in some CI environments for unknown reasons.
        num_workers = multiprocessing.cpu_count()
    except Exception as e:
        print('Could not determine number of CPUs due to the following reason:' + str(e))
        print('Defaulting to using only one process')
        num_workers = 1
    # Due to Ninja deficiency, almost 50% of build time
    # is spent waiting. Do something useful instead.
    #
    # Remove this once the following issue has been resolved:
    # https://github.com/mesonbuild/meson/pull/2082
    if not mesonlib.is_windows():  # twice as fast on Windows by *not* multiplying by 2.
        num_workers *= 2
    executor = ProcessPoolExecutor(max_workers=num_workers)

    for name, test_cases, skipped in all_tests:
        current_suite = ET.SubElement(junit_root, 'testsuite', {'name': name, 'tests': str(len(test_cases))})
        print()
        if skipped:
            print(bold('Not running %s tests.' % name))
        else:
            print(bold('Running %s tests.' % name))
        print()
        futures = []
        for t in test_cases:
            # Jenkins screws us over by automatically sorting test cases by name
            # and getting it wrong by not doing logical number sorting.
            (testnum, testbase) = t.name.split(' ', 1)
            testname = '%.3d %s' % (int(testnum), testbase)
            should_fail = False
            suite_args = []
            if name.startswith('failing'):
                should_fail = name.split('failing-')[1]
            if name.startswith('warning'):
                suite_args = ['--fatal-meson-warnings']
                should_fail = name.split('warning-')[1]
            result = executor.submit(run_test, skipped, t.as_posix(), extra_args + suite_args,
                                     system_compiler, backend, backend_flags, commands, should_fail)
            futures.append((testname, t, result))
        for (testname, t, result) in futures:
            sys.stdout.flush()
            try:
                result = result.result()
            except CancelledError:
                continue
            if (result is None) or (('MESON_SKIP_TEST' in result.stdo) and (skippable(name, t.as_posix()))):
                print(yellow('Skipping:'), t.as_posix())
                current_test = ET.SubElement(current_suite, 'testcase', {'name': testname,
                                                                         'classname': name})
                ET.SubElement(current_test, 'skipped', {})
                skipped_tests += 1
            else:
                without_install = "" if len(install_commands) > 0 else " (without install)"
                if result.msg != '':
                    print(red('Failed test{} during {}: {!r}'.format(without_install, result.step.name, t.as_posix())))
                    print('Reason:', result.msg)
                    failing_tests += 1
                    if result.step == BuildStep.configure and result.mlog != no_meson_log_msg:
                        # For configure failures, instead of printing stdout,
                        # print the meson log if available since it's a superset
                        # of stdout and often has very useful information.
                        failing_logs.append(result.mlog)
                    elif under_ci:
                        # Always print the complete meson log when running in
                        # a CI. This helps debugging issues that only occur in
                        # a hard to reproduce environment
                        failing_logs.append(result.mlog)
                        failing_logs.append(result.stdo)
                    else:
                        failing_logs.append(result.stdo)
                    failing_logs.append(result.stde)
                    if failfast:
                        print("Cancelling the rest of the tests")
                        for (_, _, res) in futures:
                            res.cancel()
                else:
                    print('Succeeded test%s: %s' % (without_install, t.as_posix()))
                    passing_tests += 1
                conf_time += result.conftime
                build_time += result.buildtime
                test_time += result.testtime
                total_time = conf_time + build_time + test_time
                log_text_file(logfile, t, result.stdo, result.stde)
                current_test = ET.SubElement(current_suite, 'testcase', {'name': testname,
                                                                         'classname': name,
                                                                         'time': '%.3f' % total_time})
                if result.msg != '':
                    ET.SubElement(current_test, 'failure', {'message': result.msg})
                stdoel = ET.SubElement(current_test, 'system-out')
                stdoel.text = result.stdo
                stdeel = ET.SubElement(current_test, 'system-err')
                stdeel.text = result.stde

            if failfast and failing_tests > 0:
                break

    print("\nTotal configuration time: %.2fs" % conf_time)
    print("Total build time: %.2fs" % build_time)
    print("Total test time: %.2fs" % test_time)
    ET.ElementTree(element=junit_root).write(xmlname, xml_declaration=True, encoding='UTF-8')
    return passing_tests, failing_tests, skipped_tests

def check_file(file: Path):
    lines = file.read_bytes().split(b'\n')
    tabdetector = re.compile(br' *\t')
    for i, line in enumerate(lines):
        if re.match(tabdetector, line):
            raise SystemExit("File {} contains a tab indent on line {:d}. Only spaces are permitted.".format(file, i + 1))
        if line.endswith(b'\r'):
            raise SystemExit("File {} contains DOS line ending on line {:d}. Only unix-style line endings are permitted.".format(file, i + 1))

def check_format():
    check_suffixes = {'.c',
                      '.cpp',
                      '.cxx',
                      '.cc',
                      '.rs',
                      '.f90',
                      '.vala',
                      '.d',
                      '.s',
                      '.m',
                      '.mm',
                      '.asm',
                      '.java',
                      '.txt',
                      '.py',
                      '.swift',
                      '.build',
                      '.md',
                      }
    for (root, _, filenames) in os.walk('.'):
        if '.dub' in root: # external deps are here
            continue
        if '.pytest_cache' in root:
            continue
        if 'meson-logs' in root or 'meson-private' in root:
            continue
        if '.eggs' in root or '_cache' in root:  # e.g. .mypy_cache
            continue
        for fname in filenames:
            file = Path(fname)
            if file.suffix.lower() in check_suffixes:
                if file.name in ('sitemap.txt', 'meson-test-run.txt'):
                    continue
                check_file(root / file)

def check_meson_commands_work():
    global backend, compile_commands, test_commands, install_commands
    testdir = PurePath('test cases', 'common', '1 trivial').as_posix()
    meson_commands = mesonlib.python_command + [get_meson_script()]
    with AutoDeletedDir(tempfile.mkdtemp(prefix='b ', dir='.')) as build_dir:
        print('Checking that configuring works...')
        gen_cmd = meson_commands + [testdir, build_dir] + backend_flags
        pc, o, e = Popen_safe(gen_cmd)
        if pc.returncode != 0:
            raise RuntimeError('Failed to configure {!r}:\n{}\n{}'.format(testdir, e, o))
        print('Checking that building works...')
        dir_args = get_backend_args_for_dir(backend, build_dir)
        pc, o, e = Popen_safe(compile_commands + dir_args, cwd=build_dir)
        if pc.returncode != 0:
            raise RuntimeError('Failed to build {!r}:\n{}\n{}'.format(testdir, e, o))
        print('Checking that testing works...')
        pc, o, e = Popen_safe(test_commands, cwd=build_dir)
        if pc.returncode != 0:
            raise RuntimeError('Failed to test {!r}:\n{}\n{}'.format(testdir, e, o))
        if install_commands:
            print('Checking that installing works...')
            pc, o, e = Popen_safe(install_commands, cwd=build_dir)
            if pc.returncode != 0:
                raise RuntimeError('Failed to install {!r}:\n{}\n{}'.format(testdir, e, o))


def detect_system_compiler():
    global system_compiler

    with AutoDeletedDir(tempfile.mkdtemp(prefix='b ', dir='.')) as build_dir:
        env = environment.Environment(None, build_dir, get_fake_options('/'))
        print()
        for lang in sorted(compilers.all_languages):
            try:
                comp = env.compiler_from_language(lang, MachineChoice.HOST)
                details = '%s %s' % (' '.join(comp.get_exelist()), comp.get_version_string())
            except mesonlib.MesonException:
                comp = None
                details = 'not found'
            print('%-7s: %s' % (lang, details))

            # note C compiler for later use by platform_fix_name()
            if lang == 'c':
                if comp:
                    system_compiler = comp.get_id()
                else:
                    raise RuntimeError("Could not find C compiler.")
        print()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Run the test suite of Meson.")
    parser.add_argument('extra_args', nargs='*',
                        help='arguments that are passed directly to Meson (remember to have -- before these).')
    parser.add_argument('--backend', default=None, dest='backend',
                        choices=backendlist)
    parser.add_argument('--failfast', action='store_true',
                        help='Stop running if test case fails')
    parser.add_argument('--no-unittests', action='store_true',
                        help='Not used, only here to simplify run_tests.py')
    parser.add_argument('--only', help='name of test(s) to run', nargs='+')
    options = parser.parse_args()
    setup_commands(options.backend)

    detect_system_compiler()
    script_dir = os.path.split(__file__)[0]
    if script_dir != '':
        os.chdir(script_dir)
    check_format()
    check_meson_commands_work()
    try:
        all_tests = detect_tests_to_run(options.only)
        (passing_tests, failing_tests, skipped_tests) = run_tests(all_tests, 'meson-test-run', options.failfast, options.extra_args)
    except StopException:
        pass
    print('\nTotal passed tests:', green(str(passing_tests)))
    print('Total failed tests:', red(str(failing_tests)))
    print('Total skipped tests:', yellow(str(skipped_tests)))
    if failing_tests > 0:
        print('\nMesonlogs of failing tests\n')
        for l in failing_logs:
            try:
                print(l, '\n')
            except UnicodeError:
                print(l.encode('ascii', errors='replace').decode(), '\n')
    for name, dirs, _ in all_tests:
        dir_names = (x.name for x in dirs)
        for k, g in itertools.groupby(dir_names, key=lambda x: x.split()[0]):
            tests = list(g)
            if len(tests) != 1:
                print('WARNING: The %s suite contains duplicate "%s" tests: "%s"' % (name, k, '", "'.join(tests)))
    raise SystemExit(failing_tests)
