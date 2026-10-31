"""Control-flow structuring: blocks + terminators -> VB AST (If/Else, Do/Loop, For, Select Case, GoTo)."""
import sys

from ir import (Const, FConst, Str, Var, Bin, Un, Call, Assign, ExprStmt, Text, Comment, FmtStmt, Raw, render,
                render_stmt, ekey, same, Deref, Index, Member)
from lift import negate

sys.setrecursionlimit(20000)

VEXIT = 'EXIT'
VROOT = 'ROOT'


class Loop:
    def __init__(self, header):
        self.header = header
        self.body = {header}
        self.latches = set()
        self.follow = None
        self.kind = 'do'
        self.outer_stops = frozenset()   # join points of the code around the loop


def dominators(nodes, succ, root):
    """Iterative dominator computation (Cooper/Harvey/Kennedy)."""
    order = []
    seen = set()
    stack = [(root, iter(succ.get(root, ())))]
    seen.add(root)
    while stack:
        v, it = stack[-1]
        nxt = next(it, None)
        if nxt is None:
            stack.pop()
            order.append(v)
            continue
        if nxt not in seen and nxt in nodes:
            seen.add(nxt)
            stack.append((nxt, iter(succ.get(nxt, ()))))
    order.reverse()
    index = {v: i for i, v in enumerate(order)}
    preds = {v: [] for v in order}
    for v in order:
        for s in succ.get(v, ()):
            if s in index:
                preds[s].append(v)
    idom = {root: root}

    def intersect(a, b):
        while a != b:
            while index[a] > index[b]:
                a = idom[a]
            while index[b] > index[a]:
                b = idom[b]
        return a
    changed = True
    while changed:
        changed = False
        for v in order[1:]:
            ps = [p for p in preds[v] if p in idom]
            if not ps:
                continue
            new = ps[0]
            for p in ps[1:]:
                new = intersect(p, new)
            if idom.get(v) != new:
                idom[v] = new
                changed = True
    return idom


def dominates(idom, a, b):
    """True if a dominates b."""
    while True:
        if a == b:
            return True
        p = idom.get(b)
        if p is None or p == b:
            return False
        b = p


