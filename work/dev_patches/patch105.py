import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# a parameter dereferenced only in branch conditions (If p1 = 2 ...) is ByRef too
patch('decompiler.py', [(
"""        byref = {}
        sizes = {}
        for b, stmts in L.block_stmts.items():
            for s in stmts:
                for e in s.exprs():""",
"""        byref = {}
        sizes = {}

        class _Term:
            def __init__(self, t):
                self.t = t

            def exprs(self):
                return (self.t[1],) if self.t[0] in ('cond', 'switch') else ()
        for b, stmts in L.block_stmts.items():
            for s in list(stmts) + [_Term(L.block_term[b])] if b in L.block_term else stmts:
                for e in s.exprs():""")])
print('patched')
