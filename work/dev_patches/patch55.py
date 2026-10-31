import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('x86sem.py', [(
"""        if kind == 'cmp':
            _, a, b, size = f
            return self.int_cond(mn, a, b)""",
"""        if kind == 'cmp':
            _, a, b, size = f
            if mn in ('ja', 'jae', 'jb', 'jbe') and isinstance(b, Const) and b.v >= 0 and size > 1 and \\
                    not isinstance(a, (SetCC, BoolVal)):
                # unsigned range check (VB `Case lo To hi` / jump-table bounds): a negative value is "above"
                neg = self.mk_rel('<', a, Const(0))
                nonneg = self.mk_rel('>=', a, Const(0))
                if mn == 'ja':
                    return Bin('Or', neg, self.mk_rel('>', a, b), ty='Boolean')
                if mn == 'jae':
                    return Bin('Or', neg, self.mk_rel('>=', a, b), ty='Boolean')
                if mn == 'jb':
                    return Bin('And', nonneg, self.mk_rel('<', a, b), ty='Boolean')
                return Bin('And', nonneg, self.mk_rel('<=', a, b), ty='Boolean')
            return self.int_cond(mn, a, b)""")])

# the jump-table bounds `If` around a Select Case: recognise the two-sided form too
patch('structure.py', [(
"""            if len(th) == 1 and th[0][0] == 'select' and not el and isinstance(c, Bin) and \\
                    c.op in ('<=', '<') and lin_same(c.a, th[0][1]):
                out.append(th[0])          # jump-table bounds check
                continue""",
"""            if len(th) == 1 and th[0][0] == 'select' and not el and is_bounds_check(c, th[0][1]):
                out.append(th[0])          # jump-table bounds check
                continue"""), (
"""def lin_same(a, b):""",
"""def is_bounds_check(c, idx):
    if isinstance(c, Bin) and c.op in ('<=', '<') and lin_same(c.a, idx):
        return True
    if isinstance(c, Bin) and c.op == 'And':
        return is_bounds_check(c.a, idx) and is_bounds_check(c.b, idx) or \\
            (is_lower(c.a, idx) and is_bounds_check(c.b, idx)) or (is_lower(c.b, idx) and is_bounds_check(c.a, idx))
    return False


def is_lower(c, idx):
    return isinstance(c, Bin) and c.op in ('>=', '>') and lin_same(c.a, idx)


def lin_same(a, b):""")])
print('patched')