class Structurer:
    def __init__(self, blocks, terms, entry, roots, is_function=False, labels=None):
        self.blocks = blocks
        self.terms = terms
        self.entry = entry
        self.roots = [r for r in roots if r in blocks and r != entry]
        self.is_function = is_function
        self.done = set()
        self.need_label = set(self.roots)
        self.extra_labels = labels or {}
        self.succ = {}
        for b, t in terms.items():
            if b not in blocks:
                continue
            if t[0] == 'goto':
                self.succ[b] = [t[1]]
            elif t[0] == 'cond':
                self.succ[b] = [t[2], t[3]]
            elif t[0] == 'switch':
                self.succ[b] = list(dict.fromkeys(t[2]))
            else:
                self.succ[b] = []
        self.nodes = set(blocks)
        for b in list(self.succ):
            self.succ[b] = [s for s in self.succ[b] if s in self.nodes]
        self.rets = {b for b, t in terms.items() if t[0] == 'ret' and b in blocks and
                     len([s for s in blocks[b] if not isinstance(s, Comment)]) <= 2}
        # dominators from a virtual root
        succ_r = dict(self.succ)
        succ_r[VROOT] = [entry] + self.roots
        self.idom = dominators(self.nodes | {VROOT}, succ_r, VROOT)
        # post-dominators: reversed graph; edges into return blocks go straight to the virtual exit
        rsucc = {VEXIT: []}
        for b in self.nodes:
            outs = self.succ.get(b, [])
            real = [s for s in outs if s not in self.rets]
            if not real or len(real) < len(outs):
                rsucc.setdefault(VEXIT, []).append(b)
            for s in real:
                rsucc.setdefault(s, []).append(b)
        self.ipdom = dominators(self.nodes | {VEXIT}, rsucc, VEXIT)
        self.loops = {}
        self.find_loops()

    # ---------------------------------------------------------------- loops
    def find_loops(self):
        for u in self.nodes:
            for h in self.succ.get(u, []):
                if h in self.idom and u in self.idom and dominates(self.idom, h, u):
                    L = self.loops.get(h) or Loop(h)
                    self.loops[h] = L
                    L.latches.add(u)
                    # natural loop body
                    stack = [u]
                    while stack:
                        v = stack.pop()
                        if v in L.body:
                            continue
                        L.body.add(v)
                        for p in self.preds(v):
                            if p not in L.body:
                                stack.append(p)
        for h, L in self.loops.items():
            exits = []
            for v in L.body:
                for s in self.succ.get(v, []):
                    if s not in L.body and s not in self.rets:
                        exits.append(s)
            if not exits:
                rexits = [s for v in L.body for s in self.succ.get(v, []) if s not in L.body and s in self.rets]
                if len(set(rexits)) == 1:
                    L.follow = rexits[0]
                continue
            if exits:
                cand = sorted(set(exits), key=lambda x: (-exits.count(x), x))
                # prefer an exit that post-dominates the header
                p = self.ipdom.get(h)
                while p is not None and p != VEXIT and p in L.body:
                    p = self.ipdom.get(p)
                if p in cand:
                    L.follow = p
                elif p is not None and p != VEXIT:
                    # the exits run some code of their own, then meet (p post-dominates the header, so
                    # every exit path passes it): the loop ends there, and the code on an exit path is
                    # emitted inside the loop before its Exit Do
                    L.follow = p
                else:
                    L.follow = cand[0]

    def preds(self, v):
        if not hasattr(self, '_preds'):
            self._preds = {}
            for b, ss in self.succ.items():
                for s in ss:
                    self._preds.setdefault(s, []).append(b)
        return self._preds.get(v, [])

    # ---------------------------------------------------------------- reachability
    def reach(self, start, stops):
        seen = set()
        stack = [start]
        while stack:
            v = stack.pop()
            if v in seen or v in stops or v in self.rets or v not in self.nodes:
                continue
            seen.add(v)
            stack.extend(self.succ.get(v, []))
        return seen

    def follow_of(self, n, loops):
        f = self.ipdom.get(n)
        if f is None or f == VEXIT:
            return None
        if loops and f not in loops[-1].body and f != loops[-1].follow:
            return None
        return f

    # ---------------------------------------------------------------- emission
    def run(self):
        ast = self.region(self.entry, frozenset(), [])
        for r in sorted(self.roots):
            if r in self.done:
                continue
            ast.append(('handler', r))
            ast.extend(self.region(r, frozenset(), []))
        return ast

    def jump(self, n, stops, loops):
        """AST for a jump to n that is not structurally inlined (or None to just stop)."""
        if loops:
            L = loops[-1]
            if n == L.follow:
                return ('exitloop', L)
            if n == L.header:
                return ('continue', L)
            for outer in loops[:-1]:
                if n == outer.follow or n == outer.header:
                    self.need_label.add(n)
                    return ('goto', n)
            if n in L.outer_stops:
                self.need_label.add(n)      # leaving the loop for a join point of the code around it
                return ('goto', n)
        if n in stops:
            return None
        if n in self.rets:
            return None
        self.need_label.add(n)
        return ('goto', n)

    def small_dup_ok(self, n, loops):
        """A short straight-line block may be repeated instead of jumped to (shared Case tails)."""
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
        while n is not None:
            if loops:
                L = loops[-1]
                if n == L.follow:
                    out.append(('exitloop', L))
                    break
                if n == L.header and n in self.done:
                    out.append(('continue', L))
                    break
                stop_outer = False
                for outer in loops[:-1]:
                    if n == outer.follow or n == outer.header:
                        self.need_label.add(n)
                        out.append(('goto', n))
                        stop_outer = True
                        break
                if stop_outer:
                    break
            if n in stops:
                if loops and n in loops[-1].outer_stops and n != loops[-1].follow:
                    # a join point outside the loop: reaching it leaves the loop (falling through
                    # would run the loop again)
                    self.need_label.add(n)
                    out.append(('goto', n))
                break
            if n in self.rets:
                out.append(('anchor', n))
                out.extend(('stmt', s) for s in self.blocks[n])
                out.append(('return', n))
                break
            if n in self.done:
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
            if n in self.loops and not (loops and loops[-1].header == n):
                L = self.loops[n]
                L.outer_stops = frozenset(stops)
                body = self.region(n, stops | ({L.follow} if L.follow else set()), loops + [L])
                out.append(('loop', L, body))
                if L.follow is None:
                    break
                n = L.follow
                if n in stops:
                    break
                continue
            self.done.add(n)
            out.append(('anchor', n))
            out.extend(('stmt', s) for s in self.blocks[n])
            t = self.terms[n]
            if t[0] == 'goto':
                n = t[1]
                continue
            if t[0] == 'ret':
                out.append(('return', n))
                break
            if t[0] in ('stop',):
                break
            if t[0] == 'cond':
                cond, a, b = t[1], t[2], t[3]
                n = self.emit_cond(out, n, cond, a, b, stops, loops)
                continue
            if t[0] == 'switch':
                n = self.emit_switch(out, n, t, stops, loops)
                continue
            break
        return out

    def is_exit_target(self, x, loops):
        if x in self.rets:
            return True
        if loops and (x == loops[-1].follow or x == loops[-1].header):
            return True
        return False

    def emit_cond(self, out, n, cond, a, b, stops, loops):
        if a == b:
            return a
        # a branch that does nothing and continues with the other one (e.g. `If App Is Nothing`)
        for x, y in ((a, b), (b, a)):
            if x in self.blocks and not self.blocks[x] and self.succ.get(x) == [y] and x not in self.loops \
                    and len(self.preds(x)) == 1:
                self.done.add(x)
                return y
        # branches that leave the loop / continue it / return
        for x, y, c in ((a, b, cond), (b, a, negate(cond))):
            if self.is_exit_target(x, loops) or x in stops or \
                    (x in self.done and not (loops and x == loops[-1].header)):
                if x in self.rets:
                    j = self.region(x, stops, loops)
                else:
                    jj = self.jump(x, stops, loops)
                    if jj is None:
                        out.append(('if', negate(c), self.region(y, stops, loops), []))
                        return x
                    j = [jj]
                out.append(('if', c, j, []))
                return y
        f = self.follow_of(n, loops)
        if f is not None:
            # (f may also be the join point of an enclosing If: the caller stops there)
            if f == b:
                then = self.region(a, stops | {f}, loops)
                out.append(('if', cond, then, []))
                return f
            if f == a:
                then = self.region(b, stops | {f}, loops)
                out.append(('if', negate(cond), then, []))
                return f
            then = self.region(a, stops | {f}, loops)
            els = self.region(b, stops | {f}, loops)
            out.append(('if', cond, then, els))
            return f
        # no merge point: early-exit style if one side is self-contained (never reaches a stop / join)
        ra = self.reach(a, stops)
        rb = self.reach(b, stops)

        def leaks(r):
            return any(s in stops for v in r for s in self.succ.get(v, []))
        if not (ra & rb) and not (leaks(ra) and leaks(rb)):
            if leaks(ra) and not leaks(rb):
                out.append(('if', negate(cond), self.region(b, stops, loops), []))
                return a
            if leaks(rb) and not leaks(ra):
                out.append(('if', cond, self.region(a, stops, loops), []))
                return b
            if len(ra) <= len(rb):
                out.append(('if', cond, self.region(a, stops, loops), []))
                return b
            out.append(('if', negate(cond), self.region(b, stops, loops), []))
            return a
        then = self.region(a, stops, loops)
        els = self.region(b, stops, loops)
        out.append(('if', cond, then, els))
        return None

    def emit_switch(self, out, n, t, stops, loops):
        idx, targets = t[1], t[2]
        uniq = list(dict.fromkeys(targets))
        f = self.follow_of(n, loops)
        cases = []
        for tg in uniq:
            vals = [i for i, x in enumerate(targets) if x == tg]
            if f is not None and tg == f:
                cases.append((vals, []))
                continue
            body = self.region(tg, stops | ({f} if f else set()), loops)
            cases.append((vals, body))
        out.append(('select', idx, cases))
        return f


