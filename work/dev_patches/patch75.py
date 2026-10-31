import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# Erase only forgets the 4-byte array variable (a neighbouring Variant temporary must survive)
patch('runtime.py', [(
"""        t = L.ptr_target(ppsa, 4)
        if isinstance(t, Var):
            L.invalidate_var(t, 4)
            L.emit(FmtStmt('Erase {0}', [t]))
            return""",
"""        t = L.ptr_target(ppsa, 4)
        if isinstance(t, Var):
            o = L.var_loc(t)
            if o is not None:
                L.st.mem.pop(o, None)
            L.emit(FmtStmt('Erase {0}', [t]))
            return""")])
print('patched')
