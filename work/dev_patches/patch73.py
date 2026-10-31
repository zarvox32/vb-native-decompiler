import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


NEW = '''

# --------------------------------------------------------------------------- strength-reduced loops
def _stmt_read_exprs(s):
    """Expressions a statement reads, keeping a Deref destination whole (its base is an address)."""
    if isinstance(s, Assign):
        if not isinstance(s.dst, (Var, Temp)):
            yield s.dst
        yield s.src
    else:
        for e in s.exprs():
            if e is not None and isinstance(e, Expr):
                yield e


def _is_array_base(t):
    return isinstance(t, Var) and t.kind in ('field', 'global', 'local') or \\
        isinstance(t, Deref) and t.off == 0xC


def _address_only(e, rk, need_base, memo):
    """True when every read of variable rk in e is a +1 term of a Deref address (an element offset)."""
    r = memo.get(id(e))
    if r is not None:
        return r
    ok = True
    if isinstance(e, (Var, Temp)) and ekey(e) == rk:
        ok = False
    elif isinstance(e, AddrOf) and isinstance(e.e, (Var, Temp)) and ekey(e.e) == rk:
        ok = False
    elif isinstance(e, Deref) and (lin := linear(e.base)) is not None and rk in lin[0]:
        terms = lin[0]
        if terms[rk][1] != 1:
            ok = False
        elif need_base and not any(_is_array_base(t) and c == 1 for k, (t, c) in terms.items() if k != rk):
            ok = False
        else:
            ok = all(_address_only(t, rk, need_base, memo) for k, (t, c) in terms.items() if k != rk)
    else:
        ok = all(_address_only(c, rk, need_base, memo) for c in e.children() if isinstance(c, Expr))
    memo[id(e)] = ok
    return ok


def _mentions(e, rk):
    return any(isinstance(x, (Var, Temp)) and ekey(x) == rk for x in e.walk())


def undo_strength_reduction(blocks, terms, new_var):
    """Loops that walk an array with a byte offset or pointer (`p = base + 48 ... p = p + 48`, every read
    `Mem(p + c)`) are rewritten with an element index: an existing counter that moves in step with the
    pointer, or a new variable from new_var(). Returns True if anything changed."""
    succ = {b: term_succs(t) for b, t in terms.items()}
    preds = {b: [] for b in blocks}
    for b, ss in succ.items():
        for x in ss:
            if x in preds:
                preds[x].append(b)
    changed = False
    done = set()
    while True:
        defs = {}
        for b, stmts in blocks.items():
            for i, s in enumerate(stmts):
                if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind in ('reg', 'local'):
                    defs.setdefault(ekey(s.dst), []).append((b, i, s))
        assigned = set(defs)
        for b, stmts in blocks.items():
            for s in stmts:
                if isinstance(s, Assign) and isinstance(s.dst, Var):
                    assigned.add(ekey(s.dst))

        def init_step(rk):
            lst = defs.get(rk, [])
            if len(lst) != 2:
                return None
            init = step = None
            for b, i, s in lst:
                lin = linear(s.src)
                if lin is None:
                    return None
                if rk in lin[0]:
                    if lin[0][rk][1] != 1 or len(lin[0]) != 1 or step is not None:
                        return None
                    step = (b, i, s, lin[1])
                else:
                    if init is not None:
                        return None
                    init = (b, i, s, lin)
            if init is None or step is None:
                return None
            return init, step

        def pos_key(b, i):
            return b, i

        def adjacent(p1, p2, rk):
            """Statement positions p1, p2: nothing between them (in execution order) reads rk."""
            (b1, i1), (b2, i2) = p1, p2
            if b1 == b2:
                lo, hi = sorted((i1, i2))
                return not any(_mentions(e, rk) for s in blocks[b1][lo + 1:hi] for e in _stmt_read_exprs(s))
            for (x1, j1), (x2, j2) in ((p1, p2), (p2, p1)):
                if x2 in succ.get(x1, []) and preds.get(x2) == [x1] and \\
                        not any(_mentions(e, rk) for s in blocks[x1][j1 + 1:] for e in _stmt_read_exprs(s)) and \\
                        not any(_mentions(e, rk) for e in term_exprs(terms[x1])) and \\
                        not any(_mentions(e, rk) for s in blocks[x2][:j2] for e in _stmt_read_exprs(s)):
                    return True
            return False

        found = None
        for rk in defs:
            if rk in done:
                continue
            r = init_step(rk)
            if r is None:
                continue
            (ib, ii, istmt, ilin), (sb, si, sstmt, S) = r
            if abs(S) < 2:
                continue
            iterms, c0 = ilin
            base = None
            if iterms:
                if len(iterms) != 1:
                    continue
                (t, c), = iterms.values()
                if c != 1 or not _is_array_base(t) or not isinstance(t, Var) or ekey(t) in assigned:
                    continue
                base = t
            memo = {}
            ok = True
            for b, stmts in blocks.items():
                for s in stmts:
                    if s is istmt or s is sstmt:
                        continue
                    if not all(_address_only(e, rk, base is None, memo) for e in _stmt_read_exprs(s)):
                        ok = False
                        break
                if not ok or not all(_address_only(e, rk, base is None, memo) for e in term_exprs(terms[b])):
                    ok = False
                    break
            if not ok:
                continue
            stride = abs(S)
            unit = 1 if S > 0 else -1
            k0, rem = divmod(c0, stride)
            # a counter that moves in step: C = c1 next to the init, C = C + unit next to the step
            counter = None
            for ck, lst in defs.items():
                if ck == rk:
                    continue
                cr = init_step(ck)
                if cr is None:
                    continue
                (cib, cii, cis, clin), (csb, csi, css, cS) = cr
                if cS != unit or clin[0] or cib != ib:
                    continue
                if not adjacent((cib, cii), (ib, ii), rk) or not adjacent((csb, csi), (sb, si), rk):
                    continue
                counter = (cis.dst, clin[1])
                break
            found = (rk, istmt, sstmt, base, stride, unit, k0, rem, counter)
            break
        if found is None:
            return changed
        rk, istmt, sstmt, base, stride, unit, k0, rem, counter = found
        done.add(rk)
        if counter is not None:
            cv, c1 = counter
            idx = Var(cv.kind, cv.off, cv.size, cv.ty, cv.name)
            d = k0 - c1
            index = idx if d == 0 else Bin('+', idx, Const(d)) if d > 0 else Bin('-', idx, Const(-d))
            for b in blocks:
                blocks[b] = [s for s in blocks[b] if s is not istmt and s is not sstmt]
        else:
            idx = new_var()
            index = idx
            istmt.dst, istmt.src = idx, Const(k0)
            sstmt.dst = idx
            sstmt.src = Bin('+' if unit > 0 else '-', Var(idx.kind, idx.off, idx.size, idx.ty, idx.name), Const(1))
        off = Bin('*', index, Const(stride))
        if rem:
            off = Bin('+', off, Const(rem))
        repl = Bin('+', base, off) if base is not None else off

        def fn(x, rk=rk, repl=repl):
            if isinstance(x, Var) and ekey(x) == rk:
                return repl
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)
            terms[b] = term_rewrite(terms[b], fn)
        changed = True


def propagate_base_copies(blocks):
    """`rCX = m_1C8` followed (in the same block) by `Mem32(rCX + rBX * 4)`: use the array variable
    itself in the address, so the element access can be recognised."""
    for b, stmts in blocks.items():
        for i, s in enumerate(stmts):
            if not (isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'reg' and
                    isinstance(s.src, Var) and s.src.kind in ('field', 'global')):
                continue
            rk, vk, src = ekey(s.dst), ekey(s.src), s.src

            def fn(x, rk=rk, src=src):
                if isinstance(x, Deref) and (lin := linear(x.base)) is not None and rk in lin[0] and \\
                        lin[0][rk][1] == 1:
                    def sub(y):
                        if isinstance(y, Var) and ekey(y) == rk:
                            return Var(src.kind, src.off, src.size, src.ty, src.name)
                        return None
                    return Deref(rewrite(x.base, sub), x.off, x.size, x.ty)
                return None
            for s2 in stmts[i + 1:]:
                stmt_rewrite(s2, fn)
                if isinstance(s2, Assign) and isinstance(s2.dst, Var) and ekey(s2.dst) in (rk, vk):
                    break
                if any(isinstance(x, Call) for e in iter_exprs_in(s2) for x in e.walk()):
                    break
'''

