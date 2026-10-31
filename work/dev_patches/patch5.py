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
"""        self.merge_chains(blocks, terms, L.cfg.entry, set(L.cfg.extra))
        return blocks, terms""",
"""        # drop blocks that became unreachable once error-check branches were resolved
        live = set()
        work = [L.cfg.entry] + [h for h in L.cfg.extra if h in blocks]
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
        self.merge_chains(blocks, terms, L.cfg.entry, set(L.cfg.extra))
        return blocks, terms""")])
print('patched')
