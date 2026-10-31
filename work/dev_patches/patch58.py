import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('vbmeta.py', [(
"""            code = im.read(va, min(end - va, 0x20000))
            votes = {}
            for m in re.finditer(rb'[\\xA1\\xA3\\x8B\\x89\\x8D\\xC7\\x66\\xD9\\xDD\\xFF\\x83\\x39\\x3B\\x81](?:[\\x05\\x0D\\x15\\x1D\\x25\\x2D\\x35\\x3D])?(....)', code):
                v = struct.unpack('<I', m.group(1))[0]
                best = None""",
"""            code = im.read(va, min(end - va, 0x20000))
            votes = {}
            values = []
            for ins in _mdd.disasm(code, va):
                for op in ins.operands:
                    if op.type == 3 and op.mem.base == 0 and op.mem.index == 0:      # [abs]
                        values.append(op.mem.disp & 0xffffffff)
                    elif op.type == 2:                                               # imm (push offset)
                        values.append(op.imm & 0xffffffff)
            for v in values:
                best = None"""), (
"""_md = Cs(CS_ARCH_X86, CS_MODE_32)""",
"""_md = Cs(CS_ARCH_X86, CS_MODE_32)
_mdd = Cs(CS_ARCH_X86, CS_MODE_32)
_mdd.detail = True""")])
print('patched')
