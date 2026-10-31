import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# fnstsw ax / test al, 0Dh / jne <jmp __vbaFPException thunk>: a floating-point error trap
patch('cfg.py', [(
"""                if mn == 'jmp':
                    o = ins.ops[0]
                    if o.kind == 'imm':
                        tgt = o.imm""",
"""                if mn == 'jmp':
                    o = ins.ops[0]
                    if o.kind == 'imm' and self._thunk_import(o.imm) in NORETURN:
                        b.term = 'noreturn'
                        b.succs = []
                        break
                    if o.kind == 'imm':
                        tgt = o.imm"""), (
"""    def _is_noreturn_call(self, ins, regfn):""",
"""    def _thunk_import(self, va):
        \"\"\"Import reached through an import thunk `jmp dword ptr [IAT]` at va.\"\"\"
        ins = self.dec.at(va)
        if ins is None or ins.mn != 'jmp':
            return None
        o = ins.ops[0]
        if o.kind == 'mem' and o.base is None and o.index is None:
            return self.im.imports.get(o.disp & 0xffffffff)
        return None

    def _is_noreturn_call(self, ins, regfn):""")])
print('patched')
