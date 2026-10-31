import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# the temporary array may already be erased when the Array(...) value is stored
patch('simplify.py', [(
"""                    items[t.dst.idx[0].v] = t.src
                    drop.add(j)
                elif any(""",
"""                    items[t.dst.idx[0].v] = t.src
                    drop.add(j)
                elif isinstance(t, FmtStmt) and t.fmt == 'Erase {0}' and isinstance(t.args[0], Var) and \
                        ekey(t.args[0]) == k and j > max(drop):
                    drop.add(j)
                elif any(""")])
print('patched')
