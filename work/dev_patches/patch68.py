import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


ME_SLOT = "Var('local', 0x7ff0, size, name='var_MeSlot')"

patch('lift.py', [(
"""            v = self.stack_var(o, size)
            if isinstance(v, Me):
                # the compiler reuses Me's stack slot as scratch space: track the value, emit nothing
                self.mem_store(o, size, val, None)
                return""",
"""            v = self.stack_var(o, size)
            if isinstance(v, Me):
                # the compiler reuses Me's stack slot as scratch space (e.g. a loop counter):
                # from then on it is an ordinary local variable
                self.ctx.me_slot_written.add(self.proc.va)
                slot = Var('local', 0x7ff0, size, name='var_MeSlot')
                s = self.emit(Assign(slot, self.fit(val, size), kind=kind))
                s.local_store = True
                self.mem_store(o, size, val, s)
                return"""), (
"""            f = self.mem_load(o, size)
            if f is not None:
                return f
            v = self.stack_var(o, size, ty)""",
"""            f = self.mem_load(o, size)
            if f is not None:
                return f
            if o == 4 and self.is_method and self.proc.va in self.ctx.me_slot_written:
                return Var('local', 0x7ff0, size, name='var_MeSlot')
            v = self.stack_var(o, size, ty)""")])

patch('x86sem.py', [(
"""        self.phis = {}            # block -> registers merged into a register variable
        init = State()""",
"""        self.phis = {}            # block -> registers merged into a register variable
        init = State()
        if self.is_method and self.proc.va in self.ctx.me_slot_written:
            init.mem[4] = (4, Me(), None)        # Me's slot holds Me until the code reuses it""")])

patch('decompiler.py', [(
"""        self.form_iid = {}            # guid -> VBObject""",
"""        self.form_iid = {}            # guid -> VBObject
        self.me_slot_written = set()  # procedures that reuse Me's stack slot as scratch""")])

# addresses do not depend on the value of the variable whose address is taken
patch('simplify.py', [(
"""def vars_read(e):
    out = set()
    for x in e.walk():
        if isinstance(x, (Var, Temp)):
            out.add(ekey(x))
    return out""",
"""def vars_read(e):
    out = set()
    stack = [e]
    seen = set()
    while stack:
        x = stack.pop()
        if id(x) in seen or isinstance(x, AddrOf):
            continue
        seen.add(id(x))
        if isinstance(x, (Var, Temp)):
            out.add(ekey(x))
        stack.extend(c for c in x.children() if isinstance(c, Expr))
    return out""")])
print('patched')
