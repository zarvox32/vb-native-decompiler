"""Symbolic execution of VB6 native x86 code into the statement IR."""
import struct

from ir import (Expr, Const, FConst, Str, Var, Member, Deref, AddrOf, Bin, Un, Call, Temp, Me, Missing,
                Unknown, Raw, Assign, ExprStmt, Text, Comment, FmtStmt, ekey, same)

REGS32 = ('eax', 'ebx', 'ecx', 'edx', 'esi', 'edi', 'ebp')
SUB16 = {'ax': 'eax', 'bx': 'ebx', 'cx': 'ecx', 'dx': 'edx', 'si': 'esi', 'di': 'edi', 'bp': 'ebp'}
SUB8L = {'al': 'eax', 'bl': 'ebx', 'cl': 'ecx', 'dl': 'edx'}
SUB8H = {'ah': 'eax', 'bh': 'ebx', 'ch': 'ecx', 'dh': 'edx'}

VT_NAMES = {0: 'Empty', 1: 'Null', 2: 'Integer', 3: 'Long', 4: 'Single', 5: 'Double', 6: 'Currency', 7: 'Date',
            8: 'String', 9: 'Object', 10: 'Error', 11: 'Boolean', 12: 'Variant', 13: 'Unknown', 17: 'Byte'}


# --------------------------------------------------------------------------- special values
class SAddr(Expr):
    """Address of a stack location, offset relative to the entry ESP (o=0: return address)."""
    ty = 'Ptr'

    def __init__(self, o):
        self.o = o

    def key(self):
        return ('sa', self.o)


class VTbl(Expr):
    """The vtable pointer of an object (Mem32(obj))."""

    def __init__(self, obj):
        self.obj = obj

    def children(self):
        return (self.obj,)

    def key(self):
        return ('vt', ekey(self.obj))


class ImportFn(Expr):
    def __init__(self, name):
        self.name = name

    def key(self):
        return ('imp', self.name)


class CodePtr(Expr):
    def __init__(self, va):
        self.va = va

    def key(self):
        return ('code', self.va)


class DataPtr(Expr):
    """Address of something in read-only image data (a GUID, a descriptor...)."""

    def __init__(self, va):
        self.va = va

    def key(self):
        return ('data', self.va)


class StrCmpResult(Expr):
    """Result of __vbaStrCmp / TextCmp (sign of lhs vs rhs)."""

    def __init__(self, a, b, text=False):
        self.a = a
        self.b = b
        self.text = text
        self.ty = 'Long'

    def children(self):
        return (self.a, self.b)

    def key(self):
        return ('strcmp', ekey(self.a), ekey(self.b))


class Hresult(Expr):
    """HRESULT returned by a COM call (only used for error checks)."""

    def __init__(self, call):
        self.call = call

    def key(self):
        return ('hr', id(self.call))


class FpuStatus(Expr):
    """FPU status word after fcom/fcomp/ftst: compares a (st0) with b."""

    def __init__(self, a, b):
        self.a = a
        self.b = b

    def children(self):
        return (self.a, self.b)

    def key(self):
        return ('fsw', ekey(self.a), ekey(self.b))


class FpuMasked(Expr):
    """(FPU status AH) AND mask."""

    def __init__(self, st, mask):
        self.st = st
        self.mask = mask

    def key(self):
        return ('fswm', ekey(self.st), self.mask)


class SetCC(Expr):
    """0/1 result of setcc."""
    ty = 'Boolean'

    def __init__(self, cond):
        self.cond = cond

    def children(self):
        return (self.cond,)

    def key(self):
        return ('setcc', ekey(self.cond))


class BoolVal(Expr):
    """A VB Boolean (0 / -1) derived from a condition."""
    ty = 'Boolean'

    def __init__(self, cond):
        self.cond = cond

    def children(self):
        return (self.cond,)

    def key(self):
        return ('bool', ekey(self.cond))


VBCMP = {'=', '<>', '<', '<=', '>', '>='}
INV = {'=': '<>', '<>': '=', '<': '>=', '>=': '<', '>': '<=', '<=': '>'}
SWAP = {'=': '=', '<>': '<>', '<': '>', '>': '<', '<=': '>=', '>=': '<='}


def is_const(e, v=None):
    return isinstance(e, Const) and (v is None or e.v == v)


def s32(v):
    v &= 0xffffffff
    return v - (1 << 32) if v & 0x80000000 else v


def s16(v):
    v &= 0xffff
    return v - (1 << 16) if v & 0x8000 else v


VBCMP = {'=', '<>', '<', '<=', '>', '>='}


def negate(c):
    """Logical negation of a condition expression."""
    if isinstance(c, Bin) and c.op in VBCMP:
        return Bin(INV[c.op], c.a, c.b, ty='Boolean')
    if isinstance(c, Un) and c.op == 'Not':
        if truth_exact(c.a):
            return c.a
        # Not is bitwise: "Not x is false" means x = -1, not "x is false" (x = 12: Not x = -13, true)
        return Bin('=', c, Const(0), ty='Boolean')
    if isinstance(c, Const):
        return Const(0 if c.v else -1, 'Boolean')
    if isinstance(c, Un) and c.op == 'neg' and truth_exact(c.a):
        return negate(c.a)          # -(cond) is 1 / 0: as true or false as cond
    if isinstance(c, Bin) and c.op in ('And', 'Or') and truth_like(c.a) and truth_like(c.b):
        # (0/-1 and 0/1 values combine with And / Or like truth values: 1 And -1 = 1)
        return Bin('Or' if c.op == 'And' else 'And', negate(c.a), negate(c.b), ty=c.ty or 'Boolean')
    if not truth_exact(c) and not isinstance(c, (BoolVal, SetCC)):
        # a number / Variant used as a truth value (nonzero = true): Not alone is bitwise (Not 8 = -9,
        # still true), so make it a Boolean first. (CBool(Null) is an error where If treats Null as false.)
        return Un('Not', Call('CBool', [c], pure=True, ty='Boolean'), ty='Boolean')
    return Un('Not', c, ty='Boolean')


