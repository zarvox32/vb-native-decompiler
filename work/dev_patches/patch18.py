import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('structure.py', [(
"""    # drop trailing 'continue' (natural end of a loop body)
    while body_end and out and out[-1][0] == 'continue' and in_loop is not None and out[-1][1] is in_loop:
        out.pop()
    return out""",
"""    # drop trailing 'continue' (natural end of a loop body), also at the end of a final If's branches
    if body_end and in_loop is not None:
        strip_tail_continue(out, in_loop)
    return out


def strip_tail_continue(nodes, L):
    while nodes and nodes[-1][0] == 'continue' and nodes[-1][1] is L:
        nodes.pop()
    if not nodes:
        return
    last = nodes[-1]
    if last[0] == 'if':
        strip_tail_continue(last[2], L)
        strip_tail_continue(last[3], L)
        if not last[2] and not last[3]:
            nodes.pop()
        elif not last[2]:
            nodes[-1] = ('if', negate(last[1]), last[3], [])
    elif last[0] == 'select':
        for vals, body in last[2]:
            strip_tail_continue(body, L)""")])

patch('emit.py', [(
"""        NAMER['fn'] = self.name_var
        try:
            lines = structure.structure(""",
"""        NAMER['fn'] = self.name_var
        if os.environ.get('VBDEC_DUMP') and int(os.environ['VBDEC_DUMP'], 16) == p.va:
            from ir import render_stmt as _rs
            for b in sorted(blocks):
                t = terms[b]
                print('-- %x %s' % (b, (t[0], render(t[1]), hex(t[2]), hex(t[3])) if t[0] == 'cond' else t))
                for s in blocks[b]:
                    print('     ', _rs(s))
        try:
            lines = structure.structure(""")])
print('patched')