# ----------------------------------------------------------------------------- AST cleanup
def is_terminal(node):
    return node and node[0] in ('return', 'exitloop', 'goto', 'continue')


def has_anchor(node):
    """Does this AST node contain a label (anything a jump can land on)?"""
    if not isinstance(node, tuple) or not node:
        return False
    if node[0] in ('anchor', 'handler'):
        return True
    for part in node[1:]:
        if isinstance(part, list) and any(has_anchor(x) for x in part):
            return True
        if isinstance(part, tuple) and has_anchor(part):
            return True
    return False


def ends_flow(node):
    """Control never continues past this node (a jump, or an If whose branches all jump)."""
    if is_terminal(node):
        return True
    if node and node[0] == 'if':
        return bool(node[2]) and bool(node[3]) and ends_flow(node[2][-1]) and ends_flow(node[3][-1])
    return False


KEEP_ANCHORS = set()


def tidy(ast, in_loop=None, body_end=False):
    """Recursive pretty transformations on the AST.  body_end: this list ends a loop body."""
    out = []
    for node in ast:
        k = node[0]
        if k == 'nop':
            continue
        if k == 'anchor' and node[1] not in KEEP_ANCHORS:
            continue
        if k == 'if':
            _, c, th, el = node
            th = tidy(th, in_loop)
            el = tidy(el, in_loop)
            # identical trailing statements in both branches -> after the If
            tail = []
            while th and el and th[-1][0] == 'stmt' and el[-1][0] == 'stmt' and \
                    render_stmt(th[-1][1]) == render_stmt(el[-1][1]):
                tail.insert(0, th.pop())
                el.pop()
            if tail:
                if th or el:
                    out.append(('if', c, th, el) if th else ('if', negate(c), el, []))
                out.extend(tail)
                continue
            if len(th) == 1 and th[0][0] == 'select' and not el and is_bounds_check(c, th[0][1]):
                out.append(th[0])          # jump-table bounds check
                continue
            if not th and el:
                c, th, el = negate(c), el, []
            if not th and not el:
                continue
            if isinstance(c, Const):
                out.extend(th if c.v else el)
                continue
            out.append(('if', c, th, el))
            continue
        if k == 'loop':
            _, L, body = node
            body = tidy(body, L, body_end=True)
            body = invert_continues(body, L)
            out.extend(make_loop(L, body))
            continue
        if k == 'select':
            _, idx, cases = node
            out.append(('select', idx, [(v, tidy(b, in_loop)) for v, b in cases]))
            continue
        out.append(node)
    # code after a jump is never run, up to the next label
    live = []
    dead = False
    for node in out:
        if dead and node[0] != 'handler' and not has_anchor(node):  # labels / handlers are reached by jumps
            continue
        dead = False
        live.append(node)
        dead = ends_flow(node)
    out = live
    # drop trailing 'continue' (natural end of a loop body), also at the end of a final If's branches
    if body_end and in_loop is not None:
        strip_tail_continue(out, in_loop)
    return out


