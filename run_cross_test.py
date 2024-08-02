#!/usr/bin/env python3

# Copyright 2013-2016 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

'''Runs the basic test suite through a cross compiler.
Not part of the main test suite because of two reasons:

1) setup of the cross build is platform specific
2) it can be slow (e.g. when invoking test apps via wine)

Eventually migrate to something fancier.'''

import sys
import os
from pathlib import Path
import argparse

from run_project_tests import gather_tests, run_tests, StopException, setup_commands
from run_project_tests import failing_logs

def runtests(cross_file, failfast):
    commontests = [('common', gather_tests(Path('test cases', 'common')), False)]
    try:
        (passing_tests, failing_tests, skipped_tests) = \
            run_tests(commontests, 'meson-cross-test-run', failfast, ['--cross-file', cross_file])
    except StopException:
        pass
    print('\nTotal passed cross tests:', passing_tests)
    print('Total failed cross tests:', failing_tests)
    print('Total skipped cross tests:', skipped_tests)
    if failing_tests > 0 and ('CI' in os.environ):
        print('\nMesonlogs of failing tests\n')
        for log in failing_logs:
            print(log, '\n')
    return failing_tests

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--failfast', action='store_true')
    parser.add_argument('cross_file')
    options = parser.parse_args()
    setup_commands('ninja')
    return runtests(options.cross_file, options.failfast)

if __name__ == '__main__':
    sys.exit(main())
