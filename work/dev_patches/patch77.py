import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# A store into a parameter's stack slot starts a separate variable (the compiler reuses the slots of
# ByRef parameters as scratch).  Reads before the store still see the parameter; emit.params_rewrite
# maps the variable back onto the parameter when it is ByVal.
patch('lift.py', [(
"""            if isinstance(val, Me) and k == 'arg':
                return
            self.materialize_refs(v)""",
"""            if isinstance(val, Me) and k == 'arg':
                return
            if k == 'arg' and isinstance(v, Var):
                self.ctx.arg_slot_written.add((self.proc.va, o))
                slot = Var('local', 0x10000 + v.off, size)
                self.materialize_refs(v)
                val = self.materialized.get(id(val), val)
                s = self.emit(Assign(slot, self.fit(val, size), kind=kind))
                s.local_store = True
                self.mem_store(o, size, val, s)
                return
            self.materialize_refs(v)"""), (
"""            if o == 4 and self.is_method and self.proc.va in self.ctx.me_slot_written:
                return Var('local', 0x7ff0, size, name='var_Scratch')""",
"""            if o == 4 and self.is_method and self.proc.va in self.ctx.me_slot_written:
                return Var('local', 0x7ff0, size, name='var_Scratch')
            if (self.proc.va, o) in self.ctx.arg_slot_written and self.loc_kind(o) == 'arg':
                e = self.st.mem.get(o)
                if e is None or not (isinstance(e[1], Var) and e[1].kind == 'arg'):
                    return Var('local', 0x10000 + o - 4, size)""")])

patch('x86sem.py', [(
"""            init.mem[4] = (4, Me(), None)        # Me's slot holds Me until the code reuses it""",
"""            init.mem[4] = (4, Me(), None)        # Me's slot holds Me until the code reuses it
        for va, o in self.ctx.arg_slot_written:
            if va == self.proc.va and not (o == 4 and self.is_method):
                init.mem[o] = (4, self.stack_var(o, 4), None)   # the parameter until overwritten""")])

patch('decompiler.py', [(
"""        self.me_slot_written = set()  # procedures that reuse Me's stack slot as scratch""",
"""        self.me_slot_written = set()  # procedures that reuse Me's stack slot as scratch
        self.arg_slot_written = set() # (procedure, stack offset) of parameter slots that are stored to""")])

patch('emit.py', [(
"""            if resvar is not None and isinstance(x, Var) and ekey(x) == resvar:
                return result
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)""",
"""            if resvar is not None and isinstance(x, Var) and ekey(x) == resvar:
                return result
            if isinstance(x, Var) and x.kind == 'local' and 0x10000 <= x.off < 0x10100 and \\
                    not byref.get(x.off - 0x10000) and not (retptr and x.off - 0x10000 == last_off):
                return Var('param', x.off - 0x10000, x.size)      # a ByVal parameter assigned by the code
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)""")])
print('patched')
