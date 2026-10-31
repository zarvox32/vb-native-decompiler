import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# If ... Then x = Array(a, b, c) Else x = Array(d, e, c): the compiler shares the tail (the last
# element stores and the Array call); copy that tail back into each branch so both literals can be
# rebuilt.
patch('simplify.py', [(
"""def collapse_array_literals(blocks):
    \"\"\"ReDim t(n) / t(0) = a ... t(n) = z / x = Array(<t>) / Erase t  ->  x = Array(a, ..., z).\"\"\"
    for b, stmts in blocks.items():""",
"""def unshare_array_tails(blocks, terms):
    import copy
    for b, stmts in list(blocks.items()):
        for si, s in enumerate(stmts):
            calls = [x for e in iter_exprs_in(s) for x in e.walk()
                     if isinstance(x, Call) and x.fn == 'Array' and getattr(x, 'items_of', None) is not None]
            if len(calls) != 1:
                continue
            k = ekey(calls[0].items_of)
            if any(isinstance(t, FmtStmt) and t.fmt.startswith('ReDim {0}(') and ekey(t.args[0]) == k
                   for t in stmts[:si]):
                continue
            preds = [p for p, t in terms.items() if p in blocks and term_succs(t) == [b]]
            npreds = sum(1 for p, t in terms.items() if p in blocks and b in term_succs(t))
            if len(preds) < 2 or len(preds) != npreds:
                continue
            if not all(any(isinstance(t, FmtStmt) and t.fmt.startswith('ReDim {0}(') and ekey(t.args[0]) == k
                           for t in blocks[p]) for p in preds):
                continue
            head = stmts[:si + 1]
            for p in preds:
                blocks[p] = blocks[p] + [copy.deepcopy(t) for t in head]
            blocks[b] = stmts[si + 1:]
            return unshare_array_tails(blocks, terms)


def collapse_array_literals(blocks, terms=None):
    \"\"\"ReDim t(n) / t(0) = a ... t(n) = z / x = Array(<t>) / Erase t  ->  x = Array(a, ..., z).\"\"\"
    if terms is not None:
        unshare_array_tails(blocks, terms)
    for b, stmts in blocks.items():"""), (
"""            call.args = [items[i] for i in range(n + 1)]
            call.items_of = None""",
"""            call.args = [items[i] for i in range(n + 1)]
            call.items_of = None
            call.__dict__.pop('_k', None)""")])

patch('emit.py', [(
"""        simplify.collapse_array_literals(blocks)""",
"""        simplify.collapse_array_literals(blocks, terms)""")])
print('patched')
