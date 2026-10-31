import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# [Me + c] reached through `lea reg, [esi + c1]` ... `[reg + c2]` is still a form field
patch('lift.py', [(
"""    def deref(self, base, idx, scale, disp, size, ty=None):
        if idx is not None:""",
"""    def deref(self, base, idx, scale, disp, size, ty=None):
        if isinstance(base, Bin) and base.op in ('+', '-') and isinstance(base.a, Me) and \\
                isinstance(base.b, Const):
            disp += base.b.v if base.op == '+' else -base.b.v
            base = base.a
        if idx is not None:""")])

# Me + i * size + c: a fixed-size array stored inside the form object
patch('simplify.py', [(
"""    bases = [(t, c) for t, c in terms.values() if c == 1 and isinstance(t, Var) and t.kind in ('field', 'global', 'local')]
    sa = [""",
"""    me = [k for k, (t, c) in terms.items() if isinstance(t, Me) and c == 1]
    if me and len(terms) == 2:
        (idx, stride), = [(t, c) for k, (t, c) in terms.items() if k != me[0]]
        if stride == d.size and stride > 0 and not isinstance(idx, Me):
            return Var('field', const, d.size), idx, stride, 0, 'fixed'
        return None
    bases = [(t, c) for t, c in terms.values() if c == 1 and isinstance(t, Var) and t.kind in ('field', 'global', 'local')]
    sa = [""")])

patch('emit.py', [(
"""            ak = ('field', o.index, off)
            if ak in self.arrays:
                strides = self.arrays[ak]
                stride, esize = max(strides)
                eh = self.elem_hints.get(ak, Counter())
                et = choose_type(eh, esize) if stride == esize else 'udt_%dbytes' % stride""",
"""            ak = ('field', o.index, off)
            if ak in self.arrays and self.array_kind.get(ak) == 'fixed':
                stride, esize = max(self.arrays[ak])
                et = choose_type(self.elem_hints.get(ak, Counter()), esize)
                later = [k2[1] for k2 in keys if k2[1] > off]
                if later:
                    n = (min(later) - off) // stride
                    out.append("Dim m_%X(%d) As %s   ' fixed-size array inside the form (%d elements before "
                               "the next variable; lower bound not recorded)" % (off, n - 1, et, n))
                else:
                    out.append("Dim m_%X() As %s   ' fixed-size array inside the form (size not recorded)"
                               % (off, et))
                continue
            if ak in self.arrays:
                strides = self.arrays[ak]
                stride, esize = max(strides)
                eh = self.elem_hints.get(ak, Counter())
                et = choose_type(eh, esize) if stride == esize else 'udt_%dbytes' % stride""")])
print('patched')
