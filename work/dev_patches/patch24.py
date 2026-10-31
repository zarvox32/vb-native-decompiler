import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# 1. phi assignments vs. the block's own branch condition
patch('x86sem.py', [(
"""    def place_phi_assignments(self, order):
        for b, regs in self.phis.items():
            blk = self.cfg.blocks[b]
            for p in blk.preds:
                st = self.out_state.get(p)
                if st is None or p not in self.block_stmts:
                    continue
                for r in sorted(regs):
                    v = st.regs.get(r)
                    if v is None or isinstance(v, Unknown):
                        continue
                    name = '%s@%x' % (r, b)
                    if isinstance(v, Var) and v.kind == 'reg' and v.off == name:
                        continue
                    s = Assign(Var('reg', name, 4), v)
                    s.addr = blk.start
                    s.phi = True
                    self.block_stmts[p].append(s)""",
"""    def place_phi_assignments(self, order):
        pending = {}
        for b, regs in self.phis.items():
            blk = self.cfg.blocks[b]
            for p in blk.preds:
                st = self.out_state.get(p)
                if st is None or p not in self.block_stmts:
                    continue
                for r in sorted(regs):
                    v = st.regs.get(r)
                    if v is None or isinstance(v, Unknown):
                        continue
                    name = '%s@%x' % (r, b)
                    if isinstance(v, Var) and v.kind == 'reg' and v.off == name:
                        continue
                    s = Assign(Var('reg', name, 4), v)
                    s.addr = blk.start
                    s.phi = True
                    pending.setdefault(p, []).append(s)
        from simplify import rewrite
        for p, assigns in pending.items():
            t = self.block_term.get(p)
            if t and t[0] in ('cond', 'switch'):
                cond = t[1]
                # the branch condition is evaluated after these assignments: express it with the new
                # values (x+1 -> x) and snapshot anything that still reads an overwritten variable
                for s in assigns:
                    k = ekey(s.src)
                    cond = rewrite(cond, lambda e, k=k, v=s.dst: v if not isinstance(e, Var) and ekey(e) == k else None)
                written = {ekey(s.dst) for s in assigns}
                if any(isinstance(x, Var) and ekey(x) in written for x in cond.walk()):
                    tmp = self.new_temp('Boolean')
                    self.block_stmts[p].append(Assign(tmp, t[1]))
                    cond = tmp
                self.block_term[p] = (t[0], cond) + t[2:]
            self.block_stmts[p].extend(assigns)"""), (
"""        if blk.term == 'switch':
            idx_reg, targets = blk.switch
            return ('switch', self.reg(idx_reg), targets)""",
"""        if blk.term == 'switch':
            idx_reg, targets = blk.switch
            idx = self.reg(idx_reg)
            bt = getattr(blk, 'byte_table', None)
            if bt is not None and isinstance(idx, Deref) and idx.size == 1:
                # two-level table: idx = byte[x + table1]; show the cases in terms of x
                base = idx.base
                if isinstance(base, Bin) and base.op == '+' and isinstance(base.b, Const) and \\
                        (base.b.v & 0xffffffff) == bt[0]:
                    return ('switch', base.a, bt[1])
            return ('switch', idx, targets)""")])

# 3. two-level jump tables in the CFG
patch('cfg.py', [(
"""                    if o.kind == 'mem' and o.index is not None and o.scale == 4 and o.base is None:
                        targets = self._jump_table(o.disp & 0xffffffff, b)""",
"""                    if o.kind == 'mem' and o.index is not None and o.scale == 4 and o.base is None:
                        targets = self._jump_table(o.disp & 0xffffffff, b)
                        bt = self._byte_table(b, o.index, o.disp & 0xffffffff)
                        if bt is not None:
                            b.byte_table = bt
                            targets = bt[1]"""), (
"""    def _jump_table(self, table, b):""",
"""    def _byte_table(self, b, reg, table2):
        \"\"\"`movzx reg, byte [x + table1] ; jmp [reg*4 + table2]`  -> (table1, targets per value of x).\"\"\"
        im = self.im
        movzx = None
        for ins in reversed(b.insns[:-1]):
            if ins.mn == 'movzx' and ins.ops[0].kind == 'reg' and ins.ops[0].reg == reg and \\
                    ins.ops[1].kind == 'mem' and ins.ops[1].size == 1:
                movzx = ins
                break
        if movzx is None:
            return None
        t1 = movzx.ops[1].disp & 0xffffffff
        n = None
        for ins in reversed(b.insns[:-1]):
            if ins.mn == 'cmp' and ins.ops[1].kind == 'imm' and ins.addr < movzx.addr:
                n = ins.ops[1].imm + 1
                break
        if n is None:
            # bound check may be in the previous block: scan back a little
            cur = b.start
            for k in range(1, 40):
                ins = self.dec.at(cur - k)
                if ins is not None and ins.mn == 'cmp' and ins.ops and len(ins.ops) == 2 and \\
                        ins.ops[1].kind == 'imm' and ins.next <= cur:
                    n = ins.ops[1].imm + 1
                    break
        if n is None or n > 1024 or not im.valid(t1, n):
            return None
        idxs = [im.u8(t1 + i) for i in range(n)]
        targets = []
        for k in idxs:
            if not im.valid(table2 + 4 * k, 4):
                return None
            t = im.u32(table2 + 4 * k)
            if not (self.proj.code_start <= t < self.proj.code_end):
                return None
            targets.append(t)
        return (t1, targets)

    def _jump_table(self, table, b):""")])
