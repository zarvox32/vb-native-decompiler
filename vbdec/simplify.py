"""Post-lift cleanup: dead stores, temp inlining, lowering of lifter-internal nodes to VB expressions."""
import struct
from collections import Counter

from ir import (Expr, Const, FConst, Str, Var, Member, Deref, Index, AddrOf, Bin, Un, Call, Temp, Me, Missing,
                Unknown, Raw, Assign, ExprStmt, Text, Comment, FmtStmt, ekey, same)
from lift import (SAddr, VTbl, ImportFn, CodePtr, DataPtr, StrCmpResult, Hresult, FpuStatus, SetCC, BoolVal,
                  negate, as_cond, VBCMP)
from x86sem import FpuAH


# --------------------------------------------------------------------------- generic expression rewriting
def rewrite(e, fn):
    """Bottom-up rewrite; fn(node) returns a replacement or None."""
    if e is None or not isinstance(e, Expr):
        return e
    if isinstance(e, Bin):
        a, b = rewrite(e.a, fn), rewrite(e.b, fn)
        if a is not e.a or b is not e.b:
            n = Bin(e.op, a, b, e.ty)
            if hasattr(e, 'w'):
                n.w = e.w           # the width the machine computed it in
            e = n
    elif isinstance(e, Un):
        a = rewrite(e.a, fn)
        if a is not e.a:
            n = Un(e.op, a, e.ty)
            if hasattr(e, 'w'):
                n.w = e.w
            e = n
    elif isinstance(e, Call):
        obj = rewrite(e.obj, fn)
        args = [rewrite(a, fn) for a in e.args]
        if obj is not e.obj or any(x is not y for x, y in zip(args, e.args)):
            n = Call(e.fn, args, obj, e.kind, e.ty, e.prop, not e.side, e.paren)
            for k in ('member', 'proc', 'decl', 'items_of'):
                if hasattr(e, k):
                    setattr(n, k, getattr(e, k))
            e = n
    elif isinstance(e, Member):
        b = rewrite(e.base, fn)
        if b is not e.base:
            e = Member(b, e.off, e.size, e.name, e.ty)
    elif isinstance(e, Deref):
        b = rewrite(e.base, fn)
        if b is not e.base:
            e = Deref(b, e.off, e.size, e.ty)
    elif isinstance(e, Index):
        a = rewrite(e.arr, fn)
        idx = [rewrite(i, fn) for i in e.idx]
        if a is not e.arr or any(x is not y for x, y in zip(idx, e.idx)):
            n = Index(a, idx, e.ty)
            for attr in ('flat', 'stride'):
                if getattr(e, attr, None) is not None:
                    setattr(n, attr, getattr(e, attr))
            e = n
    elif isinstance(e, AddrOf):
        x = rewrite(e.e, fn)
        if x is not e.e:
            e = AddrOf(x)
    elif isinstance(e, (BoolVal, SetCC)):
        c = rewrite(e.cond, fn)
        if c is not e.cond:
            e = type(e)(c)
    elif isinstance(e, StrCmpResult):
        a, b = rewrite(e.a, fn), rewrite(e.b, fn)
        e = StrCmpResult(a, b, e.text)
    elif isinstance(e, VTbl):
        o = rewrite(e.obj, fn)
        if o is not e.obj:
            e = VTbl(o)
    r = fn(e)
    return e if r is None else r


def stmt_rewrite(s, fn):
    if isinstance(s, Assign):
        s.dst = rewrite(s.dst, fn)
        s.src = rewrite(s.src, fn)
    elif isinstance(s, ExprStmt):
        s.e = rewrite(s.e, fn)
    elif isinstance(s, (Text,)):
        s.args = [rewrite(a, fn) for a in s.args]
    elif isinstance(s, FmtStmt):
        s.args = [rewrite(a, fn) for a in s.args]


def term_rewrite(t, fn):
    if t[0] == 'cond':
        return ('cond', rewrite(t[1], fn), t[2], t[3])
    if t[0] == 'switch':
        return ('switch', rewrite(t[1], fn), t[2])
    return t


def term_exprs(t):
    if t[0] in ('cond', 'switch'):
        return (t[1],)
    return ()


# --------------------------------------------------------------------------- lowering
def _factor(e):
    """(base, k) with e = base * k, factoring sums of multiples of one expression (x + x * 4 = x * 5)."""
    if isinstance(e, Bin) and e.op == '*':
        for a, b in ((e.a, e.b), (e.b, e.a)):
            if isinstance(b, Const):
                base, k = _factor(a)
                return base, k * b.v
    if isinstance(e, Bin) and e.op in ('+', '-'):
        ba, ka = _factor(e.a)
        bb, kb = _factor(e.b)
        if same(ba, bb):
            return ba, ka + kb if e.op == '+' else ka - kb
    return e, 1


def scaled_term(e):
    """`t * k` for integer arithmetic made of multiples of one expression t (the compiler's lea / shl
    chains for a multiplication): exact modulo 2^n, as the machine computes it. None if not that shape."""
    for x in e.walk():
        if isinstance(x, FConst) or getattr(x, 'ty', None) in ('Single', 'Double', 'Currency', 'Date', 'String',
                                                                'Variant'):
            return None
        if isinstance(x, Raw):
            return None
    base, k = _factor(e)
    if base is e or k == 1:
        return None
    if isinstance(e, Bin) and e.op == '*' and isinstance(e.b, Const) and e.b.v == k and same(e.a, base):
        return None                     # already in that form
    if k == 0:
        return None
    r = Un('neg', base, ty=e.ty) if k == -1 else Bin('*', base, Const(k), ty=e.ty)
    if hasattr(e, 'w'):
        r.w = e.w
    return r


def lower(e):
    """Lifter-internal nodes -> renderable VB."""
    if isinstance(e, Deref) and isinstance(e.base, AddrOf) and e.off == 0 and isinstance(e.base.e, Var):
        v = e.base.e
        return Var(v.kind, v.off, e.size, v.ty, v.name)
    if type(e).__name__ == 'VTbl' and isinstance(e.obj, AddrOf) and isinstance(e.obj.e, Var):
        return e.obj.e
    if isinstance(e, BoolVal):
        return to_bool_expr(e.cond)
    if isinstance(e, Un) and e.op == 'neg' and isinstance(e.a, SetCC):
        return to_bool_expr(e.a.cond)
    if isinstance(e, SetCC):
        c = to_bool_expr(e.cond)
        return Un('neg', c, ty='Integer') if not isinstance(c, Const) else Const(1 if c.v else 0)
    if isinstance(e, StrCmpResult):
        args = [e.a, e.b]
        if e.text:
            args.append(Raw('vbTextCompare'))
        return Call('StrComp', args, pure=True, ty='Integer')
    if isinstance(e, SAddr):
        return Raw('VarPtr(stack_%X)' % (-e.o & 0xffffffff))
    if isinstance(e, VTbl):
        return Deref(e.obj, 0, 4)
    if isinstance(e, ImportFn):
        return Raw('AddressOf %s' % e.name)
    if isinstance(e, CodePtr):
        return Raw('&H%X' % e.va)
    if isinstance(e, DataPtr):
        return Raw('&H%X' % e.va)
    if isinstance(e, Hresult):
        return Raw('0')
    if isinstance(e, FpuAH):
        return Raw('[fpu status]')
    if isinstance(e, Bin) and e.op in VBCMP and e.ty == 'Boolean':
        return simplify_cmp(e)
    if isinstance(e, Un) and e.op == 'Not' and isinstance(e.a, Bin) and e.a.op in VBCMP:
        inv = {'=': '<>', '<>': '=', '<': '>=', '>=': '<', '>': '<=', '<=': '>'}
        return Bin(inv[e.a.op], e.a.a, e.a.b, ty='Boolean')
    if isinstance(e, Un) and e.op == 'Not' and isinstance(e.a, Un) and e.a.op == 'Not' and \
            (is_bool_expr(e.a.a) or getattr(e.a.a, 'ty', None) in ('Boolean', 'Byte', 'Integer', 'Long')):
        # only an identity for whole numbers: Not Not 0.4 = 0, Not Not "12" = 12 (a Long)
        return e.a.a
    if isinstance(e, Bin) and e.op in ('+', '-', '*') and \
            e.ty not in ('Single', 'Double', 'Currency', 'Date', 'String'):
        # lea chains: x + x * 4 -> x * 5 (integer arithmetic; exact modulo 2^n, like the machine's)
        m = scaled_term(e)
        if m is not None:
            return m
    if isinstance(e, Bin) and e.op == '+' and isinstance(e.b, Un) and e.b.op == 'neg':
        return Bin('-', e.a, e.b.a, e.ty)
    if isinstance(e, Bin) and e.op == '-' and isinstance(e.b, Const) and e.b.v < 0:
        return Bin('+', e.a, Const(-e.b.v), e.ty)
    if isinstance(e, Un) and e.op == 'neg' and isinstance(e.a, Un) and e.a.op == 'neg' and \
            getattr(e.a.a, 'ty', None) == 'Boolean':
        return e.a.a
    if isinstance(e, Un) and e.op == 'neg' and isinstance(e.a, Const):
        return Const(-e.a.v, e.a.ty)
    if isinstance(e, Un) and e.op == 'neg' and isinstance(e.a, FConst):
        return FConst(-e.a.v, e.a.ty)
    return None


