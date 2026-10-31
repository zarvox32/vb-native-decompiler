import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('x86sem.py', [(
"""        for attempt in range(8):
            self.out_state = {}
            self.fixpoint(order, init)
            if not self.add_phis(order):
                break
        self.place_phi_assignments(order)
        return self""",
"""        for attempt in range(8):
            self.out_state = {}
            self.fixpoint(order, init)
            if not self.add_phis(order):
                break
        self.dup_of = {}
        self.duplicate_tail_merges(order)
        self.place_phi_assignments(order)
        return self

    def duplicate_tail_merges(self, order):
        \"\"\"A block entered from several paths that pushed different argument values (the compiler
        merged the identical tail of those paths) is lifted once per predecessor.\"\"\"
        from lift import PhiCell
        cfg = self.cfg
        for b in order:
            st = self.in_state.get(b)
            if st is None or b == cfg.entry:
                continue
            if not any(isinstance(v[1], PhiCell) for v in st.mem.values()):
                continue
            preds = [p for p in cfg.blocks[b].preds if p in self.out_state]
            if len(preds) <= 2 and self.is_diamond(preds, b):
                continue                     # two arms of one If: becomes IIf(...)
            blk = cfg.blocks[b]
            copies = []
            for p in preds:
                eid = 0x70000000 + 0x100000 + len(getattr(self, 'edge_blocks', {})) + len(copies) + \\
                    sum(len(v) for v in self.dup_of.values())
                pst = self.out_state[p].copy()
                for r in self.phis.get(b, ()):
                    pst.regs[r] = Var('reg', '%s@%x' % (r, b), 4)
                self.lift_block(blk, pst, key=eid)
                if not hasattr(self, 'edge_blocks'):
                    self.edge_blocks = {}
                self.edge_blocks[eid] = b
                t = self.block_term[p]
                self.block_term[p] = redirect(t, b, eid)
                copies.append(eid)
            self.dup_of[b] = copies

    def is_diamond(self, preds, b):
        blocks = self.cfg.blocks
        heads = set()
        for p in preds:
            pp = [q for q in blocks[p].preds] if p in blocks else []
            if blocks[p].term == 'jcc':
                heads.add(p)
            elif len(pp) == 1:
                heads.add(pp[0])
            else:
                return False
        return len(heads) == 1"""), (
"""    def lift_block(self, blk, st):
        self.st = st
        self.stmts = []""",
"""    def lift_block(self, blk, st, key=None):
        key = blk.start if key is None else key
        self.st = st
        self.stmts = []"""), (
"""        # terminator
        self.block_term[blk.start] = self.make_term(blk)
        self.block_stmts[blk.start] = self.stmts
        self.out_state[blk.start] = self.st""",
"""        # terminator
        self.block_term[key] = self.make_term(blk)
        self.block_stmts[key] = self.stmts
        self.out_state[key] = self.st"""), (
"""    def place_phi_assignments(self, order):
        pending = {}
        for b, regs in self.phis.items():
            blk = self.cfg.blocks[b]
            for p in blk.preds:""",
"""    def place_phi_assignments(self, order):
        pending = {}
        for b, regs in self.phis.items():
            blk = self.cfg.blocks[b]
            preds = []
            for p in blk.preds:
                preds.extend(self.dup_of.get(p, [p]))
            for p in preds:"""), (
"""class CdqOf(Expr):""",
"""def redirect(t, old, new):
    if t[0] == 'goto':
        return ('goto', new) if t[1] == old else t
    if t[0] == 'cond':
        return ('cond', t[1], new if t[2] == old else t[2], new if t[3] == old else t[3])
    if t[0] == 'switch':
        return ('switch', t[1], [new if x == old else x for x in t[2]])
    return t


class CdqOf(Expr):""")])

# new edge-block ids must not collide with tail copies
patch('x86sem.py', [(
"""        if not hasattr(self, 'edge_blocks'):
            self.edge_blocks = {}
        eid = 0x70000000 + len(self.edge_blocks)""",
"""        if not hasattr(self, 'edge_blocks'):
            self.edge_blocks = {}
        eid = 0x70000000 + len(self.edge_blocks)
        while eid in self.block_stmts:
            eid += 1""")])

# the original block may now be unreachable: prepare() already prunes by terminators
print('patched')
