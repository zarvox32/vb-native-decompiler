import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# `mov ecx, [p]` with the value used as data (not as an object's vtable): p is ByRef
patch('decompiler.py', [(
"""                        if isinstance(x, Deref) and isinstance(x.base, Var) and x.base.kind == 'arg' and x.off == 0:
                            byref[x.base.off] = True
                            sizes[x.base.off] = max(sizes.get(x.base.off, 0), x.size)""",
"""                        if isinstance(x, Deref) and isinstance(x.base, Var) and x.base.kind == 'arg' and x.off == 0:
                            byref[x.base.off] = True
                            sizes[x.base.off] = max(sizes.get(x.base.off, 0), x.size)
                        elif type(x).__name__ == 'VTbl' and isinstance(x.obj, Var) and x.obj.kind == 'arg':
                            byref[x.obj.off] = True
                            sizes[x.obj.off] = max(sizes.get(x.obj.off, 0), 4)""")])
print('patched')
