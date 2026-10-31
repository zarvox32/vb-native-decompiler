import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('x86sem.py', [(
"""    def find_check(self, call_ins_next):
        \"\"\"Look ahead for the __vbaHresultCheckObj(hr, obj, IID, vtoff) that follows a COM call.\"\"\"
        dec = self.cfg.dec
        cur = call_ins_next
        pushes = []
        for _ in range(16):
            ins = dec.at(cur)
            if ins is None:
                break
            if ins.mn == 'push' and ins.ops[0].kind == 'imm':
                pushes.append(ins.ops[0].imm)
                if len(pushes) >= 2:
                    off, iid = pushes[0], pushes[1]
                    return off, iid
            elif ins.mn == 'call':
                break
            elif ins.mn == 'jmp':
                if ins.ops[0].kind == 'imm' and not pushes:
                    cur = ins.ops[0].imm
                    continue
                break
            cur = ins.next
            if ins.mn in ('jge', 'jl', 'jns', 'js', 'jae') and ins.ops[0].kind == 'imm':
                pass
        return None""",
"""    def find_check(self, call_ins_next, vtoff=None):
        \"\"\"Look ahead for the __vbaHresultCheckObj(hr, obj, IID, vtoff) that follows a COM call.
        The check block may re-evaluate the object expression (nested calls), so scan past calls
        until `push vtoff ; push IID` is found.\"\"\"
        dec = self.cfg.dec
        cur = call_ins_next
        prev_imm = None
        steps = 0
        while steps < 60:
            steps += 1
            ins = dec.at(cur)
            if ins is None:
                break
            if ins.mn == 'push' and ins.ops[0].kind == 'imm':
                v = ins.ops[0].imm
                if prev_imm is not None and (vtoff is None or prev_imm == vtoff) and self.im.valid(v, 16) \\
                        and not self.im.is_code(v) or (prev_imm is not None and prev_imm == vtoff and
                                                      self.im.valid(v, 16)):
                    return prev_imm, v
                prev_imm = v
            else:
                if ins.mn not in ('mov', 'lea', 'movsx', 'shl', 'add'):
                    prev_imm = None if ins.mn != 'push' else prev_imm
            if ins.mn == 'jmp':
                if ins.ops[0].kind == 'imm' and steps < 8:
                    cur = ins.ops[0].imm
                    continue
                break
            if ins.mn == 'ret':
                break
            if ins.mn == 'call':
                o = ins.ops[0]
                if o.kind == 'mem' and o.base is None and \\
                        self.im.imports.get(o.disp & 0xffffffff) == '__vbaHresultCheckObj':
                    break
            cur = ins.next
        return None"""), (
"""        chk = self.find_check(self.cur.next)
        iface = None""",
"""        chk = self.find_check(self.cur.next, off)
        iface = None""")])
print('patched')
