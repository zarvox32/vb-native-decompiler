import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('lift.py', [(
# storing into a parameter slot does not change the parameter: nothing to materialize
"""                self.ctx.arg_slot_written.add((self.proc.va, o))
                slot = Var('local', 0x10000 + v.off, size)
                self.materialize_refs(v)
                val = self.materialized.get(id(val), val)
                s = self.emit""",
"""                self.ctx.arg_slot_written.add((self.proc.va, o))
                slot = Var('local', 0x10000 + v.off, size)
                s = self.emit"""), (
# the address of a reused parameter slot designates the scratch variable
"""    def var_loc(self, v):""",
"""    def slot_var(self, o, size, ty=None):
        \"\"\"Variable designated by the stack address o (a parameter slot reused as scratch -> its variable).\"\"\"
        if (self.proc.va, o) in self.ctx.arg_slot_written and self.loc_kind(o) == 'arg' and \\
                not (o == 4 and self.is_method):
            return Var('local', 0x10000 + o - 4, size, ty)
        return self.stack_var(o, size, ty)

    def var_loc(self, v):"""), (
"""        if isinstance(p, SAddr):
            v = self.stack_var(p.o, size, ty)
            if isinstance(v, Var):
                if ty:
                    self.note_type(v, ty)""",
"""        if isinstance(p, SAddr):
            v = self.slot_var(p.o, size, ty)
            if isinstance(v, Var):
                if ty:
                    self.note_type(v, ty)"""), (
"""            dst = self.stack_var(p.o, size, ty)
            if ty:
                self.note_type(dst, ty)
            self.materialize_refs(dst)""",
"""            dst = self.slot_var(p.o, size, ty)
            if ty:
                self.note_type(dst, ty)
            self.materialize_refs(dst)""")])

patch('x86sem.py', [(
"""        if isinstance(a, SAddr):
            return self.stack_var(a.o, 4)
        if isinstance(a, AddrOf):
            return a.e
        return a

    def proc_arg(self, a, sig, i):""",
"""        if isinstance(a, SAddr):
            return self.slot_var(a.o, 4)
        if isinstance(a, AddrOf):
            return a.e
        return a

    def proc_arg(self, a, sig, i):""")])
print('patched')
