import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('simplify.py', [(
"""            st = known(x.base)
            if st and x.off not in (0xC, 0x14, 0x10):""",
"""            st = known(x.base)
            if st:""")])

# ByRef arguments that are element addresses: show the element
patch('emit.py', [(
"""        fn = simplify.array_rewrite_fn(self.note_array, self.known_array_stride)
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)
            terms[b] = term_rewrite(terms[b], fn)""",
"""        fn = simplify.array_rewrite_fn(self.note_array, self.known_array_stride)

        def byref_elems(x):
            # user procedure arguments that are addresses of array elements (ByRef element)
            if isinstance(x, Call) and x.kind == 'proc':
                new = []
                changed = False
                for a in x.args:
                    if isinstance(a, Bin) and a.op == '+' and not getattr(a, 'ty', None):
                        d = fn(Deref(a, 0, 4))
                        if d is not None and not isinstance(d, Deref):
                            new.append(d)
                            changed = True
                            continue
                    new.append(a)
                if changed:
                    n = Call(x.fn, new, x.obj, x.kind, x.ty, x.prop, not x.side, x.paren)
                    n.proc = x.proc
                    return n
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)
                stmt_rewrite(s, byref_elems)
            terms[b] = term_rewrite(terms[b], fn)""")])

# vtable call through a register that does not hold a recognisable vtable: `this` is the last push
patch('x86sem.py', [(
"""        if o.kind == 'mem':
            base = self.reg(o.base) if o.base else None
            if isinstance(base, VTbl) and o.index is None:
                return self.com_call(base.obj, o.disp)
            if isinstance(base, (Me,)) and o.index is None:
                return self.com_call(base, o.disp)
            return self.call_unknown(ins.text)""",
"""        if o.kind == 'mem':
            base = self.reg(o.base) if o.base else None
            if isinstance(base, VTbl) and o.index is None:
                return self.com_call(base.obj, o.disp)
            if isinstance(base, (Me,)) and o.index is None:
                return self.com_call(base, o.disp)
            if o.index is None and o.base and self.pending_cells() >= 4 and (o.disp & 3) == 0 \\
                    and 0 < o.disp < 0x1000:
                # vtable pointer kept in a register: the object is the `this` argument (last push)
                this = self.cell(0)
                if not isinstance(this, (Unknown, Const)):
                    return self.com_call(this, o.disp)
            return self.call_unknown(ins.text)""")])
print('patched')
