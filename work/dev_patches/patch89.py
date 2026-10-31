import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# a merged call tail whose paths load different *addresses* into a register (lea ecx, [esi+168h] on one
# path, a constant temporary on another) is also lifted once per path: a register variable holding
# "the address of m_168 or of a temporary" has no VB equivalent
patch('x86sem.py', [(
"""            merged = merge_states([self.out_state[p] for p in preds], preds)
            if merged is None or not any(isinstance(v[1], PhiCell) and self.loc_kind(k) == 'cell'
                                         for k, v in merged.mem.items()):
                continue
            blk = cfg.blocks[b]""",
"""            addr_regs = {r for r in self.phis.get(b, ())
                         if any(isinstance(self.out_state[p].regs.get(r), (SAddr, AddrOf)) for p in preds)}
            merged = merge_states([self.out_state[p] for p in preds], preds)
            if merged is None or not addr_regs and not any(
                    isinstance(v[1], PhiCell) and self.loc_kind(k) == 'cell' for k, v in merged.mem.items()):
                continue
            blk = cfg.blocks[b]"""), (
"""                pst = self.out_state[p].copy()
                for r in self.phis.get(b, ()):
                    pst.regs[r] = Var('reg', '%s@%x' % (r, b), 4)""",
"""                pst = self.out_state[p].copy()
                for r in self.phis.get(b, ()):
                    if r not in addr_regs:
                        pst.regs[r] = Var('reg', '%s@%x' % (r, b), 4)""")])
print('patched')
