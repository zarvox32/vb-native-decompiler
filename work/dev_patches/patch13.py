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
"""        if self.loc_kind(o) in ('local', 'arg') and reads_variables(val):
            # a real variable now holds a value computed from variables: later reads must
            # see the variable itself, not a stale copy of the expression
            return
        st.mem[o] = (size, val, stmt)""",
"""        if stmt is not None and self.loc_kind(o) in ('local', 'arg') and reads_variables(val):
            # a visible assignment of a value computed from variables: later reads must see the
            # variable itself, not a (possibly stale) copy of the expression
            return
        st.mem[o] = (size, val, stmt)""")])
print('patched')
