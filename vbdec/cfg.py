"""Control-flow graph construction for VB6 native procedures.

Recognised compiler idioms:
  * FDIV-bug check  `cmp dword [ProjectInfo.lpNativeCode], 0 ; jne adj`  -> branch never taken
  * overflow traps  `jo err`                                             -> never taken
  * no-return runtime calls (__vbaErrorOverflow, __vbaGenerateBoundsError, __vbaFPException)
  * local subroutine trick `push <after> ; jmp <cleanup>` (cleanup ends with a bare `ret`)
    -> treated as a jump to <after>; the cleanup block is not part of the decompiled body
  * jump tables  `jmp dword [reg*4 + table]`
  * error handler labels from the procedure descriptor are extra entry points
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'lib'))
sys.path.insert(0, HERE)
from capstone import Cs, CS_ARCH_X86, CS_MODE_32, CS_OP_REG, CS_OP_IMM, CS_OP_MEM  # noqa: E402
from capstone import x86  # noqa: E402

NORETURN = {'__vbaErrorOverflow', '__vbaGenerateBoundsError', '__vbaFPException', '__vbaFailedFriend',
            '__vbaStopExe'}

JCC = {'je', 'jne', 'jg', 'jge', 'jl', 'jle', 'ja', 'jae', 'jb', 'jbe', 'js', 'jns', 'jo', 'jno', 'jp', 'jnp',
       'jecxz'}


class Op:
    __slots__ = ('kind', 'reg', 'imm', 'base', 'index', 'scale', 'disp', 'seg', 'size')

    def __repr__(self):
        if self.kind == 'reg':
            return self.reg
        if self.kind == 'imm':
            return hex(self.imm)
        return '[%s+%s*%d+%x]' % (self.base, self.index, self.scale, self.disp)


FULL = {'al': 'eax', 'ah': 'eax', 'ax': 'eax', 'eax': 'eax', 'bl': 'ebx', 'bh': 'ebx', 'bx': 'ebx', 'ebx': 'ebx',
        'cl': 'ecx', 'ch': 'ecx', 'cx': 'ecx', 'ecx': 'ecx', 'dl': 'edx', 'dh': 'edx', 'dx': 'edx', 'edx': 'edx',
        'si': 'esi', 'esi': 'esi', 'di': 'edi', 'edi': 'edi', 'bp': 'ebp', 'ebp': 'ebp'}
GPRS = ('eax', 'ebx', 'ecx', 'edx', 'esi', 'edi', 'ebp')


class Insn:
    __slots__ = ('addr', 'size', 'mn', 'ops', 'text', 'next', 'bytes', 'rd', 'wr')

    def __repr__(self):
        return '%x: %s' % (self.addr, self.text)


_md = Cs(CS_ARCH_X86, CS_MODE_32)
_md.detail = True


def _decode_ops(i):
    ops = []
    for o in i.operands:
        op = Op()
        op.size = o.size
        op.reg = op.base = op.index = op.seg = None
        op.imm = op.disp = 0
        op.scale = 1
        if o.type == CS_OP_REG:
            op.kind = 'reg'
            op.reg = i.reg_name(o.reg)
        elif o.type == CS_OP_IMM:
            op.kind = 'imm'
            op.imm = o.imm & 0xffffffff
        elif o.type == CS_OP_MEM:
            op.kind = 'mem'
            m = o.mem
            op.base = i.reg_name(m.base) if m.base else None
            op.index = i.reg_name(m.index) if m.index else None
            op.scale = m.scale
            op.disp = m.disp
            op.seg = i.reg_name(m.segment) if m.segment else None
        else:
            op.kind = 'other'
        ops.append(op)
    return ops


class Decoder:
    def __init__(self, im):
        self.im = im
        self.cache = {}

    def at(self, va):
        ins = self.cache.get(va)
        if ins is not None:
            return ins
        if not self.im.valid(va, 1):
            return None
        code = self.im.read(va, 16)
        for i in _md.disasm(code, va, count=1):
            ins = Insn()
            ins.addr = i.address
            ins.size = i.size
            ins.mn = i.mnemonic
            ins.text = ('%s %s' % (i.mnemonic, i.op_str)).strip()
            ins.ops = _decode_ops(i)
            ins.next = i.address + i.size
            ins.bytes = bytes(i.bytes)
            try:
                r, w = i.regs_access()
                ins.rd = {FULL[i.reg_name(x)] for x in r if i.reg_name(x) in FULL}
                ins.wr = {FULL[i.reg_name(x)] for x in w if i.reg_name(x) in FULL}
            except Exception:
                ins.rd, ins.wr = set(), set()
            if i.mnemonic in ('xor', 'sub') and len(ins.ops) == 2 and ins.ops[0].kind == 'reg' and \
                    ins.ops[1].kind == 'reg' and ins.ops[0].reg == ins.ops[1].reg:
                ins.rd = set()          # zeroing idiom: no real read
            if i.mnemonic == 'call':
                ins.wr |= {'eax', 'ecx', 'edx'}
                ins.rd |= {'ecx', 'edx'} if False else set()
            self.cache[va] = ins
            return ins
        return None


class Block:
    def __init__(self, start):
        self.start = start
        self.insns = []
        self.succs = []          # list of target addresses
        self.term = None         # 'jmp', 'jcc', 'ret', 'noreturn', 'switch', 'fall', 'stop'
        self.preds = []
        self.cond_insn = None    # the jcc instruction for 'jcc'
        self.taken = None        # jcc target
        self.fall = None         # jcc fallthrough
        self.switch = None       # (index reg insn, [targets])
        self.byte_table = None
        self.localcall = None    # cleanup subroutine address for push/jmp idiom

    @property
    def end(self):
        return self.insns[-1].next if self.insns else self.start

    def __repr__(self):
        return '<Block %x-%x %s -> %s>' % (self.start, self.end, self.term, [hex(s) for s in self.succs])


class CFG:
    def __init__(self, proj, entry, extra_entries=(), dec=None):
        self.proj = proj
        self.im = proj.im
        self.dec = dec or Decoder(self.im)
        self.entry = entry
        self.extra = list(extra_entries)
        self.blocks = {}
        self.cleanups = set()
        self.fdiv_flag = proj.native_code
        self.warnings = []
        self._build()

    # --------------------------------------------------------------
    def _import_name(self, ins):
        """Name of the import called by `call [IAT]` (or None)."""
        if ins.mn != 'call':
            return None
        o = ins.ops[0]
        if o.kind == 'mem' and o.base is None and o.index is None:
            return self.im.imports.get(o.disp & 0xffffffff)
        return None

    def _thunk_import(self, va):
        """Import reached through an import thunk `jmp dword ptr [IAT]` at va."""
        ins = self.dec.at(va)
        if ins is None or ins.mn != 'jmp':
            return None
        o = ins.ops[0]
        if o.kind == 'mem' and o.base is None and o.index is None:
            return self.im.imports.get(o.disp & 0xffffffff)
        return None

    def _is_noreturn_call(self, ins, regfn):
        if ins.mn != 'call':
            return False
        n = self._import_name(ins)
        if n is None and ins.ops[0].kind == 'reg':
            n = regfn.get(ins.ops[0].reg)
        return n in NORETURN

    def _build(self):
        dec = self.dec
        starts = {self.entry} | set(self.extra)
        work = list(starts)
        seen_insn = {}            # insn addr -> block start that contains it
        raw_blocks = {}
        lo, hi = self.proj.code_start, self.proj.code_end
        while work:
            va = work.pop()
            if va in raw_blocks:
                continue
            if not (lo <= va < hi):
                self.warnings.append('branch outside user code to %x' % va)
                continue
            if va in seen_insn and seen_insn[va] != va:
                # split an existing block at va
                self._split(raw_blocks, seen_insn, va)
                continue
            b = Block(va)
            raw_blocks[va] = b
            regfn = {}
            cur = va
            prev = None
            while True:
                ins = dec.at(cur)
                if ins is None:
                    b.term = 'stop'
                    self.warnings.append('decode failed at %x' % cur)
                    break
                if cur != va and cur in raw_blocks:
                    b.term = 'fall'
                    b.succs = [cur]
                    break
                if cur in seen_insn and seen_insn[cur] != va:
                    # falls into the middle of another block -> split it there
                    self._split(raw_blocks, seen_insn, cur)
                    b.term = 'fall'
                    b.succs = [cur]
                    break
                b.insns.append(ins)
                seen_insn[cur] = va
                mn = ins.mn
                # track `mov reg, [IAT]` for `call reg`
                if mn == 'mov' and len(ins.ops) == 2 and ins.ops[0].kind == 'reg' and ins.ops[1].kind == 'mem' \
                        and ins.ops[1].base is None and ins.ops[1].index is None:
                    n = self.im.imports.get(ins.ops[1].disp & 0xffffffff)
                    if n:
                        regfn[ins.ops[0].reg] = n
                if mn == 'jmp':
                    o = ins.ops[0]
                    if o.kind == 'imm' and self._thunk_import(o.imm) in NORETURN:
                        b.term = 'noreturn'
                        b.succs = []
                        break
                    if o.kind == 'imm':
                        tgt = o.imm
                        # push <after> ; jmp <cleanup>  (local subroutine)
                        prev = self._ret_push(b.insns[:-1]) or prev
                        # (the pushed value is the return point further down this procedure - not, say, a
                        # string literal pushed as an argument: VB keeps those in the code section too)
                        if prev is not None and prev.mn == 'push' and prev.ops[0].kind == 'imm' \
                                and self.im.is_code(prev.ops[0].imm) and prev.ops[0].imm > ins.addr \
                                and self.im.bstr(prev.ops[0].imm) is None and self._is_local_sub(tgt):
                            after = prev.ops[0].imm
                            b.localcall = tgt
                            self.cleanups.add(tgt)
                            b.term = 'jmp'
                            b.succs = [after]
                            work.append(after)
                            break
                        b.term = 'jmp'
                        b.succs = [tgt]
                        work.append(tgt)
                        break
                    if o.kind == 'mem' and o.index is not None and o.scale == 4 and o.base is None:
                        targets = self._jump_table(o.disp & 0xffffffff, b)
                        bt = self._byte_table(b, o.index, o.disp & 0xffffffff)
                        if bt is not None:
                            b.byte_table = bt
                            targets = bt[1]
                        b.term = 'switch'
                        b.switch = (o.index, targets)
                        b.succs = list(dict.fromkeys(targets))
                        work.extend(b.succs)
                        break
                    b.term = 'stop'
                    self.warnings.append('indirect jmp at %x: %s' % (ins.addr, ins.text))
                    break
                if mn in JCC:
                    tgt = ins.ops[0].imm
                    nxt = ins.next
                    if mn == 'jo':
                        cur = nxt
                        prev = ins
                        continue          # overflow trap: never taken
                    if mn == 'jno':
                        b.term = 'jmp'
                        b.succs = [tgt]
                        work.append(tgt)
                        break
                    if self._is_fdiv_check(prev, ins):
                        cur = nxt
                        prev = ins
                        continue          # FDIV workaround branch: never taken
                    b.term = 'jcc'
                    b.cond_insn = ins
                    b.taken = tgt
                    b.fall = nxt
                    b.succs = [nxt, tgt] if nxt != tgt else [nxt]
                    work.append(nxt)
                    work.append(tgt)
                    break
                if mn == 'ret':
                    # `push <after>; <free temporaries>; ret` with <after> right behind this ret: the
                    # cleanup runs inline and the ret only continues at <after> (unoptimised VB code)
                    if not ins.ops and any(x.mn == 'push' and x.ops[0].kind == 'imm' and
                                           (x.ops[0].imm & 0xffffffff) == ins.next for x in b.insns[:-1]):
                        b.insns.pop()
                        del seen_insn[cur]
                        b.term = 'jmp'
                        b.succs = [ins.next]
                        work.append(ins.next)
                        break
                    b.term = 'ret'
                    break
                if mn == 'int3' or mn == 'hlt':
                    b.term = 'stop'
                    break
                if mn == 'call' and self._is_noreturn_call(ins, regfn):
                    b.term = 'noreturn'
                    break
                prev = ins
                cur = ins.next
        # finalize: preds
        self.blocks = raw_blocks
        for b in self.blocks.values():
            for s in b.succs:
                if s in self.blocks:
                    self.blocks[s].preds.append(b.start)
        self._prune_noreturn()

    def _split(self, raw_blocks, seen_insn, at):
        owner = seen_insn[at]
        b = raw_blocks[owner]
        k = next(i for i, ins in enumerate(b.insns) if ins.addr == at)
        nb = Block(at)
        nb.insns = b.insns[k:]
        nb.succs, nb.term, nb.cond_insn, nb.taken, nb.fall, nb.switch, nb.localcall = \
            b.succs, b.term, b.cond_insn, b.taken, b.fall, b.switch, b.localcall
        b.insns = b.insns[:k]
        b.succs = [at]
        b.term = 'fall'
        b.cond_insn = b.taken = b.fall = b.switch = b.localcall = None
        raw_blocks[at] = nb
        for ins in nb.insns:
            seen_insn[ins.addr] = at

    def _is_fdiv_check(self, prev, jcc):
        if jcc.mn != 'jne' or prev is None or prev.mn != 'cmp' or not self.fdiv_flag:
            return False
        o0, o1 = prev.ops
        return o0.kind == 'mem' and o0.base is None and o0.index is None and \
            (o0.disp & 0xffffffff) == self.fdiv_flag and o1.kind == 'imm' and o1.imm == 0

    @staticmethod
    def _ret_push(insns):
        """The `push imm` before a jmp, skipping instructions that leave the stack pointer alone."""
        for ins in reversed(insns[-10:]):
            if ins.mn == 'push':
                return ins if ins.ops[0].kind == 'imm' else None
            if ins.mn in ('pop', 'call', 'ret', 'leave', 'pushal', 'popal', 'pushfd', 'popfd', 'jmp') or \
                    ins.mn in JCC and ins.mn != 'jo':
                return None
            if ins.ops and ins.ops[0].kind == 'reg' and ins.ops[0].reg == 'esp':
                return None
        return None

    def _is_local_sub(self, va):
        """A cleanup subroutine: straight-line code ending in a bare `ret` (no frame teardown)."""
        cur = va
        for _ in range(400):
            ins = self.dec.at(cur)
            if ins is None:
                return False
            if ins.mn == 'ret':
                return not ins.ops
            if ins.mn in ('jmp',) or ins.mn in JCC:
                if ins.mn == 'jmp' and ins.ops[0].kind == 'imm':
                    cur = ins.ops[0].imm
                    continue
                return False
            if ins.mn == 'pop' and ins.ops and ins.ops[0].kind == 'reg' and ins.ops[0].reg == 'ebp':
                return False
            if ins.mn == 'leave':
                return False
            cur = ins.next
        return False

    def _bound_before(self, at):
        """`cmp reg, N ; ja default` immediately before address `at` -> N + 1 (table size)."""
        for jlen in (2, 6):
            j = self.dec.at(at - jlen)
            if j is None or j.next != at or j.mn not in ('ja', 'jae', 'jg', 'jge'):
                continue
            for clen in (3, 5, 6, 2, 4):
                c = self.dec.at(j.addr - clen)
                if c is not None and c.next == j.addr and c.mn == 'cmp' and len(c.ops) == 2 and \
                        c.ops[1].kind == 'imm':
                    return c.ops[1].imm + (1 if j.mn in ('ja', 'jg') else 0)
        return None

    def _byte_table(self, b, reg, table2):
        """`movzx reg, byte [x + table1] ; jmp [reg*4 + table2]`  -> (table1, targets per value of x)."""
        im = self.im
        movzx = None
        low8 = {'eax': 'al', 'ebx': 'bl', 'ecx': 'cl', 'edx': 'dl'}.get(reg)
        for ins in reversed(b.insns[:-1]):
            if ins.mn in ('movzx', 'mov') and ins.ops[0].kind == 'reg' and \
                    (ins.ops[0].reg == reg or ins.ops[0].reg == low8) and \
                    ins.ops[1].kind == 'mem' and ins.ops[1].size == 1:
                movzx = ins
                break
        if movzx is None:
            return None
        t1 = movzx.ops[1].disp & 0xffffffff
        n = None
        for ins in reversed(b.insns[:-1]):
            if ins.mn == 'cmp' and ins.ops[1].kind == 'imm' and ins.addr < movzx.addr:
                n = ins.ops[1].imm + 1
                break
        if n is None:
            # bound check may be in the previous block: scan back a little
            cur = b.start
            for k in range(1, 40):
                ins = self.dec.at(cur - k)
                if ins is not None and ins.mn == 'cmp' and ins.ops and len(ins.ops) == 2 and \
                        ins.ops[1].kind == 'imm' and ins.next <= cur:
                    n = ins.ops[1].imm + 1
                    break
        if n is None or n > 1024 or not im.valid(t1, n):
            return None
        idxs = [im.u8(t1 + i) for i in range(n)]
        targets = []
        for k in idxs:
            if not im.valid(table2 + 4 * k, 4):
                return None
            t = im.u32(table2 + 4 * k)
            if not (self.proj.code_start <= t < self.proj.code_end):
                return None
            targets.append(t)
        return (t1, targets)

    def _jump_table(self, table, b):
        """Read jump table entries; bound by a preceding `cmp reg, N ; ja default` if present."""
        im = self.im
        n = None
        # look back in this block for cmp idx, imm
        for ins in reversed(b.insns[:-1]):
            if ins.mn == 'cmp' and ins.ops[1].kind == 'imm':
                n = ins.ops[1].imm + 1
                break
        if n is None:
            n = self._bound_before(b.insns[-1].addr if b.insns else b.start)
        targets = []
        k = 0
        while True:
            if n is not None and k >= n:
                break
            if not im.valid(table + 4 * k, 4):
                break
            t = im.u32(table + 4 * k)
            if not im.is_code(t) or not (self.proj.code_start <= t < self.proj.code_end):
                break
            targets.append(t)
            k += 1
            if k > 512:
                break
        return targets

    def _prune_noreturn(self):
        """Drop conditional edges into blocks that can only reach a no-return error."""
        dead = set()
        changed = True
        while changed:
            changed = False
            for b in self.blocks.values():
                if b.start in dead:
                    continue
                if b.term == 'noreturn' or (b.succs and all(s in dead for s in b.succs) and b.term in ('fall', 'jmp')):
                    if self._error_only(b):
                        dead.add(b.start)
                        changed = True
        self.error_blocks = dead
        for b in self.blocks.values():
            if b.term == 'jcc':
                live = [s for s in b.succs if s not in dead]
                if len(live) == 1 and len(b.succs) == 2:
                    b.term = 'jmp'
                    b.pruned_cond = b.cond_insn
                    b.succs = live
                    b.cond_insn = None
        # recompute preds without dead blocks
        for b in self.blocks.values():
            b.preds = []
        for b in self.blocks.values():
            if b.start in dead:
                continue
            for s in b.succs:
                if s in self.blocks and s not in dead:
                    self.blocks[s].preds.append(b.start)

    def _error_only(self, b):
        """True if the block does nothing but prepare/raise a runtime error."""
        for ins in b.insns:
            if ins.mn in ('push', 'mov', 'lea', 'call', 'xor', 'fnclex', 'wait', 'nop', 'add', 'sub', 'or', 'and',
                          'movsx', 'movzx', 'cmp', 'test', 'neg', 'sbb', 'fstp', 'fld', 'pop', 'jmp'):
                if ins.mn == 'call':
                    n = self._import_name(ins)
                    if n is None and ins.ops[0].kind == 'reg':
                        n = 'reg'
                    if n not in NORETURN and n != 'reg':
                        return False
                continue
            return False
        return True

    def reachable(self):
        seen = set()
        work = [self.entry] + self.extra
        while work:
            v = work.pop()
            if v in seen or v not in self.blocks:
                continue
            seen.add(v)
            b = self.blocks[v]
            if v in self.error_blocks:
                continue
            work.extend(b.succs)
        return seen


REGARG = {'__vbaI2Sgn', '__vbaStrCopy', '__vbaStrMove', '__vbaVarMove', '__vbaVarCopy', '__vbaVarDup', '__vbaFreeStr',
          '__vbaFreeObj', '__vbaFreeVar', '__vbaI2I4', '__vbaI2Abs', '__vbaI4Abs', '__vbaUI1I2', '__vbaUI1I4',
          '__vbaVarZero', '__vbaI4Sgn'}


def liveness(cfg, blocks_set=None):
    """live-in register sets per block (32-bit GPRs, esp/ebp excluded)."""
    bl = cfg.blocks
    nodes = [b for b in bl if (blocks_set is None or b in blocks_set) and b not in cfg.error_blocks]
    use, defs = {}, {}
    for b in nodes:
        u, d = set(), set()
        for ins in bl[b].insns:
            # VB runtime helpers take arguments in ecx/edx: a call reads them if they were set right before
            rd = set(ins.rd)
            if ins.mn == 'call':
                o = ins.ops[0]
                if o.kind == 'reg':
                    rd |= {'ecx', 'edx'}
                elif o.kind == 'mem' and o.base is None and o.index is None:
                    if cfg.im.imports.get(o.disp & 0xffffffff) in REGARG:
                        rd |= {'ecx', 'edx'}
            u |= (rd - d)
            d |= ins.wr
        use[b], defs[b] = u & set(GPRS), d & set(GPRS)
    live_in = {b: set() for b in nodes}
    changed = True
    while changed:
        changed = False
        for b in reversed(nodes):
            out = set()
            for s in bl[b].succs:
                out |= live_in.get(s, set())
            new = use[b] | (out - defs[b])
            if new != live_in[b]:
                live_in[b] = new
                changed = True
    return live_in
