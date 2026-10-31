import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('lift.py', [(
"""def merge_states(states):
    \"\"\"Meet of predecessor exit states (keep only agreeing values).\"\"\"
    states = [s for s in states if s is not None]
    if not states:
        return None""",
"""class PhiCell(Expr):
    \"\"\"A stack cell whose value depends on the predecessor block (VB tail-merged two statements).\"\"\"

    def __init__(self, pairs):
        self.pairs = pairs          # [(pred block, value)]

    def children(self):
        return tuple(v for p, v in self.pairs)

    def key(self):
        return ('phicell', tuple((p, ekey(v)) for p, v in self.pairs))


def merge_states(states, preds=None):
    \"\"\"Meet of predecessor exit states (keep only agreeing values).\"\"\"
    if preds is not None:
        pairs = [(p, s) for p, s in zip(preds, states) if s is not None]
        preds = [p for p, s in pairs]
        states = [s for p, s in pairs]
    states = [s for s in states if s is not None]
    if not states:
        return None
    all_states = list(states)"""), (
"""        for k in list(m.mem):
            if k not in s.mem or s.mem[k][0] != m.mem[k][0] or not same(s.mem[k][1], m.mem[k][1]):
                del m.mem[k]
    for k in list(m.mem):
        m.mem[k] = (m.mem[k][0], m.mem[k][1], None)
    return m""",
"""        for k in list(m.mem):
            if k not in s.mem or s.mem[k][0] != m.mem[k][0] or not same(s.mem[k][1], m.mem[k][1]):
                del m.mem[k]
    for k in list(m.mem):
        m.mem[k] = (m.mem[k][0], m.mem[k][1], None)
    # pending argument cells that differ per predecessor (only below every frame: k < sp of all)
    if preds is not None and len(all_states) > 1 and all(s.sp == all_states[0].sp for s in all_states):
        sp = all_states[0].sp
        keys = set(all_states[0].mem)
        for s in all_states[1:]:
            keys &= set(s.mem)
        for k in keys:
            if k in m.mem or k < sp:
                continue
            ents = [s.mem[k] for s in all_states]
            if all(e[0] == ents[0][0] for e in ents):
                m.mem[k] = (ents[0][0], PhiCell(list(zip(preds, [e[1] for e in ents]))), None)
    return m""")])

patch('x86sem.py', [(
"""                else:
                    st = merge_states([self.out_state[p] for p in preds])
                    if st is None:""",
"""                else:
                    st = merge_states([self.out_state[p] for p in preds], preds)
                    if st is not None:
                        # only outgoing-argument cells may become per-predecessor values
                        for k in list(st.mem):
                            v = st.mem[k][1]
                            if type(v).__name__ == 'PhiCell' and self.loc_kind(k) != 'cell':
                                del st.mem[k]
                    if st is None:""")])

# resolve PhiCell -> IIf(cond, a, b) in emit.prepare (before merging chains)
patch('emit.py', [(
"""        # drop blocks that became unreachable once error-check branches were resolved""",
"""        self.resolve_phicells(blocks, terms)
        # drop blocks that became unreachable once error-check branches were resolved"""), (
"""    def merge_chains(self, blocks, terms, entry, roots):""",
"""    def resolve_phicells(self, blocks, terms):
        from lift import PhiCell
        succ = {b: succs_of(t) for b, t in terms.items()}

        def reach(start, avoid):
            seen = set()
            st = [start]
            while st:
                v = st.pop()
                if v in seen or v == avoid or v not in succ:
                    continue
                seen.add(v)
                st.extend(succ[v])
            return seen

        def side_of(c, p, merge):
            t = terms[c]
            if p == c:
                return t[2] == merge
            rt, rf = reach(t[2], c), reach(t[3], c)
            if p in rt and p not in rf:
                return True
            if p in rf and p not in rt:
                return False
            return None

        def resolve(x, merge):
            preds = [p for p, v in x.pairs]
            for c, t in terms.items():
                if t[0] != 'cond':
                    continue
                sides = [side_of(c, p, merge) for p in preds]
                if None in sides or len(set(sides)) != 2:
                    continue
                tv = [v for (p, v), s in zip(x.pairs, sides) if s]
                fv = [v for (p, v), s in zip(x.pairs, sides) if not s]
                if all(same(v, tv[0]) for v in tv) and all(same(v, fv[0]) for v in fv):
                    return Call('IIf', [t[1], tv[0], fv[0]], pure=True, ty=getattr(tv[0], 'ty', None))
            return Raw('[one of: %s]' % ' | '.join(render(v) for p, v in x.pairs))

        for b, stmts in blocks.items():
            def fn(x, b=b):
                if isinstance(x, PhiCell):
                    return resolve(x, b)
                return None
            for s in stmts:
                stmt_rewrite(s, fn)
            terms[b] = term_rewrite(terms[b], fn)

    def merge_chains(self, blocks, terms, entry, roots):""")])
print('patched')
