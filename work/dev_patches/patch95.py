import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# values still pending in registers / on the FPU stack that read a reused Me or parameter slot must be
# captured before the slot is overwritten (fild [esp+2Ch] ... mov [esp+2Ch], ebx ... fmul)
patch('lift.py', [(
"""                self.ctx.me_slot_written.add(self.proc.va)
                slot = Var('local', 0x7ff0, size, name='var_Scratch')
                s = self.emit(Assign(slot, self.fit(val, size), kind=kind))""",
"""                self.ctx.me_slot_written.add(self.proc.va)
                slot = Var('local', 0x7ff0, size, name='var_Scratch')
                self.materialize_refs(slot)
                val = self.materialized.get(id(val), val)
                s = self.emit(Assign(slot, self.fit(val, size), kind=kind))"""), (
"""                self.ctx.arg_slot_written.add((self.proc.va, o))
                slot = Var('local', 0x10000 + v.off, size)
                s = self.emit(Assign(slot, self.fit(val, size), kind=kind))""",
"""                self.ctx.arg_slot_written.add((self.proc.va, o))
                slot = Var('local', 0x10000 + v.off, size)
                self.materialize_refs(slot)
                val = self.materialized.get(id(val), val)
                s = self.emit(Assign(slot, self.fit(val, size), kind=kind))""")])
print('patched')