def strip_tail_continue(nodes, L):
    while nodes and nodes[-1][0] == 'continue' and nodes[-1][1] is L:
        nodes.pop()
    if not nodes:
        return
    last = nodes[-1]
    if last[0] == 'if':
        strip_tail_continue(last[2], L)
        strip_tail_continue(last[3], L)
        if not last[2] and not last[3]:
            nodes.pop()
        elif not last[2]:
            nodes[-1] = ('if', negate(last[1]), last[3], [])
    elif last[0] == 'select':
        for vals, body in last[2]:
            strip_tail_continue(body, L)


def is_bounds_check(c, idx):
    if isinstance(c, Bin) and c.op in ('<=', '<') and lin_same(c.a, idx):
        return True
    if isinstance(c, Bin) and c.op == 'And':
        return is_bounds_check(c.a, idx) and is_bounds_check(c.b, idx) or \
            (is_lower(c.a, idx) and is_bounds_check(c.b, idx)) or (is_lower(c.b, idx) and is_bounds_check(c.a, idx))
    return False


def is_lower(c, idx):
    return isinstance(c, Bin) and c.op in ('>=', '>') and lin_same(c.a, idx)


def lin_same(a, b):
    from simplify import linear
    if same(a, b):
        return True
    la, lb = linear(a), linear(b)
    if la is None or lb is None:
        return False
    ka = {k: c for k, (t, c) in la[0].items()}
    kb = {k: c for k, (t, c) in lb[0].items()}
    return ka == kb


def invert_continues(body, L):
    """`If c Then <continue>` followed by the rest of the loop body -> `If Not c Then <rest>`."""
    for i, node in enumerate(body):
        if node[0] == 'if' and not node[3] and len(node[2]) == 1 and node[2][0][0] == 'continue' and \
                node[2][0][1] is L and i + 1 < len(body):
            rest = body[i + 1:]
            if any(x[0] == 'anchor' for x in rest):
                continue
            rest = invert_continues(rest, L)
            return body[:i] + [('if', negate(node[1]), rest, [])]
    return body


def strip_anchors(body):
    """(anchors list, remaining body) for pattern matching at the start of a body."""
    anchors = []
    i = 0
    while i < len(body) and body[i][0] == 'anchor':
        anchors.append(body[i])
        i += 1
    return anchors, body[i:]


def make_loop(L, body):
    anchors, rest = strip_anchors(body)
    # Do While: first statement is "If c Then Exit Do"
    if rest and rest[0][0] == 'if' and not rest[0][3] and len(rest[0][2]) == 1 and \
            rest[0][2][0][0] == 'exitloop' and rest[0][2][0][1] is L:
        c = rest[0][1]
        inner = rest[1:]
        return [('while', L, negate(c), anchors + inner)]
    # Loop Until: last statement is "If c Then Exit Do" ... preceded by body
    if rest and rest[-1][0] == 'if' and not rest[-1][3] and len(rest[-1][2]) == 1 and \
            rest[-1][2][0][0] == 'exitloop' and rest[-1][2][0][1] is L:
        return [('dountil', L, rest[-1][1], anchors + rest[:-1])]
    # Loop While: last is "If c Then continue"
    if rest and rest[-1][0] == 'if' and not rest[-1][3] and len(rest[-1][2]) == 1 and \
            rest[-1][2][0][0] == 'continue' and rest[-1][2][0][1] is L:
        return [('dowhile_end', L, rest[-1][1], anchors + rest[:-1])]
    return [('do', L, body)]


