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
"""    all_states = list(states)
    m = states[0].copy()
    m.flags = None""",
"""    all_states = list(states)
    m = states[0].copy()
    m.flags = states[0].flags if all(flags_equal(states[0].flags, s.flags) for s in states[1:]) else None"""), (
"""def merge_states(states, preds=None):""",
"""def flags_equal(a, b):
    if a is None or b is None or a[0] != b[0] or len(a) != len(b):
        return False
    for x, y in zip(a[1:], b[1:]):
        if isinstance(x, Expr) or isinstance(y, Expr):
            if not same(x, y):
                return False
        elif x != y:
            return False
    return True


def merge_states(states, preds=None):""")])
print('patched')
