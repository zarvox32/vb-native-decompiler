import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('decompiler.py', [(
"""                chk = dec.at(ins.next)
                if chk and chk.mn == 'call':
                    n = None
                    o = chk.ops[0]
                    if o.kind == 'mem' and o.base is None:
                        n = self.im.imports.get(o.disp & 0xffffffff)""",
"""                chk = dec.at(ins.next)
                if chk and chk.mn == 'call':
                    n = None
                    o = chk.ops[0]
                    if o.kind == 'mem' and o.base is None:
                        n = self.im.imports.get(o.disp & 0xffffffff)
                    elif o.kind == 'imm' and self.im.valid(o.imm, 6) and self.im.read(o.imm, 2) == b'\\xff\\x25':
                        n = self.im.imports.get(self.im.u32(o.imm + 2))     # call thunk -> jmp [IAT]""")])

# module functions returning their value in eax (computed, not via a local)
patch('decompiler.py', [(
"""            elif all(isinstance(e, Var) and e.kind == 'local' for e in eaxs):
                sig['ret'] = 'eax'
                sig['retvar'] = ekey(eaxs[0])
                sig['rettype'] = self._size_type(eaxs[0].size, eaxs[0])""",
"""            elif all(isinstance(e, Var) and e.kind == 'local' for e in eaxs):
                sig['ret'] = 'eax'
                sig['retvar'] = ekey(eaxs[0])
                sig['rettype'] = self._size_type(eaxs[0].size, eaxs[0])
            elif eaxs and all(isinstance(e, (BoolVal, SetCC)) or (isinstance(e, Bin) and e.ty == 'Boolean')
                              for e in eaxs):
                sig['ret'] = 'eax'
                sig['retexpr'] = True
                sig['rettype'] = 'Boolean'""")])

# emit: a computed eax return becomes `FuncName = value` before the exit
patch('emit.py', [(
"""        self.params_rewrite(p, sig, blocks, terms, fname)""",
"""        self.params_rewrite(p, sig, blocks, terms, fname)
        if sig.get('retexpr'):
            self.add_return_assignments(L, blocks, terms, fname)"""), (
"""    def merge_chains(self, blocks, terms, entry, roots):""",
"""    def add_return_assignments(self, L, blocks, terms, fname):
        \"\"\"Module Function returning a computed value in EAX: assign it to the function name.\"\"\"
        for b in list(blocks):
            if terms[b][0] != 'ret':
                continue
            st = L.out_state.get(b)
            if st is None:
                continue
            v = st.regs.get('eax')
            if v is None or isinstance(v, Unknown):
                continue
            blocks[b].append(Assign(Var('result', 0, 2, name=fname), v))

    def merge_chains(self, blocks, terms, entry, roots):""")])
print('patched')
