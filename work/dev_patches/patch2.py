import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('ir.py', [(
"""    return '%s_%X' % (v.kind, v.off)""",
"""    if isinstance(v.off, str):
        return '%s_%s' % (v.kind, v.off)
    return '%s_%X' % (v.kind, v.off)""")])

patch('lift.py', [(
"""            if isinstance(val, SAddr):
                st.sp = val.o
            else:
                self.warn('esp set to non-stack value %r' % (val,))
            return""",
"""            if isinstance(val, SAddr):
                st.sp = val.o
            else:
                self.warn('esp set to non-stack value (%s)' % (self.cur.text if self.cur else '?'))
            return""")])

patch('x86sem.py', [(
"""        if d.kind == 'reg' and d.reg == 'esp' and isinstance(b, Const):""",
"""        if d.kind == 'reg' and d.reg == 'esp' and not isinstance(b, Const):
            self.warn('non-constant stack adjustment: %s' % ins.text)
            return
        if d.kind == 'reg' and d.reg == 'esp' and isinstance(b, Const):""")])
print('patched')
