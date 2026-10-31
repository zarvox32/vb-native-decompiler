import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('runtime.py', [(
# __vbaErase takes the address of the array variable
"""    def h___vbaErase(self, L):
        flags, psa = self.std_args(L, 2)
        L.emit(FmtStmt('Erase {0}', [self.ctx.array_ref(L, psa)]))""",
"""    def h___vbaErase(self, L):
        flags, ppsa = self.std_args(L, 2)
        t = L.ptr_target(ppsa, 4)
        if isinstance(t, Var):
            L.invalidate_var(t, 4)
            L.emit(FmtStmt('Erase {0}', [t]))
            return
        L.emit(FmtStmt('Erase {0}', [self.ctx.array_ref(L, ppsa)]))"""), (
# Array(...): the items are a temporary array filled just before; emit.collapse_array_literals
# turns them back into the argument list
"""        dst, psa = self.std_args(L, 2)
        self.var_result(L, dst, Call('Array', [Unknown('items')], pure=True, ty='Variant'))""",
"""        dst, ppsa = self.std_args(L, 2)
        t = L.ptr_target(ppsa, 4)
        c = Call('Array', [Unknown('items')], pure=True, ty='Variant')
        if isinstance(t, Var):
            c.items_of = t
        self.var_result(L, dst, c)""")])

patch('simplify.py', [(
"""        if obj is not e.obj or any(x is not y for x, y in zip(args, e.args)):
            n = Call(e.fn, args, obj, e.kind, e.ty, e.prop, not e.side, e.paren)
            for k in ('member', 'proc', 'decl'):""",
"""        if obj is not e.obj or any(x is not y for x, y in zip(args, e.args)):
            n = Call(e.fn, args, obj, e.kind, e.ty, e.prop, not e.side, e.paren)
            for k in ('member', 'proc', 'decl', 'items_of'):""")])

NEW = '''

def collapse_array_literals(blocks):
    """ReDim t(n) / t(0) = a ... t(n) = z / x = Array(<t>) / Erase t  ->  x = Array(a, ..., z)."""
    for b, stmts in blocks.items():
        for si, s in enumerate(stmts):
            calls = [x for e in iter_exprs_in(s) for x in e.walk()
                     if isinstance(x, Call) and x.fn == 'Array' and getattr(x, 'items_of', None) is not None]
            if len(calls) != 1:
                continue
            call = calls[0]
            v = call.items_of
            k = ekey(v)
            redim = None
            for j in range(si - 1, -1, -1):
                t = stmts[j]
                if isinstance(t, FmtStmt) and t.fmt.startswith('ReDim {0}(') and t.args and \\
                        isinstance(t.args[0], Var) and ekey(t.args[0]) == k:
                    redim = j
                    break
            if redim is None:
                continue
            r = stmts[redim]
            if len(r.args) != 3 or not (isinstance(r.args[1], Const) and r.args[1].v == 0) or \\
                    not isinstance(r.args[2], Const):
                continue
            n = r.args[2].v
            items = {}
            drop = {redim}
            ok = True
            for j in range(redim + 1, si):
                t = stmts[j]
                if isinstance(t, Assign) and isinstance(t.dst, Index) and isinstance(t.dst.arr, Var) and \\
                        ekey(t.dst.arr) == k and len(t.dst.idx) == 1 and isinstance(t.dst.idx[0], Const):
                    items[t.dst.idx[0].v] = t.src
                    drop.add(j)
                elif any(isinstance(x, Var) and ekey(x) == k for e in iter_exprs_in(t) for x in e.walk()):
                    ok = False
                    break
            if not ok or sorted(items) != list(range(n + 1)):
                continue
            erase = None
            for j in range(si + 1, len(stmts)):
                t = stmts[j]
                if isinstance(t, FmtStmt) and t.fmt == 'Erase {0}' and isinstance(t.args[0], Var) and \\
                        ekey(t.args[0]) == k:
                    erase = j
                    break
                if any(isinstance(x, Var) and ekey(x) == k for e in iter_exprs_in(t) for x in e.walk()):
                    break
            if erase is not None:
                drop.add(erase)
            call.args = [items[i] for i in range(n + 1)]
            call.items_of = None
            blocks[b] = [t for j, t in enumerate(stmts) if j not in drop]
            return collapse_array_literals(blocks)
'''
s = open('simplify.py', encoding='utf-8').read()
if 'def collapse_array_literals' not in s:
    s = s.rstrip('\n') + '\n' + NEW
    open('simplify.py', 'w', encoding='utf-8').write(s)

patch('emit.py', [(
"""            terms[b] = term_rewrite(terms[b], fn)
            terms[b] = term_rewrite(terms[b], byref_elems)
        self.collect_types(p, L, blocks, terms)""",
"""            terms[b] = term_rewrite(terms[b], fn)
            terms[b] = term_rewrite(terms[b], byref_elems)
        simplify.collapse_array_literals(blocks)
        self.collect_types(p, L, blocks, terms)""")])
print('patched')
