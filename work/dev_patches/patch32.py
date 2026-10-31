import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# deterministic temp names: (instruction address, index within that instruction)
patch('lift.py', [(
"""    def new_temp(self, ty=None):
        self.temp_n += 1
        return Temp('t%d' % self.temp_n, ty)""",
"""    def new_temp(self, ty=None):
        addr = self.cur.addr if self.cur is not None else 0
        n = self._tcount.get(addr, 0)
        self._tcount[addr] = n + 1
        return Temp('t%x_%d' % (addr, n), ty)"""), (
"""        self.materialized = {}
        self.saved_cells = set()""",
"""        self.materialized = {}
        self._tcount = {}
        self.saved_cells = set()""")])
patch('x86sem.py', [(
"""    def lift_block(self, blk, st):
        self.st = st
        self.stmts = []""",
"""    def lift_block(self, blk, st):
        self.st = st
        self.stmts = []
        for ins in blk.insns:
            self._tcount.pop(ins.addr, None)"""), (
"""                    if isinstance(v, Var) and v.kind == 'reg' and v.off == name:
                        continue
                    s = Assign(Var('reg', name, 4), v)""",
"""                    if isinstance(v, Var) and v.kind == 'reg' and v.off == name:
                        continue
                    s = Assign(Var('reg', name, 4), v)
                    s.phi = True"""), (
"""    def fsrc(self, op, int_=False):
        if op.kind == 'reg' and op.reg.startswith('st'):
            return self.fget(int(op.reg[3]) if '(' in op.reg else 0)
        if op.kind == 'mem':
            ty = None
            if not int_:
                ty = 'Single' if op.size == 4 else 'Double'
            v = self.load(op, op.size, ty)""",
"""    def fsrc(self, op, int_=False):
        if op.kind == 'reg' and op.reg.startswith('st'):
            return self.fget(int(op.reg[3]) if '(' in op.reg else 0)
        if op.kind == 'mem':
            ty = None
            if not int_:
                ty = 'Single' if op.size == 4 else 'Double'
            v = self.load(op, op.size, ty)
            if type(v).__name__ == 'VTbl':
                v = Deref(v.obj, 0, op.size, ty)       # a number through a pointer, not a vtable""")])
# fst/fstp to memory through a pointer: same VTbl confusion on stores is handled by store paths

# renumber temps for output
patch('emit.py', [(
"""        NAMER['fn'] = self.name_var
        bypass_empty_blocks(blocks, terms, L.cfg.entry, set(L.cfg.extra))""",
"""        NAMER['fn'] = self.name_var
        bypass_empty_blocks(blocks, terms, L.cfg.entry, set(L.cfg.extra))
        renumber_temps(blocks, terms)"""), (
"""def drop_unread_regvars(blocks, terms):""",
"""def renumber_temps(blocks, terms):
    names = {}

    def fn(x):
        if isinstance(x, Temp):
            n = names.get(x.name)
            if n is None:
                n = 't%d' % (len(names) + 1)
                names[x.name] = n
            return Temp(n, x.ty)
        return None
    for b in sorted(blocks):
        for s in blocks[b]:
            if isinstance(s, Assign) and isinstance(s.dst, Temp):
                s.dst = fn(s.dst)
            stmt_rewrite(s, fn)
        terms[b] = term_rewrite(terms[b], fn)


def drop_unread_regvars(blocks, terms):""")])
print('patched')
