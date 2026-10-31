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
"""                else:
                    st = merge_states([self.out_state[p] for p in preds], preds)""",
"""                else:
                    if len(preds) >= 2:
                        # registers live here with disagreeing known incoming values become variables now
                        # (deciding inside the iteration keeps it monotonic)
                        for r in self.live_in.get(b, ()):
                            if r in self.phis.get(b, ()):
                                continue
                            known = [self.out_state[p].regs.get(r) for p in preds]
                            known = [v for v in known if v is not None and not isinstance(v, Unknown)]
                            if len(known) >= 2 and not all(same(v, known[0]) for v in known) and \\
                                    not any(isinstance(v, (Me, ImportFn)) for v in known):
                                self.phis.setdefault(b, set()).add(r)
                    st = merge_states([self.out_state[p] for p in preds], preds)""")])
print('patched')
