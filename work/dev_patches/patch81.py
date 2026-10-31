import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# reused parameter / Me slots are not prologue-initialised locals
patch('simplify.py', [(
"""        if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'local' and \\
                isinstance(s.src, Const) and s.src.v == 0:
            del stmts[i]
            continue
        break""",
"""        if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'local' and \\
                isinstance(s.src, Const) and s.src.v == 0 and s.dst.off < 0x7e00:
            del stmts[i]
            continue
        break""")])
print('patched')