def truth_exact(c):
    """Is c's value exactly True (-1) or False (0)? Then Not / And / Or act on it as logic; on other
    numbers they are bitwise (Not 1 = -2, still true)."""
    if is_condition(c):
        return True
    if isinstance(c, Const):
        return c.v in (0, -1)
    if isinstance(c, Un) and c.op == 'Not':
        return truth_exact(c.a)
    if isinstance(c, Bin) and c.op in ('And', 'Or', 'Xor'):
        return truth_exact(c.a) and truth_exact(c.b)
    if isinstance(c, Bin):
        return False
    return getattr(c, 'ty', None) == 'Boolean'


def truth_like(c):
    """True / False as -1 / 0, or as 1 / 0 (-(cond))."""
    return truth_exact(c) or (isinstance(c, Un) and c.op == 'neg' and truth_exact(c.a))


def held_type(v):
    """Type of a variable that holds a register's value exactly: the value's own type, else the width
    the machine computed it in (a 16-bit operation wraps like Integer arithmetic; a 32-bit register
    holds a Long). A set-on-condition value is 1 / 0, not a Boolean's -1 / 0. None: not known (a 4-byte
    memory value may be an object or a string)."""
    if isinstance(v, SetCC):
        return 'Integer'
    t = getattr(v, 'conv', None) or getattr(v, 'ty', None)
    if t:
        return t
    if isinstance(v, Bin):
        return {1: 'Byte', 2: 'Integer'}.get(getattr(v, 'w', None), 'Long')
    if isinstance(v, Un) and v.op in ('neg', 'Not'):
        return 'Long'
    if isinstance(v, (Var, Deref, Member)) and getattr(v, 'size', 4) <= 2:
        return 'Byte' if v.size == 1 else 'Integer'
    return None


def low16(e):
    """The low 16 bits of integer arithmetic done in 32-bit registers (`lea` chains, then `movsx eax, cx`):
    for + - * And Or Xor neg Not they are what 16-bit arithmetic gives, so the operations are marked as
    16-bit ones (w = 2: Integer arithmetic, wrapping) all the way down."""
    if isinstance(e, Bin) and e.op in ('+', '-', '*', 'And', 'Or', 'Xor') and getattr(e, 'w', None) != 2 and \
            e.ty not in ('Single', 'Double', 'Currency', 'Date', 'String', 'Variant'):
        n = Bin(e.op, low16(e.a), low16(e.b), e.ty)
        n.w = 2
        return n
    if isinstance(e, Un) and e.op in ('neg', 'Not') and getattr(e, 'w', None) != 2 and \
            e.ty not in ('Single', 'Double', 'Currency', 'Date', 'String', 'Variant'):
        n = Un(e.op, low16(e.a), e.ty)
        n.w = 2
        return n
    return e


def is_condition(c):
    """A pure truth value (comparison / Not / And-Or of those): safe for De Morgan."""
    if isinstance(c, Bin) and c.op in VBCMP:
        return True
    if isinstance(c, Un) and c.op == 'Not' and is_condition(c.a):
        return True
    if isinstance(c, Bin) and c.op in ('And', 'Or'):
        return is_condition(c.a) and is_condition(c.b)
    return False


def as_cond(e):
    """Turn a VB value used as a truth value into a condition expression."""
    if isinstance(e, (BoolVal, SetCC)):
        return e.cond
    if isinstance(e, Bin) and e.op in VBCMP:
        return e
    if isinstance(e, Un) and e.op == 'Not' and e.ty == 'Boolean':
        return Un('Not', as_cond(e.a), ty='Boolean')
    if isinstance(e, Bin) and e.op in ('And', 'Or', 'Xor') and e.ty == 'Boolean':
        return Bin(e.op, as_cond(e.a), as_cond(e.b), ty='Boolean')
    return e


# --------------------------------------------------------------------------- machine state
class State:
    __slots__ = ('regs', 'sp', 'mem', 'fpu', 'flags')

    def __init__(self):
        self.regs = {}
        self.sp = 0
        self.mem = {}          # stack offset -> (size, expr, store_stmt or None)
        self.fpu = []
        self.flags = None

    def copy(self):
        s = State()
        s.regs = dict(self.regs)
        s.sp = self.sp
        s.mem = dict(self.mem)
        s.fpu = list(self.fpu)
        s.flags = self.flags
        return s


class PhiCell(Expr):
    """A stack cell whose value depends on the predecessor block (VB tail-merged two statements)."""

    def __init__(self, pairs):
        self.pairs = pairs          # [(pred block, value)]

    def children(self):
        return tuple(v for p, v in self.pairs)

    def key(self):
        return ('phicell', tuple((p, ekey(v)) for p, v in self.pairs))


def is_simple_value(e):
    """Values that may be forwarded into later reads without duplicating a computation."""
    if isinstance(e, (Const, FConst, Str, Temp, Me, AddrOf, Var, Raw, Missing)):
        return True
    return type(e).__name__ in ('SAddr', 'DataPtr', 'CodePtr', 'ImportFn')


def contains_call(e):
    if not isinstance(e, Expr):
        return False
    return any(isinstance(x, Call) for x in e.walk())


def reads_location(e, v):
    """Does expression e read variable v (same kind/offset, ignoring address-of)?"""
    if not isinstance(e, Expr):
        return False
    stack = [e]
    seen = set()
    while stack:
        x = stack.pop()
        if id(x) in seen or isinstance(x, AddrOf):
            continue
        seen.add(id(x))
        if isinstance(x, Var) and x.kind == v.kind and x.off == v.off:
            return True
        stack.extend(c for c in x.children() if isinstance(c, Expr))
    return False


def reads_variables(e):
    """Does expression e read any variable / memory (ignoring address-of)?"""
    if not isinstance(e, Expr):
        return False
    stack = [e]
    seen = set()
    while stack:
        x = stack.pop()
        if id(x) in seen or isinstance(x, AddrOf):
            continue
        seen.add(id(x))
        if isinstance(x, (Var, Deref, Member)) or type(x).__name__ in ('VTbl',):
            return True
        stack.extend(c for c in x.children() if isinstance(c, Expr))
    return False


def flags_equal(a, b):
    if a is None or b is None or a[0] != b[0] or len(a) != len(b):
        return False
    for x, y in zip(a[1:], b[1:]):
        if isinstance(x, Expr) or isinstance(y, Expr):
            if not same(x, y):
                return False
        elif x != y:
            return False
    return True


