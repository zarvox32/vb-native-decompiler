import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# return pointer = last parameter that is only written, never read
patch('decompiler.py', [(
"""        last_off = 4 * (sig['nargs'] - 1)
        if is_method and nparams >= 1:""",
"""        last_off = 4 * (sig['nargs'] - 1)
        read_last = False
        for b, stmts in L.block_stmts.items():
            for s in stmts:
                exprs = [s.src] if isinstance(s, Assign) else list(s.exprs())
                if isinstance(s, Assign) and isinstance(s.dst, Deref):
                    exprs.append(s.dst.base if not (isinstance(s.dst.base, Var) and s.dst.base.kind == 'arg') else None)
                for e in exprs:
                    if e is None:
                        continue
                    for x in e.walk():
                        if (isinstance(x, Deref) or type(x).__name__ == 'VTbl') and \\
                                isinstance(getattr(x, 'base', getattr(x, 'obj', None)), Var) and \\
                                getattr(x, 'base', getattr(x, 'obj', None)).kind == 'arg' and \\
                                getattr(x, 'base', getattr(x, 'obj', None)).off == last_off:
                            read_last = True
        if is_method and nparams >= 1 and not read_last:""")])

# ReDim / Erase write the array variable through its pointer
patch('runtime.py', [(
"""        arr = L.ptr_target(ppsa, 4)
        self.ctx.note_array(L, arr, vt, cb, len(dims))""",
"""        arr = L.ptr_target(ppsa, 4)
        if isinstance(arr, Var):
            L.invalidate_var(arr, 4)
            o = L.var_loc(arr)
            if o is not None:
                for k in list(L.st.mem):
                    if k == o:
                        del L.st.mem[k]
        self.ctx.note_array(L, arr, vt, cb, len(dims))"""), (
"""    def h___vbaAryDestruct(self, L):
        flags, ppsa = self.std_args(L, 2)
        t = L.ptr_target(ppsa, 4)""",
"""    def h___vbaAryDestruct(self, L):
        flags, ppsa = self.std_args(L, 2)
        t = L.ptr_target(ppsa, 4)
        if isinstance(t, Var):
            L.invalidate_var(t, 4)""")])

# array registry knows whether the base is a data pointer or a SAFEARRAY*
patch('simplify.py', [(
"""        index = idx if k == 0 else Bin('+', idx, Const(k)) if k > 0 else Bin('-', idx, Const(-k))
        return psa, index, stride, field
    if len(bases) != 1:""",
"""        index = idx if k == 0 else Bin('+', idx, Const(k)) if k > 0 else Bin('-', idx, Const(-k))
        return psa, index, stride, field, 'psa'
    if len(bases) != 1:"""), (
"""        base, index, stride, field = a
        note(base, stride, x.size)""",
"""        base, index, stride, field = a[:4]
        note(base, stride, x.size, a[4] if len(a) > 4 else 'pv')"""), (
"""        if known is not None and isinstance(x, Deref) and isinstance(x.base, Var) and \\
                x.base.kind in ('field', 'global', 'local'):
            st = known(x.base)
            if st:""",
"""        if known is not None and isinstance(x, Deref) and isinstance(x.base, Var) and \\
                x.base.kind in ('field', 'global', 'local'):
            st = known(x.base)
            if st and x.off not in (0xC, 0x14, 0x10):""")])
patch('emit.py', [(
"""    def note_array(self, base, stride, size):
        k = ekey(base)""",
"""    def note_array(self, base, stride, size, kind='pv'):
        k = ekey(base)"""), (
"""        self.arrays.setdefault(k, set()).add((stride, size))""",
"""        self.arrays.setdefault(k, set()).add((stride, size))
        self.array_kind[k] = kind"""), (
"""        self.arrays = {}           # array base var -> {(stride, element size)}""",
"""        self.arrays = {}           # array base var -> {(stride, element size)}
        self.array_kind = {}       # 'pv' (data pointer) / 'psa' (SAFEARRAY*)"""), (
"""        s = self.arrays.get(k)
        if not s:
            return None
        return max(s)[0]""",
"""        s = self.arrays.get(k)
        if not s or self.array_kind.get(k) == 'psa':
            return None
        return max(s)[0]""")])

# phi: values unknown on some paths but agreeing on all known paths are not a variable
patch('x86sem.py', [(
"""                vals = [self.out_state[p].regs.get(r) for p in preds]
                known = [v for v in vals if v is not None and not isinstance(v, Unknown)]
                if not known:
                    continue
                if all(v is not None and same(v, vals[0]) for v in vals):
                    continue""",
"""                vals = [self.out_state[p].regs.get(r) for p in preds]
                known = [v for v in vals if v is not None and not isinstance(v, Unknown)]
                if not known:
                    continue
                if all(same(v, known[0]) for v in known):
                    continue""")])
patch('lift.py', [(
"""    for s in states[1:]:
        for r in list(m.regs):
            if r not in s.regs or not same(m.regs[r], s.regs[r]):
                del m.regs[r]""",
"""    for s in states[1:]:
        for r in list(m.regs):
            a, b = m.regs.get(r), s.regs.get(r)
            if a is not None and b is not None and same(a, b):
                continue
            if b is None or isinstance(b, Unknown):
                continue            # unknown on that path: keep the known value
            if isinstance(a, Unknown):
                m.regs[r] = b
                continue
            del m.regs[r]
        for r, b in s.regs.items():
            if r not in m.regs and not isinstance(b, Unknown) and all(
                    (x.regs.get(r) is None or isinstance(x.regs.get(r), Unknown) or same(x.regs.get(r), b))
                    for x in states):
                m.regs[r] = b""")])
print('patched')
