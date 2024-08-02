# Copyright 2018 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import json

from pathlib import Path
from .. import mesonlib
from . import ExtensionModule
from mesonbuild.modules import ModuleReturnValue
from . import permittedSnippetKwargs
from ..interpreterbase import (
    noPosargs, noKwargs, permittedKwargs,
    InterpreterObject, InvalidArguments
)
from ..interpreter import ExternalProgramHolder
from ..interpreterbase import flatten
from ..build import known_shmod_kwargs
from .. import mlog
from ..environment import detect_cpu_family
from ..dependencies.base import (
    DependencyMethods, ExternalDependency,
    ExternalProgram, PkgConfigDependency,
    NonExistingExternalProgram
)

mod_kwargs = set(['subdir'])
mod_kwargs.update(known_shmod_kwargs)
mod_kwargs -= set(['name_prefix', 'name_suffix'])


def run_command(python, command):
    _, stdout, _ = mesonlib.Popen_safe(python.get_command() + [
        '-c',
        command])

    return stdout.strip()


class PythonDependency(ExternalDependency):
    def __init__(self, python_holder, environment, kwargs):
        super().__init__('python', environment, None, kwargs)
        self.name = 'python'
        self.static = kwargs.get('static', False)
        self.version = python_holder.version
        self.platform = python_holder.platform
        self.pkgdep = None
        self.variables = python_holder.variables
        self.paths = python_holder.paths
        if mesonlib.version_compare(self.version, '>= 3.0'):
            self.major_version = 3
        else:
            self.major_version = 2

        if DependencyMethods.PKGCONFIG in self.methods and not python_holder.is_pypy:
            pkg_version = self.variables.get('LDVERSION') or self.version
            pkg_libdir = self.variables.get('LIBPC')
            old_pkg_libdir = os.environ.get('PKG_CONFIG_LIBDIR')
            old_pkg_path = os.environ.get('PKG_CONFIG_PATH')

            os.environ.pop('PKG_CONFIG_PATH', None)

            if pkg_libdir:
                os.environ['PKG_CONFIG_LIBDIR'] = pkg_libdir

            try:
                self.pkgdep = PkgConfigDependency('python-{}'.format(pkg_version), environment, kwargs)
            except Exception:
                pass

            if old_pkg_path is not None:
                os.environ['PKG_CONFIG_PATH'] = old_pkg_path

            if old_pkg_libdir is not None:
                os.environ['PKG_CONFIG_LIBDIR'] = old_pkg_libdir
            else:
                os.environ.pop('PKG_CONFIG_LIBDIR', None)

        if self.pkgdep and self.pkgdep.found():
            self.compile_args = self.pkgdep.get_compile_args()
            self.link_args = self.pkgdep.get_link_args()
            self.is_found = True
            self.pcdep = self.pkgdep
        else:
            self.pkgdep = None

            if DependencyMethods.SYSCONFIG in self.methods:
                if mesonlib.is_windows():
                    self._find_libpy_windows(environment)
                else:
                    self._find_libpy(python_holder, environment)

        if self.is_found:
            mlog.log('Dependency', mlog.bold(self.name), 'found:', mlog.green('YES'))
        else:
            mlog.log('Dependency', mlog.bold(self.name), 'found:', mlog.red('NO'))

    def _find_libpy(self, python_holder, environment):
        if python_holder.is_pypy:
            if self.major_version == 3:
                libname = 'pypy3-c'
            else:
                libname = 'pypy-c'
            libdir = os.path.join(self.variables.get('base'), 'bin')
            libdirs = [libdir]
        else:
            libname = 'python{}'.format(self.version)
            if 'DEBUG_EXT' in self.variables:
                libname += self.variables['DEBUG_EXT']
            if 'ABIFLAGS' in self.variables:
                libname += self.variables['ABIFLAGS']
            libdirs = []

        largs = self.compiler.find_library(libname, environment, libdirs)

        self.is_found = largs is not None

        self.link_args = largs

        inc_paths = mesonlib.OrderedSet([
            self.variables.get('INCLUDEPY'),
            self.paths.get('include'),
            self.paths.get('platinclude')])

        self.compile_args += ['-I' + path for path in inc_paths if path]

    def get_windows_python_arch(self):
        if self.platform == 'mingw':
            pycc = self.variables.get('CC')
            if pycc.startswith('x86_64'):
                return '64'
            elif pycc.startswith(('i686', 'i386')):
                return '32'
            else:
                mlog.log('MinGW Python built with unknown CC {!r}, please file'
                         'a bug'.format(pycc))
                return None
        elif self.platform == 'win32':
            return '32'
        elif self.platform in ('win64', 'win-amd64'):
            return '64'
        mlog.log('Unknown Windows Python platform {!r}'.format(self.platform))
        return None

    def get_windows_link_args(self):
        if self.platform.startswith('win'):
            vernum = self.variables.get('py_version_nodot')
            if self.static:
                libname = 'libpython{}.a'.format(vernum)
            else:
                libname = 'python{}.lib'.format(vernum)
            lib = Path(self.variables.get('base')) / 'libs' / libname
        elif self.platform == 'mingw':
            if self.static:
                libname = self.variables.get('LIBRARY')
            else:
                libname = self.variables.get('LDLIBRARY')
            lib = Path(self.variables.get('LIBDIR')) / libname
        if not lib.exists():
            mlog.log('Could not find Python3 library {!r}'.format(str(lib)))
            return None
        return [str(lib)]

    def _find_libpy_windows(self, env):
        '''
        Find python3 libraries on Windows and also verify that the arch matches
        what we are building for.
        '''
        pyarch = self.get_windows_python_arch()
        if pyarch is None:
            self.is_found = False
            return
        arch = detect_cpu_family(env.coredata.compilers)
        if arch == 'x86':
            arch = '32'
        elif arch == 'x86_64':
            arch = '64'
        else:
            # We can't cross-compile Python 3 dependencies on Windows yet
            mlog.log('Unknown architecture {!r} for'.format(arch),
                     mlog.bold(self.name))
            self.is_found = False
            return
        # Pyarch ends in '32' or '64'
        if arch != pyarch:
            mlog.log('Need', mlog.bold(self.name), 'for {}-bit, but '
                     'found {}-bit'.format(arch, pyarch))
            self.is_found = False
            return
        # This can fail if the library is not found
        largs = self.get_windows_link_args()
        if largs is None:
            self.is_found = False
            return
        self.link_args = largs
        # Compile args
        inc_paths = mesonlib.OrderedSet([
            self.variables.get('INCLUDEPY'),
            self.paths.get('include'),
            self.paths.get('platinclude')])

        self.compile_args += ['-I' + path for path in inc_paths if path]

        # https://sourceforge.net/p/mingw-w64/mailman/message/30504611/
        if pyarch == '64' and self.major_version == 2:
            self.compile_args += ['-DMS_WIN64']

        self.is_found = True

    @staticmethod
    def get_methods():
        if mesonlib.is_windows():
            return [DependencyMethods.PKGCONFIG, DependencyMethods.SYSCONFIG]
        elif mesonlib.is_osx():
            return [DependencyMethods.PKGCONFIG, DependencyMethods.EXTRAFRAMEWORK]
        else:
            return [DependencyMethods.PKGCONFIG, DependencyMethods.SYSCONFIG]

    def get_pkgconfig_variable(self, variable_name, kwargs):
        if self.pkgdep:
            return self.pkgdep.get_pkgconfig_variable(variable_name, kwargs)
        else:
            return super().get_pkgconfig_variable(variable_name, kwargs)


