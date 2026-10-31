import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# push <after> ; mov [ebp-x], reg ; wait ; jmp <cleanup>  - the return address push need not be
# right before the jmp (the function result is stored in between)
patch('cfg.py', [(
"""                        # push <after> ; jmp <cleanup>  (local subroutine)
                        if prev is not None and prev.mn == 'push' and prev.ops[0].kind == 'imm' \\""",
"""                        # push <after> ; jmp <cleanup>  (local subroutine)
                        prev = self._ret_push(b.insns[:-1]) or prev
                        if prev is not None and prev.mn == 'push' and prev.ops[0].kind == 'imm' \\"""), (
"""    def _is_local_sub(self, va):""",
"""    @staticmethod
    def _ret_push(insns):
        \"\"\"The `push imm` before a jmp, skipping instructions that leave the stack pointer alone.\"\"\"
        for ins in reversed(insns[-4:]):
            if ins.mn == 'push':
                return ins if ins.ops[0].kind == 'imm' else None
            if ins.mn in ('wait', 'nop', 'fnclex'):
                continue
            if ins.mn in ('mov', 'lea') and ins.ops[0].kind != 'reg' or \
                    ins.mn in ('mov', 'lea') and ins.ops[0].reg != 'esp':
                if ins.ops[0].kind == 'mem' and ins.ops[0].base == 'esp':
                    return None
                continue
            return None
        return None

    def _is_local_sub(self, va):""")])
print('patched')
