import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# 1. Set x = 0  ->  Set x = Nothing
patch('ir.py', [(
"""        src = render(s.src)
        if s.kind == 'set':
            return 'Set %s = %s' % (d, src)""",
"""        src = render(s.src)
        if s.kind == 'set':
            if isinstance(s.src, Const) and s.src.v == 0:
                src = 'Nothing'
            return 'Set %s = %s' % (d, src)""")])

# 3. Me + const -> field address ; 5. invalidate whole variable
patch('x86sem.py', [(
"""        if op == '+' and isinstance(a, SAddr) and isinstance(b, Const):
            return SAddr(a.o + b.v)""",
"""        if op == '+' and isinstance(a, SAddr) and isinstance(b, Const):
            return SAddr(a.o + b.v)
        if op == '+' and isinstance(a, Me) and isinstance(b, Const):
            return AddrOf(self.field(b.v, 4))"""), (
"""    def invalidate_locals_for(self, args):
        for a in args:
            if isinstance(a, Var) and a.kind == 'local':
                self.invalidate_var(a)""",
"""    def invalidate_locals_for(self, args):
        for a in args:
            if isinstance(a, Var) and a.kind == 'local':
                self.invalidate_var(a, a.size)""")])
patch('lift.py', [(
"""    def invalidate_var(self, v):
        o = self.var_loc(v)
        if o is None:
            return
        for k in list(self.st.mem):
            if o - 16 < k < o + 16 and self.loc_kind(k) != 'cell':
                del self.st.mem[k]""",
"""    def invalidate_var(self, v, size=16):
        o = self.var_loc(v)
        if o is None:
            return
        for k in list(self.st.mem):
            if o - 16 < k < o + max(size, 16) and self.loc_kind(k) != 'cell':
                del self.st.mem[k]"""), (
"""        for r, v in list(st.regs.items()):
            if isinstance(v, (Me, SAddr, ImportFn, CodePtr, DataPtr)):
                continue
            if refers(v):
                t = self.new_temp(getattr(v, 'ty', None))
                self.emit(Assign(t, v))
                st.regs[r] = t""",
"""        self.materialized = {}
        for r, v in list(st.regs.items()):
            if isinstance(v, (Me, SAddr, ImportFn, CodePtr, DataPtr)):
                continue
            if refers(v):
                t = self.materialized.get(id(v))
                if t is None:
                    t = self.new_temp(getattr(v, 'ty', None))
                    self.emit(Assign(t, v))
                    self.materialized[id(v)] = t
                st.regs[r] = t"""), (
"""            v = self.stack_var(o, size)
            if isinstance(v, Me) or isinstance(val, Me) and k == 'arg':
                return
            self.materialize_refs(v)
            s = self.emit(Assign(v, self.fit(val, size), kind=kind))""",
"""            v = self.stack_var(o, size)
            if isinstance(v, Me) or isinstance(val, Me) and k == 'arg':
                return
            self.materialize_refs(v)
            val = self.materialized.get(id(val), val)
            s = self.emit(Assign(v, self.fit(val, size), kind=kind))"""), (
"""            dst = self.load_abs(a[1], size)
            if not isinstance(dst, Var):
                dst = Deref(Const(a[1]), 0, size)
            self.materialize_refs(dst)
            self.emit(Assign(dst, self.fit(val, size), kind=kind))""",
"""            dst = self.load_abs(a[1], size)
            if not isinstance(dst, Var):
                dst = Deref(Const(a[1]), 0, size)
            self.materialize_refs(dst)
            val = self.materialized.get(id(val), val)
            self.emit(Assign(dst, self.fit(val, size), kind=kind))"""), (
"""        dst = self.deref(base, idx, scale, disp, size)
        if isinstance(dst, VTbl):
            dst = Deref(dst.obj, 0, size)
        self.materialize_refs(dst)
        self.emit(Assign(dst, self.fit(val, size), kind=kind))""",
"""        dst = self.deref(base, idx, scale, disp, size)
        if isinstance(dst, VTbl):
            dst = Deref(dst.obj, 0, size)
        self.materialize_refs(dst)
        val = self.materialized.get(id(val), val)
        self.emit(Assign(dst, self.fit(val, size), kind=kind))"""), (
"""        self.vartypes = {}
        self.var_sizes = {}
        self.callee_saved = set()""",
"""        self.vartypes = {}
        self.var_sizes = {}
        self.callee_saved = set()
        self.materialized = {}""")])

# 4. loop follow may be a return block
patch('structure.py', [(
"""            if exits:
                cand = sorted(set(exits), key=lambda x: (-exits.count(x), x))""",
"""            if not exits:
                rexits = [s for v in L.body for s in self.succ.get(v, []) if s not in L.body and s in self.rets]
                if len(set(rexits)) == 1:
                    L.follow = rexits[0]
                continue
            if exits:
                cand = sorted(set(exits), key=lambda x: (-exits.count(x), x))""")])

# 6. record types before the Variant size rule; Boolean call compared with 0
patch('emit.py', [(
"""    h = Counter({k: v for k, v in hints.items() if not k.startswith('size')})
    if size >= 16 or h.get('Variant', 0) > 0 and not h.get('String'):""",
"""    h = Counter({k: v for k, v in hints.items() if not k.startswith('size')})
    recs = [(n, t) for t, n in h.items() if n >= 100]
    if recs:
        return max(recs)[1]
    if size >= 16 or h.get('Variant', 0) > 0 and not h.get('String'):""")])
patch('simplify.py', [(
"""def simplify_cmp(e):
    a, b = e.a, e.b""",
"""def simplify_cmp(e):
    a, b = e.a, e.b
    if isinstance(b, Const) and b.v == 0 and e.op in ('=', '<>') and getattr(a, 'ty', None) == 'Boolean' and \\
            isinstance(a, (Call, Var, Temp)):
        return a if e.op == '<>' else Un('Not', a, ty='Boolean')""")])
print('patched')
