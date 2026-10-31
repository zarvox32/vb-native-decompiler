import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# __vbaVarVargNofree is register based (ecx = scratch VARIANT, edx = the VARIANT, maybe VT_BYREF):
# it takes nothing from the stack and returns the VARIANT to read
patch('runtime.py', [(
"""    def h___vbaVarVargNofree(self, L):
        p = L.reg('ecx') if False else None
        a, = self.std_args(L, 1)
        L.set_reg('eax', a)""",
"""    def h___vbaVarVargNofree(self, L):
        L.set_reg('eax', L.reg('edx'))""")])
print('patched')
