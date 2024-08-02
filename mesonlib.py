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

"""A library of random helper functionality."""

import platform, subprocess, operator, os, shutil, re

from glob import glob

from coredata import MesonException

class File:
    def __init__(self, is_built, subdir, fname):
        self.is_built = is_built
        self.subdir = subdir
        self.fname = fname

    @staticmethod
    def from_source_file(source_root, subdir, fname):
        if not os.path.isfile(os.path.join(source_root, subdir, fname)):
            raise MesonException('File %s does not exist.' % fname)
        return File(False, subdir, fname)

    @staticmethod
    def from_built_file(subdir, fname):
        return File(True, subdir, fname)

    @staticmethod
    def from_absolute_file(fname):
        return File(False, '', fname)

    def rel_to_builddir(self, build_to_src):
        if self.is_built:
            return os.path.join(self.subdir, self.fname)
        else:
            return os.path.join(build_to_src, self.subdir, self.fname)

    def endswith(self, ending):
        return self.fname.endswith(ending)

    def split(self, s):
        return self.fname.split(s)

    def __eq__(self, other):
        return (self.fname, self.subdir, self.is_built) == (other.fname, other.subdir, other.is_built)

    def __hash__(self):
        return hash((self.fname, self.subdir, self.is_built))

def flatten(item):
    if not isinstance(item, list):
        return item
    result = []
    for i in item:
        if isinstance(i, list):
            result += flatten(i)
        else:
            result.append(i)
    return result

def is_osx():
    return platform.system().lower() == 'darwin'

def is_linux():
    return platform.system().lower() == 'linux'

def is_windows():
    return platform.system().lower() == 'windows'

def is_debianlike():
    try:
        open('/etc/debian_version', 'r')
        return True
    except FileNotFoundError:
        return False

