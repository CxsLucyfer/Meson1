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

import mparser
import environment
import coredata
import dependencies
import mlog
import build
import optinterpreter
import wrap
import mesonlib
import os, sys, platform, subprocess, shutil, uuid, re
from functools import wraps

import importlib

class InterpreterException(coredata.MesonException):
    pass

class InvalidCode(InterpreterException):
    pass

class InvalidArguments(InterpreterException):
    pass

# Decorators for method calls.

def check_stringlist(a, msg='Arguments must be strings.'):
    if not isinstance(a, list):
        raise InvalidArguments('Argument not a list.')
    if not all(isinstance(s, str) for s in a):
        raise InvalidArguments(msg)

def noPosargs(f):
    @wraps(f)
    def wrapped(self, node, args, kwargs):
        if len(args) != 0:
            raise InvalidArguments('Function does not take positional arguments.')
        return f(self, node, args, kwargs)
    return wrapped

def noKwargs(f):
    @wraps(f)
    def wrapped(self, node, args, kwargs):
        if len(kwargs) != 0:
            raise InvalidArguments('Function does not take keyword arguments.')
        return f(self, node, args, kwargs)
    return wrapped

def stringArgs(f):
    @wraps(f)
    def wrapped(self, node, args, kwargs):
        assert(isinstance(args, list))
        check_stringlist(args)
        return f(self, node, args, kwargs)
    return wrapped

def stringifyUserArguments(args):
    if isinstance(args, list):
        return '[%s]' % ', '.join([stringifyUserArguments(x) for x in args])
    elif isinstance(args, int):
        return str(args)
    elif isinstance(args, str):
        return "'%s'" % args
    raise InvalidArguments('Function accepts only strings, integers, lists and lists thereof.')

class InterpreterObject():
    def __init__(self):
        self.methods = {}

    def method_call(self, method_name, args, kwargs):
        if method_name in self.methods:
            return self.methods[method_name](args, kwargs)
        raise InvalidCode('Unknown method "%s" in object.' % method_name)

class TryRunResultHolder(InterpreterObject):
    def __init__(self, res):
        super().__init__()
        self.res = res
        self.methods.update({'returncode' : self.returncode_method,
                             'compiled' : self.compiled_method,
                             'stdout' : self.stdout_method,
                             'stderr' : self.stderr_method,
                            })

    def returncode_method(self, args, kwargs):
        return self.res.returncode

    def compiled_method(self, args, kwargs):
        return self.res.compiled

    def stdout_method(self, args, kwargs):
        return self.res.stdout

    def stderr_method(self, args, kwargs):
        return self.res.stderr

class RunProcess(InterpreterObject):

    def __init__(self, command_array, source_dir, build_dir, subdir, in_builddir=False):
        super().__init__()
        pc = self.run_command(command_array, source_dir, build_dir, subdir, in_builddir)
        (stdout, stderr) = pc.communicate()
        self.returncode = pc.returncode
        self.stdout = stdout.decode().replace('\r\n', '\n')
        self.stderr = stderr.decode().replace('\r\n', '\n')
        self.methods.update({'returncode' : self.returncode_method,
                             'stdout' : self.stdout_method,
                             'stderr' : self.stderr_method,
                            })

    def run_command(self, command_array, source_dir, build_dir, subdir, in_builddir):
        cmd_name = command_array[0]
        env = {'MESON_SOURCE_ROOT' : source_dir,
               'MESON_BUILD_ROOT' : build_dir,
               'MESON_SUBDIR' : subdir}
        if in_builddir:
            cwd = os.path.join(build_dir, subdir)
        else:
            cwd = os.path.join(source_dir, subdir)
        child_env = os.environ.copy()
        child_env.update(env)
        try:
            return subprocess.Popen(command_array, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=child_env, cwd=cwd)
        except FileNotFoundError:
            pass
        # Was not a command, is a program in path?
        exe = shutil.which(cmd_name)
        if exe is not None:
            command_array = [exe] + command_array[1:]
            return subprocess.Popen(command_array, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=child_env, cwd=cwd)
        # No? Maybe it is a script in the source tree.
        fullpath = os.path.join(source_dir, subdir, cmd_name)
        command_array = [fullpath] + command_array[1:]
        try:
            return subprocess.Popen(command_array, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=child_env, cwd=cwd)
        except FileNotFoundError:
            raise InterpreterException('Could not execute command "%s".' % cmd_name)

    def returncode_method(self, args, kwargs):
        return self.returncode

    def stdout_method(self, args, kwargs):
        return self.stdout

    def stderr_method(self, args, kwargs):
        return self.stderr

class ConfigureFileHolder(InterpreterObject):

    def __init__(self, subdir, sourcename, targetname, configuration_data):
        InterpreterObject.__init__(self)
        self.held_object = build.ConfigureFile(subdir, sourcename, targetname, configuration_data)

class ConfigurationDataHolder(InterpreterObject):
    def __init__(self):
        super().__init__()
        self.used = False # These objects become immutable after use in configure_file.
        self.held_object = build.ConfigurationData()
        self.methods.update({'set': self.set_method,
                             'set10': self.set10_method,
                            })

    def is_used(self):
        return self.used

    def mark_used(self):
        self.used = True

    def validate_args(self, args):
        if len(args) != 2:
            raise InterpreterException("Configuration set requires 2 arguments.")
        if self.used:
            raise InterpreterException("Can not set values on configuration object that has been used.")
        name = args[0]
        val = args[1]
        if not isinstance(name, str):
            raise InterpreterException("First argument to set must be a string.")
        return (name, val)

    def set_method(self, args, kwargs):
        (name, val) = self.validate_args(args)
        self.held_object.values[name] = val

    def set10_method(self, args, kwargs):
        (name, val) = self.validate_args(args)
        if val:
            self.held_object.values[name] = 1
        else:
            self.held_object.values[name] = 0

    def get(self, name):
        return self.held_object.values[name]

    def keys(self):
        return self.held_object.values.keys()

# Interpreter objects can not be pickled so we must have
# these wrappers.

class DependencyHolder(InterpreterObject):
    def __init__(self, dep):
        InterpreterObject.__init__(self)
        self.held_object = dep
        self.methods.update({'found' : self.found_method})

    def found_method(self, args, kwargs):
        return self.held_object.found()

class InternalDependencyHolder(InterpreterObject):
    def __init__(self, dep):
        InterpreterObject.__init__(self)
        self.held_object = dep
        self.methods.update({'found' : self.found_method})

    def found_method(self, args, kwargs):
        return True

class ExternalProgramHolder(InterpreterObject):
    def __init__(self, ep):
        InterpreterObject.__init__(self)
        self.held_object = ep
        self.methods.update({'found': self.found_method})

    def found_method(self, args, kwargs):
        return self.found()

    def found(self):
        return self.held_object.found()

    def get_command(self):
        return self.held_object.fullpath

    def get_name(self):
        return self.held_object.name

class ExternalLibraryHolder(InterpreterObject):
    def __init__(self, el):
        InterpreterObject.__init__(self)
        self.held_object = el
        self.methods.update({'found': self.found_method})

    def found(self):
        return self.held_object.found()

    def found_method(self, args, kwargs):
        return self.found()

    def get_filename(self):
        return self.held_object.fullpath

    def get_name(self):
        return self.held_object.name

    def get_compile_args(self):
        return self.held_object.get_compile_args()

    def get_link_args(self):
        return self.held_object.get_link_args()

    def get_exe_args(self):
        return self.held_object.get_exe_args()

class GeneratorHolder(InterpreterObject):
    def __init__(self, interpreter, args, kwargs):
        super().__init__()
        self.interpreter = interpreter
        self.held_object = build.Generator(args, kwargs)
        self.methods.update({'process' : self.process_method})

    def process_method(self, args, kwargs):
        if len(kwargs) > 0:
            raise InvalidArguments('Process does not take keyword arguments.')
        check_stringlist(args)
        gl = GeneratedListHolder(self)
        [gl.add_file(os.path.join(self.interpreter.subdir, a)) for a in args]
        return gl

class GeneratedListHolder(InterpreterObject):
    def __init__(self, arg1):
        super().__init__()
        if isinstance(arg1, GeneratorHolder):
            self.held_object = build.GeneratedList(arg1.held_object)
        else:
            self.held_object = arg1

    def add_file(self, a):
        self.held_object.add_file(a)

class BuildMachine(InterpreterObject):
    def __init__(self):
        InterpreterObject.__init__(self)
        self.methods.update({'system' : self.system_method,
                             'cpu' : self.cpu_method,
                             'endian' : self.endian_method,
                            })

    # Python is inconsistent in its platform module.
    # It returns different values for the same cpu.
    # For x86 it might return 'x86', 'i686' or somesuch.
    # Do some canonicalization.
    def cpu_method(self, args, kwargs):
        trial = platform.machine().lower()
        if trial.startswith('i') and trial.endswith('86'):
            return 'x86'
        # This might be wrong. Maybe we should return the more
        # specific string such as 'armv7l'. Need to get user
        # feedback first.
        if trial.startswith('arm'):
            return 'arm'
        # Add fixes here as bugs are reported.
        return trial

    def system_method(self, args, kwargs):
        return platform.system().lower()

    def endian_method(self, args, kwargs):
        return sys.byteorder

