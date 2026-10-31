import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('cfg.py', [(
"""        for ins in reversed(b.insns[:-1]):
            if ins.mn == 'movzx' and ins.ops[0].kind == 'reg' and ins.ops[0].reg == reg and \\
                    ins.ops[1].kind == 'mem' and ins.ops[1].size == 1:
                movzx = ins
                break""",
"""        low8 = {'eax': 'al', 'ebx': 'bl', 'ecx': 'cl', 'edx': 'dl'}.get(reg)
        for ins in reversed(b.insns[:-1]):
            if ins.mn in ('movzx', 'mov') and ins.ops[0].kind == 'reg' and \\
                    (ins.ops[0].reg == reg or ins.ops[0].reg == low8) and \\
                    ins.ops[1].kind == 'mem' and ins.ops[1].size == 1:
                movzx = ins
                break""")])

patch('x86sem.py', [(
"""            if bt is not None and isinstance(idx, Deref) and idx.size == 1:
                # two-level table: idx = byte[x + table1]; show the cases in terms of x
                base = idx.base
                if isinstance(base, Bin) and base.op == '+' and isinstance(base.b, Const) and \\
                        (base.b.v & 0xffffffff) == bt[0]:
                    return ('switch', base.a, bt[1])""",
"""            if bt is not None and isinstance(idx, Deref) and idx.size == 1:
                # two-level table: idx = byte[x + table1]; show the cases in terms of x
                base = idx.base
                if (idx.off & 0xffffffff) == bt[0]:
                    return ('switch', base, bt[1])
                if isinstance(base, Bin) and base.op == '+' and isinstance(base.b, Const) and \\
                        (base.b.v & 0xffffffff) == bt[0] and idx.off == 0:
                    return ('switch', base.a, bt[1])""")])

# `(boolean expr) <> 0` -> boolean expr
patch('simplify.py', [(
"""def simplify_cmp(e):
    a, b = e.a, e.b""",
"""def is_bool_expr(x):
    if isinstance(x, Bin) and x.op in VBCMP:
        return True
    if isinstance(x, Bin) and x.op in ('And', 'Or', 'Xor'):
        return is_bool_expr(x.a) and is_bool_expr(x.b)
    if isinstance(x, Un) and x.op == 'Not':
        return is_bool_expr(x.a)
    return getattr(x, 'ty', None) == 'Boolean' and isinstance(x, (Call,))


def simplify_cmp(e):
    a, b = e.a, e.b
    if isinstance(b, Const) and b.v == 0 and e.op in ('<>', '=') and is_bool_expr(a):
        return a if e.op == '<>' else negate(a)""")])
print('patched')
