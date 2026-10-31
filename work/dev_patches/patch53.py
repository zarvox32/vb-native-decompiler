import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


code = '''

# --------------------------------------------------------------------------- condition chains
def merge_condition_chains(blocks, terms, entry, roots):
    """Branch chains that test several conditions for one target -> a single And/Or condition."""
    from lift import negate as _neg
    changed = True
    while changed:
        changed = False
        preds = {}
        for b, t in terms.items():
            for s in term_succs(t):
                preds.setdefault(s, []).append(b)
        for n, t in list(terms.items()):
            if t[0] != 'cond' or n not in blocks:
                continue
            c1, a, m = t[1], t[2], t[3]
            for flip in (False, True):
                # n: if c1 goto x else goto y  (x/y swapped when flip)
                x, y = (a, m) if not flip else (m, a)
                c = c1 if not flip else _neg(c1)
                if y == n or y not in blocks or blocks[y] or y == entry or y in roots:
                    continue
                if preds.get(y, []) != [n]:
                    continue
                ty = terms[y]
                if ty[0] != 'cond':
                    continue
                c2, p, q = ty[1], ty[2], ty[3]
                if p == x:          # c or c2 -> x ; else q
                    terms[n] = ('cond', Bin('Or', c, c2, ty='Boolean'), x, q)
                elif q == x:        # c or not c2 -> x ; else p
                    terms[n] = ('cond', Bin('Or', c, _neg(c2), ty='Boolean'), x, p)
                else:
                    continue
                del blocks[y], terms[y]
                changed = True
                break
            if changed:
                break
'''
s = open('simplify.py', encoding='utf-8').read()
if 'def merge_condition_chains' not in s:
    s += code
    open('simplify.py', 'w', encoding='utf-8').write(s)

patch('emit.py', [(
"""        NAMER['fn'] = self.name_var
        bypass_empty_blocks(blocks, terms, L.cfg.entry, set(L.cfg.extra))""",
"""        NAMER['fn'] = self.name_var
        bypass_empty_blocks(blocks, terms, L.cfg.entry, set(L.cfg.extra))
        simplify.merge_condition_chains(blocks, terms, L.cfg.entry, set(L.cfg.extra))
        simplify.strip_condition_negs(terms)""")])

# duplicate small shared blocks instead of jumping to them
patch('structure.py', [(
"""            if n in self.done:
                self.need_label.add(n)
                out.append(('goto', n))
                break
            if n in self.loops and not (loops and loops[-1].header == n):""",
"""            if n in self.done:
                if self.small_dup_ok(n, loops) and dup < 6:
                    dup += 1
                    out.extend(('stmt', s) for s in self.blocks[n])
                    t = self.terms[n]
                    if t[0] == 'goto':
                        n = t[1]
                        continue
                    if t[0] == 'ret':
                        out.append(('return', n))
                    break
                self.need_label.add(n)
                out.append(('goto', n))
                break
            if n in self.loops and not (loops and loops[-1].header == n):"""), (
"""    def region(self, n, stops, loops):
        out = []
        while n is not None:""",
"""    def small_dup_ok(self, n, loops):
        \"\"\"A short straight-line block may be repeated instead of jumped to (shared Case tails).\"\"\"
        if n in self.loops or n in self.roots:
            return False
        if loops and any(n == L.header for L in loops):
            return False
        t = self.terms.get(n)
        if t is None or t[0] not in ('goto', 'ret'):
            return False
        return len(self.blocks.get(n, ())) <= 3

    def region(self, n, stops, loops):
        out = []
        dup = 0
        while n is not None:""")])
print('patched')
