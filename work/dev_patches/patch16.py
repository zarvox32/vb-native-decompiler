import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# 2. ah from fnstsw
patch('lift.py', [(
"""            v = self.reg(SUB8H[name])
            if isinstance(v, (FpuStatus, FpuMasked)):
                return v""",
"""            v = self.reg(SUB8H[name])
            if isinstance(v, (FpuStatus, FpuMasked)) or type(v).__name__ == 'FpuAH':
                return v""")])

# 3. empty Print items ; 4. Kill takes a VARIANT*
patch('runtime.py', [(
"""        for c in codes or []:
            vt = c & 0x3f
            if byref:""",
"""        for c in codes or []:
            vt = c & 0x3f
            if vt == 0:
                continue                # empty item (e.g. `Print #1,` writes a blank line)
            if byref:"""), (
"""        L.emit(FmtStmt('Print #{0}, ' + '; '.join('{%d}' % (k + 1) for k in range(len(items))), [fnum] + items))""",
"""        if not items:
            L.emit(FmtStmt('Print #{0},', [fnum]))
            return
        L.emit(FmtStmt('Print #{0}, ' + '; '.join('{%d}' % (k + 1) for k in range(len(items))), [fnum] + items))"""), (
"""    def h_rtcKillFiles(self, L):
        s, = self.std_args(L, 1)
        L.emit(FmtStmt('Kill {0}', [s]))""",
"""    def h_rtcKillFiles(self, L):
        p, = self.std_args(L, 1)
        L.emit(FmtStmt('Kill {0}', [L.variant_at(p)]))""")])