# This class will provide both host_machine and
# target_machine
class CrossMachineInfo(InterpreterObject):
    def __init__(self, cross_info):
        InterpreterObject.__init__(self)
        self.info = cross_info
        self.methods.update({'system' : self.system_method,
                             'cpu' : self.cpu_method,
                             'endian' : self.endian_method,
                            })

    def system_method(self, args, kwargs):
        return self.info['system']

    def cpu_method(self, args, kwargs):
        return self.info['cpu']

    def endian_method(self, args, kwargs):
        return self.info['endian']

class IncludeDirsHolder(InterpreterObject):
    def __init__(self, curdir, dirs):
        super().__init__()
        self.held_object = build.IncludeDirs(curdir, dirs)

class Headers(InterpreterObject):

    def __init__(self, src_subdir, sources, kwargs):
        InterpreterObject.__init__(self)
        self.sources = sources
        self.source_subdir = src_subdir
        self.install_subdir = kwargs.get('subdir', '')
        self.custom_install_dir = kwargs.get('install_dir', None)
        if self.custom_install_dir is not None:
            if not isinstance(self.custom_install_dir, str):
                raise InterpreterException('Custom_install_dir must be a string.')

    def set_install_subdir(self, subdir):
        self.install_subdir = subdir

    def get_install_subdir(self):
        return self.install_subdir

    def get_source_subdir(self):
        return self.source_subdir

    def get_sources(self):
        return self.sources

    def get_custom_install_dir(self):
        return self.custom_install_dir

class Data(InterpreterObject):
    def __init__(self, in_sourcetree, source_subdir, sources, kwargs):
        InterpreterObject.__init__(self)
        self.in_sourcetree = in_sourcetree
        self.source_subdir = source_subdir
        self.sources = sources
        kwsource = kwargs.get('sources', [])
        if not isinstance(kwsource, list):
            kwsource = [kwsource]
        self.sources += kwsource
        check_stringlist(self.sources)
        self.install_dir = kwargs.get('install_dir', None)
        if not isinstance(self.install_dir, str):
            raise InterpreterException('Custom_install_dir must be a string.')

    def get_source_subdir(self):
        return self.source_subdir

    def get_sources(self):
        return self.sources

    def get_install_dir(self):
        return self.install_dir

class InstallDir(InterpreterObject):
    def __init__(self, source_subdir, installable_subdir, install_dir):
        InterpreterObject.__init__(self)
        self.source_subdir = source_subdir
        self.installable_subdir = installable_subdir
        self.install_dir = install_dir

class Man(InterpreterObject):

    def __init__(self, source_subdir, sources, kwargs):
        InterpreterObject.__init__(self)
        self.source_subdir = source_subdir
        self.sources = sources
        self.validate_sources()
        if len(kwargs) > 1:
            raise InvalidArguments('Man function takes at most one keyword arguments.')
        self.custom_install_dir = kwargs.get('install_dir', None)
        if self.custom_install_dir is not None and not isinstance(self.custom_install_dir, str):
            raise InterpreterException('Custom_install_dir must be a string.')

    def validate_sources(self):
        for s in self.sources:
            num = int(s.split('.')[-1])
            if num < 1 or num > 8:
                raise InvalidArguments('Man file must have a file extension of a number between 1 and 8')

    def get_custom_install_dir(self):
        return self.custom_install_dir

    def get_sources(self):
        return self.sources

    def get_source_subdir(self):
        return self.source_subdir

class GeneratedObjectsHolder(InterpreterObject):
    def __init__(self, held_object):
        super().__init__()
        self.held_object = held_object

class BuildTargetHolder(InterpreterObject):
    def __init__(self, target):
        super().__init__()
        self.held_object = target
        self.methods.update({'extract_objects' : self.extract_objects_method,
                             'extract_all_objects' : self.extract_all_objects_method})

    def is_cross(self):
        return self.held_object.is_cross()

    def extract_objects_method(self, args, kwargs):
        gobjs = self.held_object.extract_objects(args)
        return GeneratedObjectsHolder(gobjs)

    def extract_all_objects_method(self, args, kwargs):
        gobjs = self.held_object.extract_all_objects()
        return GeneratedObjectsHolder(gobjs)

class ExecutableHolder(BuildTargetHolder):
    def __init__(self, target):
        super().__init__(target)

class StaticLibraryHolder(BuildTargetHolder):
    def __init__(self, target):
        super().__init__(target)

class SharedLibraryHolder(BuildTargetHolder):
    def __init__(self, target):
        super().__init__(target)

class JarHolder(BuildTargetHolder):
    def __init__(self, target):
        super().__init__(target)

class CustomTargetHolder(InterpreterObject):
    def __init__(self, object_to_hold):
        self.held_object = object_to_hold

    def is_cross(self):
        return self.held_object.is_cross()

    def extract_objects_method(self, args, kwargs):
        gobjs = self.held_object.extract_objects(args)
        return GeneratedObjectsHolder(gobjs)

class RunTargetHolder(InterpreterObject):
    def __init__(self, name, command, args, subdir):
        self.held_object = build.RunTarget(name, command, args, subdir)

class Test(InterpreterObject):
    def __init__(self, name, exe, is_parallel, cmd_args, env, should_fail, valgrind_args, timeout):
        InterpreterObject.__init__(self)
        self.name = name
        self.exe = exe
        self.is_parallel = is_parallel
        self.cmd_args = cmd_args
        self.env = env
        self.should_fail = should_fail
        self.valgrind_args = valgrind_args
        self.timeout = timeout

    def get_exe(self):
        return self.exe

    def get_name(self):
        return self.name

