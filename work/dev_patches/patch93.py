import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# a product of two variables (m_838 * m_176) is an opaque term of a linear form, not a failure
patch('simplify.py', [(
"""                return {k: (t, c * y.v) for k, (t, c) in l[0].items()}, l[1] * y.v
        return None
    if isinstance(e, Un) and e.op == 'neg':""",
"""                return {k: (t, c * y.v) for k, (t, c) in l[0].items()}, l[1] * y.v
        return {ekey(e): (e, 1)}, 0
    if isinstance(e, Un) and e.op == 'neg':""")])
print('patched')