def merge_states(states, preds=None):
    """Meet of predecessor exit states (keep only agreeing values)."""
    if preds is not None:
        pairs = [(p, s) for p, s in zip(preds, states) if s is not None]
        preds = [p for p, s in pairs]
        states = [s for p, s in pairs]
    states = [s for s in states if s is not None]
    if not states:
        return None
    all_states = list(states)
    m = states[0].copy()
    m.flags = states[0].flags if all(flags_equal(states[0].flags, s.flags) for s in states[1:]) else None
    for s in states[1:]:
        for r in list(m.regs):
            a, b = m.regs.get(r), s.regs.get(r)
            if a is not None and b is not None and same(a, b):
                continue
            if b is None or isinstance(b, Unknown):
                continue            # unknown on that path: keep the known value
            if isinstance(a, Unknown):
                m.regs[r] = b
                continue
            del m.regs[r]
        for r, b in s.regs.items():
            if r not in m.regs and not isinstance(b, Unknown) and all(
                    (x.regs.get(r) is None or isinstance(x.regs.get(r), Unknown) or same(x.regs.get(r), b))
                    for x in states):
                m.regs[r] = b
        if len(s.fpu) != len(m.fpu) or any(not same(a, b) for a, b in zip(s.fpu, m.fpu)):
            n = min(len(s.fpu), len(m.fpu))
            m.fpu = [a if same(a, b) else Unknown('st') for a, b in zip(m.fpu[-n:], s.fpu[-n:])] if n else []
        if s.sp != m.sp:
            m.sp = min(m.sp, s.sp)
        for k in list(m.mem):
            if k not in s.mem or s.mem[k][0] != m.mem[k][0] or not same(s.mem[k][1], m.mem[k][1]):
                del m.mem[k]
    for k in list(m.mem):
        m.mem[k] = (m.mem[k][0], m.mem[k][1], None)
    # pending argument cells that differ per predecessor (only below every frame: k < sp of all)
    if preds is not None and len(all_states) > 1 and all(s.sp == all_states[0].sp for s in all_states):
        sp = all_states[0].sp
        keys = set(all_states[0].mem)
        for s in all_states[1:]:
            keys &= set(s.mem)
        for k in keys:
            if k in m.mem or k < sp:
                continue
            ents = [s.mem[k] for s in all_states]
            if all(e[0] == ents[0][0] for e in ents):
                m.mem[k] = (ents[0][0], PhiCell(list(zip(preds, [e[1] for e in ents]))), None)
    return m


def may_alias(d1, d2):
    """Can memory reads/writes through these two pointers overlap? Only provably separate when both
    addresses are the same variable part plus constants that put them at least a size apart."""
    from simplify import linear
    l1, l2 = linear(d1.base), linear(d2.base)
    if l1 is None or l2 is None:
        return True
    t1 = {k: c for k, (t, c) in l1[0].items() if c}
    t2 = {k: c for k, (t, c) in l2[0].items() if c}
    if t1 != t2:
        # two different arrays (different data pointers) never share memory in VB
        b1 = {k for k, (t, c) in l1[0].items() if c == 1 and isinstance(t, (Var, Deref)) and
              not (isinstance(t, Var) and t.kind == 'reg')}
        b2 = {k for k, (t, c) in l2[0].items() if c == 1 and isinstance(t, (Var, Deref)) and
              not (isinstance(t, Var) and t.kind == 'reg')}
        if len(b1) == 1 and len(b2) == 1 and b1 != b2:
            return False
        return True
    a1, a2 = l1[1] + d1.off, l2[1] + d2.off
    return not (a1 + d1.size <= a2 or a2 + d2.size <= a1)


