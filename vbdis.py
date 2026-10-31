"""Quick disassembly helper with import names: python dis.py exe hexaddr [maxinsns]"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_32
from vbinfo import Img


def imports(im):
    m = {}
    for e in getattr(im.pe, 'DIRECTORY_ENTRY_IMPORT', []):
        for i in e.imports:
            m[i.address] = (i.name or b'ord%d' % i.ordinal).decode()
    return m


def main():
    im = Img(sys.argv[1])
    va = int(sys.argv[2], 16)
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    imp = imports(im)
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    md.detail = False
    code = bytes(im.mem[va - im.base:va - im.base + n * 8])
    cnt = 0
    for i in md.disasm(code, va):
        s = '%x: %-6s %s' % (i.address, i.mnemonic, i.op_str)
        import re
        for h in re.findall(r'0x[0-9a-f]+', i.op_str):
            v = int(h, 16)
            if v in imp:
                s += '   ; ' + imp[v]
        print(s)
        cnt += 1
        if cnt >= n:
            break


main()
