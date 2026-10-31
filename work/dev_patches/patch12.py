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
"""    def mem_store(self, o, size, val, stmt=None):
        st = self.st
        for k in list(st.mem):
            ks = st.mem[k][0]
            if k < o + size and o < k + ks:
                del st.mem[k]
        st.mem[o] = (size, val, stmt)""",
"""    def mem_store(self, o, size, val, stmt=None):
        st = self.st
        for k in list(st.mem):
            ks = st.mem[k][0]
            if k < o + size and o < k + ks:
                del st.mem[k]
        if self.loc_kind(o) in ('local', 'arg') and reads_variables(val):
            # a real variable now holds a value computed from variables: later reads must
            # see the variable itself, not a stale copy of the expression
            return
        st.mem[o] = (size, val, stmt)"""), (
"""def merge_states(states, preds=None):""",
"""def reads_variables(e):
    \"\"\"Does expression e read any variable / memory (ignoring address-of)?\"\"\"
    if not isinstance(e, Expr):
        return False
    stack = [e]
    seen = set()
    while stack:
        x = stack.pop()
        if id(x) in seen or isinstance(x, AddrOf):
            continue
        seen.add(id(x))
        if isinstance(x, (Var, Deref, Member)) or type(x).__name__ in ('VTbl',):
            return True
        stack.extend(c for c in x.children() if isinstance(c, Expr))
    return False


def merge_states(states, preds=None):""")])
print('patched')
