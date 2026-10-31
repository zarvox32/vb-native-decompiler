"""Research helper: show example call sites of runtime imports (preceding/following instructions).
usage: python -I callsites.py <exe> [name-regex] [examples-per-import]"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'lib'))
sys.path.insert(0, HERE)
from capstone import Cs, CS_ARCH_X86, CS_MODE_32  # noqa: E402
from vbmeta import Project  # noqa: E402

pr = Project(sys.argv[1])
pat = re.compile(sys.argv[2] if len(sys.argv) > 2 else '.')
nex = int(sys.argv[3]) if len(sys.argv) > 3 else 2
im = pr.im
md = Cs(CS_ARCH_X86, CS_MODE_32)
insns = list(md.disasm(im.read(pr.code_start, pr.code_end - pr.code_start), pr.code_start))
regfn = {}
shown = {}
for idx, i in enumerate(insns):
    # track `mov reg, [IAT]` so `call reg` resolves
    m = re.match(r'mov (e[a-z]{2}), dword ptr \[(0x[0-9a-f]+)\]', '%s %s' % (i.mnemonic, i.op_str))
    if m and int(m.group(2), 16) in im.imports:
        regfn[m.group(1)] = im.imports[int(m.group(2), 16)]
    name = None
    if i.mnemonic == 'call':
        m = re.match(r'dword ptr \[(0x[0-9a-f]+)\]$', i.op_str)
        if m:
            name = im.imports.get(int(m.group(1), 16))
        elif i.op_str in regfn:
            name = regfn[i.op_str]
    if not name or not pat.search(name):
        continue
    if shown.get(name, 0) >= nex:
        continue
    shown[name] = shown.get(name, 0) + 1
    print('=== %s @%x' % (name, i.address))
    for j in insns[max(0, idx - 9):idx + 3]:
        print('   %x %s %s' % (j.address, j.mnemonic, j.op_str))
