#!/usr/bin/env python3

# Copyright 2012-2015 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from glob import glob
import os, subprocess, shutil, sys, platform, signal
from io import StringIO
import sys
import environment
import mesonlib
import mlog
import meson, meson_test
import argparse
import xml.etree.ElementTree as ET
import time

from meson import backendlist

class TestResult:
    def __init__(self, msg, stdo, stde, conftime=0, buildtime=0, testtime=0):
        self.msg = msg
        self.stdo = stdo
        self.stde = stde
        self.conftime = conftime
        self.buildtime = buildtime
        self.testtime = testtime

passing_tests = 0
failing_tests = 0
skipped_tests = 0
print_debug = 'MESON_PRINT_TEST_OUTPUT' in os.environ

test_build_dir = 'work area'
install_dir = os.path.join(os.path.split(os.path.abspath(__file__))[0], 'install dir')
meson_command = './meson.py'

class StopException(Exception):
    def __init__(self):
        super().__init__('Stopped by user')

stop = False
def stop_handler(signal, frame):
    global stop
    stop = True
signal.signal(signal.SIGINT, stop_handler)
signal.signal(signal.SIGTERM, stop_handler)

#unity_flags = ['--unity']
unity_flags = []

backend_flags = None
compile_commands = None
test_commands = None
install_commands = None

def setup_commands(backend):
    global backend_flags, compile_commands, test_commands, install_commands
    msbuild_exe = shutil.which('msbuild')
    if backend == 'vs2010' or (backend is None and msbuild_exe is not None):
        backend_flags = ['--backend=vs2010']
        compile_commands = ['msbuild']
        test_commands = ['msbuild', 'RUN_TESTS.vcxproj']
        install_commands = []
    elif backend == 'xcode' or (backend is None and mesonlib.is_osx()):
        backend_flags = ['--backend=xcode']
        compile_commands = ['xcodebuild']
        test_commands = ['xcodebuild', '-target', 'RUN_TESTS']
        install_commands = []
    else:
        backend_flags = []
        ninja_command = environment.detect_ninja()
        if ninja_command is None:
            raise RuntimeError('Could not find Ninja executable.')
        if print_debug:
            compile_commands = [ninja_command, '-v']
        else:
            compile_commands = [ninja_command]
        test_commands = [ninja_command, 'test']
        install_commands = [ninja_command, 'install']

def platform_fix_filename(fname):
    if platform.system() == 'Darwin':
        if fname.endswith('.so'):
            return fname[:-2] + 'dylib'
        return fname.replace('.so.', '.dylib.')
    elif platform.system() == 'Windows':
        if fname.endswith('.so'):
            (p, f) = os.path.split(fname)
            f = f[3:-2] + 'dll'
            return os.path.join(p, f)
        if fname.endswith('.a'):
            return fname[:-1] + 'lib'
    return fname

def validate_install(srcdir, installdir):
    if platform.system() == 'Windows':
        # Don't really know how Windows installs should work
        # so skip.
        return ''
    info_file = os.path.join(srcdir, 'installed_files.txt')
    expected = {}
    found = {}
    if os.path.exists(info_file):
        for line in open(info_file):
            expected[platform_fix_filename(line.strip())] = True
    for root, _, files in os.walk(installdir):
        for fname in files:
            found_name = os.path.join(root, fname)[len(installdir)+1:]
            found[found_name] = True
    expected = set(expected)
    found = set(found)
    missing = expected - found
    for fname in missing:
        return 'Expected file %s missing.' % fname
    extra = found - expected
    for fname in extra:
        return 'Found extra file %s.' % fname
    return ''

def log_text_file(logfile, testdir, msg, stdo, stde):
    global passing_tests, failing_tests, stop
    if msg != '':
        print('Fail:', msg)
        failing_tests += 1
    else:
        print('Success')
        passing_tests += 1
    logfile.write('%s\nstdout\n\n---\n' % testdir)
    logfile.write(stdo)
    logfile.write('\n\n---\n\nstderr\n\n---\n')
    logfile.write(stde)
    logfile.write('\n\n---\n\n')
    if print_debug:
        print(stdo)
        print(stde, file=sys.stderr)
    if stop:
        raise StopException()

def run_configure_inprocess(commandlist):
    old_stdout = sys.stdout
    sys.stdout = mystdout = StringIO()
    old_stderr = sys.stderr
    sys.stderr = mystderr = StringIO()
    returncode = meson.run(commandlist)
    sys.stdout = old_stdout
    sys.stderr = old_stderr
    return (returncode, mystdout.getvalue(), mystderr.getvalue())

