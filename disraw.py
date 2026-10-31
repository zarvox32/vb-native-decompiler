"""Disassemble from a raw memory image dump: python disraw.py dump base hexaddr count"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib'))
from capstone import Cs, CS_ARCH_X86, CS_MODE_32
d = open(sys.argv[1], 'rb').read()
base = int(sys.argv[2], 16); va = int(sys.argv[3], 16); n = int(sys.argv[4])
md = Cs(CS_ARCH_X86, CS_MODE_32)
for k, i in enumerate(md.disasm(d[va - base:va - base + n * 10], va)):
    if k >= n: break
    print('%x: %-6s %s' % (i.address, i.mnemonic, i.op_str))
