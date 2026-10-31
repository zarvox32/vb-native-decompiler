import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))

# *(&p) keeps the type known from the dereference
s = open('emit.py', encoding='utf-8').read()
old = """                v = x.base.e
                return Var(v.kind, v.off, x.size, v.ty, v.name)"""
assert old in s
s = s.replace(old, """                v = x.base.e
                return Var(v.kind, v.off, x.size, v.ty or x.ty, v.name)""", 1)
open('emit.py', 'w', encoding='utf-8').write(s)
print('patched')
