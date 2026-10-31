import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('structure.py', [(
"""from ir import (Const, FConst, Var, Bin, Un, Call, Assign, ExprStmt, Text, Comment, FmtStmt, Raw, render,
                render_stmt, ekey, same)""",
"""from ir import (Const, FConst, Str, Var, Bin, Un, Call, Assign, ExprStmt, Text, Comment, FmtStmt, Raw, render,
                render_stmt, ekey, same)""")])

patch('runtime.py', [(
"""    def h___vbaI2Abs(self, L):""",
"""    def h___vbaCopyBytes(self, L):
        # __vbaCopyBytes(len, dst, src): fixed-size record (UDT) assignment
        n, dst, src = self.std_args(L, 3)
        size = n.v if isinstance(n, Const) else 4
        val = L.deref_ptr(src, size)
        if isinstance(val, Var):
            val = Var(val.kind, val.off, size, val.ty, val.name)
        tgt = L.ptr_target(dst, size)
        if isinstance(tgt, Var):
            tgt = Var(tgt.kind, tgt.off, size, tgt.ty, tgt.name)
            L.invalidate_var(tgt, size)
        L.emit(Assign(tgt, val))
        L.set_reg('eax', dst)

    def h___vbaI2Sgn(self, L):
        L.set_reg('eax', Call('Sgn', [L.reg('ecx')], pure=True, ty='Integer'))

    def h___vbaI2Abs(self, L):""")])
patch('cfg.py', [(
"""REGARG = {'__vbaStrCopy', '__vbaStrMove', '__vbaVarMove', '__vbaVarCopy', '__vbaVarDup', '__vbaFreeStr',""",
"""REGARG = {'__vbaI2Sgn', '__vbaStrCopy', '__vbaStrMove', '__vbaVarMove', '__vbaVarCopy', '__vbaVarDup', '__vbaFreeStr',""")])
print('patched')