VARIABLES_COMMAND = '''
import sysconfig
import json

print (json.dumps (sysconfig.get_config_vars()))
'''


PATHS_COMMAND = '''
import sysconfig
import json

print (json.dumps(sysconfig.get_paths()))
'''


INSTALL_PATHS_COMMAND = '''
import sysconfig
import json

print (json.dumps(sysconfig.get_paths(scheme='posix_prefix', vars={'base': '', 'platbase': '', 'installed_base': ''})))
'''


IS_PYPY_COMMAND = '''
import sys
import json

print (json.dumps('__pypy__' in sys.builtin_module_names))
'''


class PythonInstallation(ExternalProgramHolder, InterpreterObject):
    def __init__(self, interpreter, python):
        InterpreterObject.__init__(self)
        ExternalProgramHolder.__init__(self, python)
        self.interpreter = interpreter
        prefix = self.interpreter.environment.coredata.get_builtin_option('prefix')
        self.variables = json.loads(run_command(python, VARIABLES_COMMAND))
        self.paths = json.loads(run_command(python, PATHS_COMMAND))
        install_paths = json.loads(run_command(python, INSTALL_PATHS_COMMAND))
        self.platlib_install_path = os.path.join(prefix, install_paths['platlib'][1:])
        self.purelib_install_path = os.path.join(prefix, install_paths['purelib'][1:])
        self.version = run_command(python, "import sysconfig; print (sysconfig.get_python_version())")
        self.platform = run_command(python, "import sysconfig; print (sysconfig.get_platform())")
        self.is_pypy = json.loads(run_command(python, IS_PYPY_COMMAND))

    @permittedSnippetKwargs(mod_kwargs)
    def extension_module(self, interpreter, state, args, kwargs):
        if 'subdir' in kwargs and 'install_dir' in kwargs:
            raise InvalidArguments('"subdir" and "install_dir" are mutually exclusive')

        if 'subdir' in kwargs:
            subdir = kwargs.pop('subdir', '')
            if not isinstance(subdir, str):
                raise InvalidArguments('"subdir" argument must be a string.')

            kwargs['install_dir'] = os.path.join(self.platlib_install_path, subdir)

        suffix = self.variables.get('EXT_SUFFIX') or self.variables.get('SO') or self.variables.get('.so')

        # msys2's python3 has "-cpython-36m.dll", we have to be clever
        split = suffix.rsplit('.', 1)
        suffix = split.pop(-1)
        args[0] += ''.join(s for s in split)

        kwargs['name_prefix'] = ''
        kwargs['name_suffix'] = suffix

        return interpreter.func_shared_module(None, args, kwargs)

    def dependency(self, interpreter, state, args, kwargs):
        dep = PythonDependency(self, interpreter.environment, kwargs)
        return interpreter.holderify(dep)

    @permittedSnippetKwargs(['pure', 'subdir'])
    def install_sources(self, interpreter, state, args, kwargs):
        pure = kwargs.pop('pure', False)
        if not isinstance(pure, bool):
            raise InvalidArguments('"pure" argument must be a boolean.')

        subdir = kwargs.pop('subdir', '')
        if not isinstance(subdir, str):
            raise InvalidArguments('"subdir" argument must be a string.')

        if pure:
            kwargs['install_dir'] = os.path.join(self.purelib_install_path, subdir)
        else:
            kwargs['install_dir'] = os.path.join(self.platlib_install_path, subdir)

        return interpreter.func_install_data(None, args, kwargs)

    @noPosargs
    @permittedKwargs(['pure', 'subdir'])
    def get_install_dir(self, node, args, kwargs):
        pure = kwargs.pop('pure', True)
        if not isinstance(pure, bool):
            raise InvalidArguments('"pure" argument must be a boolean.')

        subdir = kwargs.pop('subdir', '')
        if not isinstance(subdir, str):
            raise InvalidArguments('"subdir" argument must be a string.')

        if pure:
            res = os.path.join(self.purelib_install_path, subdir)
        else:
            res = os.path.join(self.platlib_install_path, subdir)

        return ModuleReturnValue(res, [])

    @noPosargs
    @noKwargs
    def language_version(self, node, args, kwargs):
        return ModuleReturnValue(self.version, [])

    @noPosargs
    @noKwargs
    def found(self, node, args, kwargs):
        return ModuleReturnValue(True, [])

    @noKwargs
    def has_path(self, node, args, kwargs):
        if len(args) != 1:
            raise InvalidArguments('has_path takes exactly one positional argument.')
        path_name = args[0]
        if not isinstance(path_name, str):
            raise InvalidArguments('has_path argument must be a string.')

        return ModuleReturnValue(path_name in self.paths, [])

    @noKwargs
    def get_path(self, node, args, kwargs):
        if len(args) not in (1, 2):
            raise InvalidArguments('get_path must have one or two arguments.')
        path_name = args[0]
        if not isinstance(path_name, str):
            raise InvalidArguments('get_path argument must be a string.')

        try:
            path = self.paths[path_name]
        except KeyError:
            if len(args) == 2:
                path = args[1]
            else:
                raise InvalidArguments('{} is not a valid path name'.format(path_name))

        return ModuleReturnValue(path, [])

    @noKwargs
    def has_variable(self, node, args, kwargs):
        if len(args) != 1:
            raise InvalidArguments('has_variable takes exactly one positional argument.')
        var_name = args[0]
        if not isinstance(var_name, str):
            raise InvalidArguments('has_variable argument must be a string.')

        return ModuleReturnValue(var_name in self.variables, [])

    @noKwargs
    def get_variable(self, node, args, kwargs):
        if len(args) not in (1, 2):
            raise InvalidArguments('get_variable must have one or two arguments.')
        var_name = args[0]
        if not isinstance(var_name, str):
            raise InvalidArguments('get_variable argument must be a string.')

        try:
            var = self.variables[var_name]
        except KeyError:
            if len(args) == 2:
                var = args[1]
            else:
                raise InvalidArguments('{} is not a valid variable name'.format(var_name))

        return ModuleReturnValue(var, [])

    def method_call(self, method_name, args, kwargs):
        try:
            fn = getattr(self, method_name)
        except AttributeError:
            raise InvalidArguments('Python object does not have method %s.' % method_name)

        if not getattr(fn, 'no-args-flattening', False):
            args = flatten(args)

        if method_name in ['extension_module', 'dependency', 'install_sources']:
            value = fn(self.interpreter, None, args, kwargs)
            return self.interpreter.holderify(value)
        elif method_name in ['has_variable', 'get_variable', 'has_path', 'get_path', 'found', 'language_version', 'get_install_dir']:
            value = fn(None, args, kwargs)
            return self.interpreter.module_method_callback(value)
        else:
            raise InvalidArguments('Python object does not have method %s.' % method_name)


