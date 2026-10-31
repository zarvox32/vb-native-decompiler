import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


arr_code = '''

# --------------------------------------------------------------------------- arrays
def linear(e):
    """Decompose an integer expression into ({term_key: (term, coef)}, const). None if not linear."""
    if isinstance(e, Const):
        return {}, e.v
    if isinstance(e, Bin) and e.op in ('+', '-'):
        a = linear(e.a)
        b = linear(e.b)
        if a is None or b is None:
            return None
        sign = 1 if e.op == '+' else -1
        terms = dict(a[0])
        for k, (t, c) in b[0].items():
            if k in terms:
                terms[k] = (t, terms[k][1] + sign * c)
            else:
                terms[k] = (t, sign * c)
        return terms, a[1] + sign * b[1]
    if isinstance(e, Bin) and e.op == '*':
        for x, y in ((e.a, e.b), (e.b, e.a)):
            if isinstance(y, Const):
                l = linear(x)
                if l is None:
                    return None
                return {k: (t, c * y.v) for k, (t, c) in l[0].items()}, l[1] * y.v
        return None
    if isinstance(e, Un) and e.op == 'neg':
        l = linear(e.a)
        if l is None:
            return None
        return {k: (t, -c) for k, (t, c) in l[0].items()}, -l[1]
    return {ekey(e): (e, 1)}, 0


def array_access(d):
    """Deref(base + i*stride + c, off) with base a variable -> (base, index expr, stride, field offset)."""
    if not isinstance(d, Deref) or not isinstance(d.base, Bin) or d.base.op not in ('+', '-'):
        return None
    l = linear(d.base)
    if l is None:
        return None
    terms, const = l
    const += d.off
    bases = [(t, c) for t, c in terms.values() if c == 1 and isinstance(t, Var) and t.kind in ('field', 'global', 'local')]
    if len(bases) != 1:
        return None
    base = bases[0][0]
    rest = [(t, c) for k, (t, c) in terms.items() if t is not base]
    if not rest:
        size = d.size
        if size <= 0 or const % size:
            return None
        return base, Const(const // size), size, 0
    if len(rest) != 1:
        return None
    idx, stride = rest[0]
    if stride <= 0:
        return None
    k, field = divmod(const, stride)
    index = idx if k == 0 else Bin('+', idx, Const(k)) if k > 0 else Bin('-', idx, Const(-k))
    return base, index, stride, field


def array_rewrite_fn(note):
    def fn(x):
        a = array_access(x)
        if a is None:
            return None
        base, index, stride, field = a
        note(base, stride, x.size)
        ix = Index(base, [index])
        if stride == x.size and field == 0:
            return ix
        return Member(ix, field, x.size, name='f%X' % field)
    return fn
'''
s = open('simplify.py', encoding='utf-8').read()
if 'def array_access' not in s:
    s += arr_code
    open('simplify.py', 'w', encoding='utf-8').write(s)

