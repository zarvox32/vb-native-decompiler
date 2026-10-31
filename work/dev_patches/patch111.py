import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


NEW = '''

def capture_with_indices(blocks, terms, new_var):
    """`p = m_3C4 + i * 12` (a With block's element pointer) whose index variable i changes while p is
    still in use cannot be inlined into the uses; keep the index in its own variable instead:
    `idx = i` / `p = m_3C4 + idx * 12`, after which propagate_addresses can name the element."""
    defs = {}
    for b, stmts in blocks.items():
        for i, s in enumerate(stmts):
            if isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)):
                defs.setdefault(ekey(s.dst), []).append((b, i, s))
    uses = count_uses(blocks, terms)
    changed = False
    for k, lst in list(defs.items()):
        if len(lst) != 1:
            continue
        b, i, s = lst[0]
        if not (isinstance(s.dst, Var) and s.dst.kind in ('local', 'reg')) or not uses.get(k):
            continue
        if not is_address_expr(s.src) or isinstance(s.src, AddrOf):
            continue
        lin = linear(s.src)
        idxs = [t for t, c in lin[0].values() if c != 1 and isinstance(t, (Var, Temp))]
        if len(idxs) != 1 or ekey(idxs[0]) not in defs:
            continue
        v = idxs[0]
        w = new_var()
        s.src = substitute(s.src, ekey(v), w)
        blocks[b].insert(blocks[b].index(s), Assign(w, v))
        changed = True
    return changed
'''
s = open('simplify.py', encoding='utf-8').read()
if 'def capture_with_indices' not in s:
    s = s.rstrip('\\n') + '\\n' + NEW
    open('simplify.py', 'w', encoding='utf-8').write(s)

patch('emit.py', [(
"""        simplify.propagate_constant_regvars(blocks, terms)
        simplify.propagate_addresses(blocks, terms, L.cfg.entry)""",
"""        simplify.propagate_constant_regvars(blocks, terms)
        simplify.propagate_addresses(blocks, terms, L.cfg.entry)
        if simplify.capture_with_indices(blocks, terms, self.new_index_var):
            simplify.propagate_addresses(blocks, terms, L.cfg.entry)""")])
print('patched')
