import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# A temporary VARIANT whose type is known (vt stored as a constant) but whose value was stored on
# several paths (x = 0 / If ... Then x = -1, then vt = vbBoolean): the value is the variable at +8.
# It used to fall back to the whole 16-byte slot, whose last store was the vt constant
# (FileExists = Dir(f) <> "" came out as `FileExists = 11`).
patch('lift.py', [(
"""        if val8 is None:
            return None
        v = val8[1]
        if byref:""",
"""        if val8 is None:
            size = {2: 2, 3: 4, 4: 4, 5: 8, 6: 8, 7: 8, 8: 4, 9: 4, 11: 2, 17: 1}.get(base)
            if byref or size is None or self.loc_kind(o + 8) not in ('local', 'cell'):
                return None
            v = Var('local', -(o + 8 + 4), size, VT_NAMES[base])
            self.note_type(v, VT_NAMES[base])
            self.var_sizes[v.key()] = max(self.var_sizes.get(v.key(), 0), size)
            return v
        v = val8[1]
        if byref:""")])
print('patched')