def hoist_tails(ast):
    """Identical trailing statements of both If branches -> after the If (all nesting levels)."""
    out = []
    for node in ast:
        k = node[0]
        if k == 'if':
            _, c, th, el = node
            th, el = hoist_tails(th), hoist_tails(el)
            tail = []
            while th and el and th[-1][0] == 'stmt' and el[-1][0] == 'stmt' and \
                    render_stmt(th[-1][1]) == render_stmt(el[-1][1]):
                tail.insert(0, th.pop())
                el.pop()
            if th or el:
                out.append(('if', c, th, el) if th else ('if', negate(c), el, []))
            out.extend(tail)
            continue
        if k in ('do', 'while', 'dountil', 'dowhile_end'):
            out.append(node[:-1] + (hoist_tails(node[-1]),))
            continue
        if k == 'select':
            out.append(('select', node[1], [(v, hoist_tails(b)) for v, b in node[2]]))
            continue
        out.append(node)
    return out


def jumps_to(nodes, target):
    """Number of GoTo / loop-continue jumps to block `target` in these AST nodes (any depth)."""
    n = 0
    for node in nodes:
        if not isinstance(node, tuple) or not node or not isinstance(node[0], str):
            continue
        if node[0] == 'goto' and node[1] == target:
            n += 1
        if node[0] == 'continue' and getattr(node[1], 'header', None) == target:
            n += 1
        for part in node[1:]:
            if isinstance(part, list):
                n += jumps_to(part, target)
                for p in part:
                    if isinstance(p, tuple) and len(p) == 2 and isinstance(p[1], list):
                        n += jumps_to(p[1], target)
    return n


def make_for_loops(ast, root=None):
    """`i = a` + `Do While i <= b ... i = i + s Loop` -> For i = a To b [Step s]."""
    root = ast if root is None else root
    out = []
    for node in ast:
        k = node[0]
        if k in ('if',):
            out.append(('if', node[1], make_for_loops(node[2], root), make_for_loops(node[3], root)))
            continue
        if k == 'select':
            out.append(('select', node[1], [(v, make_for_loops(b, root)) for v, b in node[2]]))
            continue
        if k in ('do', 'dountil', 'dowhile_end'):
            out.append(node[:-1] + (make_for_loops(node[-1], root),))
            continue
        if k == 'while':
            _, L, c, body = node
            body = make_for_loops(body, root)
            # a jump to the loop's top from outside it would enter a For loop without its start
            f = None if jumps_to(root, L.header) > jumps_to(body, L.header) else match_for(out, c, body, L)
            if f:
                var, start, end, step, newbody = f
                # remove the init statement from out
                out.pop()
                while out and out[-1][0] == 'anchor':
                    break
                out.append(('for', L, var, start, end, step, newbody))
                continue
            out.append(('while', L, c, body))
            continue
        out.append(node)
    return out


def assigns_var(nodes, v):
    """Does any statement in these AST nodes (at any depth) assign v or pass it to a procedure?"""
    for node in nodes:
        if not isinstance(node, tuple) or not node:
            continue
        if node[0] == 'stmt':
            s = node[1]
            if isinstance(s, Assign) and same(s.dst, v):
                return True
            for e in (s.exprs() if hasattr(s, 'exprs') else ()):
                if e is None:
                    continue
                for x in e.walk():
                    if isinstance(x, Call) and x.kind == 'proc' and any(same(a, v) for a in x.args):
                        return True
            continue
        for part in node[1:]:
            if isinstance(part, list) and assigns_var([p for p in part if isinstance(p, tuple)], v):
                return True
            if isinstance(part, list):
                for p in part:
                    if isinstance(p, tuple) and len(p) == 2 and isinstance(p[1], list) and assigns_var(p[1], v):
                        return True
    return False


