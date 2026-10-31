import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# Date hints from __vbaDateVar results stored with fstp
patch('runtime.py', [(
"""    def h___vbaDateVar(self, L):
        L.fpush(self.conv_var(L, 'Date'))""",
"""    def h___vbaDateVar(self, L):
        v = self.conv_var(L, 'Date')
        v.__dict__['date_hint'] = True
        L.fpush(v)""")])
patch('x86sem.py', [(
"""                tgt = self.load(d, d.size) if d.kind == 'mem' else None
                if isinstance(tgt, Var) and not mn.startswith('fist'):
                    self.note_type(tgt, 'Single' if d.size == 4 else 'Double')""",
"""                tgt = self.load(d, d.size) if d.kind == 'mem' else None
                if isinstance(tgt, Var) and not mn.startswith('fist'):
                    if getattr(v, 'date_hint', False) or getattr(v, 'ty', None) == 'Date':
                        self.note_type(tgt, 'Date')
                    else:
                        self.note_type(tgt, 'Single' if d.size == 4 else 'Double')""")])
patch('emit.py', [(
"""    for t in ('String', 'Object', 'Double', 'Single', 'Date', 'Currency'):""",
"""    if h.get('Date') and size in (4, 8):
        return 'Date'
    for t in ('String', 'Object', 'Double', 'Single', 'Date', 'Currency'):""")])

# float constants stored as raw bits
code = '''

def float_const_stores(blocks, var_type):
    """Constants written into Single variables (and Double halves) are float bit patterns."""
    import struct as _st
    for b, stmts in blocks.items():
        out = []
        i = 0
        while i < len(stmts):
            s = stmts[i]
            if isinstance(s, Assign) and isinstance(s.dst, Var) and isinstance(s.src, Const) and s.dst.size == 4:
                t = var_type(s.dst)
                if t == 'Single':
                    f = _st.unpack('<f', _st.pack('<I', s.src.v & 0xffffffff))[0]
                    s.src = FConst(f, 'Single')
                elif i + 1 < len(stmts):
                    n = stmts[i + 1]
                    hi_off = s.dst.off - 4 if s.dst.kind == 'local' else s.dst.off + 4
                    if isinstance(n, Assign) and isinstance(n.dst, Var) and n.dst.kind == s.dst.kind and \\
                            n.dst.off == hi_off and isinstance(n.src, Const):
                        big = Var(s.dst.kind, s.dst.off, 8, s.dst.ty, s.dst.name)
                        if var_type(big) in ('Double', 'Date'):
                            raw = _st.pack('<II', s.src.v & 0xffffffff, n.src.v & 0xffffffff)
                            out.append(Assign(big, FConst(_st.unpack('<d', raw)[0]), s.addr))
                            i += 2
                            continue
            out.append(s)
            i += 1
        blocks[b] = out


def strip_condition_negs(terms):
    for b, t in list(terms.items()):
        if t[0] == 'cond' and is_bool_expr(t[1]):
            terms[b] = ('cond', strip_bool_negs(t[1]), t[2], t[3])
'''
s = open('simplify.py', encoding='utf-8').read()
if 'def float_const_stores' not in s:
    s += code
    open('simplify.py', 'w', encoding='utf-8').write(s)

patch('emit.py', [(
"""        self.collect_types(p, L, blocks, terms)
        self.boolean_rewrite(blocks, terms)""",
"""        self.collect_types(p, L, blocks, terms)
        self.boolean_rewrite(blocks, terms)
        simplify.float_const_stores(blocks, self.var_type)
        simplify.strip_condition_negs(terms)""")])
print('patched')
