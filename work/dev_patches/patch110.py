import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# a local kept in a (ByRef) parameter's stack slot: var_ScratchN after parameter pN (was var_1000X)
patch('emit.py', [(
"""        if v.kind == 'local':
            n = self.cur_names.get(ekey(v))
            return n or v.name or 'var_%X' % v.off""",
"""        if v.kind == 'local':
            n = self.cur_names.get(ekey(v))
            if not n and 0x10000 <= v.off < 0x10100:
                start = 1 if self.cur_obj is not None and self.cur_obj.kind in ('form', 'class') else 0
                return 'var_Scratch%d' % ((v.off - 0x10000) // 4 - start + 1)
            return n or v.name or 'var_%X' % v.off""")])
print('patched')
