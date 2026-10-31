import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# ---- cfg.py: REGARG-aware liveness
patch('cfg.py', [(
"""            rd = set(ins.rd)
            if ins.mn == 'call':
                rd |= {'ecx', 'edx'}""",
"""            rd = set(ins.rd)
            if ins.mn == 'call':
                o = ins.ops[0]
                if o.kind == 'reg':
                    rd |= {'ecx', 'edx'}
                elif o.kind == 'mem' and o.base is None and o.index is None:
                    if cfg.im.imports.get(o.disp & 0xffffffff) in REGARG:
                        rd |= {'ecx', 'edx'}"""), (
"""def liveness(cfg, blocks_set=None):""",
"""REGARG = {'__vbaStrCopy', '__vbaStrMove', '__vbaVarMove', '__vbaVarCopy', '__vbaVarDup', '__vbaFreeStr',
          '__vbaFreeObj', '__vbaFreeVar', '__vbaI2I4', '__vbaI2Abs', '__vbaI4Abs', '__vbaUI1I2', '__vbaUI1I4',
          '__vbaVarZero', '__vbaI4Sgn'}


def liveness(cfg, blocks_set=None):""")])

# ---- x86sem.py
patch('x86sem.py', [(
"""    def run(self):
        cfg = self.cfg
        order = self.rpo()
        init = State()
        self.in_state[cfg.entry] = init
        for h in cfg.extra:
            self.in_state[h] = None
        changed = True
        rounds = 0
        while changed and rounds < 6:
            changed = False
            rounds += 1
            for b in order:
                blk = cfg.blocks[b]
                preds = [p for p in blk.preds if p in self.out_state]
                if b == cfg.entry:
                    st = init.copy()
                elif b in cfg.extra and not preds:
                    st = self.handler_state()
                else:
                    st = merge_states([self.out_state[p] for p in preds])
                    if st is None:
                        st = self.handler_state() if b in cfg.extra else State()
                        if b not in cfg.extra and preds == []:
                            st.sp = self.local_low if self.local_low is not None else 0
                old = self.out_state.get(b)
                self.lift_block(blk, st)
                new = self.out_state[b]
                if old is None or not self.state_eq(old, new):
                    changed = True
        return self""",
"""    def run(self):
        from cfg import liveness
        cfg = self.cfg
        order = self.rpo()
        self.live_in = liveness(cfg, set(order))
        self.phis = {}            # block -> registers merged into a register variable
        init = State()
        for attempt in range(8):
            self.out_state = {}
            self.fixpoint(order, init)
            if not self.add_phis(order):
                break
        self.place_phi_assignments(order)
        return self

    def fixpoint(self, order, init):
        cfg = self.cfg
        changed = True
        rounds = 0
        while changed and rounds < 8:
            changed = False
            rounds += 1
            for b in order:
                blk = cfg.blocks[b]
                preds = [p for p in blk.preds if p in self.out_state]
                if b == cfg.entry:
                    st = init.copy()
                elif b in cfg.extra and not preds:
                    st = self.handler_state()
                else:
                    st = merge_states([self.out_state[p] for p in preds])
                    if st is None:
                        st = self.handler_state() if b in cfg.extra else State()
                        if b not in cfg.extra and preds == []:
                            st.sp = self.local_low if self.local_low is not None else 0
                for r in self.phis.get(b, ()):
                    st.regs[r] = Var('reg', r, 4)
                self.in_state[b] = st.copy()
                old = self.out_state.get(b)
                self.lift_block(blk, st)
                new = self.out_state[b]
                if old is None or not self.state_eq(old, new):
                    changed = True

    def add_phis(self, order):
        \"\"\"Registers live into a join block with differing incoming values become register variables.\"\"\"
        added = False
        for b in order:
            blk = self.cfg.blocks[b]
            preds = [p for p in blk.preds if p in self.out_state]
            if len(preds) < 2:
                continue
            for r in self.live_in.get(b, ()):
                if r in self.phis.get(b, ()):
                    continue
                vals = [self.out_state[p].regs.get(r) for p in preds]
                known = [v for v in vals if v is not None and not isinstance(v, Unknown)]
                if not known:
                    continue
                if all(v is not None and same(v, vals[0]) for v in vals):
                    continue
                if any(isinstance(v, (Me, ImportFn)) for v in known):
                    continue
                self.phis.setdefault(b, set()).add(r)
                added = True
        return added

    def place_phi_assignments(self, order):
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
                    if isinstance(v, Var) and v.kind == 'reg' and v.off == r:
                        continue
                    s = Assign(Var('reg', r, 4), v)
                    s.addr = blk.start
                    s.phi = True
                    self.block_stmts[p].append(s)"""), (
"""    def make_term(self, blk):
        if blk.term == 'jcc':
            cond = self.cond_for(blk.cond_insn.mn)
            return ('cond', cond, blk.taken, blk.fall)""",
"""    def make_term(self, blk):
        if blk.term == 'jcc':
            cond = self.cond_for(blk.cond_insn.mn)
            if isinstance(cond, Const):
                return ('goto', blk.taken if cond.v else blk.fall)
            return ('cond', cond, blk.taken, blk.fall)"""), (
"""    def op_call(self, ins):
        o = ins.ops[0]""",
"""    def op_call(self, ins):
        before = (self.st.regs.get('ecx'), self.st.regs.get('edx'))
        self.dispatch_call(ins)
        # ecx/edx are caller-saved: forget them unless the handler produced a value there
        if self.st.regs.get('ecx') is before[0]:
            self.st.regs['ecx'] = Unknown('ecx')
        if self.st.regs.get('edx') is before[1]:
            self.st.regs['edx'] = Unknown('edx')

    def dispatch_call(self, ins):
        o = ins.ops[0]"""), (
"""    def proc_arg(self, a, sig, i):
        if isinstance(a, SAddr):
            # pointer to local: ByRef argument (keep the address form for retptr handling)
            return a
        return a""",
"""    def byref_arg(self, a):
        \"\"\"A pointer to a variable passed to a user procedure is a ByRef argument: show the variable.\"\"\"
        if isinstance(a, SAddr):
            return self.stack_var(a.o, 4)
        if isinstance(a, AddrOf):
            return a.e
        return a

    def proc_arg(self, a, sig, i):
        return a"""), (
"""        c = Call(name, args, kind='proc')
        c.proc = p""",
"""        args = [self.byref_arg(a) for a in args]
        c = Call(name, args, kind='proc')
        c.proc = p""")])

