import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# a parameter read with a known type (e.g. through a ByRef String variant) declares that type
patch('emit.py', [(
"""    def hints_from_expr(self, e):
        for x in e.walk():""",
"""    def hints_from_expr(self, e):
        for x in e.walk():
            if isinstance(x, Var) and x.kind == 'param' and x.ty:
                self.add_hint(ekey(x), x.ty, 1)""")])
print('patched')
