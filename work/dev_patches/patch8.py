import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


code = '''

# --------------------------------------------------------------------------- register aliases
def resolve_register_aliases(blocks, terms):
    """A register variable whose every incoming value is the value just stored into the same local
    (VB caching a For-loop counter in a register) is that local."""
    assigns = {}
    for b, stmts in blocks.items():
        for i, s in enumerate(stmts):
            if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'reg':
                assigns.setdefault(ekey(s.dst), []).append((b, i, s))
    for rk, lst in assigns.items():
        target = None
        ok = True
        for b, i, s in lst:
            stmts = blocks[b]
            found = None
            for j in range(i - 1, -1, -1):
                t = stmts[j]
                if isinstance(t, Assign) and isinstance(t.dst, Var) and t.dst.kind == 'local':
                    if same(t.src, s.src) or (isinstance(s.src, Var) and same(s.src, t.dst)):
                        found = ekey(t.dst), t.dst
                    break
            if found is None and isinstance(s.src, Var) and s.src.kind == 'local':
                found = ekey(s.src), s.src
            if found is None or (target is not None and found[0] != target[0]):
                ok = False
                break
            target = found
        if not ok or target is None:
            continue
        var = target[1]

        def fn(x, rk=rk, var=var):
            if isinstance(x, Var) and x.kind == 'reg' and ekey(x) == rk:
                return Var(var.kind, var.off, var.size, var.ty, var.name)
            return None
        for b, stmts in blocks.items():
            blocks[b] = [s for s in stmts
                         if not (isinstance(s, Assign) and isinstance(s.dst, Var) and ekey(s.dst) == rk)]
            for s in blocks[b]:
                stmt_rewrite(s, fn)
            terms[b] = term_rewrite(terms[b], fn)
'''
s = open('simplify.py', encoding='utf-8').read()
if 'def resolve_register_aliases' not in s:
    s += code
    open('simplify.py', 'w', encoding='utf-8').write(s)

patch('emit.py', [(
"""        self.params_rewrite(p, sig, blocks, terms, fname)
        self.objects_rewrite(blocks, terms)""",
"""        self.params_rewrite(p, sig, blocks, terms, fname)
        self.objects_rewrite(blocks, terms)
        simplify.resolve_register_aliases(blocks, terms)""")])
print('patched')
