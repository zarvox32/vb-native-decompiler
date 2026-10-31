import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs, count=1):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, count)
    open(path, 'w', encoding='utf-8').write(s)


# Me's reused stack slot gets a readable name
patch('lift.py', [("name='var_MeSlot'", "name='var_Scratch'")], count=-1)
patch('emit.py', [(
"""            n = self.cur_names.get(ekey(v))
            return n or 'var_%X' % v.off""",
"""            n = self.cur_names.get(ekey(v))
            return n or v.name or 'var_%X' % v.off""")])
print('patched')
