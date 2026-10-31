import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('simplify.py', [(
"""def count_uses(blocks, terms):
    uses = Counter()
    for b, stmts in blocks.items():
        for s in stmts:
            for e in iter_exprs_in(s):
                for x in e.walk():
                    if isinstance(x, (Temp, Var)):
                        uses[ekey(x)] += 1
                    elif isinstance(x, AddrOf) and isinstance(x.e, Var):
                        uses[ekey(x.e)] += 1
                    elif isinstance(x, SAddr):
                        uses[('sa', x.o)] += 1
        for e in term_exprs(terms[b]):
            for x in e.walk():
                if isinstance(x, (Temp, Var)):
                    uses[ekey(x)] += 1
    return uses""",
"""def tree_counts(e, memo):
    \"\"\"Counter of Temp/Var keys by occurrence in the expression *tree* (shared nodes counted each time).\"\"\"
    r = memo.get(id(e))
    if r is not None:
        return r
    c = Counter()
    if isinstance(e, (Temp, Var)):
        c[ekey(e)] += 1
    elif isinstance(e, AddrOf) and isinstance(e.e, Var):
        c[ekey(e.e)] += 1
    elif isinstance(e, SAddr):
        c[('sa', e.o)] += 1
    for ch in e.children():
        if ch is not None and isinstance(ch, Expr) and not (isinstance(e, AddrOf) and isinstance(ch, Var)):
            c.update(tree_counts(ch, memo))
    memo[id(e)] = c
    return c


def count_uses(blocks, terms):
    uses = Counter()
    memo = {}
    for b, stmts in blocks.items():
        for s in stmts:
            for e in iter_exprs_in(s):
                uses.update(tree_counts(e, memo))
        for e in term_exprs(terms[b]):
            uses.update(tree_counts(e, memo))
    return uses"""), (
"""def substitute(e, key, val):
    found = [False]

    def fn(x):
        if isinstance(x, (Temp, Var)) and ekey(x) == key and not found[0]:
            found[0] = True
            return val
        return None
    r = rewrite(e, fn)
    return r, found[0]""",
"""def substitute(e, key, val):
    found = [False]

    def fn(x):
        if isinstance(x, (Temp, Var)) and ekey(x) == key:
            found[0] = True
            return val
        return None
    r = rewrite(e, fn)
    return r, found[0]"""), (
"""def count_reads_in(s, k):
    n = 0
    for e in iter_exprs_in(s):
        for x in e.walk():
            if isinstance(x, (Temp, Var)) and ekey(x) == k:
                n += 1
    return n""",
"""def count_reads_in(s, k):
    memo = {}
    return sum(tree_counts(e, memo).get(k, 0) for e in iter_exprs_in(s))""")])

# the terminator inlining check also needs tree counts
patch('simplify.py', [(
"""                    if k not in live_out[b] and sum(1 for e in term_exprs(t) for x in e.walk()
                                                    if isinstance(x, Var) and ekey(x) == k) == 1:""",
"""                    if k not in live_out[b] and sum(tree_counts(e, {}).get(k, 0) for e in term_exprs(t)) == 1:""")])
print('patched')