# --------------------------------------------------------------------------- the lifter
class Lifter:
    """Lifts one procedure: per-block statement lists + terminators."""

    def __init__(self, proj, cfg, proc, rt, tlb, ctx):
        self.proj = proj
        self.im = proj.im
        self.cfg = cfg
        self.proc = proc
        self.rt = rt
        self.tlb = tlb
        self.ctx = ctx
        self.is_method = proc.obj is not None and proc.obj.kind in ('form', 'class')
        self.temp_n = 0
        self.block_stmts = {}
        self.block_term = {}
        self.out_state = {}
        self.in_state = {}
        self.warnings = []
        self.frame_R = None
        self.local_low = None
        self.ret_values = []
        self.cur = None
        self.st = None
        self.stmts = None
        self.vartypes = {}
        self.var_sizes = {}
        self.callee_saved = set()
        self.materialized = {}
        self._tcount = {}
        self.saved_cells = set()       # stack offsets holding pushed callee-saved registers

    # ------------------------------------------------------------------ helpers
    def warn(self, msg):
        self.warnings.append('%x: %s' % (self.cur.addr if self.cur else 0, msg))

    def new_temp(self, ty=None):
        addr = self.cur.addr if self.cur is not None else 0
        n = self._tcount.get(addr, 0)
        self._tcount[addr] = n + 1
        return Temp('t%x_%d' % (addr, n), ty)

    def emit(self, s):
        if self.cur is not None and not s.addr:
            s.addr = self.cur.addr
        self.stmts.append(s)
        return s

    def call_value(self, call, ty=None):
        t = self.new_temp(ty or call.ty)
        self.emit(Assign(t, call))
        return t

    def call_stmt(self, call):
        self.emit(ExprStmt(call))

    def note_type(self, e, ty, weight=1):
        """Type evidence for a variable. weight 100 = a fact the compiler wrote down (e.g. the type
        code of an Input #/Write # item), which outranks evidence from how values are used."""
        if isinstance(e, AddrOf):
            e = e.e
        if isinstance(e, VTbl):
            e = Deref(e.obj, 0, 4)      # a dword read through a pointer (not necessarily an object)
        if isinstance(e, Deref) and e.off == 0 and isinstance(e.base, Var) and e.base.kind == 'arg' and ty:
            d = self.vartypes.setdefault(('v', 'param', e.base.off), {})
            d[ty] = d.get(ty, 0) + weight
            return
        if isinstance(e, Var) and ty:
            d = self.vartypes.setdefault(e.key(), {})
            d[ty] = d.get(ty, 0) + weight

    # ------------------------------------------------------------------ stack locations
    def loc_kind(self, o):
        if o >= 4:
            return 'arg'
        if o >= 0:
            return 'ret'
        if self.frame_R is not None and o >= -4 - self.frame_R - 8:
            return 'internal'
        if self.local_low is not None and o < self.local_low:
            return 'cell'
        return 'local'

    def stack_var(self, o, size, ty=None):
        k = self.loc_kind(o)
        if k == 'arg':
            if self.is_method and o == 4:
                return Me()
            return Var('arg', o - 4, size, ty)
        if k in ('local', 'cell'):
            return Var('local', -(o + 4), size, ty)
        if k == 'internal':
            return Var('internal', -(o + 4), size, ty)
        return Var('stack', o, size, ty)

    def slot_var(self, o, size, ty=None):
        """Variable designated by the stack address o (a parameter slot reused as scratch -> its variable)."""
        if (self.proc.va, o) in self.ctx.arg_slot_written and self.loc_kind(o) == 'arg' and \
                not (o == 4 and self.is_method):
            return Var('local', 0x10000 + o - 4, size, ty)
        if o == 4 and self.is_method and self.proc.va in self.ctx.me_slot_written:
            # Me's slot reused as scratch (e.g. a constant passed ByRef): the scratch variable, not Me
            return Var('local', 0x7ff0, size, ty, name='var_Scratch')
        return self.stack_var(o, size, ty)

    def var_loc(self, v):
        """E-relative offset of a stack variable (inverse of stack_var)."""
        if isinstance(v, Var):
            if v.kind == 'local':
                return -v.off - 4
            if v.kind == 'arg':
                return v.off + 4
        return None

    def mem_store(self, o, size, val, stmt=None):
        st = self.st
        for k in list(st.mem):
            ks = st.mem[k][0]
            if k < o + size and o < k + ks:
                del st.mem[k]
        if stmt is not None and self.loc_kind(o) in ('local', 'arg'):
            target = self.stack_var(o, size)
            if isinstance(target, Var) and reads_location(val, target):
                # x = f(x): later reads must see the new x, not the expression over the old x
                return
            if not is_simple_value(val) and contains_call(val):
                # visible assignment of a call result: later reads show the variable
                return
        st.mem[o] = (size, val, stmt)

    def mem_entry(self, o, size):
        e = self.st.mem.get(o)
        if e is not None and e[0] == size:
            return e
        return None

    def mem_load(self, o, size):
        """Forwarded value for a stack store, else None."""
        e = self.st.mem.get(o)
        if e is not None and e[0] == size:
            return e[1]
        if size == 8:
            lo = self.st.mem.get(o)
            hi = self.st.mem.get(o + 4)
            if lo and hi and lo[0] == 4 and hi[0] == 4:
                a, b = lo[1], hi[1]
                if isinstance(a, Const) and isinstance(b, Const):
                    raw = struct.pack('<II', a.v & 0xffffffff, b.v & 0xffffffff)
                    return FConst(struct.unpack('<d', raw)[0])
                pa, pb = self.var_parts(a), self.var_parts(b)
                if pa and pb and pa[0] == pb[0] and pb[1] == pa[1] + 4:
                    return self.var_at(pa[0], pa[1], 8)
        return None

    def var_parts(self, e):
        """(base, byte offset) for multi-dword copies of variables."""
        if isinstance(e, Var):
            if e.kind == 'local':
                return ('local', -e.off)
            if e.kind in ('arg', 'field', 'global'):
                return (e.kind, e.off)
        return None

    def var_at(self, base, off, size):
        if base == 'local':
            return Var('local', -off, size)
        return Var(base, off, size)

    def invalidate_locals(self):
        st = self.st
        for k in list(st.mem):
            kind = self.loc_kind(k)
            if kind == 'arg' and isinstance(st.mem[k][1], Var) and st.mem[k][1].kind == 'arg' and \
                    st.mem[k][1].off == k - 4:
                continue
            if kind in ('local', 'arg'):
                del st.mem[k]

    def invalidate_after_call(self, addr_args):
        """After a call to one of the program's own procedures. VB code can change the caller's locals only
        through the addresses passed to it (ByRef arguments): those are forgotten. Other locals keep constant
        contents (e.g. the type tag of a variant being built); contents read from memory (fields...) are
        forgotten too, since the callee may have changed that memory - the local itself holds the value."""
        st = self.st
        passed = []
        for a in addr_args:
            o = a.o if isinstance(a, SAddr) else (self.var_loc(a.e) if isinstance(a, AddrOf) else None)
            if o is not None:
                v = self.stack_var(o, 4)
                size = (self.var_sizes.get(v.key()) if isinstance(v, Var) else None) or 4
                if (o + 8) in st.mem and (o + 4) not in st.mem:
                    size = max(size, 16)        # a variant being built (type tag + data): all 16 bytes
                passed.append((o, size))
        for k in list(st.mem):
            kind = self.loc_kind(k)
            if kind == 'arg' and isinstance(st.mem[k][1], Var) and st.mem[k][1].kind == 'arg' and \
                    st.mem[k][1].off == k - 4:
                continue
            if kind not in ('local', 'arg'):
                continue
            if any(o <= k < o + n for o, n in passed):
                del st.mem[k]
            elif not isinstance(st.mem[k][1], (Const, FConst, Str, SAddr, DataPtr, CodePtr, ImportFn)):
                del st.mem[k]

    def invalidate_var(self, v, size=16):
        o = self.var_loc(v)
        if o is None:
            return
        for k in list(self.st.mem):
            if o - 16 < k < o + max(size, 16) and self.loc_kind(k) != 'cell':
                del self.st.mem[k]

    # ------------------------------------------------------------------ registers
    def reg(self, name):
        st = self.st
        if name == 'esp':
            return SAddr(st.sp)
        if name in REGS32:
            v = st.regs.get(name)
            return v if v is not None else Unknown(name)
        if name in SUB16:
            return self.low(self.reg(SUB16[name]), 16)
        if name in SUB8L:
            return self.low(self.reg(SUB8L[name]), 8)
        if name in SUB8H:
            v = self.reg(SUB8H[name])
            if isinstance(v, (FpuStatus, FpuMasked)) or type(v).__name__ == 'FpuAH':
                return v
            if isinstance(v, Const):
                return Const((v.v >> 8) & 0xff)
            return Unknown(name)
        return Unknown(name)

    def low(self, v, bits):
        if isinstance(v, Const):
            x = v.v & ((1 << bits) - 1)
            return Const(s16(x) if bits == 16 else x, v.ty)
        if bits == 16 and isinstance(v, Var) and v.kind in ('local', 'reg') and v.size == 4:
            # only the low word of this dword slot is used (a function's Long result kept in an Integer,
            # cut to size): the variable is an Integer
            self.note_type(v, 'Integer', 100)
        if bits == 16:
            return low16(v)
        return v

    def set_reg(self, name, val):
        st = self.st
        if name == 'esp':
            if isinstance(val, SAddr):
                st.sp = val.o
            else:
                self.warn('esp set to non-stack value (%s)' % (self.cur.text if self.cur else '?'))
            return
        if name in REGS32:
            st.regs[name] = self.cap(val)
            return
        full = SUB16.get(name) or SUB8L.get(name) or SUB8H.get(name)
        if full:
            old = st.regs.get(full)
            if isinstance(val, Const) and isinstance(old, Const):
                if name in SUB16:
                    st.regs[full] = Const((old.v & 0xffff0000) | (val.v & 0xffff))
                elif name in SUB8L:
                    st.regs[full] = Const((old.v & 0xffffff00) | (val.v & 0xff))
                else:
                    st.regs[full] = Const((old.v & 0xffff00ff) | ((val.v & 0xff) << 8))
            else:
                st.regs[full] = val

    MAX_TREE = 120

    def cap(self, val):
        """Bind very large expressions to a temporary (keeps output readable, avoids blowup)."""
        if isinstance(val, Expr) and not isinstance(val, (Temp, Var, Const, FConst, Str, Me)) and \
                val.tree_size() > self.MAX_TREE:
            t = self.new_temp(getattr(val, 'ty', None))
            self.emit(Assign(t, val))
            return t
        return val

    # ------------------------------------------------------------------ memory operands
    def addr_of_mem(self, op):
        if op.seg == 'fs':
            return ('fs',)
        base = self.reg(op.base) if op.base else None
        idx = self.reg(op.index) if op.index else None
        disp = s32(op.disp)
        if base is None and idx is None:
            return ('abs', disp & 0xffffffff)
        if isinstance(base, SAddr) and idx is None:
            return ('stack', base.o + disp)
        if isinstance(base, SAddr) and isinstance(idx, Const):
            return ('stack', base.o + disp + idx.v * op.scale)
        if base is None and isinstance(idx, SAddr) and op.scale == 1:
            return ('stack', idx.o + disp)
        if isinstance(base, AddrOf) and idx is None:
            o = self.var_loc(base.e)
            if o is not None:
                return ('stack', o + disp)
        return ('ptr', base, idx, op.scale, disp)

    def load(self, op, size=None, ty=None):
        size = size or op.size
        if op.kind == 'reg':
            return self.reg(op.reg)
        if op.kind == 'imm':
            return self.imm_value(op.imm, size)
        return self.load_addr(self.addr_of_mem(op), size, ty)

    def mem_var(self, op, size):
        """The variable a memory operand designates (not the value its stack cell currently holds)."""
        a = self.addr_of_mem(op)
        if a[0] == 'stack':
            if self.loc_kind(a[1]) not in ('local', 'arg'):
                return None
            v = self.slot_var(a[1], size)
            return v if isinstance(v, Var) else None
        if a[0] == 'abs':
            v = self.load_abs(a[1], size)
            return v if isinstance(v, Var) else None
        if a[0] == 'ptr':
            return self.deref(a[1], a[2], a[3], a[4], size)
        return None

    def load_addr(self, a, size, ty=None):
        kind = a[0]
        if kind == 'fs':
            return Unknown('fs')
        if kind == 'abs':
            return self.load_abs(a[1], size, ty)
        if kind == 'stack':
            o = a[1]
            f = self.mem_load(o, size)
            if f is not None:
                return f
            if o == 4 and self.is_method and self.proc.va in self.ctx.me_slot_written:
                return Var('local', 0x7ff0, size, name='var_Scratch')
            if (self.proc.va, o) in self.ctx.arg_slot_written and self.loc_kind(o) == 'arg':
                e = self.st.mem.get(o)
                if e is None or not (isinstance(e[1], Var) and e[1].kind == 'arg'):
                    return Var('local', 0x10000 + o - 4, size)
            v = self.stack_var(o, size, ty)
            if isinstance(v, Var) and v.kind in ('local', 'arg'):
                self.var_sizes[v.key()] = max(self.var_sizes.get(v.key(), 0), size)
                e = self.st.mem.get(o)
                if size == 2 and v.kind == 'local' and e is not None and e[0] == 4:
                    # a dword written here (a Long function result) is read back as a word: the variable
                    # is an Integer that received the Long cut to size
                    self.note_type(v, 'Integer', 100)
            return v
        _, base, idx, scale, disp = a
        return self.deref(base, idx, scale, disp, size, ty)

    def deref(self, base, idx, scale, disp, size, ty=None):
        if isinstance(idx, Const):
            disp += idx.v * scale
            idx = None
        if isinstance(idx, Me) and scale == 1:
            base, idx = idx, base
        if isinstance(base, Bin) and base.op in ('+', '-') and isinstance(base.a, Me) and \
                isinstance(base.b, Const):
            disp += base.b.v if base.op == '+' else -base.b.v
            base = base.a
        if idx is not None:
            off = Bin('*', idx, Const(scale)) if scale != 1 else idx
            if base is None:
                return Deref(off, disp, size, ty)
            return Deref(Bin('+', base, off), disp, size, ty)
        if isinstance(base, Me):
            if disp == 0:
                return VTbl(base)
            return self.field(disp, size, ty)
        if isinstance(base, AddrOf):
            if disp == 0 and isinstance(base.e, Var) and base.e.size == size:
                return base.e
            if isinstance(base.e, Var) and base.e.kind == 'field' and base.e.size <= 4 and size <= 4:
                return self.field(base.e.off + disp, size, ty)
            if disp == 0 and isinstance(base.e, Var) and base.e.kind in ('local', 'global') and \
                    base.e.size <= 4 and size <= 4:
                return Var(base.e.kind, base.e.off, size, ty or base.e.ty, base.e.name)
            return Member(base.e, disp, size, ty=ty)
        if isinstance(base, DataPtr):
            return self.load_abs(base.va + disp, size, ty)
        if isinstance(base, Const):
            return self.load_abs((base.v + disp) & 0xffffffff, size, ty)
        if disp == 0 and size == 4:
            return VTbl(base)
        return Deref(base, disp, size, ty)

    def field_addr(self, off):
        """Address of a form field: taking it says nothing about the field's size (only accesses do)."""
        v = Var('field', off, 4)
        self.var_sizes.setdefault(v.key(), 0)
        return AddrOf(v)

    def field(self, off, size, ty=None):
        v = Var('field', off, size, ty)
        self.var_sizes[v.key()] = max(self.var_sizes.get(v.key(), 0), size)
        return v

    def section_writable(self, va):
        return self.ctx.is_data_va(va)

    def load_abs(self, va, size, ty=None):
        im = self.im
        if va in im.imports:
            return ImportFn(im.imports[va])
        if im.section_of(va) is None:
            return Deref(Const(va), 0, size)
        if self.section_writable(va):
            v = Var('global', va, size, ty)
            self.var_sizes[v.key()] = max(self.var_sizes.get(v.key(), 0), size)
            return v
        if size == 8:
            return FConst(im.f64(va))
        if size == 10:
            return FConst(0.0)
        if size == 4 and ty == 'Single':
            return FConst(im.f32(va), 'Single')
        if size == 4:
            return Const(s32(im.u32(va)))
        if size == 2:
            return Const(im.i16(va))
        if size == 1:
            return Const(im.u8(va))
        return Deref(Const(va), 0, size)

    def imm_value(self, v, size=4):
        im = self.im
        v &= 0xffffffff
        if v > 0x10000 and im.valid(v, 2):
            if v in im.imports:
                return ImportFn(im.imports[v])
            writable = self.section_writable(v)
            s = im.bstr(v) if not writable else None
            if s is not None:
                st = Str(s)
                st.va = v       # data that merely looks like a string (e.g. a Print/Input descriptor)
                return st
            if im.is_code(v) and self.proj.code_start <= v < self.proj.code_end:
                return CodePtr(v)
            if self.section_writable(v):
                return AddrOf(Var('global', v, 4))
            return DataPtr(v)
        if size == 2:
            return Const(s16(v))
        if size == 1:
            return Const(v & 0xff)
        return Const(s32(v))

    def store(self, op, val, size=None):
        size = size or op.size
        if op.kind == 'reg':
            self.set_reg(op.reg, val)
            return
        self.store_addr(self.addr_of_mem(op), val, size)

    def store_addr(self, a, val, size, kind='let'):
        if a[0] == 'fs':
            return
        if a[0] == 'abs':
            dst = self.load_abs(a[1], size)
            if not isinstance(dst, Var):
                dst = Deref(Const(a[1]), 0, size)
            self.materialize_refs(dst, val)
            val = self.materialized.get(id(val), val)
            self.emit(Assign(dst, self.fit(val, size), kind=kind))
            return
        if a[0] == 'stack':
            o = a[1]
            k = self.loc_kind(o)
            if k == 'internal' or k == 'ret':
                return
            if k == 'cell':
                self.mem_store(o, size, val, None)
                return
            v = self.stack_var(o, size)
            if isinstance(v, Me) and isinstance(val, Me):
                self.mem_store(o, size, val, None)
                return
            if isinstance(v, Me):
                # the compiler reuses Me's stack slot as scratch space (e.g. a loop counter):
                # from then on it is an ordinary local variable
                self.ctx.me_slot_written.add(self.proc.va)
                slot = Var('local', 0x7ff0, size, name='var_Scratch')
                self.materialize_refs(slot, val)
                val = self.materialized.get(id(val), val)
                s = self.emit(Assign(slot, self.fit(val, size), kind=kind))
                s.local_store = True
                self.mem_store(o, size, val, s)
                return
            if isinstance(val, Me) and k == 'arg':
                return
            if k == 'arg' and isinstance(v, Var):
                self.ctx.arg_slot_written.add((self.proc.va, o))
                slot = Var('local', 0x10000 + v.off, size)
                self.materialize_refs(slot, val)
                val = self.materialized.get(id(val), val)
                s = self.emit(Assign(slot, self.fit(val, size), kind=kind))
                s.local_store = True
                self.mem_store(o, size, val, s)
                return
            self.materialize_refs(v, val)
            val = self.materialized.get(id(val), val)
            s = self.emit(Assign(v, self.fit(val, size), kind=kind))
            s.local_store = True
            self.mem_store(o, size, val, s)
            if isinstance(v, Var):
                self.var_sizes[v.key()] = max(self.var_sizes.get(v.key(), 0), size)
            return
        _, base, idx, scale, disp = a
        dst = self.deref(base, idx, scale, disp, size)
        if isinstance(dst, VTbl):
            dst = Deref(dst.obj, 0, size)
        st_ty = getattr(self, 'store_ty', None)
        if st_ty and isinstance(dst, Deref) and dst.ty is None:
            dst.ty = st_ty          # integer / floating-point store (set by the FPU store handler)
        self.materialize_refs(dst, val)
        val = self.materialized.get(id(val), val)
        self.emit(Assign(dst, self.fit(val, size), kind=kind))

    def fit(self, val, size):
        if isinstance(val, Const) and size == 2:
            return Const(s16(val.v), val.ty)
        if isinstance(val, Const) and size == 1:
            return Const(val.v & 0xff, val.ty)
        return val

    def materialize_refs(self, loc, val=None):
        """Before overwriting `loc` (with `val`), snapshot register/FPU/flags values that refer to it."""
        k = ekey(loc)
        st = self.st
        cls = type(loc)

        def refers(e):
            # does value e read the current contents of loc? (taking its address does not)
            if not isinstance(e, Expr):
                return False
            seen = set()
            stack = [e]
            while stack:
                x = stack.pop()
                if id(x) in seen or isinstance(x, AddrOf):
                    continue
                seen.add(id(x))
                if type(x) is cls and ekey(x) == k:
                    return True
                if isinstance(loc, Deref) and isinstance(x, Deref) and may_alias(loc, x):
                    return True     # e.g. a(i) and a(j): another element of the same array, maybe this one
                stack.extend(c for c in x.children() if isinstance(c, Expr))
            return False
        self.materialized = {}
        for r, v in list(st.regs.items()):
            if isinstance(v, (Me, SAddr, ImportFn, CodePtr, DataPtr)):
                continue
            if refers(v):
                t = self.materialized.get(id(v))
                if t is None:
                    # (a Variant converted by the runtime, __vbaI2Var...: the temporary holds the result)
                    t = self.new_temp(held_type(v))
                    self.emit(Assign(t, v))
                    self.materialized[id(v)] = t
                st.regs[r] = t
        for i, v in enumerate(st.fpu):
            if refers(v):
                t = self.new_temp('Double')
                self.emit(Assign(t, v))
                st.fpu[i] = t
        for o, (sz, v, s) in list(st.mem.items()):
            if self.loc_kind(o) == 'cell' and refers(v):
                t = self.new_temp(getattr(v, 'ty', None))
                self.emit(Assign(t, v))
                st.mem[o] = (sz, t, s)
            elif refers(v):
                # a cached local value computed from loc is stale after loc changes
                del st.mem[o]
        # The pending comparison for the next conditional jump can read loc too. VB loops often do
        # `inc eax / cmp ax, 6 / mov [counter], eax / jle`: the test is on the value being stored, so
        # after the store it reads loc itself; any other test of the old contents keeps a temporary.
        if st.flags is not None:
            new = list(st.flags)
            for i, e in enumerate(st.flags):
                if i == 0 or not isinstance(e, Expr) or not refers(e):
                    continue
                if val is not None and (e is val or same(e, val)):
                    new[i] = loc
                    continue
                t = self.materialized.get(id(e))
                if t is None:
                    t = self.new_temp(getattr(e, 'ty', None))
                    self.emit(Assign(t, e))
                    self.materialized[id(e)] = t
                new[i] = t
            st.flags = tuple(new)

    # ------------------------------------------------------------------ pointers (for runtime handlers)
    def deref_ptr(self, p, size=4, ty=None):
        """Value stored at pointer p."""
        if isinstance(p, SAddr):
            return self.load_addr(('stack', p.o), size, ty)
        if isinstance(p, AddrOf):
            o = self.var_loc(p.e)
            if o is not None:
                f = self.mem_load(o, size)
                if f is not None:
                    return f
            e = p.e
            if isinstance(e, Var) and ty:
                self.note_type(e, ty)
            return e
        if isinstance(p, DataPtr):
            return self.load_abs(p.va, size, ty)
        if isinstance(p, Const):
            return self.load_abs(p.v & 0xffffffff, size, ty)
        return Deref(p, 0, size, ty)

    def ptr_target(self, p, size=4, ty=None):
        """The storage location designated by pointer p (an lvalue expression)."""
        if isinstance(p, SAddr):
            v = self.slot_var(p.o, size, ty)
            if isinstance(v, Var):
                if ty:
                    self.note_type(v, ty)
                self.var_sizes[v.key()] = max(self.var_sizes.get(v.key(), 0), size)
            return v
        if isinstance(p, AddrOf):
            if ty:
                self.note_type(p.e, ty)
            return p.e
        if isinstance(p, DataPtr):
            return Var('global', p.va, size, ty)
        if isinstance(p, Const):
            return self.load_abs(p.v & 0xffffffff, size, ty)
        return Deref(p, 0, size, ty)

    def store_ptr(self, p, val, size=4, ty=None, kind='let'):
        """Emit an assignment *p = val."""
        if isinstance(p, SAddr):
            k = self.loc_kind(p.o)
            if k == 'cell':
                self.mem_store(p.o, size, val, None)
                return None
            dst = self.slot_var(p.o, size, ty)
            if ty:
                self.note_type(dst, ty)
            self.materialize_refs(dst, val)
            s = self.emit(Assign(dst, val, kind=kind))
            s.local_store = True
            self.mem_store(p.o, size, val, s)
            if isinstance(dst, Var):
                self.var_sizes[dst.key()] = max(self.var_sizes.get(dst.key(), 0), size)
            return s
        dst = self.ptr_target(p, size, ty)
        self.materialize_refs(dst, val)
        s = self.emit(Assign(dst, val, kind=kind))
        o = self.var_loc(dst) if isinstance(dst, Var) else None
        if o is not None:
            s.local_store = True
            self.mem_store(o, size, val, s)
        return s

    def variant_at(self, p):
        """Reconstruct the VB value of a VARIANT at pointer p (by-reference variant argument)."""
        if isinstance(p, SAddr) or (isinstance(p, AddrOf) and self.var_loc(p.e) is not None):
            o = p.o if isinstance(p, SAddr) else self.var_loc(p.e)
            v = self.variant_from_mem(o)
            if v is not None:
                return v
            var = self.stack_var(o, 16)
            if isinstance(var, Var):
                self.note_type(var, 'Variant')
                self.var_sizes[var.key()] = max(self.var_sizes.get(var.key(), 0), 16)
            return var
        if isinstance(p, AddrOf):
            self.note_type(p.e, 'Variant')
            return p.e
        if isinstance(p, DataPtr):
            return Var('global', p.va, 16, 'Variant')
        return Deref(p, 0, 16, 'Variant')

    def variant_from_mem(self, o):
        """Variant built from partial stores (vt + value) at stack offset o."""
        st = self.st
        vt_e = None
        for sz in (2, 4):
            e = st.mem.get(o)
            if e and e[0] == sz and isinstance(e[1], Const):
                vt_e = e[1].v & 0xffff
        whole = st.mem.get(o)
        if whole and whole[0] == 16:
            return whole[1]
        if vt_e is None:
            return None
        val8 = st.mem.get(o + 8)
        val12 = st.mem.get(o + 12)
        vt = vt_e
        byref = bool(vt & 0x4000)
        base = vt & 0xfff
        if val8 is not None and not byref and base in VT_NAMES:
            pv = self.var_parts(val8[1])
            if pv is not None:
                self.note_type(self.var_at(pv[0], pv[1], 4), VT_NAMES[base])
        if base == 10:  # VT_ERROR
            if val8 and isinstance(val8[1], Const) and (val8[1].v & 0xffffffff) == 0x80020004:
                return Missing()
            return Unknown('error-variant')
        if base == 0 and not byref:
            return Raw('Empty')
        if base == 1 and not byref:
            return Raw('Null')
        if val8 is None:
            size = {2: 2, 3: 4, 4: 4, 5: 8, 6: 8, 7: 8, 8: 4, 9: 4, 11: 2, 17: 1}.get(base)
            if byref or size is None or self.loc_kind(o + 8) not in ('local', 'cell'):
                return None
            v = Var('local', -(o + 8 + 4), size, VT_NAMES[base])
            # the variant's type tag is the compiler's statement of what the data slot holds (a constant
            # Integer is still written as a whole dword)
            self.note_type(v, VT_NAMES[base], 100)
            self.var_sizes[v.key()] = max(self.var_sizes.get(v.key(), 0), size)
            return v
        v = val8[1]
        if byref:
            # pointer to a variable
            if isinstance(v, (SAddr, AddrOf, DataPtr)):
                return self.deref_ptr(v, {2: 2, 3: 4, 4: 4, 5: 8, 8: 4, 11: 2, 12: 16}.get(base, 4),
                                      VT_NAMES.get(base))
            return Deref(v, 0, {2: 2, 3: 4, 4: 4, 5: 8, 8: 4, 11: 2, 12: 16}.get(base, 4), VT_NAMES.get(base))
        if base == 2:
            return Const(s16(v.v), 'Integer') if isinstance(v, Const) else v
        if base == 3:
            return Const(s32(v.v), 'Long') if isinstance(v, Const) else v
        if base == 11:
            if isinstance(v, Const):
                return Const(-1 if v.v & 0xffff else 0, 'Boolean')
            return v
        if base == 17:
            return v
        if base in (4,):
            if isinstance(v, Const):
                return FConst(struct.unpack('<f', struct.pack('<I', v.v & 0xffffffff))[0], 'Single')
            return v
        if base in (5, 7, 6):
            if val8[0] == 8:
                return v
            if val12 and isinstance(v, Const) and isinstance(val12[1], Const):
                raw = struct.pack('<II', v.v & 0xffffffff, val12[1].v & 0xffffffff)
                return FConst(struct.unpack('<d', raw)[0])
            return v
        if base == 8:
            return v
        if base in (9, 13):
            return v
        return v

    # ------------------------------------------------------------------ stack args
    def push(self, val, size=4):
        self.st.sp -= size
        self.mem_store(self.st.sp, size, val, None)

    def pop(self, size=4):
        v = self.load_addr(('stack', self.st.sp), size)
        self.st.sp += size
        return v

    def cell(self, off, size=4):
        """Read an argument cell at esp+off (without popping)."""
        o = self.st.sp + off
        e = self.st.mem.get(o)
        if e is not None and e[0] == size:
            return e[1]
        f = self.mem_load(o, size)
        if f is not None:
            return f
        if self.loc_kind(o) == 'cell':
            return Unknown('arg@%d' % off)
        return self.load_addr(('stack', o), size)

    def pending_cells(self):
        """Number of bytes currently pushed as outgoing arguments (saved registers excluded)."""
        if self.local_low is None:
            return 0
        top = self.local_low
        for o in self.saved_cells:
            if self.st.sp <= o < top:
                top = o
        return max(0, top - self.st.sp)

    def consume(self, nbytes):
        """Pop nbytes of stdcall arguments (callee cleanup)."""
        st = self.st
        for k in list(st.mem):
            if st.sp <= k < st.sp + nbytes:
                del st.mem[k]
        st.sp += nbytes

    def args_by_sizes(self, sizes, start=0):
        """Read stack args (esp+start...) with the given byte sizes; returns list of exprs."""
        out = []
        off = start
        for sz in sizes:
            if sz == 16:
                out.append(self.variant_cells(off))
            elif sz == 8:
                out.append(self.cell8(off))
            else:
                out.append(self.cell(off, 4))
            off += sz
        return out, off

    def cell8(self, off):
        o = self.st.sp + off
        e = self.st.mem.get(o)
        if e is not None and e[0] == 8:
            return e[1]
        f = self.mem_load(o, 8)
        if f is not None:
            return f
        return Unknown('qword@%d' % off)

    def variant_cells(self, off):
        """By-value VARIANT argument occupying 16 bytes at esp+off."""
        o = self.st.sp + off
        whole = self.st.mem.get(o)
        if whole and whole[0] == 16:
            return whole[1]
        cells = [self.st.mem.get(o + 4 * k) for k in range(4)]
        # copied from a variable: 4 consecutive dword loads of the same variable
        parts = []
        for c in cells:
            parts.append(self.var_parts(c[1]) if c and c[0] == 4 else None)
        if all(p is not None for p in parts) and all(parts[k][0] == parts[0][0] for k in range(4)) and \
                all(parts[k][1] == parts[0][1] + 4 * k for k in range(4)):
            return self.var_at(parts[0][0], parts[0][1], 16)
        # vt (in cell 0) + value cells
        c0 = cells[0]
        if c0 and isinstance(c0[1], Const):
            vt = c0[1].v & 0xffff
            v8 = cells[2][1] if cells[2] else None
            v12 = cells[3][1] if cells[3] else None
            base = vt & 0xfff
            if base == 10:
                return Missing() if isinstance(v8, Const) and (v8.v & 0xffffffff) == 0x80020004 else Unknown('err')
            if base == 0:
                return Raw('Empty')
            if base in (2, 3, 11, 17):
                if isinstance(v8, Const):
                    if base == 11:
                        return Const(-1 if v8.v & 0xffff else 0, 'Boolean')
                    return Const(s16(v8.v) if base == 2 else s32(v8.v), VT_NAMES[base])
                return v8 if v8 is not None else Unknown('variant')
            if base == 5 or base == 7:
                if isinstance(v8, Const) and isinstance(v12, Const):
                    raw = struct.pack('<II', v8.v & 0xffffffff, v12.v & 0xffffffff)
                    return FConst(struct.unpack('<d', raw)[0])
                pa, pb = self.var_parts(v8), self.var_parts(v12)
                if pa and pb and pa[0] == pb[0] and pb[1] == pa[1] + 4:
                    return self.var_at(pa[0], pa[1], 8)
                return v8 if v8 is not None else Unknown('variant')
            if base == 4:
                if isinstance(v8, Const):
                    return FConst(struct.unpack('<f', struct.pack('<I', v8.v & 0xffffffff))[0], 'Single')
                return v8
            if vt & 0x4000 and v8 is not None:
                return self.deref_ptr(v8, 4)
            if v8 is not None:
                return v8
        if c0 and isinstance(c0[1], Var) and cells[2] and isinstance(cells[2][1], Var):
            # vt copied from variable (variant variable copy with gaps)
            p = self.var_parts(c0[1])
            if p:
                return self.var_at(p[0], p[1], 16)
        return Unknown('variant@%d' % off)