def to_bool_expr(c):
    if isinstance(c, (BoolVal, SetCC)):
        return to_bool_expr(c.cond)
    return c


def strip_bool_negs(x):
    if isinstance(x, Un) and x.op == 'neg' and is_bool_expr(x.a):
        return strip_bool_negs(x.a)
    if isinstance(x, Bin) and x.op in ('And', 'Or', 'Xor'):
        return Bin(x.op, strip_bool_negs(x.a), strip_bool_negs(x.b), ty='Boolean')
    return x


def is_bool_expr(x):
    if isinstance(x, Un) and x.op == 'neg':
        return is_bool_expr(x.a)
    if isinstance(x, Bin) and x.op in VBCMP:
        return True
    if isinstance(x, Bin) and x.op in ('And', 'Or', 'Xor'):
        return is_bool_expr(x.a) and is_bool_expr(x.b)
    if isinstance(x, Un) and x.op == 'Not':
        return is_bool_expr(x.a)
    return getattr(x, 'ty', None) == 'Boolean' and isinstance(x, (Call,))


def simplify_cmp(e):
    a, b = e.a, e.b
    if isinstance(b, Const) and b.v == 0 and e.op in ('<>', '=') and is_bool_expr(a):
        a = strip_bool_negs(a)
        return a if e.op == '<>' else negate(a)
    if isinstance(b, Const) and isinstance(a, Bin) and a.op in ('+', '-') and \
            getattr(a, 'ty', None) not in ('Single', 'Double'):
        l = linear(a)
        if l is not None and len(l[0]) == 1:
            t, coef = list(l[0].values())[0]
            # only a rearrangement that adds nothing: `x + 1 > 3` is not `x > 2` when x + 1 wraps around
            # (no overflow checks) or overflows (error 6), so a real offset stays where the code has it
            if coef == 1 and not isinstance(t, (FConst,)) and l[1] == 0 and not same(t, a):
                return Bin(e.op, t, Const(b.v - l[1]), ty='Boolean')
    if isinstance(b, Const) and b.v == 0 and e.op in ('=', '<>') and getattr(a, 'ty', None) == 'Boolean' and \
            isinstance(a, (Call, Var, Temp)):
        return a if e.op == '<>' else Un('Not', a, ty='Boolean')
    # (x = True) -> x ; (bool <> 0) -> bool
    if isinstance(b, Const) and b.v == 0 and isinstance(a, Bin) and a.op in VBCMP:
        if e.op == '<>':
            return a
        if e.op == '=':
            return negate(a)
    if isinstance(a, StrCmpResult) and isinstance(b, Const) and b.v == 0:
        return Bin(e.op, a.a, a.b, ty='Boolean')
    return None


# --------------------------------------------------------------------------- use counting
def iter_exprs_in(s):
    if isinstance(s, Assign):
        # dst: only its sub-expressions are reads (e.g. Deref base, Member base)
        d = s.dst
        if isinstance(d, (Deref,)):
            yield d.base
        elif isinstance(d, Member):
            yield from member_reads(d)
        elif isinstance(d, Call):
            if d.obj is not None:
                yield d.obj
            yield from d.args
        elif isinstance(d, Index):
            yield d.arr
            yield from d.idx
        yield s.src
    else:
        for e in s.exprs():
            if e is not None:
                yield e


def member_reads(m):
    b = m.base
    if isinstance(b, Member):
        yield from member_reads(b)
    elif isinstance(b, Var):
        return
    else:
        yield b


def tree_counts(e, memo):
    """Counter of Temp/Var keys by occurrence in the expression *tree* (shared nodes counted each time)."""
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
    return uses


def var_key_of_dst(d):
    """Key of the variable written by an assignment destination (top-level var or record member)."""
    while isinstance(d, Member):
        d = d.base
    if isinstance(d, (Var, Temp)):
        return ekey(d)
    return None


# --------------------------------------------------------------------------- passes
def remove_dead_local_stores(blocks, terms, keep_keys=()):
    """Drop stores to locals never read (temporaries whose value was consumed by forwarding)."""
    changed = True
    while changed:
        changed = False
        uses = count_uses(blocks, terms)
        for b, stmts in blocks.items():
            out = []
            for s in stmts:
                if isinstance(s, Assign):
                    k = var_key_of_dst(s.dst)
                    d = s.dst
                    while isinstance(d, Member):
                        d = d.base
                    if isinstance(d, Var) and d.kind in ('local', 'internal') and uses[k] == 0 and k not in keep_keys:
                        if s.src is not None and s.src.has_side_effects():
                            out.append(ExprStmt(s.src, s.addr))
                        changed = True
                        continue
                    if isinstance(d, Temp) and uses[k] == 0:
                        if s.src is not None and isinstance(s.src, Call) and s.src.side:
                            out.append(ExprStmt(s.src, s.addr))
                        changed = True
                        continue
                out.append(s)
            blocks[b] = out


def substitute(e, key, val):
    found = [False]

    def fn(x):
        if isinstance(x, (Temp, Var)) and ekey(x) == key:
            found[0] = True
            return val
        return None
    r = rewrite(e, fn)
    return r, found[0]


def stmt_substitute(s, key, val):
    if isinstance(s, Assign):
        d = s.dst
        if isinstance(d, (Var, Temp)):
            src, f = substitute(s.src, key, val)
            s.src = src
            return f
        d2, f1 = substitute_in_dst(d, key, val)
        s.dst = d2
        if f1:
            return True
        src, f = substitute(s.src, key, val)
        s.src = src
        return f
    if isinstance(s, ExprStmt):
        e, f = substitute(s.e, key, val)
        s.e = e
        return f
    if isinstance(s, (FmtStmt, Text)):
        for i, a in enumerate(s.args):
            a2, f = substitute(a, key, val)
            if f:
                s.args[i] = a2
                return True
    return False


def substitute_in_dst(d, key, val):
    if isinstance(d, Deref):
        b, f = substitute(d.base, key, val)
        return Deref(b, d.off, d.size, d.ty), f
    if isinstance(d, Call):
        n, f = substitute(d, key, val)
        return n, f
    if isinstance(d, Index):
        n, f = substitute(d, key, val)
        return n, f
    return d, False


def uses_key(s, key):
    for e in iter_exprs_in(s):
        for x in e.walk():
            if isinstance(x, (Temp, Var)) and ekey(x) == key:
                return True
    return False


def eval_sequence(site):
    """The nodes of a use site in the order VB evaluates them: an assignment's value before the target's
    index expressions; operands and arguments left to right, each before the operation that uses them."""
    if isinstance(site, tuple):
        exprs = list(term_exprs(site))
    elif isinstance(site, Assign):
        exprs = [site.src] + [e for e in iter_exprs_in(site) if e is not site.src]
    else:
        exprs = list(iter_exprs_in(site))
    seq = []
    for e in exprs:
        if e is None:
            continue
        stack = [(e, False)]
        while stack:
            x, done = stack.pop()
            if done:
                seq.append(x)
                continue
            stack.append((x, True))
            stack.extend((c, False) for c in reversed(list(x.children())) if isinstance(c, Expr))
    return seq


def is_memory_read(x):
    return (isinstance(x, Var) and x.kind in ('field', 'global', 'param', 'arg')) or \
        isinstance(x, (Deref, Member, Index))


