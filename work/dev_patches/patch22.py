import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# B. ByRef parameter read through VTbl (dword deref) -> the parameter
patch('emit.py', [(
"""            if isinstance(x, Deref) and isinstance(x.base, Var) and x.base.kind == 'result' and x.off == 0:
                return result""",
"""            if isinstance(x, Deref) and isinstance(x.base, Var) and x.base.kind == 'result' and x.off == 0:
                return result
            if type(x).__name__ == 'VTbl' and isinstance(x.obj, Var) and x.obj.kind in ('arg', 'param') and \\
                    not getattr(x.obj, 'name', None):
                if retptr and x.obj.off == last_off:
                    return result
                return Var('param', x.obj.off, 4)""")])

# C. GetSetting default is a VARIANT by value
patch('runtime.py', [(
"""    def h_rtcGetSetting(self, L):
        app, sec, key, pdef = self.std_args(L, 4)
        args = [app, sec, key, L.variant_at(pdef)]""",
"""    def h_rtcGetSetting(self, L):
        # rtcGetSetting(BSTR app, BSTR section, BSTR key, VARIANT default /*by value*/)
        app, sec, key = L.cell(0), L.cell(4), L.cell(8)
        dflt = L.variant_cells(12)
        L.consume(28)
        args = [app, sec, key, dflt]""")])

# E. Abs idiom: cdq ; xor eax, edx ; sub eax, edx
patch('x86sem.py', [(
"""    def op_cdq(self, ins):
        self.set_reg('edx', Unknown('cdq'))""",
"""    def op_cdq(self, ins):
        self.set_reg('edx', CdqOf(self.reg('eax')))"""), (
"""class FpuAH(Expr):""",
"""class CdqOf(Expr):
    \"\"\"edx after cdq: sign mask of eax (0 or -1).\"\"\"

    def __init__(self, a):
        self.a = a

    def children(self):
        return (self.a,)

    def key(self):
        return ('cdq', ekey(self.a))


class AbsPending(Expr):
    \"\"\"x xor sign(x): first half of the branch-free Abs idiom.\"\"\"

    def __init__(self, a):
        self.a = a

    def children(self):
        return (self.a,)

    def key(self):
        return ('absp', ekey(self.a))


class FpuAH(Expr):"""), (
"""        if d.kind == 'reg' and s.kind == 'reg' and d.reg == s.reg:
            self.set_reg(d.reg, Const(0))
            self.st.flags = ('res', Const(0), d.size)
            return
        self.binop(ins, 'Xor')""",
"""        if d.kind == 'reg' and s.kind == 'reg' and d.reg == s.reg:
            self.set_reg(d.reg, Const(0))
            self.st.flags = ('res', Const(0), d.size)
            return
        b = self.load(s)
        if isinstance(b, CdqOf) and same(b.a, a):
            self.store(d, AbsPending(a), d.size)
            return
        self.binop(ins, 'Xor')"""), (
"""    def op_sub(self, ins):
        self.binop(ins, '-')""",
"""    def op_sub(self, ins):
        d, s = ins.ops
        a = self.load(d)
        if isinstance(a, AbsPending):
            b = self.load(s)
            if isinstance(b, CdqOf) and same(b.a, a.a):
                r = Call('Abs', [a.a], pure=True, ty='Long')
                self.store(d, r, d.size)
                self.st.flags = ('res', r, d.size)
                return
        self.binop(ins, '-')""")])

# A. constant-offset access to a known array
patch('simplify.py', [(
"""def array_rewrite_fn(note):
    def fn(x):
        a = array_access(x)""",
"""def array_rewrite_fn(note, known=None):
    def fn(x):
        if known is not None and isinstance(x, Deref) and isinstance(x.base, Var) and \\
                x.base.kind in ('field', 'global', 'local'):
            st = known(x.base)
            if st:
                k, field = divmod(x.off, st)
                ix = Index(x.base, [Const(k)])
                note(x.base, st, x.size)
                return ix if (field == 0 and st == x.size) else Member(ix, field, x.size, name='f%X' % field)
        a = array_access(x)""")])
patch('emit.py', [(
"""        fn = simplify.array_rewrite_fn(self.note_array)""",
"""        fn = simplify.array_rewrite_fn(self.note_array, self.known_array_stride)"""), (
"""    def is_array(self, v):""",
"""    def known_array_stride(self, v):
        if v.kind == 'field' and self.cur_obj is not None:
            k = ('field', self.cur_obj.index, v.off)
        elif v.kind == 'local':
            k = ('local', self.cur_proc, v.off)
        else:
            k = ekey(v)
        s = self.arrays.get(k)
        if not s:
            return None
        return max(s)[0]

    def is_array(self, v):""")])
print('patched')
