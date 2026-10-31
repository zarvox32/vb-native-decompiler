import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('structure.py', [(
"""        f = self.follow_of(n, loops)
        if f is not None and f not in stops:
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
        # no merge point: early-exit style if one side is self-contained
        ra = self.reach(a, stops)
        rb = self.reach(b, stops)
        if not (ra & rb):""",
"""        f = self.follow_of(n, loops)
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
                return b"""), (
"""        if k == 'if':
            _, c, th, el = node
            th = tidy(th, in_loop)
            el = tidy(el, in_loop)""",
"""        if k == 'if':
            _, c, th, el = node
            th = tidy(th, in_loop)
            el = tidy(el, in_loop)
            # identical trailing statements in both branches -> after the If
            tail = []
            while th and el and th[-1][0] == 'stmt' and el[-1][0] == 'stmt' and \\
                    render_stmt(th[-1][1]) == render_stmt(el[-1][1]):
                tail.insert(0, th.pop())
                el.pop()
            if tail:
                if th or el:
                    out.append(('if', c, th, el) if th else ('if', negate(c), el, []))
                out.extend(tail)
                continue"""), (
"""def match_for(prev, c, body):
    # init: last real statement before the loop
    j = len(prev) - 1
    while j >= 0 and prev[j][0] == 'anchor':
        j -= 1
    if j < 0 or prev[j][0] != 'stmt' or not isinstance(prev[j][1], Assign) or j != len(prev) - 1:
        return None
    init = prev[j][1]
    var = init.dst
    if not isinstance(var, Var):
        return None
    if not (isinstance(c, Bin) and c.op in ('<=', '>=') and same(c.a, var)):
        return None""",
"""def match_for(prev, c, body):
    # init: last real statement before the loop (optionally followed by `bound = expr`)
    j = len(prev) - 1
    while j >= 0 and prev[j][0] == 'anchor':
        j -= 1
    if j < 0 or prev[j][0] != 'stmt' or not isinstance(prev[j][1], Assign):
        return None
    bound_stmt = None
    if isinstance(c, Bin) and isinstance(c.b, Var) and c.b.kind == 'local' and same(prev[j][1].dst, c.b) \\
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
            return None"""), (
"""    newbody = body[:k] + body[k + 1:]
    return var, init.src, c.b, step, newbody""",
"""    newbody = body[:k] + body[k + 1:]
    end = bound_stmt.src if bound_stmt is not None else c.b
    if bound_stmt is not None:
        prev.pop()          # the bound temp assignment (init is popped by the caller)
    return var, init.src, end, step, newbody


def uses_var(node, v):
    from simplify import iter_exprs_in
    k = node[0]
    if k == 'stmt':
        return any(isinstance(x, Var) and same(x, v) for e in iter_exprs_in(node[1]) for x in e.walk()) or \\
            (isinstance(node[1], Assign) and same(node[1].dst, v))
    if k == 'if':
        return any(isinstance(x, Var) and same(x, v) for x in node[1].walk()) or \\
            any(uses_var(n, v) for n in node[2] + node[3])
    for part in node[1:]:
        if isinstance(part, list):
            if any(uses_var(n, v) for n in part if isinstance(n, tuple)):
                return True
    return False""")])
print('patched')