# 1. loop body continue handling ; 8. switch subject ; 11. Select Case from chains ; 5. empty branch
patch('structure.py', [(
"""def tidy(ast, in_loop=None):
    \"\"\"Recursive pretty transformations on the AST.\"\"\"
    out = []""",
"""def tidy(ast, in_loop=None, body_end=False):
    \"\"\"Recursive pretty transformations on the AST.  body_end: this list ends a loop body.\"\"\"
    out = []"""), (
"""        if k == 'if':
            _, c, th, el = node
            th = tidy(th, in_loop)
            el = tidy(el, in_loop)""",
"""        if k == 'if':
            _, c, th, el = node
            th = tidy(th, in_loop)
            el = tidy(el, in_loop)
            if len(th) == 1 and th[0][0] == 'select' and not el and isinstance(c, Bin) and \\
                    c.op in ('<=', '<') and same(c.a, th[0][1]):
                out.append(th[0])          # jump-table bounds check
                continue"""), (
"""        if k == 'loop':
            _, L, body = node
            body = tidy(body, L)
            out.extend(make_loop(L, body))
            continue""",
"""        if k == 'loop':
            _, L, body = node
            body = tidy(body, L, body_end=True)
            body = invert_continues(body, L)
            out.extend(make_loop(L, body))
            continue"""), (
"""    # drop trailing 'continue' (natural end of a loop body)
    while out and out[-1][0] == 'continue' and in_loop is not None and out[-1][1] is in_loop:
        out.pop()
    return out""",
"""    # drop trailing 'continue' (natural end of a loop body)
    while body_end and out and out[-1][0] == 'continue' and in_loop is not None and out[-1][1] is in_loop:
        out.pop()
    return out


def invert_continues(body, L):
    \"\"\"`If c Then <continue>` followed by the rest of the loop body -> `If Not c Then <rest>`.\"\"\"
    for i, node in enumerate(body):
        if node[0] == 'if' and not node[3] and len(node[2]) == 1 and node[2][0][0] == 'continue' and \\
                node[2][0][1] is L and i + 1 < len(body):
            rest = body[i + 1:]
            if any(x[0] == 'anchor' for x in rest):
                continue
            rest = invert_continues(rest, L)
            return body[:i] + [('if', negate(node[1]), rest, [])]
    return body"""), (
"""def make_elseif(ast):
    out = []""",
"""def case_subject(c):
    \"\"\"(subject, [values]) if c is `X = k` or `X = a Or X = b`.\"\"\"
    if isinstance(c, Bin) and c.op == '=' and isinstance(c.b, (Const, Str)):
        return c.a, [c.b]
    if isinstance(c, Bin) and c.op == 'Or':
        a, b = case_subject(c.a), case_subject(c.b)
        if a and b and same(a[0], b[0]):
            return a[0], a[1] + b[1]
    return None


def make_select(chain, el):
    if len(chain) < 3:
        return None
    subj = None
    cases = []
    for c, body in chain:
        cs = case_subject(c)
        if cs is None or (subj is not None and not same(cs[0], subj)):
            return None
        subj = cs[0]
        cases.append((cs[1], body))
    return ('selectcase', subj, cases, el)


def make_elseif(ast):
    out = []"""), (
"""            if len(chain) > 1:
                out.append(('ifchain', chain, el))""",
"""            sel = make_select(chain, el)
            if sel is not None:
                out.append(sel)
            elif len(chain) > 1:
                out.append(('ifchain', chain, el))"""), (
"""            if k == 'select':
                _, idx, cases = node
                self.emit(depth, 'Select Case %s' % render(idx))
                for vals, body in cases:
                    self.emit(depth + 1, 'Case %s' % ', '.join(str(v) for v in vals))
                    self.render(body, depth + 2, False)
                self.emit(depth, 'End Select')
                continue""",
"""            if k == 'select':
                _, idx, cases = node
                subj, off = switch_subject(idx)
                self.emit(depth, 'Select Case %s' % render(subj))
                for vals, body in cases:
                    if not [x for x in body if x[0] != 'anchor']:
                        continue
                    self.emit(depth + 1, 'Case %s' % ', '.join(str(v - off) for v in vals))
                    self.render(body, depth + 2, False)
                self.emit(depth, 'End Select')
                continue
            if k == 'selectcase':
                _, subj, cases, el = node
                self.emit(depth, 'Select Case %s' % render(subj))
                for vals, body in cases:
                    self.emit(depth + 1, 'Case %s' % ', '.join(render(v) for v in vals))
                    self.render(body, depth + 2, False)
                if el:
                    self.emit(depth + 1, 'Case Else')
                    self.render(el, depth + 2, False)
                self.emit(depth, 'End Select')
                continue"""), (
"""def structure(blocks, terms, entry, roots, exit_kw='Sub'):""",
"""def switch_subject(idx):
    \"\"\"Jump-table index `subject + c` -> (subject, c) so case labels can be shown as subject values.\"\"\"
    from simplify import linear
    l = linear(idx)
    if l is not None:
        terms, c = l
        if len(terms) == 1:
            t, coef = list(terms.values())[0]
            if coef == 1:
                return t, c
    return idx, 0


def structure(blocks, terms, entry, roots, exit_kw='Sub'):"""), (
"""    def emit_cond(self, out, n, cond, a, b, stops, loops):
        if a == b:
            return a""",
"""    def emit_cond(self, out, n, cond, a, b, stops, loops):
        if a == b:
            return a
        # a branch that does nothing and continues with the other one (e.g. `If App Is Nothing`)
        for x, y in ((a, b), (b, a)):
            if x in self.blocks and not self.blocks[x] and self.succ.get(x) == [y] and x not in self.loops \\
                    and len(self.preds(x)) == 1:
                self.done.add(x)
                return y""")])

# make_elseif must recurse into selectcase; tidy-level helpers
patch('structure.py', [(
"""        if k == 'select':
            out.append(('select', node[1], [(v, make_elseif(b)) for v, b in node[2]]))
            continue
        out.append(node)
    return out


# -----""",
"""        if k == 'select':
            out.append(('select', node[1], [(v, make_elseif(b)) for v, b in node[2]]))
            continue
        out.append(node)
    return out


# -----""")])

