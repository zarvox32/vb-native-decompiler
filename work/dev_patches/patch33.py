import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('emit.py', [(
"""def renumber_temps(blocks, terms):
    names = {}

    def fn(x):
        if isinstance(x, Temp):
            n = names.get(x.name)
            if n is None:
                n = 't%d' % (len(names) + 1)
                names[x.name] = n
            return Temp(n, x.ty)
        return None
    for b in sorted(blocks):
        for s in blocks[b]:
            if isinstance(s, Assign) and isinstance(s.dst, Temp):
                s.dst = fn(s.dst)
            stmt_rewrite(s, fn)
        terms[b] = term_rewrite(terms[b], fn)""",
"""def renumber_temps(blocks, terms):
    names = {}
    produced = set()

    def fn(x):
        if isinstance(x, Temp) and x.name not in produced:
            n = names.get(x.name)
            if n is None:
                n = 'tmp%d' % (len(names) + 1)
                names[x.name] = n
                produced.add(n)
            return Temp(n, x.ty)
        return None
    for b in sorted(blocks):
        for s in blocks[b]:
            stmt_rewrite(s, fn)
        terms[b] = term_rewrite(terms[b], fn)""")])
print('patched')
