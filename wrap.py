# Copyright 2015 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import mlog
import urllib.request, os, hashlib, shutil
import subprocess
import sys

from wraptool import build_ssl_context

class PackageDefinition:
    def __init__(self, fname):
        self.values = {}
        ifile = open(fname)
        first = ifile.readline().strip()

        if first == '[wrap-file]':
            self.type = 'file'
        elif first == '[wrap-git]':
            self.type = 'git'
        else:
            raise RuntimeError('Invalid format of package file')
        for line in ifile:
            line = line.strip()
            if line == '':
                continue
            (k, v) = line.split('=', 1)
            k = k.strip()
            v = v.strip()
            self.values[k] = v

    def get(self, key):
        return self.values[key]

    def has_patch(self):
        return 'patch_url' in self.values

class Resolver:
    def __init__(self, subdir_root):
        self.subdir_root = subdir_root
        self.cachedir = os.path.join(self.subdir_root, 'packagecache')

    def resolve(self, packagename):
        fname = os.path.join(self.subdir_root, packagename + '.wrap')
        dirname = os.path.join(self.subdir_root, packagename)
        if not os.path.isfile(fname):
            if os.path.isdir(dirname):
                # No wrap file but dir exists -> user put it there manually.
                return packagename 
            return None
        p = PackageDefinition(fname)
        if p.type == 'file':
            if not os.path.isdir(self.cachedir):
                os.mkdir(self.cachedir)
            self.download(p, packagename)
            self.extract_package(p)
        elif p.type == 'git':
            self.get_git(p)
        else:
            raise RuntimeError('Unreachable code.')
        return p.get('directory')

    def get_git(self, p):
        checkoutdir = os.path.join(self.subdir_root, p.get('directory'))
        revno = p.get('revision')
        is_there = os.path.isdir(checkoutdir)
        if is_there:
            if revno.lower() == 'head':
                subprocess.check_call(['git', 'pull'], cwd=checkoutdir)
            else:
                if subprocess.call(['git', 'checkout', revno], cwd=checkoutdir) != 0:
                    subprocess.check_call(['git', 'fetch'], cwd=checkoutdir)
                    subprocess.check_call(['git', 'checkout', revno],
                                          cwd=checkoutdir)
        else:
            subprocess.check_call(['git', 'clone', p.get('url'),
                                   p.get('directory')], cwd=self.subdir_root)
            if revno.lower() != 'head':
                subprocess.check_call(['git', 'checkout', revno],
                                      cwd=checkoutdir)


    def get_data(self, url):
        blocksize = 10*1024
        if url.startswith('https://wrapdb.mesonbuild.com'):
            resp = urllib.request.urlopen(url, context=build_ssl_context())
        else:
            resp = urllib.request.urlopen(url)
        dlsize = int(resp.info()['Content-Length'])
        print('Download size:', dlsize)
        print('Downloading: ', end='')
        sys.stdout.flush()
        printed_dots = 0
        blocks = []
        downloaded = 0
        while True:
            block = resp.read(blocksize)
            if block == b'':
                break
            downloaded += len(block)
            blocks.append(block)
            ratio = int(downloaded/dlsize * 10)
            while printed_dots < ratio:
                print('.', end='')
                sys.stdout.flush()
                printed_dots += 1
        print('')
        resp.close()
        return b''.join(blocks)

    def get_hash(self, data):
        h = hashlib.sha256()
        h.update(data)
        hashvalue = h.hexdigest()
        return hashvalue

    def download(self, p, packagename):
        ofname = os.path.join(self.cachedir, p.get('source_filename'))
        if os.path.exists(ofname):
            mlog.log('Using', mlog.bold(packagename), 'from cache.')
            return
        srcurl = p.get('source_url')
        mlog.log('Dowloading', mlog.bold(packagename), 'from', mlog.bold(srcurl))
        srcdata = self.get_data(srcurl)
        dhash = self.get_hash(srcdata)
        expected = p.get('source_hash')
        if dhash != expected:
            raise RuntimeError('Incorrect hash for source %s:\n %s expected\n %s actual.' % (packagename, expected, dhash))
        if p.has_patch():
            purl = p.get('patch_url')
            mlog.log('Downloading patch from', mlog.bold(purl))
            pdata = self.get_data(purl)
            phash = self.get_hash(pdata)
            expected = p.get('patch_hash')
            if phash != expected:
                raise RuntimeError('Incorrect hash for patch %s:\n %s expected\n %s actual' % (packagename, expected, phash))
            open(os.path.join(self.cachedir, p.get('patch_filename')), 'wb').write(pdata)
        else:
            mlog.log('Package does not require patch.')
        open(ofname, 'wb').write(srcdata)

    def extract_package(self, package):
        if sys.version_info < (3, 5):
            try:
                import lzma
                del lzma
                try:
                    shutil.register_unpack_format('xztar', ['.tar.xz', '.txz'], shutil._unpack_tarfile, [], "xz'ed tar-file")
                except shutil.RegistryError:
                    pass
            except ImportError:
                pass
        target_dir = os.path.join(self.subdir_root, package.get('directory'))
        if os.path.isdir(target_dir):
            return
        extract_dir = self.subdir_root
        # Some upstreams ship packages that do not have a leading directory.
        # Create one for them.
        try:
            package.get('lead_directory_missing')
            os.mkdir(target_dir)
            extract_dir = target_dir
        except KeyError:
            pass
        shutil.unpack_archive(os.path.join(self.cachedir, package.get('source_filename')), extract_dir)
        if package.has_patch():
            shutil.unpack_archive(os.path.join(self.cachedir, package.get('patch_filename')), self.subdir_root)
