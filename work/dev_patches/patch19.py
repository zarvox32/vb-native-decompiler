import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('emit.py', [(
"""        self.resolve_phicells(blocks, terms)
        # a branch whose two arms are empty""",
"""        self.resolve_phicells(blocks, terms)
        drop_unread_regvars(blocks, terms)
        # a branch whose two arms are empty"""), (
"""        NAMER['fn'] = self.name_var
        if os.environ.get('VBDEC_DUMP')""",
"""        NAMER['fn'] = self.name_var
        bypass_empty_blocks(blocks, terms, L.cfg.entry, set(L.cfg.extra))
        if os.environ.get('VBDEC_DUMP')"""), (
"""def succs_of(t):""",
"""def drop_unread_regvars(blocks, terms):
    read = set()
    for b, stmts in blocks.items():
        for s in stmts:
            for e in iter_exprs_in(s):
                for x in e.walk():
                    if isinstance(x, Var) and x.kind == 'reg':
                        read.add(ekey(x))
        for e in term_exprs(terms[b]):
            for x in e.walk():
                if isinstance(x, Var) and x.kind == 'reg':
                    read.add(ekey(x))
    for b in blocks:
        blocks[b] = [s for s in blocks[b] if not (isinstance(s, Assign) and isinstance(s.dst, Var) and
                                                   s.dst.kind == 'reg' and ekey(s.dst) not in read)]


def bypass_empty_blocks(blocks, terms, entry, roots):
    \"\"\"Redirect edges around blocks that contain nothing and just jump on.\"\"\"
    fwd = {}
    for b, t in terms.items():
        if b != entry and b not in roots and not blocks.get(b) and t[0] == 'goto' and t[1] != b:
            fwd[b] = t[1]

    def final(x):
        seen = set()
        while x in fwd and x not in seen:
            seen.add(x)
            x = fwd[x]
        return x
    if not fwd:
        return
    for b, t in list(terms.items()):
        if t[0] == 'goto':
            terms[b] = ('goto', final(t[1]))
        elif t[0] == 'cond':
            a, c = final(t[2]), final(t[3])
            terms[b] = ('goto', a) if a == c else ('cond', t[1], a, c)
        elif t[0] == 'switch':
            terms[b] = ('switch', t[1], [final(x) if x is not None else None for x in t[2]])
    live = set()
    work = [entry] + [r for r in roots if r in blocks]
    while work:
        v = work.pop()
        if v in live or v not in blocks:
            continue
        live.add(v)
        work.extend(succs_of(terms[v]))
    for b in list(blocks):
        if b not in live:
            del blocks[b]
            del terms[b]


def succs_of(t):""")])
print('patched')