def run_test_inprocess(testdir):
    old_stdout = sys.stdout
    sys.stdout = mystdout = StringIO()
    old_stderr = sys.stderr
    sys.stderr = mystderr = StringIO()
    old_cwd = os.getcwd()
    os.chdir(testdir)
    returncode = meson_test.run(['meson-private/meson_test_setup.dat'])
    sys.stdout = old_stdout
    sys.stderr = old_stderr
    os.chdir(old_cwd)
    return (returncode, mystdout.getvalue(), mystderr.getvalue())


def run_test(testdir, should_succeed):
    global compile_commands
    mlog.shutdown() # Close the log file because otherwise Windows wets itself.
    shutil.rmtree(test_build_dir)
    shutil.rmtree(install_dir)
    os.mkdir(test_build_dir)
    os.mkdir(install_dir)
    print('Running test: ' + testdir)
    gen_start = time.time()
    gen_command = [meson_command, '--prefix', '/usr', '--libdir', 'lib', testdir, test_build_dir]\
        + unity_flags + backend_flags
    (returncode, stdo, stde) = run_configure_inprocess(gen_command)
    gen_time = time.time() - gen_start
    if not should_succeed:
        if returncode != 0:
            return TestResult('', stdo, stde, gen_time)
        return TestResult('Test that should have failed succeeded', stdo, stde, gen_time)
    if returncode != 0:
        return TestResult('Generating the build system failed.', stdo, stde, gen_time)
    if 'msbuild' in compile_commands[0]:
        sln_name = glob(os.path.join(test_build_dir, '*.sln'))[0]
        comp = compile_commands + [os.path.split(sln_name)[-1]]
    else:
        comp = compile_commands
    build_start = time.time()
    pc = subprocess.Popen(comp, cwd=test_build_dir,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (o, e) = pc.communicate()
    build_time = time.time() - build_start
    stdo += o.decode('utf-8')
    stde += e.decode('utf-8')
    if pc.returncode != 0:
        return TestResult('Compiling source code failed.', stdo, stde, gen_time, build_time)
    test_start = time.time()
    # Note that we don't test that running e.g. 'ninja test' actually
    # works. One hopes that this is a common enough happening that
    # it is picked up immediately on development.
    (returncode, tstdo, tstde) = run_test_inprocess(test_build_dir)
    test_time = time.time() - test_start
    stdo += tstdo
    stde += tstde
    if returncode != 0:
        return TestResult('Running unit tests failed.', stdo, stde, gen_time, build_time, test_time)
    if len(install_commands) == 0:
        print("Skipping install test")
        return TestResult('', '', '', gen_time, build_time, test_time)
    else:
        env = os.environ.copy()
        env['DESTDIR'] = install_dir
        pi = subprocess.Popen(install_commands, cwd=test_build_dir, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (o, e) = pi.communicate()
        stdo += o.decode('utf-8')
        stde += e.decode('utf-8')
        if pi.returncode != 0:
            return TestResult('Running install failed.', stdo, stde, gen_time, build_time, test_time)
        return TestResult(validate_install(testdir, install_dir), stdo, stde, gen_time, build_time, test_time)

def gather_tests(testdir):
    tests = [t.replace('\\', '/').split('/', 2)[2] for t in glob(os.path.join(testdir, '*'))]
    testlist = [(int(t.split()[0]), t) for t in tests]
    testlist.sort()
    tests = [os.path.join(testdir, t[1]) for t in testlist]
    return tests

def detect_tests_to_run():
    all_tests = []
    all_tests.append(('common', gather_tests('test cases/common'), False))
    all_tests.append(('failing', gather_tests('test cases/failing'), False))
    all_tests.append(('prebuilt object', gather_tests('test cases/prebuilt object'), False))

    all_tests.append(('platform-osx', gather_tests('test cases/osx'), False if mesonlib.is_osx() else True))
    all_tests.append(('platform-windows', gather_tests('test cases/windows'), False if mesonlib.is_windows() else True))
    all_tests.append(('platform-linux', gather_tests('test cases/linuxlike'), False if not (mesonlib.is_osx() or mesonlib.is_windows()) else True))
    all_tests.append(('framework', gather_tests('test cases/frameworks'), False if not mesonlib.is_osx() and not mesonlib.is_windows() else True))
    all_tests.append(('java', gather_tests('test cases/java'), False if not mesonlib.is_osx() and shutil.which('javac') else True))
    all_tests.append(('C#', gather_tests('test cases/csharp'), False if shutil.which('mcs') else True))
    all_tests.append(('vala', gather_tests('test cases/vala'), False if shutil.which('valac') else True))
    all_tests.append(('rust', gather_tests('test cases/rust'), False if shutil.which('rustc') else True))
    all_tests.append(('objective c', gather_tests('test cases/objc'), False if not mesonlib.is_windows() else True))
    all_tests.append(('fortran', gather_tests('test cases/fortran'), False if shutil.which('gfortran') else True))
    return all_tests

def run_tests():
    all_tests = detect_tests_to_run()
    logfile = open('meson-test-run.txt', 'w', encoding="utf_8")
    junit_root = ET.Element('testsuites')
    conf_time = 0
    build_time = 0
    test_time = 0
    try:
        os.mkdir(test_build_dir)
    except OSError:
        pass
    try:
        os.mkdir(install_dir)
    except OSError:
        pass

    for name, test_cases, skipped in all_tests:
        current_suite = ET.SubElement(junit_root, 'testsuite', {'name' : name, 'tests' : str(len(test_cases))})
        if skipped:
            print('\nNot running %s tests.\n' % name)
        else:
            print('\nRunning %s tests.\n' % name)
        for t in test_cases:
            # Jenkins screws us over by automatically sorting test cases by name
            # and getting it wrong by not doing logical number sorting.
            (testnum, testbase) = os.path.split(t)[-1].split(' ', 1)
            testname = '%.3d %s' % (int(testnum), testbase)
            if skipped:
                current_test = ET.SubElement(current_suite, 'testcase', {'name' : testname,
                                                                         'classname' : name})
                ET.SubElement(current_test, 'skipped', {})
                global skipped_tests
                skipped_tests += 1
            else:
                ts = time.time()
                result = run_test(t, name != 'failing')
                te = time.time()
                conf_time += result.conftime
                build_time += result.buildtime
                test_time += result.testtime
                log_text_file(logfile, t, result.msg, result.stdo, result.stde)
                current_test = ET.SubElement(current_suite, 'testcase', {'name' : testname,
                                                                         'classname' : name,
                                                                         'time' : '%.3f' % (te - ts)})
                if result.msg != '':
                    ET.SubElement(current_test, 'failure', {'message' : result.msg})
                stdoel = ET.SubElement(current_test, 'system-out')
                stdoel.text = result.stdo
                stdeel = ET.SubElement(current_test, 'system-err')
                stdeel.text = result.stde
    print("\nTotal configuration time: %.2fs" % conf_time)
    print("Total build time: %.2fs" % build_time)
    print("Total test time: %.2fs" % test_time)
    ET.ElementTree(element=junit_root).write('meson-test-run.xml', xml_declaration=True, encoding='UTF-8')

def check_file(fname):
    linenum = 1
    for line in open(fname, 'rb').readlines():
        if b'\t' in line:
            print("File %s contains a literal tab on line %d. Only spaces are permitted." % (fname, linenum))
            sys.exit(1)
        if b'\r' in line:
            print("File %s contains DOS line ending on line %d. Only unix-style line endings are permitted." % (fname, linenum))
            sys.exit(1)
        linenum += 1

def check_format():
    for (root, _, files) in os.walk('.'):
        for file in files:
            if file.endswith('.py') or file.endswith('.build'):
                fullname = os.path.join(root, file)
                check_file(fullname)

def generate_prebuilt_object():
    source = 'test cases/prebuilt object/1 basic/source.c'
    objectbase = 'test cases/prebuilt object/1 basic/prebuilt.'
    if shutil.which('cl'):
        objectfile = objectbase + 'obj'
        cmd = ['cl', '/nologo', '/Fo'+objectfile, '/c', source]
    else:
        if mesonlib.is_windows():
            objectfile = objectbase + 'obj'
        else:
            objectfile = objectbase + 'o'
        if shutil.which('cc'):
            cmd = 'cc'
        elif shutil.which('gcc'):
            cmd = 'gcc'
        else:
            raise RuntimeError("Could not find C compiler.")
        cmd = [cmd, '-c', source, '-o', objectfile]
    subprocess.check_call(cmd)
    return objectfile

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Run the test suite of Meson.")
    parser.add_argument('--backend', default=None, dest='backend',
                        choices = backendlist)
    options = parser.parse_args()
    setup_commands(options.backend)

    script_dir = os.path.split(__file__)[0]
    if script_dir != '':
        os.chdir(script_dir)
    check_format()
    pbfile = generate_prebuilt_object()
    try:
        run_tests()
    except StopException:
        pass
    os.unlink(pbfile)
    print('\nTotal passed tests:', passing_tests)
    print('Total failed tests:', failing_tests)
    print('Total skipped tests:', skipped_tests)
    sys.exit(failing_tests)