def exe_exists(arglist):
    try:
        p = subprocess.Popen(arglist, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.communicate()
        if p.returncode == 0:
            return True
    except FileNotFoundError:
        pass
    return False

def detect_vcs(source_dir):
    vcs_systems = [
        dict(name = 'git',        cmd = 'git', repo_dir = '.git', get_rev = 'git describe --dirty=+', rev_regex = '(.*)', dep = '.git/logs/HEAD'),
        dict(name = 'mercurial',  cmd = 'hg',  repo_dir = '.hg',  get_rev = 'hg id -n',               rev_regex = '(.*)', dep = '.hg/dirstate'),
        dict(name = 'subversion', cmd = 'svn', repo_dir = '.svn', get_rev = 'svn info',               rev_regex = 'Revision: (.*)', dep = '.svn/wc.db'),
        dict(name = 'bazaar',     cmd = 'bzr', repo_dir = '.bzr', get_rev = 'bzr revno',              rev_regex = '(.*)', dep = '.bzr'),
    ]

    segs = source_dir.replace('\\', '/').split('/')
    for i in range(len(segs), -1, -1):
        curdir = '/'.join(segs[:i])
        for vcs in vcs_systems:
            if os.path.isdir(os.path.join(curdir, vcs['repo_dir'])) and shutil.which(vcs['cmd']):
                vcs['wc_dir'] = curdir
                return vcs
    return None

def version_compare(vstr1, vstr2):
    if '-' in vstr1:
        vstr1 = vstr1.split('-')[0]
    if vstr2.startswith('>='):
        cmpop = operator.ge
        vstr2 = vstr2[2:]
    elif vstr2.startswith('<='):
        cmpop = operator.le
        vstr2 = vstr2[2:]
    elif vstr2.startswith('!='):
        cmpop = operator.ne
        vstr2 = vstr2[2:]
    elif vstr2.startswith('=='):
        cmpop = operator.eq
        vstr2 = vstr2[2:]
    elif vstr2.startswith('='):
        cmpop = operator.eq
        vstr2 = vstr2[1:]
    elif vstr2.startswith('>'):
        cmpop = operator.gt
        vstr2 = vstr2[1:]
    elif vstr2.startswith('<'):
        cmpop = operator.lt
        vstr2 = vstr2[1:]
    else:
        cmpop = operator.eq
    varr1 = [int(x) for x in vstr1.split('.')]
    varr2 = [int(x) for x in vstr2.split('.')]
    return cmpop(varr1, varr2)

def default_libdir():
    try:
        archpath = subprocess.check_output(['dpkg-architecture', '-qDEB_HOST_MULTIARCH']).decode().strip()
        return 'lib/' + archpath
    except FileNotFoundError:
        pass
    if os.path.isdir('/usr/lib64'):
        return 'lib64'
    return 'lib'

def get_library_dirs():
    if is_windows():
        return ['C:/mingw/lib'] # Fixme
    if is_osx():
        return ['/usr/lib'] # Fix me as well.
    # The following is probably Debian/Ubuntu specific.
    # /usr/local/lib is first because it contains stuff
    # installed by the sysadmin and is probably more up-to-date
    # than /usr/lib. If you feel that this search order is
    # problematic, please raise the issue on the mailing list.
    unixdirs = ['/usr/local/lib', '/usr/lib', '/lib']
    plat = subprocess.check_output(['uname', '-m']).decode().strip()
    # This is a terrible hack. I admit it and I'm really sorry.
    # I just don't know what the correct solution is.
    if plat == 'i686':
        plat = 'i386'
    if plat.startswith('arm'):
        plat = 'arm'
    unixdirs += glob('/usr/lib/' + plat + '*')
    if os.path.exists('/usr/lib64'):
        unixdirs.append('/usr/lib64')
    unixdirs += glob('/lib/' + plat + '*')
    if os.path.exists('/lib64'):
        unixdirs.append('/lib64')
    unixdirs += glob('/lib/' + plat + '*')
    return unixdirs


def do_replacement(regex, line, confdata):
    match = re.search(regex, line)
    while match:
        varname = match.group(1)
        if varname in confdata.keys():
            var = confdata.get(varname)
            if isinstance(var, str):
                pass
            elif isinstance(var, int):
                var = str(var)
            else:
                raise RuntimeError('Tried to replace a variable with something other than a string or int.')
        else:
            var = ''
        line = line.replace('@' + varname + '@', var)
        match = re.search(regex, line)
    return line

def do_mesondefine(line, confdata):
    arr = line.split()
    if len(arr) != 2:
        raise MesonException('#mesondefine does not contain exactly two tokens: %s', line.strip())
    varname = arr[1]
    try:
        v = confdata.get(varname)
    except KeyError:
        return '/* undef %s */\n' % varname
    if isinstance(v, bool):
        if v:
            return '#define %s\n' % varname
        else:
            return '#undef %s\n' % varname
    elif isinstance(v, int):
        return '#define %s %d\n' % (varname, v)
    elif isinstance(v, str):
        return '#define %s %s\n' % (varname, v)
    else:
        raise MesonException('#mesondefine argument "%s" is of unknown type.' % varname)


def do_conf_file(src, dst, confdata):
    data = open(src).readlines()
    regex = re.compile('@(.*?)@')
    result = []
    for line in data:
        if line.startswith('#mesondefine'):
            line = do_mesondefine(line, confdata)
        else:
            line = do_replacement(regex, line, confdata)
        result.append(line)
    dst_tmp = dst + '~'
    open(dst_tmp, 'w').writelines(result)
    shutil.copymode(src, dst_tmp)
    replace_if_different(dst, dst_tmp)


def replace_if_different(dst, dst_tmp):
    # If contents are identical, don't touch the file to prevent
    # unnecessary rebuilds.
    try:
        if open(dst, 'r').read() == open(dst_tmp, 'r').read():
            os.unlink(dst_tmp)
            return
    except FileNotFoundError:
        pass
    os.replace(dst_tmp, dst)

