# Copyright 2012-2014 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
import coredata
from glob import glob
from compilers import *
import configparser

build_filename = 'meson.build'

def find_coverage_tools():
    gcovr_exe = 'gcovr'
    lcov_exe = 'lcov'
    genhtml_exe = 'genhtml'

    if not mesonlib.exe_exists([gcovr_exe, '--version']):
        gcovr_exe = None
    if not mesonlib.exe_exists([lcov_exe, '--version']):
        lcov_exe = None
    if not mesonlib.exe_exists([genhtml_exe, '--version']):
        genhtml_exe = None
    return (gcovr_exe, lcov_exe, genhtml_exe)

def find_valgrind():
    valgrind_exe = 'valgrind'
    if not mesonlib.exe_exists([valgrind_exe, '--version']):
        valgrind_exe = None
    return valgrind_exe

def detect_ninja():
    for n in ['ninja', 'ninja-build']:
        try:
            p = subprocess.Popen([n, '--version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            continue
        p.communicate()
        if p.returncode == 0:
            return n


class Environment():
    private_dir = 'meson-private'
    log_dir = 'meson-logs'
    coredata_file = os.path.join(private_dir, 'coredata.dat')
    version_regex = '\d+(\.\d+)+(-[a-zA-Z0-9]+)?'
    def __init__(self, source_dir, build_dir, main_script_file, options):
        assert(os.path.isabs(main_script_file))
        assert(not os.path.islink(main_script_file))
        self.source_dir = source_dir
        self.build_dir = build_dir
        self.meson_script_file = main_script_file
        self.scratch_dir = os.path.join(build_dir, Environment.private_dir)
        self.log_dir = os.path.join(build_dir, Environment.log_dir)
        os.makedirs(self.scratch_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        try:
            cdf = os.path.join(self.get_build_dir(), Environment.coredata_file)
            self.coredata = coredata.load(cdf)
        except FileNotFoundError:
            self.coredata = coredata.CoreData(options)
        if self.coredata.cross_file:
            self.cross_info = CrossBuildInfo(self.coredata.cross_file)
        else:
            self.cross_info = None
        self.cmd_line_options = options.projectoptions

        # List of potential compilers.
        if mesonlib.is_windows():
            self.default_c = ['cl', 'cc', 'gcc']
            self.default_cpp = ['cl', 'c++']
        else:
            self.default_c = ['cc']
            self.default_cpp = ['c++']
        self.default_objc = ['cc']
        self.default_objcpp = ['c++']
        self.default_fortran = ['gfortran', 'g95', 'f95', 'f90', 'f77']
        self.default_static_linker = 'ar'
        self.vs_static_linker = 'lib'

        cross = self.is_cross_build()
        if (not cross and mesonlib.is_windows()) \
        or (cross and self.cross_info.has_host() and self.cross_info.config['host_machine']['system'] == 'windows'):
            self.exe_suffix = 'exe'
            self.import_lib_suffix = 'lib'
            self.shared_lib_suffix = 'dll'
            self.shared_lib_prefix = ''
            self.static_lib_suffix = 'lib'
            self.static_lib_prefix = ''
            self.object_suffix = 'obj'
        else:
            self.exe_suffix = ''
            if (not cross and mesonlib.is_osx()) or \
            (cross and self.cross_info.has_host() and self.cross_info.config['host_machine']['system'] == 'darwin'):
                self.shared_lib_suffix = 'dylib'
            else:
                self.shared_lib_suffix = 'so'
            self.shared_lib_prefix = 'lib'
            self.static_lib_suffix = 'a'
            self.static_lib_prefix = 'lib'
            self.object_suffix = 'o'
            self.import_lib_suffix = self.shared_lib_suffix

    def is_cross_build(self):
        return self.cross_info is not None

    def generating_finished(self):
        cdf = os.path.join(self.get_build_dir(), Environment.coredata_file)
        coredata.save(self.coredata, cdf)

    def get_script_dir(self):
        return os.path.dirname(self.meson_script_file)

    def get_log_dir(self):
        return self.log_dir

    def get_coredata(self):
        return self.coredata

    def get_build_command(self):
        return self.meson_script_file

    def is_header(self, fname):
        return is_header(fname)

    def is_source(self, fname):
        return is_source(fname)

    def is_object(self, fname):
        return is_object(fname)

    def merge_options(self, options):
        for (name, value) in options.items():
            if name not in self.coredata.user_options:
                self.coredata.user_options[name] = value
            else:
                oldval = self.coredata.user_options[name]
                if type(oldval) != type(value):
                    self.coredata.user_options[name] = value

    def detect_c_compiler(self, want_cross):
        evar = 'CC'
        if self.is_cross_build() and want_cross:
            compilers = [self.cross_info.config['binaries']['c']]
            ccache = []
            is_cross = True
            exe_wrap = self.cross_info.config['binaries'].get('exe_wrapper', None)
        elif evar in os.environ:
            compilers = os.environ[evar].split()
            ccache = []
            is_cross = False
            exe_wrap = None
        else:
            compilers = self.default_c
            ccache = self.detect_ccache()
            is_cross = False
            exe_wrap = None
        for compiler in compilers:
            try:
                basename = os.path.basename(compiler).lower()
                if basename == 'cl' or basename == 'cl.exe':
                    arg = '/?'
                else:
                    arg = '--version'
                p = subprocess.Popen([compiler] + [arg], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
            except OSError:
                continue
            (out, err) = p.communicate()
            out = out.decode()
            err = err.decode()
            vmatch = re.search(Environment.version_regex, out)
            if vmatch:
                version = vmatch.group(0)
            else:
                version = 'unknown version'
            if 'apple' in out and 'Free Software Foundation' in out:
                return GnuCCompiler(ccache + [compiler], version, GCC_OSX, is_cross, exe_wrap)
            if (out.startswith('cc') or 'gcc' in out) and \
                'Free Software Foundation' in out:
                return GnuCCompiler(ccache + [compiler], version, GCC_STANDARD, is_cross, exe_wrap)
            if 'clang' in out:
                return ClangCCompiler(ccache + [compiler], version, is_cross, exe_wrap)
            if 'Microsoft' in out:
                # Visual Studio prints version number to stderr but
                # everything else to stdout. Why? Lord only knows.
                version = re.search(Environment.version_regex, err).group()
                return VisualStudioCCompiler([compiler], version, is_cross, exe_wrap)
        raise EnvironmentException('Unknown compiler(s): "' + ', '.join(compilers) + '"')

    def detect_fortran_compiler(self, want_cross):
        evar = 'FC'
        if self.is_cross_build() and want_cross:
            compilers = [self.cross_info['fortran']]
            is_cross = True
            exe_wrap = self.cross_info.get('exe_wrapper', None)
        elif evar in os.environ:
            compilers = os.environ[evar].split()
            is_cross = False
            exe_wrap = None
        else:
            compilers = self.default_fortran
            is_cross = False
            exe_wrap = None
        for compiler in compilers:
            for arg in ['--version', '-V']:
                try:
                   p = subprocess.Popen([compiler] + [arg],
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
                except OSError:
                   continue
                (out, err) = p.communicate()
                out = out.decode()
                err = err.decode()

                version = 'unknown version'
                vmatch = re.search(Environment.version_regex, out)
                if vmatch:
                    version = vmatch.group(0)

                if 'GNU Fortran' in out:
                  return GnuFortranCompiler([compiler], version, GCC_STANDARD, is_cross, exe_wrap)

                if 'G95' in out:
                  return G95FortranCompiler([compiler], version, is_cross, exe_wrap)

                if 'Sun Fortran' in err:
                  version = 'unknown version'
                  vmatch = re.search(Environment.version_regex, err)
                  if vmatch:
                      version = vmatch.group(0)
                  return SunFortranCompiler([compiler], version, is_cross, exe_wrap)

                if 'ifort (IFORT)' in out:
                  return IntelFortranCompiler([compiler], version, is_cross, exe_wrap)
                
                if 'PathScale EKOPath(tm)' in err:
                  return PathScaleFortranCompiler([compiler], version, is_cross, exe_wrap)

                if 'pgf90' in out:
                  return PGIFortranCompiler([compiler], version, is_cross, exe_wrap)

                if 'Open64 Compiler Suite' in err:
                  return Open64FortranCompiler([compiler], version, is_cross, exe_wrap)

                if 'NAG Fortran' in err:
                  return NAGFortranCompiler([compiler], version, is_cross, exe_wrap)

        raise EnvironmentException('Unknown compiler(s): "' + ', '.join(compilers) + '"')

    def get_scratch_dir(self):
        return self.scratch_dir

    def get_depfixer(self):
        path = os.path.split(__file__)[0]
        return os.path.join(path, 'depfixer.py')

    def detect_cpp_compiler(self, want_cross):
        evar = 'CXX'
        if self.is_cross_build() and want_cross:
            compilers = [self.cross_info.config['binaries']['cpp']]
            ccache = []
            is_cross = True
            exe_wrap = self.cross_info.config['binaries'].get('exe_wrapper', None)
        elif evar in os.environ:
            compilers = os.environ[evar].split()
            ccache = []
            is_cross = False
            exe_wrap = None
        else:
            compilers = self.default_cpp
            ccache = self.detect_ccache()
            is_cross = False
            exe_wrap = None
        for compiler in compilers:
            basename = os.path.basename(compiler).lower()
            if basename == 'cl' or basename == 'cl.exe':
                arg = '/?'
            else:
                arg = '--version'
            try:
                p = subprocess.Popen([compiler, arg],
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
            except OSError:
                continue
            (out, err) = p.communicate()
            out = out.decode()
            err = err.decode()
            vmatch = re.search(Environment.version_regex, out)
            if vmatch:
                version = vmatch.group(0)
            else:
                version = 'unknown version'
            if 'apple' in out and 'Free Software Foundation' in out:
                return GnuCPPCompiler(ccache + [compiler], version, GCC_OSX, is_cross, exe_wrap)
            if (out.startswith('c++ ') or 'g++' in out or 'GCC' in out) and \
                'Free Software Foundation' in out:
                return GnuCPPCompiler(ccache + [compiler], version, GCC_STANDARD, is_cross, exe_wrap)
            if 'clang' in out:
                return ClangCPPCompiler(ccache + [compiler], version, is_cross, exe_wrap)
            if 'Microsoft' in out:
                version = re.search(Environment.version_regex, err).group()
                return VisualStudioCPPCompiler([compiler], version, is_cross, exe_wrap)
        raise EnvironmentException('Unknown compiler(s) "' + ', '.join(compilers) + '"')

    def detect_objc_compiler(self, want_cross):
        if self.is_cross_build() and want_cross:
            exelist = [self.cross_info['objc']]
            is_cross = True
            exe_wrap = self.cross_info.get('exe_wrapper', None)
        else:
            exelist = self.get_objc_compiler_exelist()
            is_cross = False
            exe_wrap = None
        try:
            p = subprocess.Popen(exelist + ['--version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise EnvironmentException('Could not execute ObjC compiler "%s"' % ' '.join(exelist))
        (out, err) = p.communicate()
        out = out.decode()
        err = err.decode()
        vmatch = re.search(Environment.version_regex, out)
        if vmatch:
            version = vmatch.group(0)
        else:
            version = 'unknown version'
        if (out.startswith('cc ') or 'gcc' in out) and \
            'Free Software Foundation' in out:
            return GnuObjCCompiler(exelist, version, is_cross, exe_wrap)
        if out.startswith('Apple LLVM'):
            return ClangObjCCompiler(exelist, version, is_cross, exe_wrap)
        if 'apple' in out and 'Free Software Foundation' in out:
            return GnuObjCCompiler(exelist, version, is_cross, exe_wrap)
        raise EnvironmentException('Unknown compiler "' + ' '.join(exelist) + '"')

    def detect_objcpp_compiler(self, want_cross):
        if self.is_cross_build() and want_cross:
            exelist = [self.cross_info['objcpp']]
            is_cross = True
            exe_wrap = self.cross_info.get('exe_wrapper', None)
        else:
            exelist = self.get_objcpp_compiler_exelist()
            is_cross = False
            exe_wrap = None
        try:
            p = subprocess.Popen(exelist + ['--version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise EnvironmentException('Could not execute ObjC++ compiler "%s"' % ' '.join(exelist))
        (out, err) = p.communicate()
        out = out.decode()
        err = err.decode()
        vmatch = re.search(Environment.version_regex, out)
        if vmatch:
            version = vmatch.group(0)
        else:
            version = 'unknown version'
        if (out.startswith('c++ ') or out.startswith('g++')) and \
            'Free Software Foundation' in out:
            return GnuObjCPPCompiler(exelist, version, is_cross, exe_wrap)
        if out.startswith('Apple LLVM'):
            return ClangObjCPPCompiler(exelist, version, is_cross, exe_wrap)
        if 'apple' in out and 'Free Software Foundation' in out:
            return GnuObjCPPCompiler(exelist, version, is_cross, exe_wrap)
        raise EnvironmentException('Unknown compiler "' + ' '.join(exelist) + '"')

    def detect_java_compiler(self):
        exelist = ['javac']
        try:
            p = subprocess.Popen(exelist + ['-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise EnvironmentException('Could not execute Java compiler "%s"' % ' '.join(exelist))
        (out, err) = p.communicate()
        out = out.decode()
        err = err.decode()
        vmatch = re.search(Environment.version_regex, err)
        if vmatch:
            version = vmatch.group(0)
        else:
            version = 'unknown version'
        if 'javac' in err:
            return JavaCompiler(exelist, version)
        raise EnvironmentException('Unknown compiler "' + ' '.join(exelist) + '"')

    def detect_cs_compiler(self):
        exelist = ['mcs']
        try:
            p = subprocess.Popen(exelist + ['--version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise EnvironmentException('Could not execute C# compiler "%s"' % ' '.join(exelist))
        (out, err) = p.communicate()
        out = out.decode()
        err = err.decode()
        vmatch = re.search(Environment.version_regex, out)
        if vmatch:
            version = vmatch.group(0)
        else:
            version = 'unknown version'
        if 'Mono' in out:
            return MonoCompiler(exelist, version)
        raise EnvironmentException('Unknown compiler "' + ' '.join(exelist) + '"')

    def detect_vala_compiler(self):
        exelist = ['valac']
        try:
            p = subprocess.Popen(exelist + ['--version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise EnvironmentException('Could not execute Vala compiler "%s"' % ' '.join(exelist))
        (out, _) = p.communicate()
        out = out.decode()
        vmatch = re.search(Environment.version_regex, out)
        if vmatch:
            version = vmatch.group(0)
        else:
            version = 'unknown version'
        if 'Vala' in out:
            return ValaCompiler(exelist, version)
        raise EnvironmentException('Unknown compiler "' + ' '.join(exelist) + '"')

    def detect_rust_compiler(self):
        exelist = ['rustc']
        try:
            p = subprocess.Popen(exelist + ['--version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise EnvironmentException('Could not execute Rust compiler "%s"' % ' '.join(exelist))
        (out, _) = p.communicate()
        out = out.decode()
        vmatch = re.search(Environment.version_regex, out)
        if vmatch:
            version = vmatch.group(0)
        else:
            version = 'unknown version'
        if 'rustc' in out:
            return RustCompiler(exelist, version)
        raise EnvironmentException('Unknown compiler "' + ' '.join(exelist) + '"')

    def detect_static_linker(self, compiler):
        if compiler.is_cross:
            linker = self.cross_info.config['binaries']['ar']
        else:
            evar = 'AR'
            if evar in os.environ:
                linker = os.environ[evar].strip()
            if isinstance(compiler, VisualStudioCCompiler):
                linker= self.vs_static_linker
            else:
                linker = self.default_static_linker
        basename = os.path.basename(linker).lower()
        if basename == 'lib' or basename == 'lib.exe':
            arg = '/?'
        else:
            arg = '--version'
        try:
            p = subprocess.Popen([linker, arg], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise EnvironmentException('Could not execute static linker "%s".' % linker)
        (out, err) = p.communicate()
        out = out.decode()
        err = err.decode()
        if '/OUT:' in out or '/OUT:' in err:
            return VisualStudioLinker([linker])
        if p.returncode == 0:
            return ArLinker([linker])
        if p.returncode == 1 and err.startswith('usage'): # OSX
            return ArLinker([linker])
        raise EnvironmentException('Unknown static linker "%s"' % linker)

    def detect_ccache(self):
        try:
            has_ccache = subprocess.call(['ccache', '--version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            has_ccache = 1
        if has_ccache == 0:
            cmdlist = ['ccache']
        else:
            cmdlist = []
        return cmdlist

    def get_objc_compiler_exelist(self):
        ccachelist = self.detect_ccache()
        evar = 'OBJCC'
        if evar in os.environ:
            return os.environ[evar].split()
        return ccachelist + self.default_objc

    def get_objcpp_compiler_exelist(self):
        ccachelist = self.detect_ccache()
        evar = 'OBJCXX'
        if evar in os.environ:
            return os.environ[evar].split()
        return ccachelist + self.default_objcpp

    def get_source_dir(self):
        return self.source_dir

    def get_build_dir(self):
        return self.build_dir

    def get_exe_suffix(self):
        return self.exe_suffix

    # On Windows the library has suffix dll
    # but you link against a file that has suffix lib.
    def get_import_lib_suffix(self):
        return self.import_lib_suffix

    def get_shared_lib_prefix(self):
        return self.shared_lib_prefix

    def get_shared_lib_suffix(self):
        return self.shared_lib_suffix

    def get_static_lib_prefix(self):
        return self.static_lib_prefix

    def get_static_lib_suffix(self):
        return self.static_lib_suffix

    def get_object_suffix(self):
        return self.object_suffix

    def get_prefix(self):
        return self.coredata.prefix

    def get_libdir(self):
        return self.coredata.libdir

    def get_bindir(self):
        return self.coredata.bindir

    def get_includedir(self):
        return self.coredata.includedir

    def get_mandir(self):
        return self.coredata.mandir

    def get_datadir(self):
        return self.coredata.datadir

    def find_library(self, libname, dirs):
        if dirs is None:
            dirs = mesonlib.get_library_dirs()
        suffixes = [self.get_shared_lib_suffix(), self.get_static_lib_suffix()]
        prefix = self.get_shared_lib_prefix()
        for d in dirs:
            for suffix in suffixes:
                trial = os.path.join(d, prefix + libname + '.' + suffix)
                if os.path.isfile(trial):
                    return trial


def get_args_from_envvars(lang):
    if lang == 'c':
        compile_args = os.environ.get('CFLAGS', '').split()
        link_args = compile_args + os.environ.get('LDFLAGS', '').split()
        compile_args += os.environ.get('CPPFLAGS', '').split()
    elif lang == 'cpp':
        compile_args = os.environ.get('CXXFLAGS', '').split()
        link_args = compile_args + os.environ.get('LDFLAGS', '').split()
        compile_args += os.environ.get('CPPFLAGS', '').split()
    elif lang == 'objc':
        compile_args = os.environ.get('OBJCFLAGS', '').split()
        link_args = compile_args + os.environ.get('LDFLAGS', '').split()
        compile_args += os.environ.get('CPPFLAGS', '').split()
    elif lang == 'objcpp':
        compile_args = os.environ.get('OBJCXXFLAGS', '').split()
        link_args = compile_args + os.environ.get('LDFLAGS', '').split()
        compile_args += os.environ.get('CPPFLAGS', '').split()
    elif lang == 'fortran':
        compile_args = os.environ.get('FFLAGS', '').split()
        link_args = compile_args + os.environ.get('LDFLAGS', '').split()
    else:
        compile_args = []
        link_args = []
    return (compile_args, link_args)

class CrossBuildInfo():
    def __init__(self, filename):
        self.config = {}
        self.parse_datafile(filename)
        if 'target_machine' in self.config:
            return
        if not 'host_machine' in self.config:
            raise coredata.MesonException('Cross info file must have either host or a target machine.')
        if not 'properties' in self.config:
            raise coredata.MesonException('Cross file is missing "properties".')
        if not 'binaries' in self.config:
            raise coredata.MesonException('Cross file is missing "binaries".')

    def ok_type(self, i):
        return isinstance(i, str) or isinstance(i, int) or isinstance(i, bool)

    def parse_datafile(self, filename):
        config = configparser.ConfigParser()
        config.read(filename)
        # This is a bit hackish at the moment.
        for s in config.sections():
            self.config[s] = {}
            for entry in config[s]:
                value = config[s][entry]
                if ' ' in entry or '\t' in entry or "'" in entry or '"' in entry:
                    raise EnvironmentException('Malformed variable name %s in cross file..' % varname)
                try:
                    res = eval(value, {'true' : True, 'false' : False})
                except Exception:
                    raise EnvironmentException('Malformed value in cross file variable %s.' % varname)
                if self.ok_type(res):
                    self.config[s][entry] = res
                elif isinstance(res, list):
                    for i in res:
                        if not self.ok_type(i):
                            raise EnvironmentException('Malformed value in cross file variable %s.' % varname)
                    self.items[varname] = res
                else:
                    raise EnvironmentException('Malformed value in cross file variable %s.' % varname)

    def has_host(self):
        return 'host_machine' in self.config

    def has_target(self):
        return 'target_machine' in self.config

    # Wehn compiling a cross compiler we use the native compiler for everything.
    # But not when cross compiling a cross compiler.
    def need_cross_compiler(self):
        return 'host_machine' in self.config
