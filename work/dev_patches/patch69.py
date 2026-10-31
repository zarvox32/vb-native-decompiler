import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# storing Me back into its own slot is not a reuse of the slot
patch('lift.py', [(
"""            if isinstance(v, Me):
                # the compiler reuses Me's stack slot""",
"""            if isinstance(v, Me) and isinstance(val, Me):
                self.mem_store(o, size, val, None)
                return
            if isinstance(v, Me):
                # the compiler reuses Me's stack slot""")])
print('patched')
