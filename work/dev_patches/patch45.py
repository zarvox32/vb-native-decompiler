import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))

code = '''

# --------------------------------------------------------------------------- address propagation (With blocks)
def is_address_expr(e):
    """AddrOf(x) or an element address `base + i*k + c` (no calls)."""
    if isinstance(e, AddrOf):
        return True
    if isinstance(e, Bin) and e.op in ('+', '-') and not any(isinstance(x, Call) for x in e.walk()):
        l = linear(e)
        if l is None:
            return False
        bases = [t for t, c in l[0].values() if c == 1 and isinstance(t, (Var, Deref)) and
                 (not isinstance(t, Var) or t.kind in ('field', 'global', 'local'))]
        return len(bases) >= 1 and len(l[0]) >= 1 and (len(l[0]) > 1 or l[1] != 0)
    return False


def vars_read(e):
    out = set()
    for x in e.walk():
        if isinstance(x, (Var, Temp)):
            out.add(ekey(x))
    return out


def propagate_addresses(blocks, terms, entry):
    """Variables that only ever hold an address (VB `With` temporaries, pointers kept in registers)
    are replaced by that address at each read with a single reaching definition."""
    # candidate variables: every definition is an address expression
    defs = {}
    for b, stmts in blocks.items():
        for i, s in enumerate(stmts):
            if isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and \\
                    (isinstance(s.dst, Temp) or s.dst.kind in ('local', 'reg')):
                defs.setdefault(ekey(s.dst), []).append((b, i, s))
    cand = {k for k, lst in defs.items() if all(is_address_expr(s.src) for b, i, s in lst)}
    if not cand:
        return
    succ = {b: term_succs(t) for b, t in terms.items()}
    preds = {b: [] for b in blocks}
    for b, ss in succ.items():
        for x in ss:
            if x in preds:
                preds[x].append(b)
    # reaching definitions (per candidate variable) at block entry
    out_defs = {b: {} for b in blocks}
    in_defs = {b: {} for b in blocks}
    changed = True
    order = sorted(blocks)
    while changed:
        changed = False
        for b in order:
            ind = {}
            for p in preds[b]:
                for k, ds in out_defs[p].items():
                    ind.setdefault(k, set()).update(ds)
            cur = {k: set(v) for k, v in ind.items()}
            for i, s in enumerate(blocks[b]):
                if isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in cand:
                    cur[ekey(s.dst)] = {(b, i)}
            if cur != out_defs[b] or ind != in_defs[b]:
                out_defs[b] = cur
                in_defs[b] = ind
                changed = True
    stmt_at = {(b, i): s for b, stmts in blocks.items() for i, s in enumerate(stmts)}

    def assigned_between(dpos, upos, names):
        """Is any variable in names assigned on a path from definition dpos to use upos?"""
        db, di = dpos
        ub, ui = upos
        if db == ub and di < ui:
            seg = blocks[db][di + 1:ui]
            return any(isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in names for s in seg)
        # statements after the def, in blocks between, and before the use
        if any(isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in names
               for s in blocks[db][di + 1:]):
            return True
        if any(isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in names
               for s in blocks[ub][:ui]):
            return True
        fwd = set()
        st = list(succ.get(db, []))
        while st:
            v = st.pop()
            if v in fwd or v == db or v not in blocks:
                continue
            fwd.add(v)
            st.extend(succ.get(v, []))
        back = set()
        st = list(preds.get(ub, []))
        while st:
            v = st.pop()
            if v in back or v == ub or v not in blocks:
                continue
            back.add(v)
            st.extend(preds.get(v, []))
        for v in fwd & back:
            if any(isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in names
                   for s in blocks[v]):
                return True
        return False

    def make_fn(pos, live):
        def fn(x):
            if isinstance(x, (Var, Temp)) and ekey(x) in cand:
                ds = live.get(ekey(x))
                if ds and len(ds) == 1:
                    d = next(iter(ds))
                    src = stmt_at[d].src
                    names = vars_read(src)
                    if ekey(x) not in names and not assigned_between(d, pos, names):
                        return src
            return None
        return fn

    for b in order:
        cur = {k: set(v) for k, v in in_defs[b].items()}
        for i, s in enumerate(blocks[b]):
            if not (isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in cand):
                fn = make_fn((b, i), cur)
                if isinstance(s, Assign):
                    if not isinstance(s.dst, (Var, Temp)):
                        s.dst = rewrite(s.dst, fn)
                    s.src = rewrite(s.src, fn)
                else:
                    stmt_rewrite(s, fn)
            if isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in cand:
                cur[ekey(s.dst)] = {(b, i)}
        fn = make_fn((b, len(blocks[b])), cur)
        terms[b] = term_rewrite(terms[b], fn)
'''
s = open('simplify.py', encoding='utf-8').read()
if 'def propagate_addresses' not in s:
    s += code
    open('simplify.py', 'w', encoding='utf-8').write(s)

p = 'emit.py'
s = open(p, encoding='utf-8').read()
old = """        simplify.propagate_constant_regvars(blocks, terms)"""
new = """        simplify.propagate_constant_regvars(blocks, terms)
        simplify.propagate_addresses(blocks, terms, L.cfg.entry)"""
assert old in s
s = s.replace(old, new, 1)
open(p, 'w', encoding='utf-8').write(s)
print('patched')
