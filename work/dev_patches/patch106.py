import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# a call does not stop a parameter slot that still holds the parameter from holding it (reading it
# afterwards reads the parameter - even if the call changed it through a ByRef argument)
patch('lift.py', [(
"""    def invalidate_locals(self):
        st = self.st
        for k in list(st.mem):
            if self.loc_kind(k) in ('local', 'arg'):
                del st.mem[k]""",
"""    def invalidate_locals(self):
        st = self.st
        for k in list(st.mem):
            kind = self.loc_kind(k)
            if kind == 'arg' and isinstance(st.mem[k][1], Var) and st.mem[k][1].kind == 'arg' and \\
                    st.mem[k][1].off == k - 4:
                continue
            if kind in ('local', 'arg'):
                del st.mem[k]""")])
print('patched')
