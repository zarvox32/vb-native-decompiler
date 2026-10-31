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
"""        if stmt is not None and self.loc_kind(o) in ('local', 'arg'):
            target = self.stack_var(o, size)
            if isinstance(target, Var) and reads_location(val, target):
                # x = f(x): later reads must see the new x, not the expression over the old x
                return
        st.mem[o] = (size, val, stmt)""",
"""        if stmt is not None and self.loc_kind(o) in ('local', 'arg'):
            target = self.stack_var(o, size)
            if isinstance(target, Var) and reads_location(val, target):
                # x = f(x): later reads must see the new x, not the expression over the old x
                return
            if not is_simple_value(val):
                # visible assignment of a computed value: later reads show the variable
                return
        st.mem[o] = (size, val, stmt)"""), (
"""def reads_location(e, v):""",
"""def is_simple_value(e):
    \"\"\"Values that may be forwarded into later reads without duplicating a computation.\"\"\"
    if isinstance(e, (Const, FConst, Str, Temp, Me, AddrOf, Var, Raw, Missing)):
        return True
    return type(e).__name__ in ('SAddr', 'DataPtr', 'CodePtr', 'ImportFn')


def reads_location(e, v):""")])

patch('runtime.py', [(
"""    def h_rtcSplit(self, L):
        dst, pexpr, pdelim, count, cmp_ = self.std_args(L, 5)
        args = [L.variant_at(pexpr), L.variant_at(pdelim)]""",
"""    def h_rtcSplit(self, L):
        # rtcSplit(VARIANT* ret, BSTR expression, VARIANT* delimiter, long limit, long compare)
        dst, expr, pdelim, count, cmp_ = self.std_args(L, 5)
        args = [expr, L.variant_at(pdelim)]""")])
print('patched')