s = open('simplify.py', encoding='utf-8').read()
if 'def undo_strength_reduction' not in s:
    s = s.rstrip('\n') + '\n' + NEW
    open('simplify.py', 'w', encoding='utf-8').write(s)

patch('emit.py', [(
"""        simplify.fold_mirrored_registers(blocks, terms, L.cfg.entry)
        for _ in range(4):""",
"""        simplify.fold_mirrored_registers(blocks, terms, L.cfg.entry)
        simplify.propagate_base_copies(blocks)
        for _ in range(4):"""), (
"""            simplify.clean_statements(blocks)
        simplify.lower_all(blocks, terms)
        fn = simplify.array_rewrite_fn(self.note_array, self.known_array_stride)""",
"""            simplify.clean_statements(blocks)
        if simplify.undo_strength_reduction(blocks, terms, self.new_index_var):
            simplify.remove_dead_stores_liveness(blocks, terms)
            simplify.inline_temps(blocks, terms, lambda d, k: False)
            simplify.clean_statements(blocks)
        simplify.lower_all(blocks, terms)
        fn = simplify.array_rewrite_fn(self.note_array, self.known_array_stride)"""), (
"""    # ---------------------------------------------------------------- main per-proc pipeline
    def proc_text(self, p, L):""",
"""    def new_index_var(self):
        \"\"\"A fresh local for an array index recovered from a pointer/offset loop.\"\"\"
        self.index_vars = getattr(self, 'index_vars', 0) + 1
        n = self.index_vars
        return Var('local', 0x7e00 + 4 * n, 4, name='idx' if n == 1 else 'idx%d' % n)

    # ---------------------------------------------------------------- main per-proc pipeline
    def proc_text(self, p, L):"""), (
"""        self.reg_names = {}
        self.param_types = {}
        self.cur_records = dict(self.dc.records)""",
"""        self.reg_names = {}
        self.index_vars = 0
        self.param_types = {}
        self.cur_records = dict(self.dc.records)""")])
print('patched')