def has_calls(nodes):
    for node in nodes:
        if not isinstance(node, tuple) or not node:
            continue
        if node[0] == 'stmt':
            s = node[1]
            for e in (s.exprs() if hasattr(s, 'exprs') else ()):
                if e is not None and any(isinstance(x, Call) and x.side for x in e.walk()):
                    return True
            if isinstance(s, (FmtStmt, Text)):
                return True
            continue
        for part in node[1:]:
            if isinstance(part, list):
                if has_calls([p for p in part if isinstance(p, tuple) and p and isinstance(p[0], str)]):
                    return True
                for p in part:
                    if isinstance(p, tuple) and len(p) == 2 and isinstance(p[1], list) and has_calls(p[1]):
                        return True
    return False


def match_for(prev, c, body, L=None):
    # init: last real statement before the loop (optionally followed by `bound = expr`)
    j = len(prev) - 1
    while j >= 0 and prev[j][0] == 'anchor':
        j -= 1
    if j < 0 or prev[j][0] != 'stmt' or not isinstance(prev[j][1], Assign):
        return None
    bound_stmt = None
    if isinstance(c, Bin) and isinstance(c.b, Var) and c.b.kind == 'local' and same(prev[j][1].dst, c.b) \
            and j >= 1 and prev[j - 1][0] == 'stmt' and isinstance(prev[j - 1][1], Assign):
        bound_stmt = prev[j][1]
        j -= 1
    init = prev[j][1]
    var = init.dst
    if not isinstance(var, Var):
        return None
    if not (isinstance(c, Bin) and c.op in ('<=', '>=') and same(c.a, var)):
        return None
    if bound_stmt is not None:
        if any(uses_var(n, bound_stmt.dst) for n in body):
            return None
    # increment: last statement of body
    k = len(body) - 1
    while k >= 0 and body[k][0] == 'anchor':
        k -= 1
    if k < 0 or body[k][0] != 'stmt' or not isinstance(body[k][1], Assign):
        return None
    inc = body[k][1]
    if not same(inc.dst, var) or not isinstance(inc.src, Bin) or inc.src.op not in ('+', '-') or \
            not same(inc.src.a, var) or not isinstance(inc.src.b, (Const, FConst)):
        return None
    step = inc.src.b.v if inc.src.op == '+' else -inc.src.b.v
    if (step > 0) != (c.op == '<='):
        return None
    # the counter must not be assigned elsewhere in the body
    for node in body[:k]:
        if node[0] == 'stmt' and isinstance(node[1], Assign) and same(node[1].dst, var):
            return None
    newbody = body[:k] + body[k + 1:]
    end = bound_stmt.src if bound_stmt is not None else c.b
    # a For loop is entered only at its top, and fixes its end value when it starts: the Do loop must
    # have no jump into its body, and (unless the compiler saved the end in a temporary) an end that the
    # body cannot change
    if L is not None and any(has_anchor(n) and not (n[0] == 'anchor' and n[1] == L.header) for n in body):
        return None
    if assigns_var(newbody, var):
        return None
    if bound_stmt is None and not isinstance(end, (Const, FConst)):
        for x in end.walk():
            if isinstance(x, Var) and (assigns_var(newbody, x) or x.kind not in ('local', 'reg', 'param')
                                       and has_calls(newbody)):
                return None
            if isinstance(x, (Call, Deref, Index, Member)) and not isinstance(x, Var):
                if isinstance(x, Call) or has_calls(newbody):
                    return None
    if bound_stmt is not None:
        prev.pop()          # the bound temp assignment (init is popped by the caller)
    return var, init.src, end, step, newbody


def uses_var(node, v):
    from simplify import iter_exprs_in
    k = node[0]
    if k == 'stmt':
        return any(isinstance(x, Var) and same(x, v) for e in iter_exprs_in(node[1]) for x in e.walk()) or \
            (isinstance(node[1], Assign) and same(node[1].dst, v))
    if k == 'if':
        return any(isinstance(x, Var) and same(x, v) for x in node[1].walk()) or \
            any(uses_var(n, v) for n in node[2] + node[3])
    for part in node[1:]:
        if isinstance(part, list):
            if any(uses_var(n, v) for n in part if isinstance(n, tuple)):
                return True
    return False


def case_subject(c):
    """(subject, [values]) if c is `X = k` or `X = a Or X = b`."""
    if isinstance(c, Bin) and c.op == '=' and isinstance(c.b, (Const, Str)):
        return c.a, [c.b]
    if isinstance(c, Bin) and c.op == 'Or':
        a, b = case_subject(c.a), case_subject(c.b)
        if a and b and same(a[0], b[0]):
            return a[0], a[1] + b[1]
    return None


