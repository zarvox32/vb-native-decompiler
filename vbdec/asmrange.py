"""Debug helper: disassemble a code range with import names and string literals.
usage: python -I asmrange.py <exe> <start hex> <end hex>   |   <exe> find <import-or-literal> [proc hex]"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'lib'))
sys.path.insert(0, HERE)
from capstone import Cs, CS_ARCH_X86, CS_MODE_32  # noqa: E402
from pe import Image  # noqa: E402

if sys.argv[1].endswith('.bin'):
    im = Image(path=sys.argv[1], mapped=open(sys.argv[1], 'rb').read(), base=0x400000)   # molebox dump
else:
    im = Image(sys.argv[1])
md = Cs(CS_ARCH_X86, CS_MODE_32)


def annotate(i):
    notes = []
    for h in re.findall(r'0x[0-9a-f]+', i.op_str):
        v = int(h, 16)
        if v in im.imports:
            notes.append(im.imports[v])
        elif v > 0x400000 and im.valid(v, 2):
            s = im.bstr(v)
            if s is not None:
                notes.append(repr(s)[:60])
    return ('   ; ' + ', '.join(notes)) if notes else ''


if sys.argv[2] == 'find':
    pat = sys.argv[3]
    start = int(sys.argv[4], 16) if len(sys.argv) > 4 else 0x401000
    code = im.read(start, 0x20000)
    ins = list(md.disasm(code, start))
    for k, i in enumerate(ins):
        if pat in annotate(i):
            for j in ins[max(0, k - 14):k + 6]:
                print('%x: %-6s %s%s' % (j.address, j.mnemonic, j.op_str, annotate(j)))
            print('----')
else:
    a, b = int(sys.argv[2], 16), int(sys.argv[3], 16)
    for i in md.disasm(im.read(a, b - a), a):
        print('%x: %-6s %s%s' % (i.address, i.mnemonic, i.op_str, annotate(i)))