def order_conflict(site, k, src):
    """Would moving `src` (computed before the site) to its use of k inside the site change what happens?
    Everything the site evaluates before that use would now run before src: a call there must not be able
    to change what src reads, and src's own calls must not change what is read there."""
    side, reads, fail = has_side_effects(src), reads_memory(src), may_fail(src)
    src_locals = {ekey(x) for x in src.walk() if isinstance(x, Var)}
    for x in eval_sequence(site):
        if isinstance(x, (Temp, Var)) and ekey(x) == k:
            return False
        if isinstance(x, Call) and x.side:
            if side or reads or fail:       # (an error must still come before the call's effects)
                return True
            # a variable passed to the call (ByRef) may be changed by it
            if any(isinstance(a, Var) and ekey(a) in src_locals for a in x.args):
                return True
        elif side and is_memory_read(x):
            return True
        elif (side or fail) and may_fail(x) and not isinstance(x, (Var, Temp, Const)):
            return True                     # keep the order of two things that can fail
    return False


def byref_use(site, k):
    """Is variable k passed by address (ByRef) to a procedure in this statement / terminator?"""
    exprs = list(term_exprs(site)) if isinstance(site, tuple) else list(iter_exprs_in(site))
    return any(isinstance(x, Var) and ekey(x) == k and getattr(x, 'byref_arg', False)
               for e in exprs if e is not None for x in e.walk())


def is_lvalue(e):
    """A variable or element VB would pass by reference (replacing a ByRef temporary with it would let
    the called procedure change it)."""
    return isinstance(e, (Var, Index, Member, Deref)) and not isinstance(e, Temp)


def split_locals(blocks, terms, entry, roots=(), value_type=None):
    """The compiler reuses stack slots for unrelated temporaries (a Long here, a Single there): give each
    independent use of a local its own variable. Definitions that reach a common use stay together
    (def-use webs over the reaching definitions); a local passed ByRef counts as read and written there.
    Returns {new variable key: original key}."""
    def is_loc(x):
        return isinstance(x, Var) and x.kind == 'local'
    succ = {b: [s for s in term_succs(t) if s in blocks] for b, t in terms.items() if b in blocks}
    preds = {b: [] for b in blocks}
    for b, ss in succ.items():
        for s in ss:
            preds[s].append(b)
    defs_of = {}            # var key -> [def id]
    gen = {}                # block -> {key: def id} (last def in the block)
    events = {}             # block -> [(i, 'use'|'def'|'both', key, def id or None)]
    for b, stmts in blocks.items():
        ev = []
        for i, s in enumerate(stmts):
            reads = set()
            byref = set()
            for e in iter_exprs_in(s):
                for x in e.walk():
                    if is_loc(x):
                        reads.add(ekey(x))
                        if getattr(x, 'byref_arg', False):
                            byref.add(ekey(x))
                    elif isinstance(x, AddrOf) and is_loc(x.e):
                        reads.add(ekey(x.e))
                        byref.add(ekey(x.e))
            for k in reads:
                ev.append((i, 'use', k, None))
            for k in byref:
                d = (b, i, k)
                defs_of.setdefault(k, []).append(d)
                ev.append((i, 'def', k, d))
            if isinstance(s, Assign) and is_loc(s.dst):
                k = ekey(s.dst)
                d = (b, i, k)
                defs_of.setdefault(k, []).append(d)
                ev.append((i, 'def', k, d))
        for e in term_exprs(terms[b]):
            for x in e.walk():
                if is_loc(x):
                    ev.append((len(stmts), 'use', ekey(x), None))
        events[b] = ev
        g = {}
        for i, kind, k, d in ev:
            if kind == 'def':
                g[k] = d
        gen[b] = g
    if not any(len(v) > 1 for v in defs_of.values()):
        return {}
    ENTRY = 'entry'
    # reaching definitions: block -> {key: set(def ids)}
    inn = {b: {} for b in blocks}
    out = {b: {} for b in blocks}
    starts = [entry] + [r for r in roots if r in blocks]
    for r in starts:
        if r in blocks:
            # at the procedure start nothing is assigned yet; an error handler can be entered from anywhere
            inn[r] = {k: ({ENTRY} if r == entry else set(v) | {ENTRY}) for k, v in defs_of.items()}
    changed = True
    rounds = 0
    while changed and rounds < 50:
        changed = False
        rounds += 1
        for b in blocks:
            if b not in starts:
                new = {}
                for p in preds[b]:
                    for k, ds in out[p].items():
                        new.setdefault(k, set()).update(ds)
                if new != inn[b]:
                    inn[b] = new
                    changed = True
            o = {k: set(v) for k, v in inn[b].items()}
            for k, d in gen[b].items():
                o[k] = {d}
            if o != out[b]:
                out[b] = o
                changed = True
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, c):
        ra, rc = find(a), find(c)
        if ra != rc:
            parent[ra] = rc
    use_web = {}            # (b, i, key) -> a def id of its web
    for b, ev in events.items():
        cur = {k: set(v) for k, v in inn[b].items()}
        for i, kind, k, d in ev:
            if kind == 'use':
                ds = cur.get(k) or {ENTRY}
                ds = {(ENTRY, k) if x == ENTRY else x for x in ds}
                first = next(iter(ds))
                for x in ds:
                    union(first, x)
                use_web[(b, i, k)] = first
            else:
                # a ByRef pass reads what reaches it and writes the same variable: one web
                if (b, i, k) in use_web:
                    union(d, use_web[(b, i, k)])
                cur[k] = {d}
    # number the webs of each variable
    webs = {}
    for k, ds in defs_of.items():
        roots_ = {}
        for d in ds + [(ENTRY, k)]:
            roots_.setdefault(find(d), []).append(d)
        if len(roots_) > 1:
            webs[k] = roots_
    if value_type is not None:
        # only split where the separate uses hold different kinds of values (a Long here, a Single
        # there); a slot reused for the same kind of value stays one variable
        for k in list(webs):
            kinds = set()
            for root, ds in webs[k].items():
                tys = set()
                for d in ds:
                    if d[0] == ENTRY:
                        continue
                    b, i, _ = d
                    s = blocks[b][i]
                    if isinstance(s, Assign) and is_loc(s.dst) and ekey(s.dst) == k:
                        t = value_type(s.src)
                        if t:
                            tys.add(t)
                if len(tys) == 1:
                    kinds.add(next(iter(tys)))
            if len(kinds) < 2:
                del webs[k]
    if not webs:
        return {}
    rename = {}             # (def or use position) -> new key
    new_keys = {}
    for k, groups in webs.items():
        # the web that holds the start value (or the largest) keeps the name
        order = sorted(groups.items(), key=lambda g: (not any(d[0] == ENTRY for d in g[1]), -len(g[1])))
        for n, (root, ds) in enumerate(order):
            if n == 0:
                continue
            new_keys[(k, root)] = n + 1

    def key_for(k, web_root):
        n = new_keys.get((k, find(web_root)))
        return n

    def renamed(v, n):
        nv = Var(v.kind, v.off + 0x1000000 * n, v.size, v.ty, ('%s_%d' % (v.name, n)) if v.name else None)
        if getattr(v, 'byref_arg', False):
            nv.byref_arg = True
        return nv
    for b, stmts in blocks.items():
        for i, s in enumerate(stmts):
            def fn(x, i=i, b=b):
                if is_loc(x) and ekey(x) in webs:
                    k = ekey(x)
                    w = use_web.get((b, i, k))
                    if w is None:
                        return None
                    n = key_for(k, w)
                    return renamed(x, n) if n else None
                return None
            # reads first (sources, indexes...), then the destination by its definition's web
            if isinstance(s, Assign):
                if not (is_loc(s.dst)):
                    s.dst = rewrite(s.dst, fn)
                s.src = rewrite(s.src, fn)
                if is_loc(s.dst) and ekey(s.dst) in webs:
                    n = key_for(ekey(s.dst), (b, i, ekey(s.dst)))
                    if n:
                        s.dst = renamed(s.dst, n)
            else:
                stmt_rewrite(s, fn)

        def tfn(x, b=b):
            if is_loc(x) and ekey(x) in webs:
                k = ekey(x)
                w = use_web.get((b, len(blocks[b]), k))
                n = key_for(k, w) if w is not None else None
                return renamed(x, n) if n else None
            return None
        terms[b] = term_rewrite(terms[b], tfn)
    return {('v', 'local', k[2] + 0x1000000 * n): k for (k, r), n in new_keys.items()}


def has_side_effects(e):
    return any(isinstance(x, Call) and x.side for x in e.walk())


SAFE_FUNCTIONS = {'Len', 'LenB', 'Trim', 'LTrim', 'RTrim', 'LCase', 'UCase', 'CStr', 'Str', 'CBool'}


def may_fail(e):
    """Can evaluating e raise a run-time error (a conversion that overflows, a division by zero, a
    string function given bad arguments...)? Such an evaluation must not move past other effects."""
    for x in e.walk():
        if isinstance(x, Call) and x.kind == 'rt' and x.fn not in SAFE_FUNCTIONS:
            return True
        if isinstance(x, Bin) and x.op in ('/', '\\', 'Mod', '^'):
            return True
    return False


