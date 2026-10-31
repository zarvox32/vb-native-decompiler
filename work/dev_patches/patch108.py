import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# A short merged tail that uses a register loaded with a different form variable on each path
# (mov ecx, [esi+1C8h] / mov ecx, [esi+1F0h] ... call [ecx+ebx*4]->Play) is lifted per path, so the
# array element can be named (m_1C8(rBX).Play instead of Mem32(rBX * 4 + rCX).Play).
patch('x86sem.py', [(
"""            addr_regs = {r for r in self.phis.get(b, ())
                         if any(isinstance(self.out_state[p].regs.get(r), (SAddr, AddrOf)) for p in preds)}""",
"""            addr_regs = {r for r in self.phis.get(b, ())
                         if any(isinstance(self.out_state[p].regs.get(r), (SAddr, AddrOf)) for p in preds)}
            if len(cfg.blocks[b].insns) <= 12:
                for r in self.phis.get(b, ()):
                    vals = [self.out_state[p].regs.get(r) for p in preds]
                    if all(isinstance(v, Var) and v.kind in ('field', 'global') for v in vals) and \\
                            not all(same(v, vals[0]) for v in vals):
                        addr_regs.add(r)""")])
print('patched')
