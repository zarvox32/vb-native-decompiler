import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('emit.py', [(
"""        if sig and sig.get('retvar'):
            resvar = sig['retvar']
        result = Var('result', 0, 4, name=fname)""",
"""        if sig and sig.get('retvar'):
            resvar = sig['retvar']
        result = Var('result', 0, 4, name=fname)
        byref = (sig or {}).get('byref', {})
        # writes into the stack slot of a ByRef parameter are compiler scratch reuse, not the parameter
        for b, stmts in blocks.items():
            for s in stmts:
                if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'arg' and \\
                        byref.get(s.dst.off) and not (retptr and s.dst.off == last_off):
                    s.dst = Var('local', 0x10000 + s.dst.off, s.dst.size)"""), (
"""            if isinstance(x, Var) and x.kind == 'arg':
                if retptr and x.off == last_off:
                    return result
                return Var('param', x.off, x.size)""",
"""            if isinstance(x, Var) and x.kind == 'arg':
                if retptr and x.off == last_off:
                    return result
                if byref.get(x.off):
                    return AddrOf(Var('param', x.off, 4))     # the pointer value of a ByRef parameter
                return Var('param', x.off, x.size)
            if isinstance(x, (Deref,)) and isinstance(x.base, AddrOf) and x.off == 0 and isinstance(x.base.e, Var):
                v = x.base.e
                return Var(v.kind, v.off, x.size, v.ty, v.name)
            if type(x).__name__ == 'VTbl' and isinstance(x.obj, AddrOf) and isinstance(x.obj.e, Var):
                return x.obj.e""")])

code = '''

def propagate_constant_regvars(blocks, terms):
    """A register variable whose every assignment is the same stable value is that value."""
    vals = {}
    bad = set()
    for b, stmts in blocks.items():
        for s in stmts:
            if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'reg':
                k = ekey(s.dst)
                if not isinstance(s.src, (AddrOf, Const, Str, Me)) and not (
                        isinstance(s.src, Var) and s.src.kind in ('param',)):
                    bad.add(k)
                elif k in vals and not same(vals[k], s.src):
                    bad.add(k)
                else:
                    vals[k] = s.src
    good = {k: v for k, v in vals.items() if k not in bad}
    if not good:
        return

    def fn(x):
        if isinstance(x, Var) and x.kind == 'reg' and ekey(x) in good:
            return good[ekey(x)]
        if isinstance(x, Deref) and isinstance(x.base, AddrOf) and x.off == 0 and isinstance(x.base.e, Var):
            v = x.base.e
            return Var(v.kind, v.off, x.size, v.ty, v.name)
        return None
    for b in blocks:
        blocks[b] = [s for s in blocks[b] if not (isinstance(s, Assign) and isinstance(s.dst, Var) and
                                                   s.dst.kind == 'reg' and ekey(s.dst) in good)]
        for s in blocks[b]:
            stmt_rewrite(s, fn)
        terms[b] = term_rewrite(terms[b], fn)
'''
s = open('simplify.py', encoding='utf-8').read()
if 'def propagate_constant_regvars' not in s:
    s += code
    open('simplify.py', 'w', encoding='utf-8').write(s)

patch('emit.py', [(
"""        simplify.remove_zero_inits(blocks, L.cfg.entry)""",
"""        simplify.remove_zero_inits(blocks, L.cfg.entry)
        simplify.propagate_constant_regvars(blocks, terms)""")])

# lowering: Deref(AddrOf(x)) -> x everywhere
patch('simplify.py', [(
"""def lower(e):
    \"\"\"Lifter-internal nodes -> renderable VB.\"\"\"""",
"""def lower(e):
    \"\"\"Lifter-internal nodes -> renderable VB.\"\"\"
    if isinstance(e, Deref) and isinstance(e.base, AddrOf) and e.off == 0 and isinstance(e.base.e, Var):
        v = e.base.e
        return Var(v.kind, v.off, e.size, v.ty, v.name)
    if type(e).__name__ == 'VTbl' and isinstance(e.obj, AddrOf) and isinstance(e.obj.e, Var):
        return e.obj.e""")])
print('patched')
