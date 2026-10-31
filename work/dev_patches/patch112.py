import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# substitute() returns (expression, found)
patch('simplify.py', [(
"""        s.src = substitute(s.src, ekey(v), w)""",
"""        s.src = substitute(s.src, ekey(v), w)[0]""")])
print('patched')