# ---- emit.py
patch('emit.py', [(
"""            elif t[0] == 'switch':
                terms[b] = ('switch', t[1], [x if x in blocks else None for x in t[2]])
        return blocks, terms""",
"""            elif t[0] == 'switch':
                terms[b] = ('switch', t[1], [x if x in blocks else None for x in t[2]])
        self.merge_chains(blocks, terms, L.cfg.entry, set(L.cfg.extra))
        return blocks, terms

    def merge_chains(self, blocks, terms, entry, roots):
        \"\"\"Merge b -> t when b ends in an unconditional jump and t has no other predecessor.\"\"\"
        changed = True
        while changed:
            changed = False
            preds = defaultdict(set)
            for b, t in terms.items():
                for x in succs_of(t):
                    preds[x].add(b)
            for b in list(blocks):
                t = terms[b]
                if t[0] != 'goto':
                    continue
                n = t[1]
                if n == b or n == entry or n in roots or n not in blocks or preds[n] != {b}:
                    continue
                blocks[b] = blocks[b] + blocks[n]
                terms[b] = terms[n]
                del blocks[n]
                del terms[n]
                changed = True
                break"""), (
"""        if v.kind == 'internal':
            return 'frame_%X' % v.off
        return None""",
"""        if v.kind == 'internal':
            return 'frame_%X' % v.off
        if v.kind == 'reg':
            return 'r' + v.off[1:].upper()
        return None"""), (
"""TYPE_SET = {""",
"""def succs_of(t):
    if t[0] == 'goto':
        return [t[1]]
    if t[0] == 'cond':
        return [t[2], t[3]]
    if t[0] == 'switch':
        return [x for x in t[2] if x is not None]
    return []


TYPE_SET = {"""), (
"""            for e in term_exprs(terms[b]):
                for x in e.walk():
                    if isinstance(x, (Var, Temp)) and (not isinstance(x, Var) or x.kind == 'local'):
                        used[ekey(x)] = x""",
"""            for e in term_exprs(terms[b]):
                for x in e.walk():
                    if isinstance(x, (Var, Temp)) and (not isinstance(x, Var) or x.kind in ('local', 'reg')):
                        used[ekey(x)] = x
        for b, stmts in blocks.items():
            for s in stmts:
                for e in list(iter_exprs_in(s)) + ([s.dst] if isinstance(s, Assign) else []):
                    for x in e.walk():
                        if isinstance(x, Var) and x.kind == 'reg':
                            used[ekey(x)] = x""")])
print('patched')
