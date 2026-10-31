import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# Stores into a ByVal parameter's slot are mapped back onto the parameter only after the clean-up:
# the compiler's own scratch use of the slot (fild [esp+2Ch] operands) is removed or inlined first,
# so only the program's assignments to the parameter remain (no more `p2 = m_5E0 - 1` noise).
patch('emit.py', [(
"""            if isinstance(x, Var) and x.kind == 'local' and 0x10000 <= x.off < 0x10100 and \\
                    not byref.get(x.off - 0x10000) and not (retptr and x.off - 0x10000 == last_off):
                return Var('param', x.off - 0x10000, x.size)      # a ByVal parameter assigned by the code
            return None""",
"""            return None"""), (
"""        if simplify.undo_strength_reduction(blocks, terms, self.new_index_var):""",
"""        sig = dc.signatures.get(p.va) or {}
        byref = sig.get('byref', {})
        last_off = 4 * (sig.get('nargs', 0) - 1)

        def byval_param(x):
            if isinstance(x, Var) and x.kind == 'local' and 0x10000 <= x.off < 0x10100 and \\
                    not byref.get(x.off - 0x10000) and not (sig.get('retptr') and x.off - 0x10000 == last_off):
                return Var('param', x.off - 0x10000, x.size)      # a ByVal parameter assigned by the code
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, byval_param)
            terms[b] = term_rewrite(terms[b], byval_param)
        if simplify.undo_strength_reduction(blocks, terms, self.new_index_var):""")])
print('patched')