# 9. dynamic arrays (SAFEARRAY*) ; 10. linear compare simplification
patch('simplify.py', [(
"""    bases = [(t, c) for t, c in terms.values() if c == 1 and isinstance(t, Var) and t.kind in ('field', 'global', 'local')]
    if len(bases) != 1:
        return None
    base = bases[0][0]
    rest = [(t, c) for k, (t, c) in terms.items() if t is not base]""",
"""    bases = [(t, c) for t, c in terms.values() if c == 1 and isinstance(t, Var) and t.kind in ('field', 'global', 'local')]
    sa = [(t, c) for t, c in terms.values() if c == 1 and isinstance(t, Deref) and t.off == 0xC and
          isinstance(t.base, Var)]
    if len(bases) != 1 and len(sa) == 1:
        # dynamic array: pvData = psa->pvData (+0xC), index adjusted by psa->lLbound (+0x14)
        pv = sa[0][0]
        psa = pv.base
        rest = [(t, c) for k, (t, c) in terms.items() if t is not pv]
        lb = [(t, c) for t, c in rest if isinstance(t, Deref) and t.off == 0x14 and same(t.base, psa)]
        rest = [(t, c) for t, c in rest if not (isinstance(t, Deref) and t.off == 0x14 and same(t.base, psa))]
        if len(rest) != 1 or len(lb) > 1:
            return None
        idx, stride = rest[0]
        if lb and lb[0][1] != -stride:
            return None
        if stride <= 0:
            return None
        k, field = divmod(const, stride)
        index = idx if k == 0 else Bin('+', idx, Const(k)) if k > 0 else Bin('-', idx, Const(-k))
        return psa, index, stride, field
    if len(bases) != 1:
        return None
    base = bases[0][0]
    rest = [(t, c) for k, (t, c) in terms.items() if t is not base]"""), (
"""def simplify_cmp(e):
    a, b = e.a, e.b""",
"""def simplify_cmp(e):
    a, b = e.a, e.b
    if isinstance(b, Const) and isinstance(a, Bin) and a.op in ('+', '-') and \\
            getattr(a, 'ty', None) not in ('Single', 'Double'):
        l = linear(a)
        if l is not None and len(l[0]) == 1 and l[1] != 0:
            t, coef = list(l[0].values())[0]
            if coef == 1 and not isinstance(t, (FConst,)):
                return Bin(e.op, t, Const(b.v - l[1]), ty='Boolean')"""), (
"""def resolve_register_aliases(blocks, terms):""",
"""def fold_phi_copies(blocks, terms):
    \"\"\"`R = v` at the end of each predecessor + `X = R` at the start of the join block (R read only
    there) -> `X = v` in each predecessor (a VB Select Case / If storing into one variable).\"\"\"
    reads = Counter()
    for b, stmts in blocks.items():
        for s in stmts:
            for e in iter_exprs_in(s):
                for x in e.walk():
                    if isinstance(x, Var) and x.kind == 'reg':
                        reads[ekey(x)] += 1
        for e in term_exprs(terms[b]):
            for x in e.walk():
                if isinstance(x, Var) and x.kind == 'reg':
                    reads[ekey(x)] += 1
    for b, stmts in blocks.items():
        if not stmts:
            continue
        s = stmts[0]
        if not (isinstance(s, Assign) and isinstance(s.src, Var) and s.src.kind == 'reg' and
                isinstance(s.dst, Var) and s.dst.kind in ('local', 'field', 'global') and s.kind == 'let'):
            continue
        rk = ekey(s.src)
        if reads[rk] != 1:
            continue
        dst = s.dst
        for b2, st2 in blocks.items():
            for t in st2:
                if isinstance(t, Assign) and isinstance(t.dst, Var) and ekey(t.dst) == rk:
                    t.dst = Var(dst.kind, dst.off, dst.size, dst.ty, dst.name)
        del stmts[0]


def remove_zero_inits(blocks, entry):
    \"\"\"VB zero-initialises string/object/variant locals in the prologue; drop those stores.\"\"\"
    stmts = blocks.get(entry)
    if not stmts:
        return
    i = 0
    while i < len(stmts):
        s = stmts[i]
        if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'local' and \\
                isinstance(s.src, Const) and s.src.v == 0:
            del stmts[i]
            continue
        break


def resolve_register_aliases(blocks, terms):""")])

# emit: call the new passes
patch('emit.py', [(
"""        simplify.resolve_register_aliases(blocks, terms)""",
"""        simplify.remove_zero_inits(blocks, L.cfg.entry)
        simplify.fold_phi_copies(blocks, terms)
        simplify.resolve_register_aliases(blocks, terms)""")])
print('patched')