def reads_memory(e):
    """Does the value depend on memory a call could change (fields, globals, parameters, pointers)?"""
    for x in e.walk():
        if isinstance(x, Var) and x.kind in ('field', 'global', 'param', 'arg'):
            return True
        if isinstance(x, (Deref, Member, Index)):
            return True
    return False


def uses_key_expr(e, k):
    return any(isinstance(x, (Temp, Var)) and ekey(x) == k for x in e.walk())


def inline_temps(blocks, terms, local_ok):
    """Stack-like inlining of single-use temporaries into the statement that uses them."""
    changed = True
    while changed:
        changed = False
        uses = count_uses(blocks, terms)
        defs = Counter()
        for b, stmts in blocks.items():
            for s in stmts:
                if isinstance(s, Assign):
                    k = var_key_of_dst(s.dst)
                    if k is not None and isinstance(s.dst, (Temp, Var)):
                        defs[k] += 1
        for b in list(blocks):
            stmts = blocks[b]
            i = 0
            # consider each statement (and the terminator) as a use site
            sites = list(range(len(stmts))) + ['term']
            new = list(stmts)
            pos = 0
            while pos <= len(new):
                site = 'term' if pos == len(new) else pos
                j = pos - 1
                while j >= 0:
                    d = new[j]
                    if not (isinstance(d, Assign) and isinstance(d.dst, (Temp, Var))):
                        break
                    k = ekey(d.dst)
                    if isinstance(d.dst, Var):
                        if not (d.dst.kind == 'local' and local_ok(d, k) and defs[k] == 1):
                            break
                    if uses[k] != 1:
                        break
                    # keep the order of calls and memory reads (d runs before the whole site)
                    if order_conflict(terms[b] if site == 'term' else new[site], k, d.src):
                        break
                    # the use must be in the site
                    if site == 'term':
                        t = terms[b]
                        if not any(isinstance(x, (Temp, Var)) and ekey(x) == k for e in term_exprs(t) for x in e.walk()):
                            break
                        terms[b] = term_rewrite(t, lambda x, k=k, v=d.src: v if isinstance(x, (Temp, Var)) and ekey(x) == k else None)
                    else:
                        if not uses_key(new[site], k):
                            break
                        if not stmt_substitute(new[site], k, d.src):
                            break
                    del new[j]
                    pos -= 1
                    j -= 1
                    if site != 'term':
                        site -= 1           # the use site moved up with the deletion
                    changed = True
                pos += 1
            blocks[b] = new


def lower_all(blocks, terms):
    for b, stmts in blocks.items():
        for s in stmts:
            stmt_rewrite(s, lower)
        terms[b] = term_rewrite(terms[b], lower)


def clean_statements(blocks):
    """Remove no-op statements (self-assignments, assignments of hresults)."""
    for b, stmts in blocks.items():
        out = []
        for s in stmts:
            if isinstance(s, Assign) and same(s.dst, s.src):
                continue
            if isinstance(s, Assign) and isinstance(s.src, Hresult):
                continue
            if isinstance(s, ExprStmt) and not isinstance(s.e, Call):
                continue
            out.append(s)
        blocks[b] = out


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
                if isinstance(s, Assign) and is_local_var(s.dst) and ekey(s.dst) not in live and \
                        not getattr(s, 'keep', False):
                    if s.src is not None and s.src.has_side_effects():
                        out.append(ExprStmt(s.src, s.addr))
                    changed = True
                    continue
                out.append(s)
            blocks[b] = out


def count_reads_in(s, k):
    memo = {}
    return sum(tree_counts(e, memo).get(k, 0) for e in iter_exprs_in(s))


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
                if isinstance(d, Assign) and is_local_var(d.dst) and d.dst.kind in ('local', 'reg'):
                    k = ekey(d.dst)
                    j = i + 1
                    if isinstance(d.src, (Const, Str, FConst)):
                        while j < len(stmts) - 1 and not uses_key(stmts[j], k) and not (
                                isinstance(stmts[j], Assign) and isinstance(stmts[j].dst, (Var, Temp)) and
                                ekey(stmts[j].dst) == k):
                            j += 1
                    u = stmts[j]
                    la = live_after_list(stmts, live_out[b], terms[b])
                    if count_reads_in(u, k) == 1 and k not in la[j] and not (
                            isinstance(u, Assign) and is_local_var(u.dst) and ekey(u.dst) == k and
                            count_reads_in(u, k) == 0) and not order_conflict(u, k, d.src) and \
                            not (byref_use(u, k) and is_lvalue(d.src)):
                        if stmt_substitute(u, k, d.src):
                            del stmts[i]
                            changed = True
                            continue
                i += 1
            # into the terminator
            if stmts:
                d = stmts[-1]
                t = terms[b]
                if isinstance(d, Assign) and is_local_var(d.dst) and d.dst.kind in ('local', 'reg') and \
                        t[0] in ('cond', 'switch'):
                    k = ekey(d.dst)
                    if k not in live_out[b] and sum(tree_counts(e, {}).get(k, 0) for e in term_exprs(t)) == 1 and \
                            not order_conflict(t, k, d.src):
                        terms[b] = term_rewrite(t, lambda x, k=k, v=d.src: v if isinstance(x, Var) and ekey(x) == k else None)
                        del stmts[-1]
                        changed = True


# --------------------------------------------------------------------------- arrays
def linear(e):
    """Decompose an integer expression into ({term_key: (term, coef)}, const). None if not linear."""
    if isinstance(e, Const):
        return {}, e.v
    if isinstance(e, Bin) and e.op in ('+', '-'):
        a = linear(e.a)
        b = linear(e.b)
        if a is None or b is None:
            return None
        sign = 1 if e.op == '+' else -1
        terms = dict(a[0])
        for k, (t, c) in b[0].items():
            if k in terms:
                terms[k] = (t, terms[k][1] + sign * c)
            else:
                terms[k] = (t, sign * c)
        return terms, a[1] + sign * b[1]
    if isinstance(e, Bin) and e.op == '*':
        for x, y in ((e.a, e.b), (e.b, e.a)):
            if isinstance(y, Const):
                l = linear(x)
                if l is None:
                    return None
                return {k: (t, c * y.v) for k, (t, c) in l[0].items()}, l[1] * y.v
        return {ekey(e): (e, 1)}, 0
    if isinstance(e, Un) and e.op == 'neg':
        l = linear(e.a)
        if l is None:
            return None
        return {k: (t, -c) for k, (t, c) in l[0].items()}, -l[1]
    return {ekey(e): (e, 1)}, 0


