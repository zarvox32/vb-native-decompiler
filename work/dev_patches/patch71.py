import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# frameless procedures reserve a 4-byte local with `push ecx` before saving registers
patch('decompiler.py', [(
"""                    sp -= ins.ops[1].imm
                    low = sp
                elif ins.mn == 'push' and ins.ops[0].kind == 'reg' and ins.ops[0].reg in ('ebx', 'esi', 'edi', 'ebp'):""",
"""                    sp -= ins.ops[1].imm
                    low = sp
                elif ins.mn == 'push' and ins.ops[0].kind == 'reg' and ins.ops[0].reg in ('eax', 'ecx', 'edx') \
                        and sp == low:
                    sp -= 4
                    low = sp
                elif ins.mn == 'push' and ins.ops[0].kind == 'reg' and ins.ops[0].reg in ('ebx', 'esi', 'edi', 'ebp'):""")])
print('patched')
