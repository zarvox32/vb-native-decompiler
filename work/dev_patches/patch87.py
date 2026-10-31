import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# unoptimised code (2.0): mov edx, 1 / imul edx, edx, 38h / movzx cx, byte ptr [eax + edx + 3F0h]
# - a constant index register is part of the displacement, and Me may be the index register
patch('lift.py', [(
"""    def deref(self, base, idx, scale, disp, size, ty=None):
        if isinstance(base, Bin)""",
"""    def deref(self, base, idx, scale, disp, size, ty=None):
        if isinstance(idx, Const):
            disp += idx.v * scale
            idx = None
        if isinstance(idx, Me) and scale == 1:
            base, idx = idx, base
        if isinstance(base, Bin)""")])
print('patched')
