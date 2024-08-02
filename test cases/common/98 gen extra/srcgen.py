#!/usr/bin/env python3

import sys
import os
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--input', dest='input',
                    help='the input file')
parser.add_argument('--output', dest='output',
                    help='the output file')
parser.add_argument('--upper', dest='upper', action='store_true', default=False,
                    help='Convert to upper case.')

c_templ = '''int %s() {
    return 0;
}
'''

options = parser.parse_args(sys.argv[1:])

funcname = open(options.input).readline().strip()
if options.upper:
    funcname = funcname.upper()

open(options.output, 'w').write(c_templ % funcname)
