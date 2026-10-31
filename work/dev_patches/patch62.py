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
"""        from lift import PhiCell
        cfg = self.cfg
        for b in order:
            st = self.in_state.get(b)
            if st is None or b == cfg.entry:
                continue
            if not any(isinstance(v[1], PhiCell) for v in st.mem.values()):
                continue
            preds = [p for p in cfg.blocks[b].preds if p in self.out_state]
            blk = cfg.blocks[b]
            copies = []
            for p in preds:""",
"""        from lift import PhiCell
        cfg = self.cfg
        for b in order:
            if b == cfg.entry or b in cfg.extra:
                continue
            preds = []
            for p in cfg.blocks[b].preds:
                preds.extend(self.dup_of.get(p, [p]))
            preds = [p for p in preds if p in self.out_state]
            if len(preds) < 2 or len(preds) > 12:
                continue
            merged = merge_states([self.out_state[p] for p in preds], preds)
            if merged is None or not any(isinstance(v[1], PhiCell) and self.loc_kind(k) == 'cell'
                                         for k, v in merged.mem.items()):
                continue
            blk = cfg.blocks[b]
            copies = []
            for p in preds:""")])
print('patched')