class SubprojectHolder(InterpreterObject):

    def __init__(self, subinterpreter):
        super().__init__()
        self.subinterpreter = subinterpreter
        self.methods.update({'get_variable' : self.get_variable_method,
                            })

    def get_variable_method(self, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Get_variable takes one argument.')
        varname = args[0]
        if not isinstance(varname, str):
            raise InterpreterException('Get_variable takes a string argument.')
        return self.subinterpreter.variables[varname]

class CompilerHolder(InterpreterObject):
    def __init__(self, compiler, env):
        InterpreterObject.__init__(self)
        self.compiler = compiler
        self.environment = env
        self.methods.update({'compiles': self.compiles_method,
                             'get_id': self.get_id_method,
                             'sizeof': self.sizeof_method,
                             'has_header': self.has_header_method,
                             'run' : self.run_method,
                             'has_function' : self.has_function_method,
                             'has_member' : self.has_member_method,
                             'has_type' : self.has_type_method,
                             'alignment' : self.alignment_method,
                             'version' : self.version_method,
                             'cmd_array' : self.cmd_array_method,
                            })

    def version_method(self, args, kwargs):
        return self.compiler.version

    def cmd_array_method(self, args, kwargs):
        return self.compiler.exelist

    def alignment_method(self, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Alignment method takes exactly one positional argument.')
        check_stringlist(args)
        typename = args[0]
        result = self.compiler.alignment(typename, self.environment)
        mlog.log('Checking for alignment of "', mlog.bold(typename), '": ', result, sep='')
        return result

    def run_method(self, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Run method takes exactly one positional argument.')
        check_stringlist(args)
        code = args[0]
        testname = kwargs.get('name', '')
        if not isinstance(testname, str):
            raise InterpreterException('Testname argument must be a string.')
        result = self.compiler.run(code)
        if len(testname) > 0:
            if not result.compiled:
                h = mlog.red('DID NOT COMPILE')
            elif result.returncode == 0:
                h = mlog.green('YES')
            else:
                h = mlog.red('NO (%d)' % result.returncode)
            mlog.log('Checking if "', mlog.bold(testname), '" runs : ', h, sep='')
        return TryRunResultHolder(result)

    def get_id_method(self, args, kwargs):
        return self.compiler.get_id()

    def has_member_method(self, args, kwargs):
        if len(args) != 2:
            raise InterpreterException('Has_member takes exactly two arguments.')
        check_stringlist(args)
        typename = args[0]
        membername = args[1]
        prefix = kwargs.get('prefix', '')
        if not isinstance(prefix, str):
            raise InterpreterException('Prefix argument of has_function must be a string.')
        had = self.compiler.has_member(typename, membername, prefix)
        if had:
            hadtxt = mlog.green('YES')
        else:
            hadtxt = mlog.red('NO')
        mlog.log('Checking whether type "', mlog.bold(typename),
                 '" has member "', mlog.bold(membername), '": ', hadtxt, sep='')
        return had

    def has_function_method(self, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Has_function takes exactly one argument.')
        check_stringlist(args)
        funcname = args[0]
        prefix = kwargs.get('prefix', '')
        if not isinstance(prefix, str):
            raise InterpreterException('Prefix argument of has_function must be a string.')
        had = self.compiler.has_function(funcname, prefix, self.environment)
        if had:
            hadtxt = mlog.green('YES')
        else:
            hadtxt = mlog.red('NO')
        mlog.log('Checking for function "', mlog.bold(funcname), '": ', hadtxt, sep='')
        return had

    def has_type_method(self, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Has_type takes exactly one argument.')
        check_stringlist(args)
        typename = args[0]
        prefix = kwargs.get('prefix', '')
        if not isinstance(prefix, str):
            raise InterpreterException('Prefix argument of has_type must be a string.')
        had = self.compiler.has_type(typename, prefix)
        if had:
            hadtxt = mlog.green('YES')
        else:
            hadtxt = mlog.red('NO')
        mlog.log('Checking for type "', mlog.bold(typename), '": ', hadtxt, sep='')
        return had

    def sizeof_method(self, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Sizeof takes exactly one argument.')
        check_stringlist(args)
        element = args[0]
        prefix = kwargs.get('prefix', '')
        if not isinstance(prefix, str):
            raise InterpreterException('Prefix argument of sizeof must be a string.')
        esize = self.compiler.sizeof(element, prefix, self.environment)
        mlog.log('Checking for size of "%s": %d' % (element, esize))
        return esize

    def compiles_method(self, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('compiles method takes exactly one argument.')
        check_stringlist(args)
        string = args[0]
        testname = kwargs.get('name', '')
        if not isinstance(testname, str):
            raise InterpreterException('Testname argument must be a string.')
        result = self.compiler.compiles(string)
        if len(testname) > 0:
            if result:
                h = mlog.green('YES')
            else:
                h = mlog.red('NO')
            mlog.log('Checking if "', mlog.bold(testname), '" compiles : ', h, sep='')
        return result

    def has_header_method(self, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('has_header method takes exactly one argument.')
        check_stringlist(args)
        string = args[0]
        haz = self.compiler.has_header(string)
        if haz:
            h = mlog.green('YES')
        else:
            h = mlog.red('NO')
        mlog.log('Has header "%s":' % string, h)
        return haz

class ModuleState:
    pass

class ModuleHolder(InterpreterObject):
    def __init__(self, modname, module, interpreter):
        InterpreterObject.__init__(self)
        self.modname = modname
        self.held_object = module
        self.interpreter = interpreter

    def method_call(self, method_name, args, kwargs):
        try:
            fn = getattr(self.held_object, method_name)
        except AttributeError:
            raise InvalidArguments('Module %s does not have method %s.' % (self.modname, method_name))
        state = ModuleState()
        state.build_to_src = os.path.relpath(self.interpreter.environment.get_source_dir(),
                                             self.interpreter.environment.get_build_dir())
        state.subdir = self.interpreter.subdir
        state.environment = self.interpreter.environment
        state.project_name = self.interpreter.build.project_name
        state.compilers = self.interpreter.build.compilers
        state.targets = self.interpreter.build.targets
        state.headers = self.interpreter.build.get_headers()
        state.man = self.interpreter.build.get_man()
        state.pkgconfig_gens = self.interpreter.build.pkgconfig_gens
        state.global_args = self.interpreter.build.global_args
        value = fn(state, args, kwargs)
        return self.interpreter.module_method_callback(value)

class MesonMain(InterpreterObject):
    def __init__(self, build, interpreter):
        InterpreterObject.__init__(self)
        self.build = build
        self.interpreter = interpreter
        self.methods.update({'get_compiler': self.get_compiler_method,
                             'is_cross_build' : self.is_cross_build_method,
                             'has_exe_wrapper' : self.has_exe_wrapper_method,
                             'is_unity' : self.is_unity_method,
                             'is_subproject' : self.is_subproject_method,
                             'current_source_dir' : self.current_source_dir_method,
                             'current_build_dir' : self.current_build_dir_method,
                             'source_root' : self.source_root_method,
                             'build_root' : self.build_root_method,
                             'add_install_script' : self.add_install_script_method,
                             'install_dependency_manifest': self.install_dependency_manifest_method,
                             'project_version': self.project_version_method,
                            })

    def add_install_script_method(self, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Set_install_script takes exactly one argument.')
        check_stringlist(args)
        scriptbase = args[0]
        scriptfile = os.path.join(self.interpreter.environment.source_dir,
                                  self.interpreter.subdir, scriptbase)
        if not os.path.isfile(scriptfile):
            raise InterpreterException('Can not find install script %s.' % scriptbase)
        self.build.install_scripts.append(build.InstallScript([scriptfile]))

    def current_source_dir_method(self, args, kwargs):
        src = self.interpreter.environment.source_dir
        sub = self.interpreter.subdir
        if sub == '':
            return src
        return os.path.join(src, sub)

    def current_build_dir_method(self, args, kwargs):
        src = self.interpreter.environment.build_dir
        sub = self.interpreter.subdir
        if sub == '':
            return src
        return os.path.join(src, sub)

    def source_root_method(self, args, kwargs):
        return self.interpreter.environment.source_dir

    def build_root_method(self, args, kwargs):
        return self.interpreter.environment.build_dir

    def has_exe_wrapper_method(self, args, kwargs):
        if self.is_cross_build_method(None, None) and 'binaries' in self.build.environment.cross_info.config:
            return 'exe_wrap' in self.build.environment.cross_info.config['binaries']
        return True  # This is semantically confusing.

    def is_cross_build_method(self, args, kwargs):
        return self.build.environment.is_cross_build()

    def get_compiler_method(self, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('get_compiler_method must have one and only one argument.')
        cname = args[0]
        native = kwargs.get('native', None)
        if native is None:
            if self.build.environment.is_cross_build():
                native = False
            else:
                native = True
        if not isinstance(native, bool):
            raise InterpreterException('Type of "native" must be a boolean.')
        if native:
            clist = self.build.compilers
        else:
            clist = self.build.cross_compilers
        for c in clist:
            if c.get_language() == cname:
                return CompilerHolder(c, self.build.environment)
        raise InterpreterException('Tried to access compiler for unspecified language "%s".' % cname)

    def is_unity_method(self, args, kwargs):
        return self.build.environment.coredata.unity

    def is_subproject_method(self, args, kwargs):
        return self.interpreter.is_subproject()

    def install_dependency_manifest_method(self, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Must specify manifest install file name')
        if not isinstance(args[0], str):
            raise InterpreterException('Argument must be a string.')
        self.build.dep_manifest_name = args[0]

    def project_version_method(self, args, kwargs):
        return self.build.dep_manifest[self.interpreter.active_projectname]

class Interpreter():

    def __init__(self, build, subproject='', subdir='', subproject_dir='subprojects'):
        self.build = build
        self.subproject = subproject
        self.subdir = subdir
        self.source_root = build.environment.get_source_dir()
        self.subproject_dir = subproject_dir
        option_file = os.path.join(self.source_root, self.subdir, 'meson_options.txt')
        if os.path.exists(option_file):
            oi = optinterpreter.OptionInterpreter(self.subproject, \
                                                  self.build.environment.cmd_line_options)
            oi.process(option_file)
            self.build.environment.merge_options(oi.options)
        mesonfile = os.path.join(self.source_root, self.subdir, environment.build_filename)
        if not os.path.isfile(mesonfile):
            raise InvalidArguments('Missing Meson file in %s' % mesonfile)
        code = open(mesonfile).read()
        if len(code.strip()) == 0:
            raise InvalidCode('Builder file is empty.')
        assert(isinstance(code, str))
        try:
            self.ast = mparser.Parser(code).parse()
        except coredata.MesonException as me:
            me.file = environment.build_filename
            raise me
        self.sanity_check_ast()
        self.variables = {}
        self.builtin = {}
        self.builtin['build_machine'] = BuildMachine()
        if not self.build.environment.is_cross_build():
            self.builtin['host_machine'] = self.builtin['build_machine']
            self.builtin['target_machine'] = self.builtin['build_machine']
        else:
            cross_info = self.build.environment.cross_info
            if cross_info.has_host():
                self.builtin['host_machine'] = CrossMachineInfo(cross_info.config['host_machine'])
            else:
                self.builtin['host_machine'] = self.builtin['build_machine']
            if cross_info.has_target():
                self.builtin['target_machine'] = CrossMachineInfo(cross_info.config['target_machine'])
            else:
                self.builtin['target_machine'] = self.builtin['host_machine']
        self.builtin['meson'] = MesonMain(build, self)
        self.environment = build.environment
        self.build_func_dict()
        self.build_def_files = [os.path.join(self.subdir, environment.build_filename)]
        self.coredata = self.environment.get_coredata()
        self.generators = []
        self.visited_subdirs = {}
        self.global_args_frozen = False
        self.subprojects = {}
        self.subproject_stack = []

    def build_func_dict(self):
        self.funcs = {'project' : self.func_project,
                      'message' : self.func_message,
                      'error' : self.func_error,
                      'executable': self.func_executable,
                      'dependency' : self.func_dependency,
                      'static_library' : self.func_static_lib,
                      'shared_library' : self.func_shared_lib,
                      'jar' : self.func_jar,
                      'build_target': self.func_build_target,
                      'custom_target' : self.func_custom_target,
                      'run_target' : self.func_run_target,
                      'generator' : self.func_generator,
                      'test' : self.func_test,
                      'install_headers' : self.func_install_headers,
                      'install_man' : self.func_install_man,
                      'subdir' : self.func_subdir,
                      'install_data' : self.func_install_data,
                      'install_subdir' : self.func_install_subdir,
                      'configure_file' : self.func_configure_file,
                      'include_directories' : self.func_include_directories,
                      'add_global_arguments' : self.func_add_global_arguments,
                      'add_languages' : self.func_add_languages,
                      'find_program' : self.func_find_program,
                      'find_library' : self.func_find_library,
                      'configuration_data' : self.func_configuration_data,
                      'run_command' : self.func_run_command,
                      'gettext' : self.func_gettext,
                      'option' : self.func_option,
                      'get_option' : self.func_get_option,
                      'subproject' : self.func_subproject,
                      'pkgconfig_gen' : self.func_pkgconfig_gen,
                      'vcs_tag' : self.func_vcs_tag,
                      'set_variable' : self.func_set_variable,
                      'import' : self.func_import,
                      'files' : self.func_files,
                      'declare_dependency': self.func_declare_dependency,
                     }

    def module_method_callback(self, invalues):
        unwrap_single = False
        if invalues is None:
            return
        if not isinstance(invalues, list):
            unwrap_single = True
            invalues = [invalues]
        outvalues = []
        for v in invalues:
            if isinstance(v, build.CustomTarget):
                if v.name in self.build.targets:
                    raise InterpreterException('Tried to create target %s which already exists.' % v.name)
                self.build.targets[v.name] = v
                outvalues.append(CustomTargetHolder(v))
            elif isinstance(v, int) or isinstance(v, str):
                outvalues.append(v)
            elif isinstance(v, build.Executable):
                if v.name in self.build.targets:
                    raise InterpreterException('Tried to create target %s which already exists.' % v.name)
                self.build.targets[v.name] = v
                outvalues.append(ExecutableHolder(v))
            elif isinstance(v, list):
                outvalues.append(self.module_method_callback(v))
            elif isinstance(v, build.GeneratedList):
                outvalues.append(GeneratedListHolder(v))
            elif isinstance(v, build.RunTarget):
                if v.name in self.build.targets:
                    raise InterpreterException('Tried to create target %s which already exists.' % v.name)
                self.build.targets[v.name] = v
            elif isinstance(v, build.InstallScript):
                self.build.install_scripts.append(v)
            else:
                print(v)
                raise InterpreterException('Module returned a value of unknown type.')
        if len(outvalues) == 1 and unwrap_single:
            return outvalues[0]
        return outvalues

    def get_build_def_files(self):
        return self.build_def_files

    def get_variables(self):
        return self.variables

    def sanity_check_ast(self):
        if not isinstance(self.ast, mparser.CodeBlockNode):
            raise InvalidCode('AST is of invalid type. Possibly a bug in the parser.')
        if len(self.ast.lines) == 0:
            raise InvalidCode('No statements in code.')
        first = self.ast.lines[0]
        if not isinstance(first, mparser.FunctionNode) or first.func_name != 'project':
            raise InvalidCode('First statement must be a call to project')

    def run(self):
        self.evaluate_codeblock(self.ast)
        mlog.log('Build targets in project:', mlog.bold(str(len(self.build.targets))))

    def evaluate_codeblock(self, node):
        if node is None:
            return
        if not isinstance(node, mparser.CodeBlockNode):
            e = InvalidCode('Tried to execute a non-codeblock. Possibly a bug in the parser.')
            e.lineno = node.lineno
            e.colno = node.colno
            raise e
        statements = node.lines
        i = 0
        while i < len(statements):
            cur = statements[i]
            try:
                self.evaluate_statement(cur)
            except Exception as e:
                if not(hasattr(e, 'lineno')):
                    e.lineno = cur.lineno
                    e.colno = cur.colno
                    e.file = os.path.join(self.subdir, 'meson.build')
                raise e
            i += 1 # In THE FUTURE jump over blocks and stuff.

    def get_variable(self, varname):
        if varname in self.builtin:
            return self.builtin[varname]
        if varname in self.variables:
            return self.variables[varname]
        raise InvalidCode('Unknown variable "%s".' % varname)

    def func_set_variable(self, node, args, kwargs):
        if len(args) != 2:
            raise InvalidCode('Set_variable takes two arguments.')
        varname = args[0]
        value = self.to_native(args[1])
        self.set_variable(varname, value)

    @stringArgs
    @noKwargs
    def func_import(self, node, args, kwargs):
        if len(args) != 1:
            raise InvalidCode('Import takes one argument.')
        modname = args[0]
        if not modname in self.environment.coredata.modules:
            module = importlib.import_module('modules.' + modname).initialize()
            self.environment.coredata.modules[modname] = module
        return ModuleHolder(modname, self.environment.coredata.modules[modname], self)

    @stringArgs
    @noKwargs
    def func_files(self, node, args, kwargs):
        return [mesonlib.File.from_source_file(self.environment.source_dir, self.subdir, fname) for fname in args]

    @noPosargs
    def func_declare_dependency(self, node, args, kwargs):
        incs = kwargs.get('include_directories', [])
        if not isinstance(incs, list):
            incs = [incs]
        libs = kwargs.get('link_with', [])
        if not isinstance(libs, list):
            libs = [libs]
        sources = kwargs.get('sources', [])
        if not isinstance(sources, list):
            sources = [sources]
        sources = self.source_strings_to_files(self.flatten(sources))
        dep = dependencies.InternalDependency(incs, libs, sources)
        return InternalDependencyHolder(dep)

    def set_variable(self, varname, variable):
        if variable is None:
            raise InvalidCode('Can not assign None to variable.')
        if not isinstance(varname, str):
            raise InvalidCode('First argument to set_variable must be a string.')
        if not self.is_assignable(variable):
            raise InvalidCode('Assigned value not of assignable type.')
        if re.fullmatch('[_a-zA-Z][_0-9a-zA-Z]*', varname) is None:
            raise InvalidCode('Invalid variable name: ' + varname)
        if varname in self.builtin:
            raise InvalidCode('Tried to overwrite internal variable "%s"' % varname)
        self.variables[varname] = variable

    def evaluate_statement(self, cur):
        if isinstance(cur, mparser.FunctionNode):
            return self.function_call(cur)
        elif isinstance(cur, mparser.AssignmentNode):
            return self.assignment(cur)
        elif isinstance(cur, mparser.MethodNode):
            return self.method_call(cur)
        elif isinstance(cur, mparser.StringNode):
            return cur.value
        elif isinstance(cur, mparser.BooleanNode):
            return cur.value
        elif isinstance(cur, mparser.IfClauseNode):
            return self.evaluate_if(cur)
        elif isinstance(cur, mparser.IdNode):
            return self.get_variable(cur.value)
        elif isinstance(cur, mparser.ComparisonNode):
            return self.evaluate_comparison(cur)
        elif isinstance(cur, mparser.ArrayNode):
            return self.evaluate_arraystatement(cur)
        elif isinstance(cur, mparser.NumberNode):
            return cur.value
        elif isinstance(cur, mparser.AndNode):
            return self.evaluate_andstatement(cur)
        elif isinstance(cur, mparser.OrNode):
            return self.evaluate_orstatement(cur)
        elif isinstance(cur, mparser.NotNode):
            return self.evaluate_notstatement(cur)
        elif isinstance(cur, mparser.UMinusNode):
            return self.evaluate_uminusstatement(cur)
        elif isinstance(cur, mparser.ArithmeticNode):
            return self.evaluate_arithmeticstatement(cur)
        elif isinstance(cur, mparser.ForeachClauseNode):
            return self.evaluate_foreach(cur)
        elif isinstance(cur, mparser.PlusAssignmentNode):
            return self.evaluate_plusassign(cur)
        elif isinstance(cur, mparser.IndexNode):
            return self.evaluate_indexing(cur)
        elif self.is_elementary_type(cur):
            return cur
        else:
            raise InvalidCode("Unknown statement.")

    def validate_arguments(self, args, argcount, arg_types):
        if argcount is not None:
            if argcount != len(args):
                raise InvalidArguments('Expected %d arguments, got %d.' %
                                       (argcount, len(args)))
        for i in range(min(len(args), len(arg_types))):
            wanted = arg_types[i]
            actual = args[i]
            if wanted != None:
                if not isinstance(actual, wanted):
                    raise InvalidArguments('Incorrect argument type.')

    def func_run_command(self, node, args, kwargs):
        if len(args) < 1:
            raise InterpreterException('Not enough arguments')
        cmd = args[0]
        cargs = args[1:]
        if isinstance(cmd, ExternalProgramHolder):
            cmd = cmd.get_command()
        elif isinstance(cmd, str):
            cmd = [cmd]
        else:
            raise InterpreterException('First argument is of incorrect type.')
        check_stringlist(cargs, 'Run_command arguments must be strings.')
        args = cmd + cargs
        in_builddir = kwargs.get('in_builddir', False)
        if not isinstance(in_builddir, bool):
            raise InterpreterException('in_builddir must be boolean.')
        return RunProcess(args, self.environment.source_dir, self.environment.build_dir,
                          self.subdir, in_builddir)

    @stringArgs
    def func_gettext(self, nodes, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Gettext requires one positional argument (package name).')
        packagename = args[0]
        languages = kwargs.get('languages', None)
        check_stringlist(languages, 'Argument languages must be a list of strings.')
        # TODO: check that elements are strings
        if len(self.build.pot) > 0:
            raise InterpreterException('More than one gettext definition currently not supported.')
        self.build.pot.append((packagename, languages, self.subdir))

    def func_option(self, nodes, args, kwargs):
        raise InterpreterException('Tried to call option() in build description file. All options must be in the option file.')

    def func_pkgconfig_gen(self, nodes, args, kwargs):
        if len(args) > 0:
            raise InterpreterException('Pkgconfig_gen takes no positional arguments.')
        libs = kwargs.get('libraries', [])
        if not isinstance(libs, list):
            libs = [libs]
        for l in libs:
            if not (isinstance(l, SharedLibraryHolder) or isinstance(l, StaticLibraryHolder)):
                raise InterpreterException('Library argument not a library object.')
        subdirs = kwargs.get('subdirs', ['.'])
        if not isinstance(subdirs, list):
            subdirs = [subdirs]
        for h in subdirs:
            if not isinstance(h, str):
                raise InterpreterException('Header argument not string.')
        version = kwargs.get('version', '')
        if not isinstance(version, str):
            raise InterpreterException('Version must be a string.')
        name = kwargs.get('name', None)
        if not isinstance(name, str):
            raise InterpreterException('Name not specified.')
        filebase = kwargs.get('filebase', name)
        if not isinstance(filebase, str):
            raise InterpreterException('Filebase must be a string.')
        description = kwargs.get('description', None)
        if not isinstance(description, str):
            raise InterpreterException('Description is not a string.')
        p = build.PkgConfigGenerator(libs, subdirs, name, description, version, filebase)
        self.build.pkgconfig_gens.append(p)

    @stringArgs
    @noKwargs
    def func_subproject(self, nodes, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Subproject takes exactly one argument')
        dirname = args[0]
        if self.subdir != '':
            segs = os.path.split(self.subdir)
            if len(segs) != 2 or segs[0] != self.subproject_dir:
                raise InterpreterException('Subprojects must be defined at the root directory.')
        if dirname in self.subproject_stack:
            fullstack = self.subproject_stack + [dirname]
            incpath = ' => '.join(fullstack)
            raise InterpreterException('Recursive include of subprojects: %s.' % incpath)
        if dirname in self.subprojects:
            return self.subprojects[dirname]
        subdir = os.path.join(self.subproject_dir, dirname)
        r = wrap.Resolver(os.path.join(self.build.environment.get_source_dir(), self.subproject_dir))
        resolved = r.resolve(dirname)
        if resolved is None:
            raise InterpreterException('Subproject directory does not exist and can not be downloaded.')
        subdir = os.path.join(self.subproject_dir, resolved)
        os.makedirs(os.path.join(self.build.environment.get_build_dir(), subdir), exist_ok=True)
        self.global_args_frozen = True
        mlog.log('\nExecuting subproject ', mlog.bold(dirname), '.\n', sep='')
        subi = Interpreter(self.build, dirname, subdir, self.subproject_dir)
        subi.subprojects = self.subprojects

        subi.subproject_stack = self.subproject_stack + [dirname]
        current_active = self.active_projectname
        subi.run()
        self.active_projectname = current_active
        mlog.log('\nSubproject', mlog.bold(dirname), 'finished.')
        self.build.subprojects[dirname] = True
        self.subprojects.update(subi.subprojects)
        self.subprojects[dirname] = SubprojectHolder(subi)
        self.build_def_files += subi.build_def_files
        return self.subprojects[dirname]

    @stringArgs
    @noKwargs
    def func_get_option(self, nodes, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Argument required for get_option.')
        optname = args[0]
        if optname not in coredata.builtin_options and self.is_subproject():
            optname = self.subproject + ':' + optname
        try:
            return self.environment.get_coredata().get_builtin_option(optname)
        except RuntimeError:
            pass
        if optname not in self.environment.coredata.user_options:
            raise InterpreterException('Tried to access unknown option "%s".' % optname)
        return self.environment.coredata.user_options[optname].value

    @noKwargs
    def func_configuration_data(self, node, args, kwargs):
        if len(args) != 0:
            raise InterpreterException('configuration_data takes no arguments')
        return ConfigurationDataHolder()

    @stringArgs
    def func_project(self, node, args, kwargs):
        if len(args) < 2:
            raise InvalidArguments('Not enough arguments to project(). Needs at least the project name and one language')

        if not self.is_subproject():
            self.build.project_name = args[0]
        self.active_projectname = args[0]
        self.build.dep_manifest[args[0]] = kwargs.get('version', 'undefined')
        if self.subproject in self.build.projects:
            raise InvalidCode('Second call to project().')
        if not self.is_subproject() and 'subproject_dir' in kwargs:
            self.subproject_dir = kwargs['subproject_dir']

        self.build.projects[self.subproject] = args[0]
        mlog.log('Project name: ', mlog.bold(args[0]), sep='')
        self.add_languages(node, args[1:])
        langs = self.coredata.compilers.keys()
        if 'vala' in langs:
            if not 'c' in langs:
                raise InterpreterException('Compiling Vala requires a C compiler')

    @noKwargs
    @stringArgs
    def func_add_languages(self, node, args, kwargs):
        self.add_languages(node, args)

    @noKwargs
    def func_message(self, node, args, kwargs):
        # reduce arguments again to avoid flattening posargs
        (posargs, kwargs) = self.reduce_arguments(node.args)
        if len(posargs) != 1:
            raise InvalidArguments('Expected 1 argument, got %d' % len(posargs))

        arg = posargs[0]
        if isinstance(arg, list):
            argstr = stringifyUserArguments(arg)
        elif isinstance(arg, str):
            argstr = arg
        elif isinstance(arg, int):
            argstr = str(arg)
        else:
            raise InvalidArguments('Function accepts only strings, integers, lists and lists thereof.')

        mlog.log(mlog.bold('Message:'), argstr)
        return


    @noKwargs
    def func_error(self, node, args, kwargs):
        self.validate_arguments(args, 1, [str])
        raise InterpreterException('Error encountered: ' + args[0])

    def add_languages(self, node, args):
        need_cross_compiler = self.environment.is_cross_build() and self.environment.cross_info.need_cross_compiler()
        for lang in args:
            lang = lang.lower()
            if lang in self.coredata.compilers:
                comp = self.coredata.compilers[lang]
                cross_comp = self.coredata.cross_compilers.get(lang, None)
            else:
                cross_comp = None
                if lang == 'c':
                    comp = self.environment.detect_c_compiler(False)
                    if need_cross_compiler:
                        cross_comp = self.environment.detect_c_compiler(True)
                elif lang == 'cpp':
                    comp = self.environment.detect_cpp_compiler(False)
                    if need_cross_compiler:
                        cross_comp = self.environment.detect_cpp_compiler(True)
                elif lang == 'objc':
                    comp = self.environment.detect_objc_compiler(False)
                    if need_cross_compiler:
                        cross_comp = self.environment.detect_objc_compiler(True)
                elif lang == 'objcpp':
                    comp = self.environment.detect_objcpp_compiler(False)
                    if need_cross_compiler:
                        cross_comp = self.environment.detect_objcpp_compiler(True)
                elif lang == 'java':
                    comp = self.environment.detect_java_compiler()
                    if need_cross_compiler:
                        cross_comp = comp # Java is platform independent.
                elif lang == 'cs':
                    comp = self.environment.detect_cs_compiler()
                    if need_cross_compiler:
                        cross_comp = comp # C# is platform independent.
                elif lang == 'vala':
                    comp = self.environment.detect_vala_compiler()
                    if need_cross_compiler:
                        cross_comp = comp # Vala is too (I think).
                elif lang == 'rust':
                    comp = self.environment.detect_rust_compiler()
                    if need_cross_compiler:
                        cross_comp = comp # FIXME, probably not correct.
                elif lang == 'fortran':
                    comp = self.environment.detect_fortran_compiler(False)
                    if need_cross_compiler:
                        cross_comp = self.environment.detect_fortran_compiler(True)
                else:
                    raise InvalidCode('Tried to use unknown language "%s".' % lang)
                comp.sanity_check(self.environment.get_scratch_dir())
                self.coredata.compilers[lang] = comp
                if cross_comp is not None:
                    self.coredata.cross_compilers[lang] = cross_comp
            mlog.log('Native %s compiler: ' % lang, mlog.bold(' '.join(comp.get_exelist())), ' (%s %s)' % (comp.id, comp.version), sep='')
            if not comp.get_language() in self.coredata.external_args:
                (ext_compile_args, ext_link_args) = environment.get_args_from_envvars(comp.get_language())
                self.coredata.external_args[comp.get_language()] = ext_compile_args
                self.coredata.external_link_args[comp.get_language()] = ext_link_args
            self.build.add_compiler(comp)
            if need_cross_compiler:
                mlog.log('Cross %s compiler: ' % lang, mlog.bold(' '.join(cross_comp.get_exelist())), ' (%s %s)' % (cross_comp.id, cross_comp.version), sep='')
                self.build.add_cross_compiler(cross_comp)
            if self.environment.is_cross_build() and not need_cross_compiler:
                self.build.add_cross_compiler(comp)

    def func_find_program(self, node, args, kwargs):
        self.validate_arguments(args, 1, [str])
        required = kwargs.get('required', True)
        if not isinstance(required, bool):
            raise InvalidArguments('"required" argument must be a boolean.')
        exename = args[0]
        if exename in self.coredata.ext_progs and\
           self.coredata.ext_progs[exename].found():
            return ExternalProgramHolder(self.coredata.ext_progs[exename])
        # Search for scripts relative to current subdir.
        search_dir = os.path.join(self.environment.get_source_dir(), self.subdir)
        extprog = dependencies.ExternalProgram(exename, search_dir=search_dir)
        progobj = ExternalProgramHolder(extprog)
        self.coredata.ext_progs[exename] = extprog
        if required and not progobj.found():
            raise InvalidArguments('Program "%s" not found.' % exename)
        return progobj

    def func_find_library(self, node, args, kwargs):
        self.validate_arguments(args, 1, [str])
        required = kwargs.get('required', True)
        if not isinstance(required, bool):
            raise InvalidArguments('"required" argument must be a boolean.')
        libname = args[0]
        if libname in self.coredata.ext_libs and\
           self.coredata.ext_libs[libname].found():
            return ExternalLibraryHolder(self.coredata.ext_libs[libname])
        if 'dirs' in kwargs:
            search_dirs = kwargs['dirs']
            if not isinstance(search_dirs, list):
                search_dirs = [search_dirs]
            for i in search_dirs:
                if not isinstance(i, str):
                    raise InvalidCode('Directory entry is not a string.')
                if not os.path.isabs(i):
                    raise InvalidCode('Search directory %s is not an absolute path.' % i)
        else:
            search_dirs = None
        result = self.environment.find_library(libname, search_dirs)
        extlib = dependencies.ExternalLibrary(libname, result)
        libobj = ExternalLibraryHolder(extlib)
        self.coredata.ext_libs[libname] = extlib
        if required and not libobj.found():
            raise InvalidArguments('External library "%s" not found.' % libname)
        return libobj

    def func_dependency(self, node, args, kwargs):
        self.validate_arguments(args, 1, [str])
        name = args[0]
        identifier = dependencies.get_dep_identifier(name, kwargs)
        if identifier in self.coredata.deps:
            dep = self.coredata.deps[identifier]
        else:
            dep = dependencies.Dependency() # Returns always false for dep.found()
        if not dep.found():
            dep = dependencies.find_external_dependency(name, self.environment, kwargs)
        self.coredata.deps[identifier] = dep
        return DependencyHolder(dep)

    def func_executable(self, node, args, kwargs):
        return self.build_target(node, args, kwargs, ExecutableHolder)

    def func_static_lib(self, node, args, kwargs):
        return self.build_target(node, args, kwargs, StaticLibraryHolder)

    def func_shared_lib(self, node, args, kwargs):
        return self.build_target(node, args, kwargs, SharedLibraryHolder)

    def func_jar(self, node, args, kwargs):
        return self.build_target(node, args, kwargs, JarHolder)

    def func_build_target(self, node, args, kwargs):
        if 'target_type' not in kwargs:
            raise InterpreterException('Missing target_type keyword argument')
        target_type = kwargs.pop('target_type')
        if target_type == 'executable':
            return self.func_executable(node, args, kwargs)
        elif target_type == 'shared_library':
            return self.func_shared_lib(node, args, kwargs)
        elif target_type == 'static_library':
            return self.func_static_lib(node, args, kwargs)
        elif target_type == 'jar':
            return self.func_jar(node, args, kwargs)
        else:
            raise InterpreterException('Unknown target_type.')

    def func_vcs_tag(self, node, args, kwargs):
        fallback = kwargs.pop('fallback', None)
        if not isinstance(fallback, str):
            raise InterpreterException('Keyword argument must exist and be a string.')
        replace_string = kwargs.pop('replace_string', '@VCS_TAG@')
        regex_selector = '(.*)' # default regex selector for custom command: use complete output
        vcs_cmd = kwargs.get('command', None)
        if vcs_cmd and not isinstance(vcs_cmd, list):
            vcs_cmd = [vcs_cmd]
        source_dir = os.path.normpath(os.path.join(self.environment.get_source_dir(), self.subdir))
        if vcs_cmd:
            # Is the command an executable in path or maybe a script in the source tree?
            vcs_cmd[0] = shutil.which(vcs_cmd[0]) or os.path.join(source_dir, vcs_cmd[0])
        else:
            vcs = mesonlib.detect_vcs(source_dir)
            if vcs:
                mlog.log('Found %s repository at %s' % (vcs['name'], vcs['wc_dir']))
                vcs_cmd = vcs['get_rev'].split()
                regex_selector = vcs['rev_regex']
            else:
                vcs_cmd = [' '] # executing this cmd will fail in vcstagger.py and force to use the fallback string
        scriptfile = os.path.join(self.environment.get_script_dir(), 'vcstagger.py')
        # vcstagger.py parameters: infile, outfile, fallback, source_dir, replace_string, regex_selector, command...
        kwargs['command'] = [sys.executable, scriptfile, '@INPUT0@', '@OUTPUT0@', fallback, source_dir, replace_string, regex_selector] + vcs_cmd
        kwargs.setdefault('build_always', True)
        return self.func_custom_target(node, [kwargs['output']], kwargs)

    @stringArgs
    def func_custom_target(self, node, args, kwargs):
        if len(args) != 1:
            raise InterpreterException('Incorrect number of arguments')
        name = args[0]
        tg = CustomTargetHolder(build.CustomTarget(name, self.subdir, kwargs))
        self.add_target(name, tg.held_object)
        return tg

    @stringArgs
    @noKwargs
    def func_run_target(self, node, args, kwargs):
        if len(args) < 2:
            raise InterpreterException('Incorrect number of arguments')
        name = args[0]
        command = args[1]
        cmd_args = args[2:]
        tg = RunTargetHolder(name, command, cmd_args, self.subdir)
        self.add_target(name, tg.held_object)
        return tg

    def func_generator(self, node, args, kwargs):
        gen = GeneratorHolder(self, args, kwargs)
        self.generators.append(gen)
        return gen

    def func_test(self, node, args, kwargs):
        if len(args) != 2:
            raise InterpreterException('Incorrect number of arguments')
        if not isinstance(args[0], str):
            raise InterpreterException('First argument of test must be a string.')
        if not isinstance(args[1], (ExecutableHolder, JarHolder, ExternalProgramHolder)):
            raise InterpreterException('Second argument must be executable.')
        par = kwargs.get('is_parallel', True)
        if not isinstance(par, bool):
            raise InterpreterException('Keyword argument is_parallel must be a boolean.')
        cmd_args = kwargs.get('args', [])
        if not isinstance(cmd_args, list):
            cmd_args = [cmd_args]
        for i in cmd_args:
            if not isinstance(i, (str, mesonlib.File)):
                raise InterpreterException('Command line arguments must be strings')
        envlist = kwargs.get('env', [])
        if not isinstance(envlist, list):
            envlist = [envlist]
        env = {}
        for e in envlist:
            if '=' not in e:
                raise InterpreterException('Env var definition must be of type key=val.')
            (k, val) = e.split('=', 1)
            k = k.strip()
            val = val.strip()
            if ' ' in k:
                raise InterpreterException('Env var key must not have spaces in it.')
            env[k] = val
        valgrind_args = kwargs.get('valgrind_args', [])
        if not isinstance(valgrind_args, list):
            valgrind_args = [valgrind_args]
        for a in valgrind_args:
            if not isinstance(a, str):
                raise InterpreterException('Valgrind_arg not a string.')
        should_fail = kwargs.get('should_fail', False)
        if not isinstance(should_fail, bool):
            raise InterpreterException('Keyword argument should_fail must be a boolean.')
        timeout = kwargs.get('timeout', 30)
        if not isinstance(timeout, int):
            raise InterpreterException('Timeout must be an integer.')
        t = Test(args[0], args[1].held_object, par, cmd_args, env, should_fail, valgrind_args, timeout)
        self.build.tests.append(t)
        mlog.debug('Adding test "', mlog.bold(args[0]), '".', sep='')

    @stringArgs
    def func_install_headers(self, node, args, kwargs):
        h = Headers(self.subdir, args, kwargs)
        self.build.headers.append(h)
        return h

    @stringArgs
    def func_install_man(self, node, args, kwargs):
        m = Man(self.subdir, args, kwargs)
        self.build.man.append(m)
        return m

    @noKwargs
    def func_subdir(self, node, args, kwargs):
        self.validate_arguments(args, 1, [str])
        if '..' in args[0]:
            raise InvalidArguments('Subdir contains ..')
        if self.subdir == '' and args[0] == self.subproject_dir:
            raise InvalidArguments('Must not go into subprojects dir with subdir(), use subproject() instead.')
        prev_subdir = self.subdir
        subdir = os.path.join(prev_subdir, args[0])
        if subdir in self.visited_subdirs:
            raise InvalidArguments('Tried to enter directory "%s", which has already been visited.'\
                                   % subdir)
        self.visited_subdirs[subdir] = True
        self.subdir = subdir
        try:
            os.mkdir(os.path.join(self.environment.build_dir, subdir))
        except FileExistsError:
            pass
        buildfilename = os.path.join(self.subdir, environment.build_filename)
        self.build_def_files.append(buildfilename)
        absname = os.path.join(self.environment.get_source_dir(), buildfilename)
        if not os.path.isfile(absname):
            raise InterpreterException('Nonexistant build def file %s.' % buildfilename)
        code = open(absname).read()
        assert(isinstance(code, str))
        try:
            codeblock = mparser.Parser(code).parse()
        except coredata.MesonException as me:
            me.file = buildfilename
            raise me
        self.evaluate_codeblock(codeblock)
        self.subdir = prev_subdir

    @stringArgs
    def func_install_data(self, node, args, kwargs):
        data = Data(True, self.subdir, args, kwargs)
        self.build.data.append(data)
        return data

    @stringArgs
    def func_install_subdir(self, node, args, kwargs):
        if len(args) != 1:
            raise InvalidArguments('Install_subdir requires exactly one argument.')
        if not 'install_dir' in kwargs:
            raise InvalidArguments('Missing keyword argument install_dir')
        install_dir = kwargs['install_dir']
        if not isinstance(install_dir, str):
            raise InvalidArguments('Keyword argument install_dir not a string.')
        idir = InstallDir(self.subdir, args[0], install_dir)
        self.build.install_dirs.append(idir)
        return idir

    def func_configure_file(self, node, args, kwargs):
        if len(args) > 0:
            raise InterpreterException("configure_file takes only keyword arguments.")
        if not 'input' in kwargs:
            raise InterpreterException('Required keyword argument "input" not defined.')
        if not 'output' in kwargs:
            raise InterpreterException('Required keyword argument "output" not defined.')
        inputfile = kwargs['input']
        output = kwargs['output']
        if not isinstance(inputfile, str):
            raise InterpreterException('Input must be a string.')
        if not isinstance(output, str):
            raise InterpreterException('Output must be a string.')
        if 'configuration' in kwargs:
            conf = kwargs['configuration']
            if not isinstance(conf, ConfigurationDataHolder):
                raise InterpreterException('Argument "configuration" is not of type configuration_data')

            conffile = os.path.join(self.subdir, inputfile)
            if conffile not in self.build_def_files:
                self.build_def_files.append(conffile)
            os.makedirs(os.path.join(self.environment.build_dir, self.subdir), exist_ok=True)
            ifile_abs = os.path.join(self.environment.source_dir, self.subdir, inputfile)
            ofile_abs = os.path.join(self.environment.build_dir, self.subdir, output)
            mesonlib.do_conf_file(ifile_abs, ofile_abs, conf.held_object)
            conf.mark_used()
        elif 'command' in kwargs:
            res = self.func_run_command(node, kwargs['command'], {})
            if res.returncode != 0:
                raise InterpreterException('Running configure command failed.\n%s\n%s' %
                                           (res.stdout, res.stderr))
        else:
            raise InterpreterException('Configure_file must have either "configuration" or "command".')
        if isinstance(kwargs.get('install_dir', None), str):
            self.build.data.append(Data(False, self.subdir, [output], kwargs))
        return mesonlib.File.from_built_file(self.subdir, output)

    @stringArgs
    @noKwargs
    def func_include_directories(self, node, args, kwargs):
        absbase = os.path.join(self.environment.get_source_dir(), self.subdir)
        for a in args:
            absdir = os.path.join(absbase, a)
            if not os.path.isdir(absdir):
                raise InvalidArguments('Include dir %s does not exist.' % a)
        i = IncludeDirsHolder(self.subdir, args)
        return i

    @stringArgs
    def func_add_global_arguments(self, node, args, kwargs):
        if self.subproject != '':
            raise InvalidCode('Global arguments can not be set in subprojects because there is no way to make that reliable.')
        if self.global_args_frozen:
            raise InvalidCode('Tried to set global arguments after a build target has been declared.\nThis is not permitted. Please declare all global arguments before your targets.')
        if not 'language' in kwargs:
            raise InvalidCode('Missing language definition in add_global_arguments')
        lang = kwargs['language'].lower()
        if lang in self.build.global_args:
            self.build.global_args[lang] += args
        else:
            self.build.global_args[lang] = args

    def flatten(self, args):
        if isinstance(args, mparser.StringNode):
            return args.value
        if isinstance(args, str):
            return args
        if isinstance(args, InterpreterObject):
            return args
        if isinstance(args, int):
            return args
        result = []
        for a in args:
            if isinstance(a, list):
                rest = self.flatten(a)
                result = result + rest
            elif isinstance(a, mparser.StringNode):
                result.append(a.value)
            else:
                result.append(a)
        return result

    def source_strings_to_files(self, sources):
        results = []
        for s in sources:
            if isinstance(s, mesonlib.File) or isinstance(s, GeneratedListHolder) or \
            isinstance(s, CustomTargetHolder):
                pass
            elif isinstance(s, str):
                s = mesonlib.File.from_source_file(self.environment.source_dir, self.subdir, s)
            else:
                raise InterpreterException("Source item is not string or File-type object.")
            results.append(s)
        return results

    def add_target(self, name, tobj):
        if name in coredata.forbidden_target_names:
            raise InvalidArguments('Target name "%s" is reserved for Meson\'s internal use. Please rename.'\
                                   % name)
        # To permit an executable and a shared library to have the
        # same name, such as "foo.exe" and "libfoo.a".
        idname = tobj.get_id()
        if idname in self.build.targets:
            raise InvalidCode('Tried to create target "%s", but a target of that name already exists.' % name)
        self.build.targets[idname] = tobj
        if idname not in self.coredata.target_guids:
            self.coredata.target_guids[idname] = str(uuid.uuid4()).upper()

    def build_target(self, node, args, kwargs, targetholder):
        name = args[0]
        sources = args[1:]
        if self.environment.is_cross_build():
            if kwargs.get('native', False):
                is_cross = False
            else:
                is_cross = True
        else:
            is_cross = False
        try:
            kw_src = self.flatten(kwargs['sources'])
            if not isinstance(kw_src, list):
                kw_src = [kw_src]
        except KeyError:
            kw_src = []
        sources += kw_src
        sources = self.source_strings_to_files(sources)
        objs = self.flatten(kwargs.get('objects', []))
        kwargs['dependencies'] = self.flatten(kwargs.get('dependencies', []))
        if not isinstance(objs, list):
            objs = [objs]
        self.check_sources_exist(os.path.join(self.source_root, self.subdir), sources)
        if targetholder is ExecutableHolder:
            targetclass = build.Executable
        elif targetholder is SharedLibraryHolder:
            targetclass = build.SharedLibrary
        elif targetholder is StaticLibraryHolder:
            targetclass = build.StaticLibrary
        elif targetholder is JarHolder:
            targetclass = build.Jar
        else:
            mlog.debug('Unknown target type:', str(targetholder))
            raise RuntimeError('Unreachable code')
        target = targetclass(name, self.subdir, self.subproject, is_cross, sources, objs, self.environment, kwargs)
        l = targetholder(target)
        self.add_target(name, l.held_object)
        self.global_args_frozen = True
        return l

    def check_sources_exist(self, subdir, sources):
        for s in sources:
            if not isinstance(s, str):
                continue # This means a generated source and they always exist.
            fname = os.path.join(subdir, s)
            if not os.path.isfile(fname):
                raise InterpreterException('Tried to add non-existing source %s.' % s)

    def function_call(self, node):
        func_name = node.func_name
        (posargs, kwargs) = self.reduce_arguments(node.args)
        if func_name in self.funcs:
            return self.funcs[func_name](node, self.flatten(posargs), kwargs)
        else:
            raise InvalidCode('Unknown function "%s".' % func_name)

    def is_assignable(self, value):
        if isinstance(value, InterpreterObject) or \
            isinstance(value, dependencies.Dependency) or\
            isinstance(value, str) or\
            isinstance(value, int) or \
            isinstance(value, list) or \
            isinstance(value, mesonlib.File):
            return True
        return False

    def assignment(self, node):
        assert(isinstance(node, mparser.AssignmentNode))
        var_name = node.var_name
        if not isinstance(var_name, str):
            raise InvalidArguments('Tried to assign value to a non-variable.')
        value = self.evaluate_statement(node.value)
        value = self.to_native(value)
        if not self.is_assignable(value):
            raise InvalidCode('Tried to assign an invalid value to variable.')
        self.set_variable(var_name, value)
        return value

    def reduce_arguments(self, args):
        assert(isinstance(args, mparser.ArgumentNode))
        if args.incorrect_order():
            raise InvalidArguments('All keyword arguments must be after positional arguments.')
        reduced_pos = [self.evaluate_statement(arg) for arg in args.arguments]
        reduced_kw = {}
        for key in args.kwargs.keys():
            if not isinstance(key, str):
                raise InvalidArguments('Keyword argument name is not a string.')
            a = args.kwargs[key]
            reduced_kw[key] = self.evaluate_statement(a)
        if not isinstance(reduced_pos, list):
            reduced_pos = [reduced_pos]
        return (reduced_pos, reduced_kw)

    def string_method_call(self, obj, method_name, args):
        obj = self.to_native(obj)
        if method_name == 'strip':
            return obj.strip()
        elif method_name == 'format':
            return self.format_string(obj, args)
        elif method_name == 'split':
            (posargs, _) = self.reduce_arguments(args)
            if len(posargs) > 1:
                raise InterpreterException('Split()  must have at most one argument.')
            elif len(posargs) == 1:
                s = posargs[0]
                if not isinstance(s, str):
                    raise InterpreterException('Split() argument must be a string')
                return obj.split(s)
            else:
                return obj.split()
        raise InterpreterException('Unknown method "%s" for a string.' % method_name)

    def to_native(self, arg):
        if isinstance(arg, mparser.StringNode) or \
           isinstance(arg, mparser.NumberNode) or \
           isinstance(arg, mparser.BooleanNode):
            return arg.value
        return arg

    def format_string(self, templ, args):
        templ = self.to_native(templ)
        if isinstance(args, mparser.ArgumentNode):
            args = args.arguments
        for (i, arg) in enumerate(args):
            arg = self.to_native(self.evaluate_statement(arg))
            if isinstance(arg, bool): # Python boolean is upper case.
                arg = str(arg).lower()
            templ = templ.replace('@{}@'.format(i), str(arg))
        return templ

    def method_call(self, node):
        invokable = node.source_object
        if isinstance(invokable, mparser.IdNode):
            object_name = invokable.value
            obj = self.get_variable(object_name)
        else:
            obj = self.evaluate_statement(invokable)
        method_name = node.name
        if method_name == 'extract_objects' and self.environment.coredata.unity:
            raise InterpreterException('Single object files can not be extracted in Unity builds.')
        args = node.args
        if isinstance(obj, mparser.StringNode):
            obj = obj.get_value()
        if isinstance(obj, str):
            return self.string_method_call(obj, method_name, args)
        if isinstance(obj, list):
            return self.array_method_call(obj, method_name, self.reduce_arguments(args)[0])
        if not isinstance(obj, InterpreterObject):
            raise InvalidArguments('Variable "%s" is not callable.' % object_name)
        (args, kwargs) = self.reduce_arguments(args)
        if method_name == 'extract_objects':
            self.validate_extraction(obj.held_object)
        return obj.method_call(method_name, args, kwargs)

    # Only permit object extraction from the same subproject
    def validate_extraction(self, buildtarget):
        if not self.subdir.startswith(self.subproject_dir):
            if buildtarget.subdir.startswith(self.subproject_dir):
                raise InterpreterException('Tried to extract objects from a subproject target.')
        else:
            if not buildtarget.subdir.startswith(self.subproject_dir):
                raise InterpreterException('Tried to extract objects from the main project from a subproject.')
            if self.subdir.split('/')[1] != buildtarget.subdir.split('/')[1]:
                raise InterpreterException('Tried to extract objects from a different subproject.')

    def array_method_call(self, obj, method_name, args):
        if method_name == 'contains':
            return self.check_contains(obj, args)
        elif method_name == 'length':
            return len(obj)
        elif method_name == 'get':
            index = args[0]
            if not isinstance(index, int):
                raise InvalidArguments('Array index must be a number.')
            if index < -len(obj) or index >= len(obj):
                raise InvalidArguments('Array index %s is out of bounds for array of size %d.' % (index, len(obj)))
            return obj[index]
        raise InterpreterException('Arrays do not have a method called "%s".' % method_name)

    def check_contains(self, obj, args):
        if len(args) != 1:
            raise InterpreterException('Contains method takes exactly one argument.')
        item = args[0]
        for element in obj:
            if isinstance(element, list):
                found = self.check_contains(element, args)
                if found:
                    return True
            try:
                if element == item:
                    return True
            except Exception:
                pass
        return False

    def evaluate_if(self, node):
        assert(isinstance(node, mparser.IfClauseNode))
        for i in node.ifs:
            result = self.evaluate_statement(i.condition)
            if not(isinstance(result, bool)):
                print(result)
                raise InvalidCode('If clause does not evaluate to true or false.')
            if result:
                self.evaluate_codeblock(i.block)
                return
        if not isinstance(node.elseblock, mparser.EmptyNode):
            self.evaluate_codeblock(node.elseblock)

    def evaluate_foreach(self, node):
        assert(isinstance(node, mparser.ForeachClauseNode))
        varname = node.varname.value
        items = self.evaluate_statement(node.items)
        if not isinstance(items, list):
            raise InvalidArguments('Items of foreach loop is not an array')
        for item in items:
            self.set_variable(varname, item)
            self.evaluate_codeblock(node.block)

    def evaluate_plusassign(self, node):
        assert(isinstance(node, mparser.PlusAssignmentNode))
        varname = node.var_name
        addition = self.evaluate_statement(node.value)
        # Remember that all variables are immutable. We must always create a
        # full new variable and then assign it.
        old_variable = self.get_variable(varname)
        if not isinstance(old_variable, list):
            raise InvalidArguments('The += operator currently only works with arrays.')
        # Add other data types here.
        else:
            if isinstance(addition, list):
                new_value = old_variable + addition
            else:
                new_value = old_variable + [addition]
        self.set_variable(varname, new_value)

    def evaluate_indexing(self, node):
        assert(isinstance(node, mparser.IndexNode))
        iobject = self.evaluate_statement(node.iobject)
        if not isinstance(iobject, list):
            raise InterpreterException('Tried to index a non-array object.')
        index = self.evaluate_statement(node.index)
        if not isinstance(index, int):
            raise InterpreterException('Index value is not an integer.')
        if index < -len(iobject) or index >= len(iobject):
            raise InterpreterException('Index %d out of bounds of array of size %d.' % (index, len(iobject)))
        return iobject[index]

    def is_elementary_type(self, v):
        if isinstance(v, (int, float, str, bool, list)):
            return True
        return False

    def evaluate_comparison(self, node):
        v1 = self.evaluate_statement(node.left)
        v2 = self.evaluate_statement(node.right)
        if self.is_elementary_type(v1):
            val1 = v1
        else:
            val1 = v1.value
        if self.is_elementary_type(v2):
            val2 = v2
        else:
            val2 = v2.value
        if node.ctype == '==':
            return val1 == val2
        elif node.ctype == '!=':
            return val1 != val2
        else:
            raise InvalidCode('You broke me.')

    def evaluate_andstatement(self, cur):
        l = self.evaluate_statement(cur.left)
        if isinstance(l, mparser.BooleanNode):
            l = l.value
        if not isinstance(l, bool):
            raise InterpreterException('First argument to "and" is not a boolean.')
        if not l:
            return False
        r = self.evaluate_statement(cur.right)
        if isinstance(r, mparser.BooleanNode):
            r = r.value
        if not isinstance(r, bool):
            raise InterpreterException('Second argument to "and" is not a boolean.')
        return r

    def evaluate_orstatement(self, cur):
        l = self.evaluate_statement(cur.left)
        if isinstance(l, mparser.BooleanNode):
            l = l.get_value()
        if not isinstance(l, bool):
            raise InterpreterException('First argument to "or" is not a boolean.')
        if l:
            return True
        r = self.evaluate_statement(cur.right)
        if isinstance(r, mparser.BooleanNode):
            r = r.get_value()
        if not isinstance(r, bool):
            raise InterpreterException('Second argument to "or" is not a boolean.')
        return r

    def evaluate_notstatement(self, cur):
        v = self.evaluate_statement(cur.value)
        if isinstance(v, mparser.BooleanNode):
            v = v.value
        if not isinstance(v, bool):
            raise InterpreterException('Argument to "not" is not a boolean.')
        return not v

    def evaluate_uminusstatement(self, cur):
        v = self.evaluate_statement(cur.value)
        if isinstance(v, mparser.NumberNode):
            v = v.value
        if not isinstance(v, int):
            raise InterpreterException('Argument to negation is not an integer.')
        return -v

    def evaluate_arithmeticstatement(self, cur):
        l = self.to_native(self.evaluate_statement(cur.left))
        r = self.to_native(self.evaluate_statement(cur.right))

        if cur.operation == 'add':
            try:
                return l + r
            except Exception as e:
                raise InvalidCode('Invalid use of addition: ' + str(e))
        elif cur.operation == 'sub':
            if not isinstance(l, int) or not isinstance(r, int):
                raise InvalidCode('Subtraction works only with integers.')
            return l - r
        elif cur.operation == 'mul':
            if not isinstance(l, int) or not isinstance(r, int):
                raise InvalidCode('Multiplication works only with integers.')
            return l * r
        elif cur.operation == 'div':
            if not isinstance(l, int) or not isinstance(r, int):
                raise InvalidCode('Division works only with integers.')
            return l // r
        else:
            raise InvalidCode('You broke me.')

    def evaluate_arraystatement(self, cur):
        (arguments, kwargs) = self.reduce_arguments(cur.args)
        if len(kwargs) > 0:
            raise InvalidCode('Keyword arguments are invalid in array construction.')
        return arguments

    def is_subproject(self):
        return self.subproject != ''
