import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# frameless procedures: `sub esp, N ; push regs`
patch('decompiler.py', [(
"""        if not saw_ebp:
            return {'R': None, 'local_low': 0, 'base_sp': 0, 'rtmi': None, 'seh': False}""",
"""        if not saw_ebp:
            # frameless: optional `sub esp, N` followed by register saves
            sp = 0
            low = 0
            cur = p.va
            for _ in range(8):
                ins = dec.at(cur)
                if ins is None:
                    break
                if ins.mn == 'sub' and ins.ops[0].kind == 'reg' and ins.ops[0].reg == 'esp' and \\
                        ins.ops[1].kind == 'imm' and sp == 0:
                    sp -= ins.ops[1].imm
                    low = sp
                elif ins.mn == 'push' and ins.ops[0].kind == 'reg' and ins.ops[0].reg in ('ebx', 'esi', 'edi', 'ebp'):
                    sp -= 4
                else:
                    break
                cur = ins.next
            return {'R': None, 'local_low': low, 'base_sp': sp, 'rtmi': None, 'seh': False, 'frameless': True}""")])

# saved-register pushes are not call arguments
patch('lift.py', [(
"""    def pending_cells(self):
        \"\"\"Number of bytes currently pushed as outgoing arguments.\"\"\"
        if self.local_low is None:
            return 0
        return max(0, self.local_low - self.st.sp)""",
"""    def pending_cells(self):
        \"\"\"Number of bytes currently pushed as outgoing arguments (saved registers excluded).\"\"\"
        if self.local_low is None:
            return 0
        top = self.local_low
        for o in self.saved_cells:
            if self.st.sp <= o < top:
                top = o
        return max(0, top - self.st.sp)"""), (
"""        self.callee_saved = set()
        self.materialized = {}""",
"""        self.callee_saved = set()
        self.materialized = {}
        self.saved_cells = set()       # stack offsets holding pushed callee-saved registers""")])

patch('x86sem.py', [(
"""    def op_push(self, ins):
        s = ins.ops[0]
        if s.kind == 'imm':
            v = self.imm_value(s.imm)
        else:
            v = self.load(s, 4)""",
"""    def op_push(self, ins):
        s = ins.ops[0]
        if s.kind == 'imm':
            v = self.imm_value(s.imm)
        else:
            v = self.load(s, 4)
        if s.kind == 'reg' and s.reg in ('ebx', 'esi', 'edi', 'ebp') and isinstance(v, Unknown) and \\
                v.text == s.reg:
            # saving a callee-saved register (its value on entry): not an argument
            self.saved_cells.add(self.st.sp - 4)"""), (
"""    def handler_state(self):
        \"\"\"State at an error-handler label: registers unknown, esp at frame bottom.\"\"\"
        st = State()
        st.sp = self.local_low if self.local_low is not None else 0""",
"""    def handler_state(self):
        \"\"\"State at an error-handler label: registers unknown, esp at frame bottom.\"\"\"
        st = State()
        st.sp = self.local_low if self.local_low is not None else 0
        for r in ('ebx', 'esi', 'edi'):
            st.regs[r] = Unknown('handler_' + r)""")])

# lifter: frameless procedures keep ebp as a general register; locals start below `sub esp`
patch('decompiler.py', [(
"""        L.frame_R = fi['R']
        L.local_low = fi['base_sp']""",
"""        L.frame_R = fi['R']
        L.local_low = fi['base_sp']
        L.frameless = fi.get('frameless', False)"""), (
"""    def frame_info(self, p):""",
"""    def frame_info(self, p):
        fi = self._frame_info(p)
        return fi

    def _frame_info(self, p):""")])

# ebp participates in liveness / register variables
patch('cfg.py', [(
"""        'si': 'esi', 'esi': 'esi', 'di': 'edi', 'edi': 'edi'}
GPRS = ('eax', 'ebx', 'ecx', 'edx', 'esi', 'edi')""",
"""        'si': 'esi', 'esi': 'esi', 'di': 'edi', 'edi': 'edi', 'bp': 'ebp', 'ebp': 'ebp'}
GPRS = ('eax', 'ebx', 'ecx', 'edx', 'esi', 'edi', 'ebp')""")])
print('patched')