patch('cfg.py', [(
"""        self.switch = None       # (index reg insn, [targets])""",
"""        self.switch = None       # (index reg insn, [targets])
        self.byte_table = None""")])

# 4. register variables may be inlined like locals
patch('simplify.py', [(
"""                d = stmts[i]
                if isinstance(d, Assign) and is_local_var(d.dst) and d.dst.kind == 'local':
                    k = ekey(d.dst)
                    u = stmts[i + 1]""",
"""                d = stmts[i]
                if isinstance(d, Assign) and is_local_var(d.dst) and d.dst.kind in ('local', 'reg'):
                    k = ekey(d.dst)
                    u = stmts[i + 1]"""), (
"""                if isinstance(d, Assign) and is_local_var(d.dst) and d.dst.kind == 'local' and t[0] in ('cond', 'switch'):""",
"""                if isinstance(d, Assign) and is_local_var(d.dst) and d.dst.kind in ('local', 'reg') and \\
                        t[0] in ('cond', 'switch'):""")])

# 2. boolean materialised through two branches: R = 0/1 (or -1/0) in the arms -> comparison
code = '''

def fold_boolean_phis(blocks, terms):
    """cond -> (A: R = 1 | B: R = 0) -> J   ==>   R = <cond as 0/1> at the end of the cond block."""
    from lift import SetCC, BoolVal
    changed = True
    while changed:
        changed = False
        for c, t in list(terms.items()):
            if t[0] != 'cond':
                continue
            a, b = t[2], t[3]
            if a not in blocks or b not in blocks:
                continue
            sa, sb = blocks[a], blocks[b]
            if len(sa) != 1 or len(sb) != 1:
                continue
            x, y = sa[0], sb[0]
            if not (isinstance(x, Assign) and isinstance(y, Assign) and isinstance(x.dst, Var) and
                    x.dst.kind == 'reg' and same(x.dst, y.dst) and isinstance(x.src, Const) and
                    isinstance(y.src, Const)):
                continue
            if terms[a][0] != 'goto' or terms[b][0] != 'goto' or terms[a][1] != terms[b][1]:
                continue
            pa = sum(1 for tt in terms.values() for s in term_succs(tt) if s == a)
            pb = sum(1 for tt in terms.values() for s in term_succs(tt) if s == b)
            if pa != 1 or pb != 1:
                continue
            va, vb = x.src.v, y.src.v
            cond = t[1]
            if (va, vb) == (1, 0):
                val = SetCC(cond)
            elif (va, vb) == (0, 1):
                val = SetCC(negate(cond))
            elif (va, vb) == (-1, 0):
                val = BoolVal(cond)
            elif (va, vb) == (0, -1):
                val = BoolVal(negate(cond))
            else:
                continue
            j = terms[a][1]
            blocks[c].append(Assign(x.dst, val, x.addr))
            terms[c] = ('goto', j)
            del blocks[a], terms[a], blocks[b], terms[b]
            changed = True
'''
s = open('simplify.py', encoding='utf-8').read()
if 'def fold_boolean_phis' not in s:
    s += code
    open('simplify.py', 'w', encoding='utf-8').write(s)

patch('emit.py', [(
"""        simplify.remove_zero_inits(blocks, L.cfg.entry)
        simplify.fold_phi_copies(blocks, terms)""",
"""        simplify.remove_zero_inits(blocks, L.cfg.entry)
        simplify.fold_boolean_phis(blocks, terms)
        self.merge_chains(blocks, terms, L.cfg.entry, set(L.cfg.extra))
        simplify.fold_phi_copies(blocks, terms)""")])

# lowering: -(0/1 comparison) is the VB Boolean
patch('simplify.py', [(
"""    if isinstance(e, BoolVal):
        return to_bool_expr(e.cond)""",
"""    if isinstance(e, BoolVal):
        return to_bool_expr(e.cond)
    if isinstance(e, Un) and e.op == 'neg' and isinstance(e.a, SetCC):
        return to_bool_expr(e.a.cond)"""), (
"""    if isinstance(e, Un) and e.op == 'neg' and isinstance(e.a, Const):""",
"""    if isinstance(e, Un) and e.op == 'neg' and isinstance(e.a, Un) and e.a.op == 'neg' and \\
            getattr(e.a.a, 'ty', None) == 'Boolean':
        return e.a.a
    if isinstance(e, Un) and e.op == 'neg' and isinstance(e.a, Const):""")])
print('patched')
