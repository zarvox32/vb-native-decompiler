import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# A branch to the point where the enclosing construct continues (a stop) cannot be written as a jump:
# the rest of the region only runs when the branch is not taken.  This used to emit `If c Then <nothing>`
# and then run the rest unconditionally, losing the condition (2.0: If m_844 = 2 Then m_844 = 0 ...).
patch('structure.py', [(
"""                j = self.region(x, stops, loops) if x in self.rets else [self.jump(x, stops, loops) or ('nop',)]
                out.append(('if', c, j, []))
                return y""",
"""                if x in self.rets:
                    j = self.region(x, stops, loops)
                else:
                    jj = self.jump(x, stops, loops)
                    if jj is None:
                        out.append(('if', negate(c), self.region(y, stops, loops), []))
                        return x
                    j = [jj]
                out.append(('if', c, j, []))
                return y""")])
print('patched')