patch('emit.py', [(
"""        simplify.lower_all(blocks, terms)
        self.collect_types(p, L, blocks, terms)
        self.boolean_rewrite(blocks, terms)""",
"""        simplify.lower_all(blocks, terms)
        fn = simplify.array_rewrite_fn(self.note_array)
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)
            terms[b] = term_rewrite(terms[b], fn)
        self.collect_types(p, L, blocks, terms)
        self.boolean_rewrite(blocks, terms)"""), (
"""    # ---------------------------------------------------------------- types
    def var_type(self, v):""",
"""    def note_array(self, base, stride, size):
        k = ekey(base)
        if base.kind == 'field' and self.cur_obj is not None:
            k = ('field', self.cur_obj.index, base.off)
        elif base.kind == 'local':
            k = ('local', self.cur_proc, base.off)
        self.arrays.setdefault(k, set()).add((stride, size))

    def is_array(self, v):
        if v.kind == 'field' and self.cur_obj is not None:
            return ('field', self.cur_obj.index, v.off) in self.arrays
        if v.kind == 'local':
            return ('local', self.cur_proc, v.off) in self.arrays
        return ekey(v) in self.arrays

    # ---------------------------------------------------------------- types
    def var_type(self, v):"""), (
"""        self.proc_excluded = {}    # proc va -> Exclusion (whole procedure omitted)""",
"""        self.proc_excluded = {}    # proc va -> Exclusion (whole procedure omitted)
        self.arrays = {}           # array base var -> {(stride, element size)}
        self.cur_proc = None"""), (
"""        sig = dc.signatures.get(p.va) or {}
        fname = dc.proc_name(p)
        self.cur_obj = p.obj""",
"""        sig = dc.signatures.get(p.va) or {}
        fname = dc.proc_name(p)
        self.cur_obj = p.obj
        self.cur_proc = p.va"""), (
"""                if isinstance(s, Assign) and isinstance(s.dst, Var) and isinstance(s.src, Const):
                    self.add_hint(ekey(s.dst), 'BoolConst' if s.src.v in (0, -1) else 'IntConst', 1)""",
"""                if isinstance(s, Assign) and isinstance(s.dst, Var) and isinstance(s.src, Const):
                    if s.src.v == -1:
                        self.add_hint(ekey(s.dst), 'BoolConst', 1)
                    elif s.src.v != 0:
                        self.add_hint(ekey(s.dst), 'IntConst', 1)
                if isinstance(s, Assign) and isinstance(s.dst, Index) and isinstance(s.dst.arr, Var):
                    t = expr_type(s.src)
                    if s.kind == 'set':
                        t = t if t and t not in TYPE_SET else 'Object'
                    if t:
                        self.add_hint(('elem',) + ekey(s.dst.arr), t, 1)"""), (
"""            if isinstance(x, Bin) and x.op in ('=', '<>') and isinstance(x.a, Var) and isinstance(x.b, Const):
                self.add_hint(ekey(x.a), 'BoolConst' if x.b.v in (0, -1) else 'IntConst', 1)""",
"""            if isinstance(x, Bin) and x.op in ('=', '<>') and isinstance(x.a, Var) and isinstance(x.b, Const):
                if x.b.v == -1:
                    self.add_hint(ekey(x.a), 'BoolConst', 1)
                elif x.b.v != 0:
                    self.add_hint(ekey(x.a), 'IntConst', 1)"""), (
"""    def add_hint(self, k, t, n=1):
        if k is None or t is None:
            return""",
"""    def add_hint(self, k, t, n=1):
        if k is None or t is None:
            return
        if k[0] == 'elem':
            kk = k[1:]
            if kk[1] == 'field' and self.cur_obj is not None:
                self.elem_hints[('field', self.cur_obj.index, kk[2])][t] += n
            elif kk[1] == 'global':
                self.elem_hints[('global', kk[2])][t] += n
            return"""), (
"""        self.arrays = {}           # array base var -> {(stride, element size)}""",
"""        self.arrays = {}           # array base var -> {(stride, element size)}
        self.elem_hints = defaultdict(Counter)"""), (
"""        for k in keys:
            off = k[1]
            if (o.index, off) in self.control_by_storage:
                continue
            t = choose_type(self.field_hints.get(k, Counter()), self.field_sizes.get(k, 4))
            out.append('Dim m_%X As %s' % (off, t))
        return out""",
"""        for k in keys:
            off = k[1]
            if (o.index, off) in self.control_by_storage:
                continue
            ak = ('field', o.index, off)
            if ak in self.arrays:
                strides = self.arrays[ak]
                stride, esize = max(strides)
                eh = self.elem_hints.get(ak, Counter())
                et = choose_type(eh, esize) if stride == esize else 'udt_%dbytes' % stride
                out.append("Dim m_%X() As %s   ' array (element size %d bytes)" % (off, et, stride))
                continue
            t = choose_type(self.field_hints.get(k, Counter()), self.field_sizes.get(k, 4))
            out.append('Dim m_%X As %s' % (off, t))
        return out""")])
print('patched')
