import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))

# push <after> ; add cx, si ; jo trap ; mov [ebp-14h], ecx ; wait ; jmp <cleanup>
# (the function result is computed between the push and the jmp)
s = open('cfg.py', encoding='utf-8').read()
start = s.index("        for ins in reversed(insns[-4:]):")
end = s.index("    def _is_local_sub(self, va):")
s = s[:start] + """        for ins in reversed(insns[-10:]):
            if ins.mn == 'push':
                return ins if ins.ops[0].kind == 'imm' else None
            if ins.mn in ('pop', 'call', 'ret', 'leave', 'pushal', 'popal', 'pushfd', 'popfd', 'jmp') or \\
                    ins.mn in JCC and ins.mn != 'jo':
                return None
            if ins.ops and ins.ops[0].kind == 'reg' and ins.ops[0].reg == 'esp':
                return None
        return None

""" + s[end:]
open('cfg.py', 'w', encoding='utf-8').write(s)
print('patched')
