import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('cfg.py', [(
"""        n = None
        # look back in this block for cmp idx, imm
        for ins in reversed(b.insns[:-1]):
            if ins.mn == 'cmp' and ins.ops[1].kind == 'imm':
                n = ins.ops[1].imm + 1
                break
        if n is None:
            # find the bounding cmp in predecessors (common: cmp/ja in previous block)
            for pb in self.blocks.values() if self.blocks else []:
                pass""",
"""        n = None
        # look back in this block for cmp idx, imm
        for ins in reversed(b.insns[:-1]):
            if ins.mn == 'cmp' and ins.ops[1].kind == 'imm':
                n = ins.ops[1].imm + 1
                break
        if n is None:
            n = self._bound_before(b.insns[-1].addr if b.insns else b.start)""")])
patch('cfg.py', [(
"""    def _byte_table(self, b, reg, table2):""",
"""    def _bound_before(self, at):
        \"\"\"`cmp reg, N ; ja default` immediately before address `at` -> N + 1 (table size).\"\"\"
        for jlen in (2, 6):
            j = self.dec.at(at - jlen)
            if j is None or j.next != at or j.mn not in ('ja', 'jae', 'jg', 'jge'):
                continue
            for clen in (3, 5, 6, 2, 4):
                c = self.dec.at(j.addr - clen)
                if c is not None and c.next == j.addr and c.mn == 'cmp' and len(c.ops) == 2 and \\
                        c.ops[1].kind == 'imm':
                    return c.ops[1].imm + (1 if j.mn in ('ja', 'jg') else 0)
        return None

    def _byte_table(self, b, reg, table2):""")])
print('patched')
