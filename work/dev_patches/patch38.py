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
"""            v = self.stack_var(o, size)
            if isinstance(v, Me) or isinstance(val, Me) and k == 'arg':
                return""",
"""            v = self.stack_var(o, size)
            if isinstance(v, Me):
                # the compiler reuses Me's stack slot as scratch space: track the value, emit nothing
                self.mem_store(o, size, val, None)
                return
            if isinstance(val, Me) and k == 'arg':
                return""")])

patch('structure.py', [(
"""        self.rets = {b for b, t in terms.items() if t[0] == 'ret' and b in blocks and
                     len([s for s in blocks[b] if not isinstance(s, Comment)]) <= 1}""",
"""        self.rets = {b for b, t in terms.items() if t[0] == 'ret' and b in blocks and
                     len([s for s in blocks[b] if not isinstance(s, Comment)]) <= 2}"""), (
"""            if t[0] == 'goto':
                n = t[1]
                continue
            if t[0] in ('stop',):
                break""",
"""            if t[0] == 'goto':
                n = t[1]
                continue
            if t[0] == 'ret':
                out.append(('return', n))
                break
            if t[0] in ('stop',):
                break""")])
print('patched')
