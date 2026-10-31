import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('lift.py', [(
"""    def note_type(self, e, ty):
        if isinstance(e, AddrOf):
            e = e.e
        if isinstance(e, Var) and ty:""",
"""    def note_type(self, e, ty):
        if isinstance(e, AddrOf):
            e = e.e
        if isinstance(e, Deref) and e.off == 0 and isinstance(e.base, Var) and e.base.kind == 'arg' and ty:
            d = self.vartypes.setdefault(('v', 'param', e.base.off), {})
            d[ty] = d.get(ty, 0) + 1
            return
        if isinstance(e, Var) and ty:""")])
patch('x86sem.py', [(
"""            if type(v).__name__ == 'VTbl':
                v = Deref(v.obj, 0, op.size, ty)       # a number through a pointer, not a vtable
            if isinstance(v, Var):""",
"""            if type(v).__name__ == 'VTbl':
                v = Deref(v.obj, 0, op.size, ty)       # a number through a pointer, not a vtable
            if isinstance(v, Deref) and not int_:
                self.note_type(v, 'Single' if op.size == 4 else 'Double')
            if isinstance(v, Var):""")])
print('patched')
