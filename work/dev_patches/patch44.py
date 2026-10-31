import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('simplify.py', [(
"""    \"\"\"A register variable whose every assignment is the same stable value is that value.\"\"\"
    vals = {}
    bad = set()
    for b, stmts in blocks.items():
        for s in stmts:
            if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'reg':
                k = ekey(s.dst)
                if not isinstance(s.src, (AddrOf, Const, Str, Me)) and not (
                        isinstance(s.src, Var) and s.src.kind in ('param',)):
                    bad.add(k)
                elif k in vals and not same(vals[k], s.src):
                    bad.add(k)
                else:
                    vals[k] = s.src""",
"""    \"\"\"A register variable whose every assignment is the same stable value is that value.\"\"\"
    vals = {}
    bad = set()
    tdefs = {}
    tcount = Counter()
    for b, stmts in blocks.items():
        for s in stmts:
            if isinstance(s, Assign) and isinstance(s.dst, Temp):
                tdefs[ekey(s.dst)] = s.src
                tcount[ekey(s.dst)] += 1

    def resolve(v):
        seen = 0
        while isinstance(v, Temp) and tcount.get(ekey(v)) == 1 and seen < 10:
            v = tdefs[ekey(v)]
            seen += 1
        return v
    for b, stmts in blocks.items():
        for s in stmts:
            if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'reg':
                k = ekey(s.dst)
                src = resolve(s.src)
                if not isinstance(src, (AddrOf, Const, Str, Me)) and not (
                        isinstance(src, Var) and src.kind in ('param',)):
                    bad.add(k)
                elif k in vals and not same(vals[k], src):
                    bad.add(k)
                else:
                    vals[k] = src""")])
print('patched')
