import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# interface pointers: IFoo* is an object by value, IFoo** is ByRef
patch('x86sem.py', [(
"""        if t.endswith('*'):
            base = t[:-1]
            if isinstance(v, (SAddr, AddrOf, DataPtr)):""",
"""        if t.endswith('*') and self.is_iface_type(t[:-1]) and not t.endswith('**'):
            return v
        if t.endswith('*'):
            base = t[:-1]
            if isinstance(v, (SAddr, AddrOf, DataPtr)):"""), (
"""    def self_method_call(self, obj, off, info):""",
"""    def is_iface_type(self, t):
        if t in ('IUnknown', 'IDispatch', 'Object'):
            return True
        it = self.tlb.by_name.get(t)
        return it is not None and it.kind in ('interface', 'dispatch', 'coclass')

    def self_method_call(self, obj, off, info):""")])

# liveness-based cleanup for locals
simp_add = '''

# --------------------------------------------------------------------------- local-variable liveness
def is_local_var(x):
    return isinstance(x, Var) and x.kind in ('local', 'reg')


def stmt_reads(s):
    """Keys of local/register variables read by a statement (address-taking counts as a read)."""
    r = set()
    for e in iter_exprs_in(s):
        for x in e.walk():
            if is_local_var(x):
                r.add(ekey(x))
            elif isinstance(x, AddrOf) and is_local_var(x.e):
                r.add(ekey(x.e))
    if isinstance(s, Assign):
        d = s.dst
        while isinstance(d, Member):
            d = d.base
            if is_local_var(d):
                r.add(ekey(d))       # partial write keeps earlier stores alive
    return r


def stmt_kills(s):
    if isinstance(s, Assign) and is_local_var(s.dst):
        return {ekey(s.dst)}
    return set()


def term_reads(t):
    r = set()
    for e in term_exprs(t):
        for x in e.walk():
            if is_local_var(x):
                r.add(ekey(x))
    return r


def term_succs(t):
    if t[0] == 'goto':
        return [t[1]]
    if t[0] == 'cond':
        return [t[2], t[3]]
    if t[0] == 'switch':
        return [x for x in t[2] if x is not None]
    return []


def block_liveness(blocks, terms):
    use, kill = {}, {}
    for b, stmts in blocks.items():
        u, k = set(), set()
        for s in stmts:
            u |= stmt_reads(s) - k
            k |= stmt_kills(s)
        u |= term_reads(terms[b]) - k
        use[b], kill[b] = u, k
    live_in = {b: set() for b in blocks}
    changed = True
    while changed:
        changed = False
        for b in blocks:
            out = set()
            for s in term_succs(terms[b]):
                out |= live_in.get(s, set())
            new = use[b] | (out - kill[b])
            if new != live_in[b]:
                live_in[b] = new
                changed = True
    live_out = {}
    for b in blocks:
        out = set()
        for s in term_succs(terms[b]):
            out |= live_in.get(s, set())
        live_out[b] = out
    return live_out


def live_after_list(stmts, live_out, term):
    """live set after each statement of a block."""
    live = set(live_out) | term_reads(term)
    res = [None] * len(stmts)
    for i in range(len(stmts) - 1, -1, -1):
        res[i] = set(live)
        s = stmts[i]
        live = (live - stmt_kills(s)) | stmt_reads(s)
    return res


def remove_dead_stores_liveness(blocks, terms):
    """Stores to locals whose value is never read are removed (per definition)."""
    changed = True
    while changed:
        changed = False
        live_out = block_liveness(blocks, terms)
        for b, stmts in blocks.items():
            la = live_after_list(stmts, live_out[b], terms[b])
            out = []
            for s, live in zip(stmts, la):
                if isinstance(s, Assign) and is_local_var(s.dst) and ekey(s.dst) not in live and \\
                        not getattr(s, 'keep', False):
                    if s.src is not None and s.src.has_side_effects():
                        out.append(ExprStmt(s.src, s.addr))
                    changed = True
                    continue
                out.append(s)
            blocks[b] = out


def count_reads_in(s, k):
    n = 0
    for e in iter_exprs_in(s):
        for x in e.walk():
            if isinstance(x, (Temp, Var)) and ekey(x) == k:
                n += 1
    return n


def inline_locals(blocks, terms):
    """`x = e` immediately followed by a statement reading x once, x dead afterwards -> inline e."""
    changed = True
    while changed:
        changed = False
        live_out = block_liveness(blocks, terms)
        for b in list(blocks):
            stmts = blocks[b]
            i = 0
            while i < len(stmts) - 1:
                d = stmts[i]
                if isinstance(d, Assign) and is_local_var(d.dst) and d.dst.kind == 'local':
                    k = ekey(d.dst)
                    u = stmts[i + 1]
                    la = live_after_list(stmts, live_out[b], terms[b])
                    if count_reads_in(u, k) == 1 and k not in la[i + 1] and not (
                            isinstance(u, Assign) and is_local_var(u.dst) and ekey(u.dst) == k and
                            count_reads_in(u, k) == 0):
                        if stmt_substitute(u, k, d.src):
                            del stmts[i]
                            changed = True
                            continue
                i += 1
            # into the terminator
            if stmts:
                d = stmts[-1]
                t = terms[b]
                if isinstance(d, Assign) and is_local_var(d.dst) and d.dst.kind == 'local' and t[0] in ('cond', 'switch'):
                    k = ekey(d.dst)
                    if k not in live_out[b] and sum(1 for e in term_exprs(t) for x in e.walk()
                                                    if isinstance(x, Var) and ekey(x) == k) == 1:
                        terms[b] = term_rewrite(t, lambda x, k=k, v=d.src: v if isinstance(x, Var) and ekey(x) == k else None)
                        del stmts[-1]
                        changed = True
'''
s = open('simplify.py', encoding='utf-8').read()
if 'def block_liveness' not in s:
    s += simp_add
    open('simplify.py', 'w', encoding='utf-8').write(s)

patch('emit.py', [(
"""        for _ in range(4):
            simplify.lower_all(blocks, terms)
            simplify.remove_dead_local_stores(blocks, terms)
            simplify.inline_temps(blocks, terms, lambda d, k: True)
            simplify.clean_statements(blocks)""",
"""        for _ in range(4):
            simplify.lower_all(blocks, terms)
            simplify.remove_dead_stores_liveness(blocks, terms)
            simplify.remove_dead_local_stores(blocks, terms)
            simplify.inline_temps(blocks, terms, lambda d, k: False)
            simplify.inline_locals(blocks, terms)
            simplify.clean_statements(blocks)"""), (
"""    specific = [(n, t) for t, n in h.items() if t not in TYPE_SET and t not in ('Array', 'Ptr', 'Hex', 'Cond')]""",
"""    specific = [(n, t) for t, n in h.items() if t not in TYPE_SET and
                t not in ('Array', 'Ptr', 'Hex', 'Cond', 'BoolConst', 'IntConst')]""")])
print('patched')
