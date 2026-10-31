import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# a constant can be inlined across statements that do not touch the variable
# (VB passes literals ByRef through a temporary that the compiler fills early)
patch('simplify.py', [(
"""                if isinstance(d, Assign) and is_local_var(d.dst) and d.dst.kind in ('local', 'reg'):
                    k = ekey(d.dst)
                    u = stmts[i + 1]
                    la = live_after_list(stmts, live_out[b], terms[b])
                    if count_reads_in(u, k) == 1 and k not in la[i + 1] and not (""",
"""                if isinstance(d, Assign) and is_local_var(d.dst) and d.dst.kind in ('local', 'reg'):
                    k = ekey(d.dst)
                    j = i + 1
                    if isinstance(d.src, (Const, Str, FConst)):
                        while j < len(stmts) - 1 and not uses_key(stmts[j], k) and not (
                                isinstance(stmts[j], Assign) and isinstance(stmts[j].dst, (Var, Temp)) and
                                ekey(stmts[j].dst) == k):
                            j += 1
                    u = stmts[j]
                    la = live_after_list(stmts, live_out[b], terms[b])
                    if count_reads_in(u, k) == 1 and k not in la[j] and not (""")])
print('patched')
