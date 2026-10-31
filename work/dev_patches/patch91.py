import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# lea eax, [esi+38h] / mov word ptr [eax], cx: the pointer's size was a guess (4) - the access decides
# (was rendered m_38.field_0)
patch('lift.py', [(
"""        if isinstance(base, AddrOf):
            if disp == 0 and isinstance(base.e, Var) and base.e.size == size:
                return base.e
            return Member(base.e, disp, size, ty=ty)""",
"""        if isinstance(base, AddrOf):
            if disp == 0 and isinstance(base.e, Var) and base.e.size == size:
                return base.e
            if isinstance(base.e, Var) and base.e.kind == 'field' and base.e.size <= 4 and size <= 4:
                return self.field(base.e.off + disp, size, ty)
            if disp == 0 and isinstance(base.e, Var) and base.e.kind in ('local', 'global') and \\
                    base.e.size <= 4 and size <= 4:
                return Var(base.e.kind, base.e.off, size, ty or base.e.ty, base.e.name)
            return Member(base.e, disp, size, ty=ty)""")])
print('patched')