def make_select(chain, el):
    if len(chain) < 3:
        return None
    subj = None
    cases = []
    for c, body in chain:
        cs = case_subject(c)
        if cs is None or (subj is not None and not same(cs[0], subj)):
            return None
        subj = cs[0]
        cases.append((cs[1], body))
    return ('selectcase', subj, cases, el)


def make_elseif(ast):
    out = []
    for node in ast:
        k = node[0]
        if k == 'if':
            _, c, th, el = node
            th = make_elseif(th)
            el = make_elseif(el)
            chain = [(c, th)]
            while True:
                a, rest = strip_anchors(el)
                if len(rest) == 1 and rest[0][0] == 'if' and not a:
                    _, c2, th2, el2 = rest[0]
                    chain.append((c2, th2))
                    el = el2
                    continue
                if len(rest) == 1 and rest[0][0] == 'ifchain' and not a:
                    chain.extend(rest[0][1])
                    el = rest[0][2]
                    continue
                break
            sel = make_select(chain, el)
            if sel is not None:
                out.append(sel)
            elif len(chain) > 1:
                out.append(('ifchain', chain, el))
            else:
                out.append(('if', c, th, el))
            continue
        if k in ('do', 'while', 'dountil', 'dowhile_end'):
            out.append(node[:-1] + (make_elseif(node[-1]),))
            continue
        if k == 'for':
            out.append(node[:-1] + (make_elseif(node[-1]),))
            continue
        if k == 'select':
            out.append(('select', node[1], [(v, make_elseif(b)) for v, b in node[2]]))
            continue
        out.append(node)
    return out


# ----------------------------------------------------------------------------- rendering
class Renderer:
    def __init__(self, need_label, exit_kw='Sub', label_name=None):
        self.need_label = need_label
        self.exit_kw = exit_kw
        self.lines = []
        self.label_name = label_name or (lambda n: 'L_%X' % n if n < 0x70000000 else 'L_E%d' % (n - 0x70000000))
        self.loop_kinds = []

    def emit(self, depth, text):
        self.lines.append('    ' * depth + text)

    def render(self, ast, depth=1, top=True):
        n = len(ast)
        for i, node in enumerate(ast):
            k = node[0]
            last = top and all(x[0] in ('anchor',) for x in ast[i + 1:])
            if k == 'anchor':
                if node[1] in self.need_label:
                    self.lines.append('%s:' % self.label_name(node[1]))
                continue
            if k == 'handler':
                if not (self.lines and self.lines[-1].strip() in ('Exit %s' % self.exit_kw,)) and \
                        not self.ends_terminal():
                    self.emit(depth, 'Exit %s' % self.exit_kw)
                continue
            if k == 'stmt':
                self.emit(depth, render_stmt(node[1]))
                continue
            if k == 'return':
                if not last:
                    self.emit(depth, 'Exit %s' % self.exit_kw)
                continue
            if k == 'goto':
                self.emit(depth, 'GoTo %s' % self.label_name(node[1]))
                continue
            if k == 'exitloop':
                kind = self.loop_kind(node[1])
                self.emit(depth, 'Exit %s' % ('For' if kind == 'for' else 'Do'))
                continue
            if k == 'continue':
                self.need_label.add(node[1].header)
                self.emit(depth, 'GoTo %s' % self.label_name(node[1].header))
                continue
            if k == 'if':
                _, c, th, el = node
                self.render_if([(c, th)], el, depth)
                continue
            if k == 'ifchain':
                self.render_if(node[1], node[2], depth)
                continue
            if k == 'do':
                self.with_loop(node[1], 'do', lambda: (self.emit(depth, 'Do'), self.render(node[2], depth + 1, False),
                                                        self.emit(depth, 'Loop')))
                continue
            if k == 'while':
                self.with_loop(node[1], 'do', lambda: (self.emit(depth, 'Do While %s' % render(node[2])),
                                                        self.render(node[3], depth + 1, False),
                                                        self.emit(depth, 'Loop')))
                continue
            if k == 'dountil':
                self.with_loop(node[1], 'do', lambda: (self.emit(depth, 'Do'), self.render(node[3], depth + 1, False),
                                                        self.emit(depth, 'Loop Until %s' % render(node[2]))))
                continue
            if k == 'dowhile_end':
                self.with_loop(node[1], 'do', lambda: (self.emit(depth, 'Do'), self.render(node[3], depth + 1, False),
                                                        self.emit(depth, 'Loop While %s' % render(node[2]))))
                continue
            if k == 'for':
                _, L, var, start, end, step, body = node
                st = '' if step == 1 else ' Step %s' % (int(step) if float(step) == int(step) else step)
                self.with_loop(L, 'for', lambda: (
                    self.emit(depth, 'For %s = %s To %s%s' % (render(var), render(start), render(end), st)),
                    self.render(body, depth + 1, False),
                    self.emit(depth, 'Next')))
                continue
            if k == 'select':
                _, idx, cases = node
                subj, off = switch_subject(idx)
                self.emit(depth, 'Select Case %s' % render(subj))
                for vals, body in cases:
                    if not [x for x in body if x[0] != 'anchor']:
                        continue
                    self.emit(depth + 1, 'Case %s' % ', '.join(str(v - off) for v in vals))
                    self.render(body, depth + 2, False)
                self.emit(depth, 'End Select')
                continue
            if k == 'selectcase':
                _, subj, cases, el = node
                self.emit(depth, 'Select Case %s' % render(subj))
                for vals, body in cases:
                    self.emit(depth + 1, 'Case %s' % ', '.join(render(v) for v in vals))
                    self.render(body, depth + 2, False)
                if el:
                    self.emit(depth + 1, 'Case Else')
                    self.render(el, depth + 2, False)
                self.emit(depth, 'End Select')
                continue
            self.emit(depth, "' <%s>" % k)

    def ends_terminal(self):
        for l in reversed(self.lines):
            s = l.strip()
            if not s:
                continue
            return s.startswith('GoTo ') or s.startswith('Exit ') or s == 'End'
        return False

    def with_loop(self, L, kind, fn):
        self.loop_kinds.append((L, kind))
        fn()
        self.loop_kinds.pop()

    def loop_kind(self, L):
        for l, k in reversed(self.loop_kinds):
            if l is L:
                return k
        return 'do'

    def render_if(self, chain, el, depth):
        # single-line If for one simple jump/statement
        if len(chain) == 1 and not el:
            c, th = chain[0]
            body = [x for x in th if x[0] != 'anchor']
            if len(body) == 1 and body[0][0] in ('stmt', 'return', 'goto', 'exitloop') and \
                    not any(x[0] == 'anchor' and x[1] in self.need_label for x in th):
                save = self.lines
                self.lines = []
                self.render(body, 0, False)
                inner = self.lines
                self.lines = save
                if len(inner) == 1 and len(inner[0]) < 90:
                    self.emit(depth, 'If %s Then %s' % (render(c), inner[0].strip()))
                    return
        for i, (c, th) in enumerate(chain):
            self.emit(depth, '%s %s Then' % ('If' if i == 0 else 'ElseIf', render(c)))
            self.render(th, depth + 1, False)
        if el:
            self.emit(depth, 'Else')
            self.render(el, depth + 1, False)
        self.emit(depth, 'End If')


