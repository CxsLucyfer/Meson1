#!/usr/bin/env python

import sys

with open(sys.argv[1], 'rb') as ifile:
    with open(sys.argv[2], 'w') as ofile:
        ofile.write('Everything ok.\n')
