import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


code = '''

def fold_mirrored_registers(blocks, terms, entry):
    """Join block starting with `X = R` (R a register variable, X a local): when every read of R happens
    in blocks dominated by that join and nothing else assigns X there, R is X."""
    from structure import dominators, dominates
    succ = {b: [s for s in term_succs(t) if s in blocks] for b, t in terms.items()}
    succ['ROOT'] = [entry]
    idom = dominators(set(blocks) | {'ROOT'}, succ, 'ROOT')
    for b in list(blocks):
        stmts = blocks.get(b)
        if not stmts:
            continue
        s0 = stmts[0]
        if not (isinstance(s0, Assign) and isinstance(s0.src, Var) and s0.src.kind == 'reg' and
                isinstance(s0.dst, Var) and s0.dst.kind == 'local' and s0.kind == 'let'):
            continue
        rk, xk = ekey(s0.src), ekey(s0.dst)
        if not s0.src.off.endswith('@%x' % b):
            continue
        ok = True
        for b2, st2 in blocks.items():
            dom = b2 in idom and dominates(idom, b, b2)
            for i, s in enumerate(st2):
                if s is s0:
                    continue
                reads_r = any(isinstance(x, Var) and ekey(x) == rk for e in iter_exprs_in(s) for x in e.walk())
                if reads_r and not dom:
                    ok = False
                if isinstance(s, Assign) and isinstance(s.dst, Var) and ekey(s.dst) == xk and dom:
                    ok = False
            if any(isinstance(x, Var) and ekey(x) == rk for e in term_exprs(terms[b2]) for x in e.walk()) and not dom:
                ok = False
            if not ok:
                break
        if not ok:
            continue
        x = s0.dst

        def fn(e, rk=rk, x=x):
            if isinstance(e, Var) and ekey(e) == rk:
                return Var(x.kind, x.off, x.size, x.ty, x.name)
            return None
        del stmts[0]
        for b2, st2 in blocks.items():
            for s in st2:
                if isinstance(s, Assign) and isinstance(s.dst, Var) and ekey(s.dst) == rk:
                    s.dst = Var(x.kind, x.off, x.size, x.ty, x.name)
                stmt_rewrite(s, fn)
            terms[b2] = term_rewrite(terms[b2], fn)
'''
s = open('simplify.py', encoding='utf-8').read()
if 'def fold_mirrored_registers' not in s:
    s += code
    open('simplify.py', 'w', encoding='utf-8').write(s)

patch('emit.py', [(
"""        simplify.fold_phi_copies(blocks, terms)
        simplify.resolve_register_aliases(blocks, terms)""",
"""        simplify.fold_phi_copies(blocks, terms)
        simplify.resolve_register_aliases(blocks, terms)
        simplify.fold_mirrored_registers(blocks, terms, L.cfg.entry)""")])

# `X Is 0` -> `X Is Nothing`
patch('ir.py', [(
"""    if isinstance(e, Bin):
        p = PREC.get(e.op, 7)""",
"""    if isinstance(e, Bin) and e.op == 'Is':
        a, b = e.a, e.b
        if isinstance(a, Const) and a.v == 0:
            a, b = b, a
        right = 'Nothing' if isinstance(b, Const) and b.v == 0 else render(b, 8)
        s = '%s Is %s' % (render(a, 8), right)
        return '(%s)' % s if PREC['Is'] < parent_prec else s
    if isinstance(e, Bin):
        p = PREC.get(e.op, 7)""")])
print('patched')