def switch_subject(idx):
    """Jump-table index `subject + c` -> (subject, c) so case labels can be shown as subject values."""
    from simplify import linear
    l = linear(idx)
    if l is not None:
        terms, c = l
        if len(terms) == 1:
            t, coef = list(terms.values())[0]
            if coef == 1:
                return t, c
    return idx, 0


LOST = []          # (entry, number of statements the structurer did not place) - a decompiler bug if any


def placed_statements(ast, out):
    for node in ast:
        if node[0] == 'stmt':
            out.add(id(node[1]))
        elif node[0] == 'if':
            placed_statements(node[2], out)
            placed_statements(node[3], out)
        elif node[0] == 'loop':
            placed_statements(node[2], out)
        elif node[0] == 'select':
            for _, body in node[2]:
                placed_statements(body, out)
    return out


def structure(blocks, terms, entry, roots, exit_kw='Sub'):
    s = Structurer(blocks, terms, entry, roots)
    ast = s.run()
    placed = placed_statements(ast, set())
    reach = s.reach(entry, frozenset()) | {r2 for r in roots for r2 in s.reach(r, frozenset())}
    lost = [st for b in reach for st in blocks.get(b, ()) if id(st) not in placed]
    if lost:
        LOST.append((entry, len(lost)))
    KEEP_ANCHORS.clear()
    KEEP_ANCHORS.update(s.need_label)
    KEEP_ANCHORS.update(s.loops)
    ast = tidy(ast)
    ast = hoist_tails(ast)
    ast = make_for_loops(ast)
    ast = make_elseif(ast)
    r = Renderer(set(s.need_label), exit_kw)
    r.render(ast)
    # labels referenced by tidy-generated gotos may appear after rendering began; re-render if needed
    if r.need_label - s.need_label:
        r2 = Renderer(set(r.need_label), exit_kw)
        r2.render(ast)
        return r2.lines
    return r.lines