class PythonModule(ExtensionModule):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.snippets.add('find_installation')

    # https://www.python.org/dev/peps/pep-0397/
    def _get_win_pythonpath(self, name_or_path):
        if name_or_path not in ['python2', 'python3']:
            return None
        ver = {'python2': '-2', 'python3': '-3'}[name_or_path]
        cmd = ['py', ver, '-c', "import sysconfig; print(sysconfig.get_config_var('BINDIR'))"]
        _, stdout, _ = mesonlib.Popen_safe(cmd)
        dir = stdout.strip()
        if os.path.exists(dir):
            return os.path.join(dir, 'python')
        else:
            return None

    @permittedSnippetKwargs(['required'])
    def find_installation(self, interpreter, state, args, kwargs):
        required = kwargs.get('required', True)
        if not isinstance(required, bool):
            raise InvalidArguments('"required" argument must be a boolean.')

        if len(args) > 1:
            raise InvalidArguments('find_installation takes zero or one positional argument.')

        if args:
            name_or_path = args[0]
            if not isinstance(name_or_path, str):
                raise InvalidArguments('find_installation argument must be a string.')
        else:
            name_or_path = None

        if not name_or_path:
            mlog.log("Using meson's python {}".format(mesonlib.python_command))
            python = ExternalProgram('python3', mesonlib.python_command, silent=True)
        else:
            if mesonlib.is_windows():
                pythonpath = self._get_win_pythonpath(name_or_path)
                if pythonpath is not None:
                    name_or_path = pythonpath
            python = ExternalProgram(name_or_path, silent = True)
            # Last ditch effort, python2 or python3 can be named python
            # on various platforms, let's not give up just yet, if an executable
            # named python is available and has a compatible version, let's use
            # it
            if not python.found() and name_or_path in ['python2', 'python3']:
                python = ExternalProgram('python', silent = True)
                if python.found():
                    version = run_command(python, "import sysconfig; print (sysconfig.get_python_version())")
                    if not version or \
                            name_or_path == 'python2' and mesonlib.version_compare(version, '>= 3.0') or \
                            name_or_path == 'python3' and not mesonlib.version_compare(version, '>= 3.0'):
                        python = NonExistingExternalProgram()

        if not python.found():
            if required:
                raise mesonlib.MesonException('{} not found'.format(name_or_path or 'python'))
            res = ExternalProgramHolder(NonExistingExternalProgram())
        else:
            # Sanity check, we expect to have something that at least quacks in tune
            version = run_command(python, "import sysconfig; print (sysconfig.get_python_version())")
            if not version:
                res = ExternalProgramHolder(NonExistingExternalProgram())
                if required:
                    raise mesonlib.MesonException('{} is not a valid python'.format(python))
            else:
                res = PythonInstallation(interpreter, python)

        return res


def initialize(*args, **kwargs):
    return PythonModule(*args, **kwargs)
