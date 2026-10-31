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
"""        blocks, terms = self.prepare(p, L)
        self.params_rewrite(p, sig, blocks, terms, fname)""",
"""        blocks, terms = self.prepare(p, L)
        simplify.merge_variant_copies(blocks)
        self.params_rewrite(p, sig, blocks, terms, fname)"""), (
"""            if isinstance(x, Deref) and isinstance(x.base, Var) and x.base.kind in ('arg', 'param') and                     x.off == 0 and not getattr(x.base, 'name', None):
                if retptr and x.base.off == last_off:
                    return result
                return Var('param', x.base.off, x.size)""",
"""            if isinstance(x, Deref) and isinstance(x.base, Var) and x.base.kind in ('arg', 'param') and \\
                    x.off == 0 and not getattr(x.base, 'name', None):
                if retptr and x.base.off == last_off:
                    return Var('result', 0, x.size, name=fname)
                return Var('param', x.base.off, x.size)""")])

patch('simplify.py', [(
"""    if len(bases) != 1:
        return None
    base = bases[0][0]
    rest = [(t, c) for k, (t, c) in terms.items() if t is not base]
    if not rest:
        size = d.size
        if size <= 0 or const % size:
            return None
        return base, Const(const // size), size, 0
    if len(rest) != 1:
        return None""",
"""    if len(bases) != 1:
        return None
    base = bases[0][0]
    rest = [(t, c) for k, (t, c) in terms.items() if t is not base]
    if not rest:
        size = d.size
        if size <= 0 or const % size:
            return None
        return base, Const(const // size), size, 0
    if len(rest) >= 2:
        # column-major multi-dimensional array: coefficients stride, stride*d1, stride*d1*d2 ...
        rest.sort(key=lambda tc: tc[1])
        stride = rest[0][1]
        if stride <= 0 or any(c % stride for t, c in rest):
            return None
        for (t1, c1), (t2, c2) in zip(rest, rest[1:]):
            if c2 % c1 or c2 <= c1:
                return None
        k, field = divmod(const, stride)
        idxs = [t for t, c in rest]
        if k:
            idxs[0] = Bin('+', idxs[0], Const(k)) if k > 0 else Bin('-', idxs[0], Const(-k))
        return base, idxs, stride, field
    if len(rest) != 1:
        return None"""), (
"""        base, index, stride, field = a
        note(base, stride, x.size)
        ix = Index(base, [index])""",
"""        base, index, stride, field = a
        note(base, stride, x.size)
        ix = Index(base, index if isinstance(index, list) else [index])""")])
print('patched')