def array_access(d):
    """Deref(base + i*stride + c, off) with base a variable -> (base, index expr, stride, field offset)."""
    if not isinstance(d, Deref) or not isinstance(d.base, Bin) or d.base.op not in ('+', '-'):
        return None
    l = linear(d.base)
    if l is None:
        return None
    terms, const = l
    const += d.off
    me = [k for k, (t, c) in terms.items() if isinstance(t, Me) and c == 1]
    if me and len(terms) == 2:
        (idx, stride), = [(t, c) for k, (t, c) in terms.items() if k != me[0]]
        if stride == d.size and stride > 0 and not isinstance(idx, Me):
            return Var('field', const, d.size), idx, stride, 0, 'fixed'
        return None
    bases = [(t, c) for t, c in terms.values() if c == 1 and isinstance(t, Var) and t.kind in ('field', 'global', 'local')]
    sa = [(t, c) for t, c in terms.values() if c == 1 and isinstance(t, Deref) and t.off == 0xC and
          isinstance(t.base, Var)]
    if len(bases) != 1 and len(sa) == 1:
        # dynamic array: pvData = psa->pvData (+0xC), index adjusted by psa->lLbound (+0x14)
        pv = sa[0][0]
        psa = pv.base
        rest = [(t, c) for k, (t, c) in terms.items() if t is not pv]
        lb = [(t, c) for t, c in rest if isinstance(t, Deref) and t.off == 0x14 and same(t.base, psa)]
        rest = [(t, c) for t, c in rest if not (isinstance(t, Deref) and t.off == 0x14 and same(t.base, psa))]
        if not rest and len(lb) == 1 and lb[0][1] < 0:
            stride = -lb[0][1]
            k, field = divmod(const, stride)
            return psa, Const(k), stride, field, 'psa'
        if len(rest) != 1 or len(lb) > 1:
            return None
        idx, stride = rest[0]
        if lb and lb[0][1] != -stride:
            return None
        if stride <= 0:
            return None
        k, field = divmod(const, stride)
        index = idx if k == 0 else Bin('+', idx, Const(k)) if k > 0 else Bin('-', idx, Const(-k))
        return psa, index, stride, field, 'psa'
    if len(bases) != 1:
        return None
    base = bases[0][0]
    rest = [(t, c) for k, (t, c) in terms.items() if t is not base]
    if not rest:
        size = d.size
        if size <= 0 or const % size:
            return None
        return base, Const(const // size), size, 0
    if len(rest) >= 2:
        # column-major multi-dimensional array: coefficients stride, stride*d1, stride*d1*d2 ...
        rest.sort(key=lambda tc: tc[1])
        stride = rest[0][1]
        if stride <= 0 or any(c % stride for t, c in rest):
            return None
        for (t1, c1), (t2, c2) in zip(rest, rest[1:]):
            if c2 % c1 or c2 <= c1:
                return None
        k, field = divmod(const, stride)
        idxs = [t for t, c in rest]
        mults = [c // stride for t, c in rest]
        if k:
            idxs[0] = Bin('+', idxs[0], Const(k)) if k > 0 else Bin('-', idxs[0], Const(-k))
        return base, idxs, stride, field, 'pv', (mults, k)
    if len(rest) != 1:
        return None
    idx, stride = rest[0]
    if stride <= 0:
        return None
    k, field = divmod(const, stride)
    index = idx if k == 0 else Bin('+', idx, Const(k)) if k > 0 else Bin('-', idx, Const(-k))
    return base, index, stride, field


def array_rewrite_fn(note, known=None):
    def fn(x):
        if known is not None and isinstance(x, Deref) and isinstance(x.base, Bin) and x.base.op == '+' and \
                isinstance(x.base.a, Var) and isinstance(x.base.b, Const) and \
                x.base.a.kind in ('field', 'global', 'local'):
            st = known(x.base.a)
            if st:
                k, field = divmod(x.base.b.v + x.off, st)
                ix = Index(x.base.a, [Const(k)], x.ty if field == 0 and st == x.size else None)
                ix.stride = st
                note(x.base.a, st, x.size)
                return ix if (field == 0 and st == x.size) else Member(ix, field, x.size, name='f%X' % field, ty=x.ty)
        if known is not None and isinstance(x, Deref) and isinstance(x.base, Var) and \
                x.base.kind in ('field', 'global', 'local'):
            st = known(x.base)
            if st:
                k, field = divmod(x.off, st)
                ix = Index(x.base, [Const(k)], x.ty if field == 0 and st == x.size else None)
                ix.stride = st
                note(x.base, st, x.size)
                return ix if (field == 0 and st == x.size) else Member(ix, field, x.size, name='f%X' % field, ty=x.ty)
        a = array_access(x)
        if a is None:
            return None
        base, index, stride, field = a[:4]
        note(base, stride, x.size, a[4] if len(a) > 4 else 'pv')
        ix = Index(base, index if isinstance(index, list) else [index])
        ix.stride = stride
        if len(a) > 5:
            ix.flat = (list(a[1]), a[5])    # the element number: sum of index * multiplier, + constant
        if stride == x.size and field == 0:
            ix.ty = x.ty          # the element's type, when the access said (e.g. a string copy)
            return ix
        return Member(ix, field, x.size, name='f%X' % field, ty=x.ty)
    return fn


# --------------------------------------------------------------------------- register aliases
def fold_phi_copies(blocks, terms):
    """`R = v` at the end of each predecessor + `X = R` at the start of the join block (R read only
    there) -> `X = v` in each predecessor (a VB Select Case / If storing into one variable)."""
    reads = Counter()
    for b, stmts in blocks.items():
        for s in stmts:
            for e in iter_exprs_in(s):
                for x in e.walk():
                    if isinstance(x, Var) and x.kind == 'reg':
                        reads[ekey(x)] += 1
        for e in term_exprs(terms[b]):
            for x in e.walk():
                if isinstance(x, Var) and x.kind == 'reg':
                    reads[ekey(x)] += 1
    for b, stmts in blocks.items():
        if not stmts:
            continue
        s = stmts[0]
        if not (isinstance(s, Assign) and isinstance(s.src, Var) and s.src.kind == 'reg' and
                isinstance(s.dst, Var) and s.dst.kind in ('local', 'field', 'global') and s.kind == 'let'):
            continue
        rk = ekey(s.src)
        if reads[rk] != 1:
            continue
        dst = s.dst
        for b2, st2 in blocks.items():
            for t in st2:
                if isinstance(t, Assign) and isinstance(t.dst, Var) and ekey(t.dst) == rk:
                    t.dst = Var(dst.kind, dst.off, dst.size, dst.ty, dst.name)
        del stmts[0]


def remove_zero_inits(blocks, entry):
    """VB zero-initialises string/object/variant locals in the prologue; drop those stores."""
    stmts = blocks.get(entry)
    if not stmts:
        return
    i = 0
    while i < len(stmts):
        s = stmts[i]
        if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind in ('local', 'result') and \
                isinstance(s.src, Const) and s.src.v == 0 and s.dst.off < 0x7e00:
            del stmts[i]
            continue
        break


def resolve_register_aliases(blocks, terms):
    """A register variable whose every incoming value is the value just stored into the same local
    (VB caching a For-loop counter in a register) is that local."""
    assigns = {}
    for b, stmts in blocks.items():
        for i, s in enumerate(stmts):
            if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'reg':
                assigns.setdefault(ekey(s.dst), []).append((b, i, s))
    for rk, lst in assigns.items():
        target = None
        ok = True
        for b, i, s in lst:
            stmts = blocks[b]
            found = None
            seen = set()
            for j in range(i - 1, -1, -1):
                t = stmts[j]
                if isinstance(t, Assign) and isinstance(t.dst, Var) and t.dst.kind == 'local':
                    k = ekey(t.dst)
                    if k not in seen and (same(t.src, s.src) or (isinstance(s.src, Var) and same(s.src, t.dst))):
                        found = k, t.dst
                        break
                    seen.add(k)
            if found is None and isinstance(s.src, Var) and s.src.kind == 'local':
                found = ekey(s.src), s.src
            if found is None or (target is not None and found[0] != target[0]):
                ok = False
                break
            target = found
        if not ok or target is None:
            continue
        var = target[1]

        def fn(x, rk=rk, var=var):
            if isinstance(x, Var) and x.kind == 'reg' and ekey(x) == rk:
                return Var(var.kind, var.off, var.size, var.ty, var.name)
            return None
        for b, stmts in blocks.items():
            blocks[b] = [s for s in stmts
                         if not (isinstance(s, Assign) and isinstance(s.dst, Var) and ekey(s.dst) == rk)]
            for s in blocks[b]:
                stmt_rewrite(s, fn)
            terms[b] = term_rewrite(terms[b], fn)


def fold_boolean_phis(blocks, terms):
    """cond -> (A: R = 1 | B: R = 0) -> J   ==>   R = <cond as 0/1> at the end of the cond block."""
    from lift import SetCC, BoolVal
    changed = True
    while changed:
        changed = False
        for c, t in list(terms.items()):
            if t[0] != 'cond':
                continue
            a, b = t[2], t[3]
            if a not in blocks or b not in blocks:
                continue
            sa, sb = blocks[a], blocks[b]
            if len(sa) != 1 or len(sb) != 1:
                continue
            x, y = sa[0], sb[0]
            if not (isinstance(x, Assign) and isinstance(y, Assign) and isinstance(x.dst, Var) and
                    x.dst.kind == 'reg' and same(x.dst, y.dst) and isinstance(x.src, Const) and
                    isinstance(y.src, Const)):
                continue
            if terms[a][0] != 'goto' or terms[b][0] != 'goto' or terms[a][1] != terms[b][1]:
                continue
            pa = sum(1 for tt in terms.values() for s in term_succs(tt) if s == a)
            pb = sum(1 for tt in terms.values() for s in term_succs(tt) if s == b)
            if pa != 1 or pb != 1:
                continue
            va, vb = x.src.v, y.src.v
            cond = t[1]
            if (va, vb) == (1, 0):
                val = SetCC(cond)
            elif (va, vb) == (0, 1):
                val = SetCC(negate(cond))
            elif (va, vb) == (-1, 0):
                val = BoolVal(cond)
            elif (va, vb) == (0, -1):
                val = BoolVal(negate(cond))
            else:
                continue
            j = terms[a][1]
            blocks[c].append(Assign(x.dst, val, x.addr))
            terms[c] = ('goto', j)
            del blocks[a], terms[a], blocks[b], terms[b]
            changed = True


# --------------------------------------------------------------------------- 16-byte (Variant) copies
def var_part(e):
    """(kind, byte offset in the variable's natural direction) for dword reads of a variable."""
    if isinstance(e, Var) and e.size == 4:
        if e.kind == 'local':
            return ('local', -e.off)
        if e.kind in ('field', 'global', 'arg', 'param'):
            return (e.kind, e.off)
    return None


def merge_variant_copies(blocks):
    """*(P+0..12) = parts of one variable  ->  *P = variable (a VARIANT copy)."""
    for b, stmts in blocks.items():
        i = 0
        while i + 3 < len(stmts):
            grp = stmts[i:i + 4]
            ok = all(isinstance(s, Assign) and isinstance(s.dst, Deref) and s.dst.size == 4 for s in grp)
            if ok:
                base = grp[0].dst.base
                offs = [s.dst.off for s in grp]
                parts = [var_part(s.src) for s in grp]
                if all(same(s.dst.base, base) for s in grp) and offs == [0, 4, 8, 12] and \
                        all(p is not None for p in parts) and all(p[0] == parts[0][0] for p in parts) and \
                        [p[1] - parts[0][1] for p in parts] == [0, 4, 8, 12]:
                    kind, o = parts[0]
                    src = Var(kind, -o if kind == 'local' else o, 16, 'Variant')
                    stmts[i:i + 4] = [Assign(Deref(base, 0, 16), src, grp[0].addr)]
            i += 1


def propagate_constant_regvars(blocks, terms):
    """A register variable whose every assignment is the same stable value is that value."""
    vals = {}
    bad = set()
    tdefs = {}
    tcount = Counter()
    for b, stmts in blocks.items():
        for s in stmts:
            if isinstance(s, Assign) and isinstance(s.dst, Temp):
                tdefs[ekey(s.dst)] = s.src
                tcount[ekey(s.dst)] += 1

    def resolve(v):
        seen = 0
        while isinstance(v, Temp) and tcount.get(ekey(v)) == 1 and seen < 10:
            v = tdefs[ekey(v)]
            seen += 1
        return v
    for b, stmts in blocks.items():
        for s in stmts:
            if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'reg':
                k = ekey(s.dst)
                src = resolve(s.src)
                if not isinstance(src, (AddrOf, Const, Str, Me)) and not (
                        isinstance(src, Var) and src.kind in ('param',)):
                    bad.add(k)
                elif k in vals and not same(vals[k], src):
                    bad.add(k)
                else:
                    vals[k] = src
    good = {k: v for k, v in vals.items() if k not in bad}
    if not good:
        return

    def fn(x):
        if isinstance(x, Var) and x.kind == 'reg' and ekey(x) in good:
            return good[ekey(x)]
        if isinstance(x, Deref) and isinstance(x.base, AddrOf) and x.off == 0 and isinstance(x.base.e, Var):
            v = x.base.e
            return Var(v.kind, v.off, x.size, v.ty, v.name)
        return None
    for b in blocks:
        blocks[b] = [s for s in blocks[b] if not (isinstance(s, Assign) and isinstance(s.dst, Var) and
                                                   s.dst.kind == 'reg' and ekey(s.dst) in good)]
        for s in blocks[b]:
            stmt_rewrite(s, fn)
        terms[b] = term_rewrite(terms[b], fn)


def float_const_stores(blocks, var_type, float_evidence=None):
    """Constants written into Single variables (and Double halves) are float bit patterns."""
    import struct as _st
    for b, stmts in blocks.items():
        out = []
        i = 0
        while i < len(stmts):
            s = stmts[i]
            if isinstance(s, Assign) and isinstance(s.dst, Var) and isinstance(s.src, Const) and s.dst.size == 4                     and s.dst.kind in ('field', 'global', 'local'):
                t = var_type(s.dst)
                if t == 'Single':
                    f = _st.unpack('<f', _st.pack('<I', s.src.v & 0xffffffff))[0]
                    s.src = FConst(f, 'Single')
                elif i + 1 < len(stmts):
                    n = stmts[i + 1]
                    hi_off = s.dst.off - 4 if s.dst.kind == 'local' else s.dst.off + 4
                    if isinstance(n, Assign) and isinstance(n.dst, Var) and n.dst.kind == s.dst.kind and \
                            n.dst.off == hi_off and isinstance(n.src, Const):
                        big = Var(s.dst.kind, s.dst.off, 8, s.dst.ty, s.dst.name)
                        # two dword constants make one Double only for a variable known to be one (the
                        # compiler's scratch slots for ByRef constants sit side by side)
                        if var_type(big) in ('Double', 'Date') and \
                                (float_evidence is None or float_evidence(big) in ('Double', 'Date')):
                            raw = _st.pack('<II', s.src.v & 0xffffffff, n.src.v & 0xffffffff)
                            out.append(Assign(big, FConst(_st.unpack('<d', raw)[0]), s.addr))
                            i += 2
                            continue
            out.append(s)
            i += 1
        blocks[b] = out


def strip_condition_negs(terms):
    for b, t in list(terms.items()):
        if t[0] == 'cond' and is_bool_expr(t[1]):
            terms[b] = ('cond', strip_bool_negs(t[1]), t[2], t[3])


# --------------------------------------------------------------------------- address propagation (With blocks)
def is_address_expr(e):
    """AddrOf(x) or an element address `base + i*k + c` (no calls)."""
    if isinstance(e, AddrOf):
        return True
    if isinstance(e, Bin) and e.op in ('+', '-') and not any(isinstance(x, Call) for x in e.walk()):
        l = linear(e)
        if l is None:
            return False
        bases = [t for t, c in l[0].values() if c == 1 and isinstance(t, (Var, Deref)) and
                 (not isinstance(t, Var) or t.kind in ('field', 'global', 'local'))]
        return len(bases) >= 1 and len(l[0]) >= 1 and (len(l[0]) > 1 or l[1] != 0)
    return False


def vars_read(e):
    out = set()
    stack = [e]
    seen = set()
    while stack:
        x = stack.pop()
        if id(x) in seen or isinstance(x, AddrOf):
            continue
        seen.add(id(x))
        if isinstance(x, (Var, Temp)):
            out.add(ekey(x))
        stack.extend(c for c in x.children() if isinstance(c, Expr))
    return out


def propagate_addresses(blocks, terms, entry):
    """Variables that only ever hold an address (VB `With` temporaries, pointers kept in registers)
    are replaced by that address at each read with a single reaching definition."""
    # candidate variables: every definition is an address expression
    defs = {}
    for b, stmts in blocks.items():
        for i, s in enumerate(stmts):
            if isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and \
                    (isinstance(s.dst, Temp) or s.dst.kind in ('local', 'reg')):
                defs.setdefault(ekey(s.dst), []).append((b, i, s))
    cand = {k for k, lst in defs.items() if all(is_address_expr(s.src) for b, i, s in lst)}
    if not cand:
        return
    succ = {b: term_succs(t) for b, t in terms.items()}
    preds = {b: [] for b in blocks}
    for b, ss in succ.items():
        for x in ss:
            if x in preds:
                preds[x].append(b)
    # reaching definitions (per candidate variable) at block entry
    out_defs = {b: {} for b in blocks}
    in_defs = {b: {} for b in blocks}
    changed = True
    order = sorted(blocks)
    while changed:
        changed = False
        for b in order:
            ind = {}
            for p in preds[b]:
                for k, ds in out_defs[p].items():
                    ind.setdefault(k, set()).update(ds)
            cur = {k: set(v) for k, v in ind.items()}
            for i, s in enumerate(blocks[b]):
                if isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in cand:
                    cur[ekey(s.dst)] = {(b, i)}
            if cur != out_defs[b] or ind != in_defs[b]:
                out_defs[b] = cur
                in_defs[b] = ind
                changed = True
    stmt_at = {(b, i): s for b, stmts in blocks.items() for i, s in enumerate(stmts)}

    def assigned_between(dpos, upos, names):
        """Is any variable in names assigned on a path from definition dpos to use upos?"""
        db, di = dpos
        ub, ui = upos
        if db == ub and di < ui:
            seg = blocks[db][di + 1:ui]
            return any(isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in names for s in seg)
        # statements after the def, in blocks between, and before the use
        if any(isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in names
               for s in blocks[db][di + 1:]):
            return True
        if any(isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in names
               for s in blocks[ub][:ui]):
            return True
        fwd = set()
        st = list(succ.get(db, []))
        while st:
            v = st.pop()
            if v in fwd or v == db or v not in blocks:
                continue
            fwd.add(v)
            st.extend(succ.get(v, []))
        back = set()
        st = list(preds.get(ub, []))
        while st:
            v = st.pop()
            if v in back or v == ub or v not in blocks:
                continue
            back.add(v)
            st.extend(preds.get(v, []))
        for v in fwd & back:
            if any(isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in names
                   for s in blocks[v]):
                return True
        return False

    def make_fn(pos, live):
        def fn(x):
            if isinstance(x, (Var, Temp)) and ekey(x) in cand:
                ds = live.get(ekey(x))
                if ds and len(ds) == 1:
                    d = next(iter(ds))
                    src = stmt_at[d].src
                    names = vars_read(src)
                    if ekey(x) not in names and not assigned_between(d, pos, names):
                        return src
            return None
        return fn

    for b in order:
        cur = {k: set(v) for k, v in in_defs[b].items()}
        for i, s in enumerate(blocks[b]):
            if not (isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in cand):
                fn = make_fn((b, i), cur)
                if isinstance(s, Assign):
                    if not isinstance(s.dst, (Var, Temp)):
                        s.dst = rewrite(s.dst, fn)
                    s.src = rewrite(s.src, fn)
                else:
                    stmt_rewrite(s, fn)
            if isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and ekey(s.dst) in cand:
                cur[ekey(s.dst)] = {(b, i)}
        fn = make_fn((b, len(blocks[b])), cur)
        terms[b] = term_rewrite(terms[b], fn)


# --------------------------------------------------------------------------- condition chains
def unconditional_ok(e, options=None):
    """May e be evaluated where the machine code may skip it? VB's And/Or evaluate both sides, so a second
    test that calls something, can raise an error (division, overflow, array bounds, an object) or reads
    through an object must stay a nested If."""
    options = options or {}
    for x in e.walk():
        if isinstance(x, Call):
            if x.side or x.kind != 'rt' or x.fn not in ('CBool', 'Abs', 'Sgn', 'Len', 'LenB', 'Int', 'Fix'):
                return False
            if x.fn in ('Abs', 'Int', 'Fix') and options.get('overflow_checks', True):
                return False            # Abs(-32768) overflows
        if isinstance(x, Bin) and x.op in ('/', '\\', 'Mod', '^'):
            return False
        if isinstance(x, Bin) and x.op in ('+', '-', '*') and options.get('overflow_checks', True):
            return False
        if isinstance(x, Un) and x.op == 'neg' and options.get('overflow_checks', True):
            return False
        if isinstance(x, (Index, Deref)):      # (even unchecked: a port would read outside the array)
            return False
        if isinstance(x, (Raw, Unknown)):
            return False
    return True


def merge_condition_chains(blocks, terms, entry, roots, options=None):
    """Branch chains that test several conditions for one target -> a single And/Or condition (only when
    the second test is harmless to evaluate when the first one already decided)."""
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
                if not unconditional_ok(c2, options):
                    continue
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


def fold_mirrored_registers(blocks, terms, entry):
    """Join block starting with `X = R` (R a register variable, X a local): when every read of R happens
    in blocks dominated by that join and nothing else assigns X there, R is X."""
    from structure import dominators, dominates
    succ = {b: [s for s in term_succs(t) if s in blocks] for b, t in terms.items()}
    succ['ROOT'] = [entry]
    idom = dominators(set(blocks) | {'ROOT'}, succ, 'ROOT')
    merged = []         # (register key, local key): the local now stands for the register's value
    for b in list(blocks):
        stmts = blocks.get(b)
        if not stmts:
            continue
        s0 = stmts[0]
        if not (isinstance(s0, Assign) and isinstance(s0.src, Var) and s0.src.kind == 'reg' and
                isinstance(s0.dst, Var) and s0.dst.kind == 'local' and s0.kind == 'let'):
            continue
        rk, xk = ekey(s0.src), ekey(s0.dst)
        if not s0.src.off.endswith('@%x' % b):
            continue
        ok = True
        # loop body of b: blocks dominated by b from which b can be reached again
        back = set()
        stk = [p for p, ss in succ.items() if b in ss and p != 'ROOT']
        while stk:
            v = stk.pop()
            if v in back or v == 'ROOT':
                continue
            back.add(v)
            stk.extend(p for p, ss in succ.items() if v in ss and p != 'ROOT')
        body = {v for v in back if v in idom and dominates(idom, b, v)} | {b}
        for b2, st2 in blocks.items():
            dom = b2 in idom and dominates(idom, b, b2)
            x_assigned_here = False
            for i, s in enumerate(st2):
                if s is s0:
                    continue
                reads_r = any(isinstance(x, Var) and ekey(x) == rk for e in iter_exprs_in(s) for x in e.walk())
                if reads_r and (not dom or (b2 not in body and x_assigned_here)):
                    ok = False
                if isinstance(s, Assign) and isinstance(s.dst, Var) and ekey(s.dst) == xk:
                    if b2 in body:
                        ok = False
                    x_assigned_here = True
            if any(isinstance(x, Var) and ekey(x) == rk for e in term_exprs(terms[b2]) for x in e.walk()) and not dom:
                ok = False
            if not ok:
                break
        if not ok:
            continue
        x = s0.dst
        merged.append((rk, xk))

        def fn(e, rk=rk, x=x):
            if isinstance(e, Var) and ekey(e) == rk:
                return Var(x.kind, x.off, x.size, x.ty, x.name)
            return None
        del stmts[0]
        for b2, st2 in blocks.items():
            for s in st2:
                if isinstance(s, Assign) and isinstance(s.dst, Var) and ekey(s.dst) == rk:
                    s.dst = Var(x.kind, x.off, x.size, x.ty, x.name)
                stmt_rewrite(s, fn)
            terms[b2] = term_rewrite(terms[b2], fn)
    return merged


# --------------------------------------------------------------------------- strength-reduced loops
def _stmt_read_exprs(s):
    """Expressions a statement reads, keeping a Deref destination whole (its base is an address)."""
    if isinstance(s, Assign):
        if not isinstance(s.dst, (Var, Temp)):
            yield s.dst
        yield s.src
    else:
        for e in s.exprs():
            if e is not None and isinstance(e, Expr):
                yield e


def _is_array_base(t):
    return isinstance(t, Var) and t.kind in ('field', 'global', 'local') or \
        isinstance(t, Deref) and t.off == 0xC


def _address_only(e, rk, need_base, memo):
    """True when every read of variable rk in e is a +1 term of a Deref address (an element offset)."""
    r = memo.get(id(e))
    if r is not None:
        return r
    ok = True
    if isinstance(e, (Var, Temp)) and ekey(e) == rk:
        ok = False
    elif isinstance(e, AddrOf) and isinstance(e.e, (Var, Temp)) and ekey(e.e) == rk:
        ok = False
    elif isinstance(e, Deref) and (lin := linear(e.base)) is not None and rk in lin[0]:
        terms = lin[0]
        if terms[rk][1] != 1:
            ok = False
        elif need_base and not any(_is_array_base(t) and c == 1 for k, (t, c) in terms.items() if k != rk):
            ok = False
        else:
            ok = all(_address_only(t, rk, need_base, memo) for k, (t, c) in terms.items() if k != rk)
    else:
        ok = all(_address_only(c, rk, need_base, memo) for c in e.children() if isinstance(c, Expr))
    memo[id(e)] = ok
    return ok


def _mentions(e, rk):
    return any(isinstance(x, (Var, Temp)) and ekey(x) == rk for x in e.walk())


def undo_strength_reduction(blocks, terms, new_var):
    """Loops that walk an array with a byte offset or pointer (`p = base + 48 ... p = p + 48`, every read
    `Mem(p + c)`) are rewritten with an element index: an existing counter that moves in step with the
    pointer, or a new variable from new_var(). Returns True if anything changed."""
    succ = {b: term_succs(t) for b, t in terms.items()}
    preds = {b: [] for b in blocks}
    for b, ss in succ.items():
        for x in ss:
            if x in preds:
                preds[x].append(b)
    changed = False
    done = set()
    while True:
        defs = {}
        for b, stmts in blocks.items():
            for i, s in enumerate(stmts):
                if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind in ('reg', 'local'):
                    defs.setdefault(ekey(s.dst), []).append((b, i, s))
        assigned = set(defs)
        for b, stmts in blocks.items():
            for s in stmts:
                if isinstance(s, Assign) and isinstance(s.dst, Var):
                    assigned.add(ekey(s.dst))

        def init_step(rk):
            lst = defs.get(rk, [])
            if len(lst) != 2:
                return None
            init = step = None
            for b, i, s in lst:
                lin = linear(s.src)
                if lin is None:
                    return None
                if rk in lin[0]:
                    if lin[0][rk][1] != 1 or len(lin[0]) != 1 or step is not None:
                        return None
                    step = (b, i, s, lin[1])
                else:
                    if init is not None:
                        return None
                    init = (b, i, s, lin)
            if init is None or step is None:
                return None
            return init, step

        def pos_key(b, i):
            return b, i

        def adjacent(p1, p2, rk):
            """Statement positions p1, p2: nothing between them (in execution order) reads rk."""
            (b1, i1), (b2, i2) = p1, p2
            if b1 == b2:
                lo, hi = sorted((i1, i2))
                return not any(_mentions(e, rk) for s in blocks[b1][lo + 1:hi] for e in _stmt_read_exprs(s))
            for (x1, j1), (x2, j2) in ((p1, p2), (p2, p1)):
                if x2 in succ.get(x1, []) and preds.get(x2) == [x1] and \
                        not any(_mentions(e, rk) for s in blocks[x1][j1 + 1:] for e in _stmt_read_exprs(s)) and \
                        not any(_mentions(e, rk) for e in term_exprs(terms[x1])) and \
                        not any(_mentions(e, rk) for s in blocks[x2][:j2] for e in _stmt_read_exprs(s)):
                    return True
            return False

        found = None
        for rk in defs:
            if rk in done:
                continue
            r = init_step(rk)
            if r is None:
                continue
            (ib, ii, istmt, ilin), (sb, si, sstmt, S) = r
            if abs(S) < 2:
                continue
            iterms, c0 = ilin
            base = None
            if iterms:
                if len(iterms) != 1:
                    continue
                (t, c), = iterms.values()
                if c != 1 or not _is_array_base(t) or not isinstance(t, Var) or ekey(t) in assigned:
                    continue
                base = t
            memo = {}
            ok = True
            for b, stmts in blocks.items():
                for s in stmts:
                    if s is istmt or s is sstmt:
                        continue
                    if not all(_address_only(e, rk, base is None, memo) for e in _stmt_read_exprs(s)):
                        ok = False
                        break
                if not ok or not all(_address_only(e, rk, base is None, memo) for e in term_exprs(terms[b])):
                    ok = False
                    break
            if not ok:
                continue
            stride = abs(S)
            unit = 1 if S > 0 else -1
            k0, rem = divmod(c0, stride)
            # a counter that moves in step: C = c1 next to the init, C = C + unit next to the step
            counter = None
            for ck, lst in defs.items():
                if ck == rk:
                    continue
                cr = init_step(ck)
                if cr is None:
                    continue
                (cib, cii, cis, clin), (csb, csi, css, cS) = cr
                if cS != unit or clin[0] or cib != ib:
                    continue
                if not adjacent((cib, cii), (ib, ii), rk) or not adjacent((csb, csi), (sb, si), rk):
                    continue
                counter = (cis.dst, clin[1])
                break
            found = (rk, istmt, sstmt, base, stride, unit, k0, rem, counter)
            break
        if found is None:
            return changed
        rk, istmt, sstmt, base, stride, unit, k0, rem, counter = found
        done.add(rk)
        if counter is not None:
            cv, c1 = counter
            idx = Var(cv.kind, cv.off, cv.size, cv.ty, cv.name)
            d = k0 - c1
            index = idx if d == 0 else Bin('+', idx, Const(d)) if d > 0 else Bin('-', idx, Const(-d))
            for b in blocks:
                blocks[b] = [s for s in blocks[b] if s is not istmt and s is not sstmt]
        else:
            idx = new_var()
            index = idx
            istmt.dst, istmt.src = idx, Const(k0)
            sstmt.dst = idx
            sstmt.src = Bin('+' if unit > 0 else '-', Var(idx.kind, idx.off, idx.size, idx.ty, idx.name), Const(1))
        off = Bin('*', index, Const(stride))
        if rem:
            off = Bin('+', off, Const(rem))
        repl = Bin('+', base, off) if base is not None else off

        def fn(x, rk=rk, repl=repl):
            if isinstance(x, Var) and ekey(x) == rk:
                return repl
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)
            terms[b] = term_rewrite(terms[b], fn)
        changed = True


def propagate_base_copies(blocks):
    """`rCX = m_1C8` followed (in the same block) by `Mem32(rCX + rBX * 4)`: use the array variable
    itself in the address, so the element access can be recognised."""
    for b, stmts in blocks.items():
        for i, s in enumerate(stmts):
            if not (isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'reg' and
                    isinstance(s.src, Var) and s.src.kind in ('field', 'global')):
                continue
            rk, vk, src = ekey(s.dst), ekey(s.src), s.src

            def fn(x, rk=rk, src=src):
                if isinstance(x, Deref) and (lin := linear(x.base)) is not None and rk in lin[0] and \
                        lin[0][rk][1] == 1:
                    def sub(y):
                        if isinstance(y, Var) and ekey(y) == rk:
                            return Var(src.kind, src.off, src.size, src.ty, src.name)
                        return None
                    return Deref(rewrite(x.base, sub), x.off, x.size, x.ty)
                return None
            for s2 in stmts[i + 1:]:
                stmt_rewrite(s2, fn)
                if isinstance(s2, Assign) and isinstance(s2.dst, Var) and ekey(s2.dst) in (rk, vk):
                    break
                if any(isinstance(x, Call) for e in iter_exprs_in(s2) for x in e.walk()):
                    break


def unshare_array_tails(blocks, terms):
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
    """ReDim t(n) / t(0) = a ... t(n) = z / x = Array(<t>) / Erase t  ->  x = Array(a, ..., z)."""
    if terms is not None:
        unshare_array_tails(blocks, terms)
    for b, stmts in blocks.items():
        for si, s in enumerate(stmts):
            calls = [x for e in iter_exprs_in(s) for x in e.walk()
                     if isinstance(x, Call) and x.fn == 'Array' and getattr(x, 'items_of', None) is not None]
            if len(calls) != 1:
                continue
            call = calls[0]
            v = call.items_of
            k = ekey(v)
            redim = None
            for j in range(si - 1, -1, -1):
                t = stmts[j]
                if isinstance(t, FmtStmt) and t.fmt.startswith('ReDim {0}(') and t.args and \
                        isinstance(t.args[0], Var) and ekey(t.args[0]) == k:
                    redim = j
                    break
            if redim is None:
                continue
            r = stmts[redim]
            if len(r.args) != 3 or not (isinstance(r.args[1], Const) and r.args[1].v == 0) or \
                    not isinstance(r.args[2], Const):
                continue
            n = r.args[2].v
            items = {}
            drop = {redim}
            ok = True
            for j in range(redim + 1, si):
                t = stmts[j]
                if isinstance(t, Assign) and isinstance(t.dst, Index) and isinstance(t.dst.arr, Var) and \
                        ekey(t.dst.arr) == k and len(t.dst.idx) == 1 and isinstance(t.dst.idx[0], Const):
                    items[t.dst.idx[0].v] = t.src
                    drop.add(j)
                elif isinstance(t, FmtStmt) and t.fmt == 'Erase {0}' and isinstance(t.args[0], Var) and \
                        ekey(t.args[0]) == k and j > max(drop):
                    drop.add(j)
                elif any(isinstance(x, Var) and ekey(x) == k for e in iter_exprs_in(t) for x in e.walk()):
                    ok = False
                    break
            if not ok or sorted(items) != list(range(n + 1)):
                continue
            erase = None
            for j in range(si + 1, len(stmts)):
                t = stmts[j]
                if isinstance(t, FmtStmt) and t.fmt == 'Erase {0}' and isinstance(t.args[0], Var) and \
                        ekey(t.args[0]) == k:
                    erase = j
                    break
                if any(isinstance(x, Var) and ekey(x) == k for e in iter_exprs_in(t) for x in e.walk()):
                    break
            if erase is not None:
                drop.add(erase)
            call.args = [items[i] for i in range(n + 1)]
            call.items_of = None
            call.__dict__.pop('_k', None)
            blocks[b] = [t for j, t in enumerate(stmts) if j not in drop]
            return collapse_array_literals(blocks)


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
        s.src = substitute(s.src, ekey(v), w)[0]
        blocks[b].insert(blocks[b].index(s), Assign(w, v))
        changed = True
    return changed
