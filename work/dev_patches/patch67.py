import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# ByRef arguments to user procedures: the variable, not its address
patch('emit.py', [(
"""        def byref_elems(x):
            # user procedure arguments that are addresses of array elements (ByRef element)
            if isinstance(x, Call) and x.kind == 'proc':
                new = []
                changed = False
                for a in x.args:""",
"""        def byref_elems(x):
            # user procedure arguments that are addresses of array elements (ByRef element)
            if isinstance(x, Call) and x.kind == 'proc':
                new = []
                changed = False
                for a in x.args:
                    if isinstance(a, AddrOf) and isinstance(a.e, (Var, Member, Index)):
                        new.append(a.e)        # passing a variable ByRef
                        changed = True
                        continue"""), (
"""        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)
                stmt_rewrite(s, byref_elems)
            terms[b] = term_rewrite(terms[b], fn)""",
"""        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)
                stmt_rewrite(s, byref_elems)
            terms[b] = term_rewrite(terms[b], fn)
            terms[b] = term_rewrite(terms[b], byref_elems)""")])

# a register that held Me can be reused for something else: Me does not block a register variable
patch('x86sem.py', [(
"""                            if len(known) >= 2 and not all(same(v, known[0]) for v in known) and \\
                                    not any(isinstance(v, (Me, ImportFn)) for v in known):""",
"""                            if len(known) >= 2 and not all(same(v, known[0]) for v in known) and \\
                                    not any(isinstance(v, ImportFn) for v in known):"""), (
"""                if all(same(v, known[0]) for v in known):
                    continue
                if any(isinstance(v, (Me, ImportFn)) for v in known):
                    continue""",
"""                if all(same(v, known[0]) for v in known):
                    continue
                if any(isinstance(v, ImportFn) for v in known):
                    continue""")])
print('patched')
