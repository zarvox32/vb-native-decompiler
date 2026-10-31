import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))

# a block ending in `jmp <__vbaFPException thunk>` only raises an error
s = open('cfg.py', encoding='utf-8').read()
old = "'movsx', 'movzx', 'cmp', 'test', 'neg', 'sbb', 'fstp', 'fld', 'pop'):"
assert old in s
s = s.replace(old, "'movsx', 'movzx', 'cmp', 'test', 'neg', 'sbb', 'fstp', 'fld', 'pop', 'jmp'):", 1)
open('cfg.py', 'w', encoding='utf-8').write(s)
print('patched')
