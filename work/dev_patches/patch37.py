import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# only empty return blocks are duplicated as `Exit Sub`; others are ordinary join points
patch('structure.py', [(
"""        self.rets = {b for b, t in terms.items() if t[0] == 'ret' and b in blocks}""",
"""        self.rets = {b for b, t in terms.items() if t[0] == 'ret' and b in blocks and
                     len([s for s in blocks[b] if not isinstance(s, Comment)]) <= 1}""")])

# constant-offset accesses use the array's known element stride
patch('simplify.py', [(
"""def array_rewrite_fn(note, known=None):
    def fn(x):""",
"""def array_rewrite_fn(note, known=None):
    def fn(x):
        if known is not None and isinstance(x, Deref) and isinstance(x.base, Bin) and x.base.op == '+' and \\
                isinstance(x.base.a, Var) and isinstance(x.base.b, Const) and \\
                x.base.a.kind in ('field', 'global', 'local'):
            st = known(x.base.a)
            if st:
                k, field = divmod(x.base.b.v + x.off, st)
                ix = Index(x.base.a, [Const(k)])
                note(x.base.a, st, x.size)
                return ix if (field == 0 and st == x.size) else Member(ix, field, x.size, name='f%X' % field)""")])
print('patched')
