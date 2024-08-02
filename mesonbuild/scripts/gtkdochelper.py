#!/usr/bin/env python3
# Copyright 2015-2016 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys, os
import subprocess
import shutil
import argparse
from mesonbuild.mesonlib import MesonException
from mesonbuild.scripts import destdir_join

parser = argparse.ArgumentParser()

parser.add_argument('--sourcedir', dest='sourcedir')
parser.add_argument('--builddir', dest='builddir')
parser.add_argument('--subdir', dest='subdir')
parser.add_argument('--headerdir', dest='headerdir')
parser.add_argument('--mainfile', dest='mainfile')
parser.add_argument('--modulename', dest='modulename')
parser.add_argument('--htmlargs', dest='htmlargs', default='')
parser.add_argument('--scanargs', dest='scanargs', default='')
parser.add_argument('--fixxrefargs', dest='fixxrefargs', default='')

def gtkdoc_run_check(cmd, cwd):
    p = subprocess.Popen(cmd, cwd=cwd,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (stde, stdo) = p.communicate()
    if p.returncode != 0:
        err_msg = ["{!r} failed with status {:d}".format(cmd[0], p.returncode)]
        if stde:
            err_msg.append(stde.decode(errors='ignore'))
        if stdo:
            err_msg.append(stdo.decode(errors='ignore'))
        raise MesonException('\n'.join(err_msg))

def build_gtkdoc(source_root, build_root, doc_subdir, src_subdir,
                 main_file, module, html_args, scan_args, fixxref_args):
    abs_src = os.path.join(source_root, src_subdir)
    abs_out = os.path.join(build_root, doc_subdir)
    htmldir = os.path.join(abs_out, 'html')
    scan_cmd = ['gtkdoc-scan',
                '--module=' + module,
                '--source-dir=' + abs_src] + scan_args
    gtkdoc_run_check(scan_cmd, abs_out)

    # Make docbook files
    if main_file.endswith('sgml'):
        modeflag = '--sgml-mode'
    else:
        modeflag = '--xml-mode'
    mkdb_cmd = ['gtkdoc-mkdb',
                '--module=' + module,
                '--output-format=xml',
                modeflag,
                '--source-dir=' + abs_src]
    main_abs = os.path.join(source_root, doc_subdir, main_file)
    if len(main_file) > 0:
        # Yes, this is the flag even if the file is in xml.
        mkdb_cmd.append('--main-sgml-file=' + main_file)
    gtkdoc_run_check(mkdb_cmd, abs_out)

    # Make HTML documentation
    shutil.rmtree(htmldir, ignore_errors=True)
    try:
        os.mkdir(htmldir)
    except Exception:
        pass
    mkhtml_cmd = ['gtkdoc-mkhtml', 
                  '--path=' + abs_src,
                  module,
                  ] + html_args
    if len(main_file) > 0:
        mkhtml_cmd.append('../' + main_file)
    else:
        mkhtml_cmd.append('%s-docs.xml' % module)
    # html gen must be run in the HTML dir
    gtkdoc_run_check(mkhtml_cmd, os.path.join(abs_out, 'html'))

    # Fix cross-references in HTML files
    fixref_cmd = ['gtkdoc-fixxref',
                  '--module=' + module,
                  '--module-dir=html'] + fixxref_args
    gtkdoc_run_check(fixref_cmd, abs_out)

def install_gtkdoc(build_root, doc_subdir, install_prefix, datadir, module):
    source = os.path.join(build_root, doc_subdir, 'html')
    final_destination = os.path.join(install_prefix, datadir, module)
    shutil.rmtree(final_destination, ignore_errors=True)
    shutil.copytree(source, final_destination)

def run(args):
    options = parser.parse_args(args)
    if len(options.htmlargs) > 0:
        htmlargs = options.htmlargs.split('@@')
    else:
        htmlargs = []
    if len(options.scanargs) > 0:
        scanargs = options.scanargs.split('@@')
    else:
        scanargs = []
    if len(options.fixxrefargs) > 0:
        fixxrefargs = options.fixxrefargs.split('@@')
    else:
        fixxrefargs = []
    build_gtkdoc(options.sourcedir,
                 options.builddir,
                 options.subdir,
                 options.headerdir,
                 options.mainfile,
                 options.modulename,
                 htmlargs,
                 scanargs,
                 fixxrefargs)

    if 'MESON_INSTALL_PREFIX' in os.environ:
        destdir = os.environ.get('DESTDIR', '')
        installdir = destdir_join(destdir, os.environ['MESON_INSTALL_PREFIX'])
        install_gtkdoc(options.builddir,
                       options.subdir,
                       installdir,
                       'share/gtk-doc/html',
                       options.modulename)
    return 0

if __name__ == '__main__':
    sys.exit(run(sys.argv[1:]))
