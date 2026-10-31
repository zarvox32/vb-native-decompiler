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
"""        if stmt is not None and self.loc_kind(o) in ('local', 'arg') and reads_variables(val):
            # a visible assignment of a value computed from variables: later reads must see the
            # variable itself, not a (possibly stale) copy of the expression
            return
        st.mem[o] = (size, val, stmt)""",
"""        if stmt is not None and self.loc_kind(o) in ('local', 'arg'):
            target = self.stack_var(o, size)
            if isinstance(target, Var) and reads_location(val, target):
                # x = f(x): later reads must see the new x, not the expression over the old x
                return
        st.mem[o] = (size, val, stmt)"""), (
"""def reads_variables(e):""",
"""def reads_location(e, v):
    \"\"\"Does expression e read variable v (same kind/offset, ignoring address-of)?\"\"\"
    if not isinstance(e, Expr):
        return False
    stack = [e]
    seen = set()
    while stack:
        x = stack.pop()
        if id(x) in seen or isinstance(x, AddrOf):
            continue
        seen.add(id(x))
        if isinstance(x, Var) and x.kind == v.kind and x.off == v.off:
            return True
        stack.extend(c for c in x.children() if isinstance(c, Expr))
    return False


def reads_variables(e):"""), (
"""        for o, (sz, v, s) in list(st.mem.items()):
            if self.loc_kind(o) == 'cell' and refers(v):
                t = self.new_temp(getattr(v, 'ty', None))
                self.emit(Assign(t, v))
                st.mem[o] = (sz, t, s)""",
"""        for o, (sz, v, s) in list(st.mem.items()):
            if self.loc_kind(o) == 'cell' and refers(v):
                t = self.new_temp(getattr(v, 'ty', None))
                self.emit(Assign(t, v))
                st.mem[o] = (sz, t, s)
            elif refers(v):
                # a cached local value computed from loc is stale after loc changes
                del st.mem[o]""")])
print('patched')
