import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# a by-reference VARIANT pointing at a parameter tells the parameter's type (vt 4008h: ByRef String)
patch('lift.py', [(
"""            if isinstance(v, (SAddr, AddrOf, DataPtr)):
                return self.deref_ptr(v, {2: 2, 3: 4, 4: 4, 5: 8, 8: 4, 11: 2, 12: 16}.get(base, 4),
                                      VT_NAMES.get(base))
            return Deref(v, 0, 4)""",
"""            if isinstance(v, (SAddr, AddrOf, DataPtr)):
                return self.deref_ptr(v, {2: 2, 3: 4, 4: 4, 5: 8, 8: 4, 11: 2, 12: 16}.get(base, 4),
                                      VT_NAMES.get(base))
            return Deref(v, 0, {2: 2, 3: 4, 4: 4, 5: 8, 8: 4, 11: 2, 12: 16}.get(base, 4), VT_NAMES.get(base))""")])

patch('emit.py', [(
"""                if retptr and x.base.off == last_off:
                    return Var('result', 0, x.size, name=fname)
                return Var('param', x.base.off, x.size)""",
"""                if retptr and x.base.off == last_off:
                    return Var('result', 0, x.size, name=fname)
                return Var('param', x.base.off, x.size, x.ty)""")])

# a function whose result pointer receives four dwords returns a Variant
patch('decompiler.py', [(
"""                        sig['retptr'] = True
                        sig['rettype'] = self._size_type(s.dst.size, s.src)""",
"""                        sig['retptr'] = True
                        sig['rettype'] = self._size_type(s.dst.size, s.src)
            for b, stmts in L.block_stmts.items():
                for s in stmts:
                    if sig.get('retptr') and isinstance(s, Assign) and isinstance(s.dst, Deref) and \\
                            isinstance(s.dst.base, Var) and s.dst.base.kind == 'arg' and \\
                            s.dst.base.off == last_off and s.dst.off == 12:
                        sig['rettype'] = 'Variant'""")])

# VB initialises a function's result (Empty / 0) on entry: not a statement of the program
patch('simplify.py', [(
"""        if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'local' and \\
                isinstance(s.src, Const) and s.src.v == 0 and s.dst.off < 0x7e00:""",
"""        if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind in ('local', 'result') and \\
                isinstance(s.src, Const) and s.src.v == 0 and s.dst.off < 0x7e00:""")])
print('patched')
