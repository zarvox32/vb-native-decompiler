import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# 1. one register variable per (join block, register)
patch('x86sem.py', [(
"""                for r in self.phis.get(b, ()):
                    st.regs[r] = Var('reg', r, 4)""",
"""                for r in self.phis.get(b, ()):
                    st.regs[r] = Var('reg', '%s@%x' % (r, b), 4)"""), (
"""                for r in sorted(regs):
                    v = st.regs.get(r)
                    if v is None or isinstance(v, Unknown):
                        continue
                    if isinstance(v, Var) and v.kind == 'reg' and v.off == r:
                        continue
                    s = Assign(Var('reg', r, 4), v)""",
"""                for r in sorted(regs):
                    v = st.regs.get(r)
                    if v is None or isinstance(v, Unknown):
                        continue
                    name = '%s@%x' % (r, b)
                    if isinstance(v, Var) and v.kind == 'reg' and v.off == name:
                        continue
                    s = Assign(Var('reg', name, 4), v)""")])

patch('emit.py', [(
"""        if v.kind == 'reg':
            return 'r' + v.off[1:].upper()
        return None""",
"""        if v.kind == 'reg':
            n = self.reg_names.get(v.off)
            if n is None:
                base = 'r' + v.off.split('@')[0][1:].upper()
                used = set(self.reg_names.values())
                n = base
                k = 2
                while n in used:
                    n = '%s%d' % (base, k)
                    k += 1
                self.reg_names[v.off] = n
            return n
        return None"""), (
"""        self.cur_obj = p.obj
        self.cur_proc = p.va""",
"""        self.cur_obj = p.obj
        self.cur_proc = p.va
        self.reg_names = {}"""), (
"""        self.arrays = {}           # array base var -> {(stride, element size)}""",
"""        self.arrays = {}           # array base var -> {(stride, element size)}
        self.reg_names = {}"""), (
"""        self.resolve_phicells(blocks, terms)""",
"""        self.resolve_phicells(blocks, terms)
        # a branch whose two arms are empty and rejoin (left over from tail-merged pushes) is a no-op
        for b in list(terms):
            t = terms[b]
            if t[0] == 'cond':
                ends = []
                for x in (t[2], t[3]):
                    while x in blocks and not blocks[x] and terms[x][0] == 'goto' and x != b:
                        x = terms[x][1]
                    ends.append(x)
                if ends[0] == ends[1]:
                    terms[b] = ('goto', ends[0])""")])

# 2. alias scan continues past assignments of other locals
patch('simplify.py', [(
"""            stmts = blocks[b]
            found = None
            for j in range(i - 1, -1, -1):
                t = stmts[j]
                if isinstance(t, Assign) and isinstance(t.dst, Var) and t.dst.kind == 'local':
                    if same(t.src, s.src) or (isinstance(s.src, Var) and same(s.src, t.dst)):
                        found = ekey(t.dst), t.dst
                    break""",
"""            stmts = blocks[b]
            found = None
            seen = set()
            for j in range(i - 1, -1, -1):
                t = stmts[j]
                if isinstance(t, Assign) and isinstance(t.dst, Var) and t.dst.kind == 'local':
                    k = ekey(t.dst)
                    if k not in seen and (same(t.src, s.src) or (isinstance(s.src, Var) and same(s.src, t.dst))):
                        found = k, t.dst
                        break
                    seen.add(k)"""), (
"""        if l is not None and len(l[0]) == 1 and l[1] != 0:
            t, coef = list(l[0].values())[0]
            if coef == 1 and not isinstance(t, (FConst,)):
                return Bin(e.op, t, Const(b.v - l[1]), ty='Boolean')""",
"""        if l is not None and len(l[0]) == 1:
            t, coef = list(l[0].values())[0]
            if coef == 1 and not isinstance(t, (FConst,)) and (l[1] != 0 or not same(t, a)):
                return Bin(e.op, t, Const(b.v - l[1]), ty='Boolean')""")])

# 3. cache values of visible assignments unless self-referential or containing calls
patch('lift.py', [(
"""            if not is_simple_value(val):
                # visible assignment of a computed value: later reads show the variable
                return""",
"""            if not is_simple_value(val) and contains_call(val):
                # visible assignment of a call result: later reads show the variable
                return"""), (
"""def reads_location(e, v):""",
"""def contains_call(e):
    if not isinstance(e, Expr):
        return False
    return any(isinstance(x, Call) for x in e.walk())


def reads_location(e, v):""")])

# 6. bounds check fold with linear equivalence
patch('structure.py', [(
"""            if len(th) == 1 and th[0][0] == 'select' and not el and isinstance(c, Bin) and \\
                    c.op in ('<=', '<') and same(c.a, th[0][1]):""",
"""            if len(th) == 1 and th[0][0] == 'select' and not el and isinstance(c, Bin) and \\
                    c.op in ('<=', '<') and lin_same(c.a, th[0][1]):"""), (
"""def invert_continues(body, L):""",
"""def lin_same(a, b):
    from simplify import linear
    if same(a, b):
        return True
    la, lb = linear(a), linear(b)
    if la is None or lb is None:
        return False
    ka = {k: c for k, (t, c) in la[0].items()}
    kb = {k: c for k, (t, c) in lb[0].items()}
    return ka == kb


def invert_continues(body, L):""")])

# 7. Print/Write variants are passed by pointer
patch('runtime.py', [(
"""                elif vt == 12 and not (c & 0x80):
                    items.append(L.variant_at(L.cell(off)))
                    off += 4""",
"""                elif vt == 12:
                    items.append(L.variant_at(L.cell(off)))
                    off += 4""")])
print('patched')
