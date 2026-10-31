"""x86 instruction semantics for the VB6 lifter, call dispatch, and per-procedure driver."""
import re
import struct

from ir import (Expr, Const, FConst, Str, Var, Member, Deref, AddrOf, Bin, Un, Call, Temp, Me, Missing,
                Unknown, Raw, Assign, ExprStmt, Text, Comment, FmtStmt, ekey, same)
from lift import (Lifter, State, merge_states, SAddr, VTbl, ImportFn, CodePtr, DataPtr, StrCmpResult, Hresult,
                  FpuStatus, SetCC, BoolVal, negate, as_cond, is_const, s32, s16, VBCMP, INV, SWAP,
                  SUB8H, SUB8L, SUB16, flags_equal, held_type)
from cfg import JCC as JCC_ALL

SUB8L_REGS = set(SUB8L)
# DirectInput property name -> the record its value is passed in (DIPROP_* use DIPROPLONG otherwise)
DIPROP_RECORDS = {'DIPROP_RANGE': 'DIPROPRANGE', 'DIPROP_GUIDANDPATH': 'DIPROPGUIDANDPATH',
                  'DIPROP_INSTANCENAME': 'DIPROPSTRING', 'DIPROP_PRODUCTNAME': 'DIPROPSTRING',
                  'DIPROP_KEYNAME': 'DIPROPSTRING', 'DIPROP_CPOINTS': 'DIPROPCPOINTS'}
FULL_REG = dict(SUB16, **SUB8L)     # al / ax -> eax

OUTCOMES = ('lt', 'eq', 'gt')
FSW = {'lt': 0x01, 'eq': 0x40, 'gt': 0x00}   # C0 / C3 bits of AH after fcom


def render_obj(e):
    from ir import render
    return render(e, 99)


def redirect(t, old, new):
    if t[0] == 'goto':
        return ('goto', new) if t[1] == old else t
    if t[0] == 'cond':
        return ('cond', t[1], new if t[2] == old else t[2], new if t[3] == old else t[3])
    if t[0] == 'switch':
        return ('switch', t[1], [new if x == old else x for x in t[2]])
    return t


class CdqOf(Expr):
    """edx after cdq: sign mask of eax (0 or -1)."""

    def __init__(self, a):
        self.a = a

    def children(self):
        return (self.a,)

    def key(self):
        return ('cdq', ekey(self.a))


class AbsPending(Expr):
    """x xor sign(x): first half of the branch-free Abs idiom."""

    def __init__(self, a):
        self.a = a

    def children(self):
        return (self.a,)

    def key(self):
        return ('absp', ekey(self.a))


class FpuAH(Expr):
    """AH register derived from the FPU status word, tracked per compare outcome."""

    def __init__(self, a, b, vals):
        self.a = a
        self.b = b
        self.vals = vals            # outcome -> int

    def key(self):
        return ('fah', ekey(self.a), ekey(self.b), tuple(sorted(self.vals.items())))


def flags_of(res, size=4):
    """Concrete flags dict for an 8-bit/32-bit integer result."""
    m = (1 << (size * 8)) - 1
    r = res & m
    zf = r == 0
    sf = bool(r >> (size * 8 - 1))
    pf = bin(r & 0xff).count('1') % 2 == 0
    return {'ZF': zf, 'SF': sf, 'CF': False, 'OF': False, 'PF': pf}


def jcc_taken(mn, f):
    if mn == 'je':
        return f['ZF']
    if mn == 'jne':
        return not f['ZF']
    if mn == 'jp':
        return f['PF']
    if mn == 'jnp':
        return not f['PF']
    if mn == 'jb':
        return f['CF']
    if mn == 'jae':
        return not f['CF']
    if mn == 'jbe':
        return f['CF'] or f['ZF']
    if mn == 'ja':
        return not f['CF'] and not f['ZF']
    if mn == 'js':
        return f['SF']
    if mn == 'jns':
        return not f['SF']
    if mn == 'jl':
        return f['SF'] != f['OF']
    if mn == 'jge':
        return f['SF'] == f['OF']
    if mn == 'jle':
        return f['ZF'] or f['SF'] != f['OF']
    if mn == 'jg':
        return not f['ZF'] and f['SF'] == f['OF']
    return None


SET_TO_J = {'sete': 'je', 'setne': 'jne', 'setl': 'jl', 'setge': 'jge', 'setle': 'jle', 'setg': 'jg',
            'setb': 'jb', 'setae': 'jae', 'setbe': 'jbe', 'seta': 'ja', 'sets': 'js', 'setns': 'jns',
            'setp': 'jp', 'setnp': 'jnp', 'setz': 'je', 'setnz': 'jne'}

REL_BY_SET = {
    frozenset(['lt']): '<', frozenset(['eq']): '=', frozenset(['gt']): '>',
    frozenset(['lt', 'eq']): '<=', frozenset(['gt', 'eq']): '>=', frozenset(['lt', 'gt']): '<>',
}

JREL_SIGNED = {'je': '=', 'jne': '<>', 'jl': '<', 'jge': '>=', 'jg': '>', 'jle': '<=',
               'jb': '<', 'jae': '>=', 'ja': '>', 'jbe': '<='}


class ProcLifter(Lifter):
    # ================================================================== driver
    def run(self):
        from cfg import liveness
        cfg = self.cfg
        order = self.rpo()
        self.live_in = liveness(cfg, set(order))
        self.phis = {}            # block -> registers merged into a register variable
        init = State()
        if self.is_method and self.proc.va in self.ctx.me_slot_written:
            init.mem[4] = (4, Me(), None)        # Me's slot holds Me until the code reuses it
        for o in range(4, 4 + (self.proc.ret_n or 0), 4):
            if not (o == 4 and self.is_method):
                init.mem[o] = (4, self.stack_var(o, 4), None)   # the parameter until overwritten
        for attempt in range(8):
            self.out_state = {}
            self.fixpoint(order, init)
            if not self.add_phis(order):
                break
        self.dup_of = {}
        self.duplicate_tail_merges(order)
        self.place_phi_assignments(order)
        return self

    def duplicate_tail_merges(self, order):
        """A block entered from several paths that pushed different argument values (the compiler
        merged the identical tail of those paths) is lifted once per predecessor."""
        from lift import PhiCell
        cfg = self.cfg
        for b in order:
            if b == cfg.entry or b in cfg.extra:
                continue
            preds = []
            for p in cfg.blocks[b].preds:
                preds.extend(self.dup_of.get(p, [p]))
            preds = [p for p in preds if p in self.out_state]
            if len(preds) < 2 or len(preds) > 12:
                continue
            addr_regs = {r for r in self.phis.get(b, ())
                         if any(isinstance(self.out_state[p].regs.get(r), (SAddr, AddrOf)) for p in preds)}
            if len(cfg.blocks[b].insns) <= 12:
                for r in self.phis.get(b, ()):
                    vals = [self.out_state[p].regs.get(r) for p in preds]
                    if all(isinstance(v, Var) and v.kind in ('field', 'global') for v in vals) and \
                            not all(same(v, vals[0]) for v in vals):
                        addr_regs.add(r)
            fpus = [self.out_state[p].fpu for p in preds]
            fpu_diff = bool(fpus[0]) and all(len(f) == len(fpus[0]) for f in fpus) and \
                any(not same(a, b) for f in fpus[1:] for a, b in zip(f, fpus[0]))
            # a join that starts by testing flags each path set differently (cmp ... ; jmp join ; join: jne)
            first = cfg.blocks[b].insns[0] if cfg.blocks[b].insns else None
            flags_diff = first is not None and first.mn in JCC_ALL and \
                all(self.out_state[p].flags is not None for p in preds) and \
                not all(flags_equal(self.out_state[p].flags, self.out_state[preds[0]].flags) for p in preds)
            merged = merge_states([self.out_state[p] for p in preds], preds)
            if merged is None or not addr_regs and not fpu_diff and not flags_diff and not any(
                    isinstance(v[1], PhiCell) and self.loc_kind(k) == 'cell' for k, v in merged.mem.items()):
                continue
            blk = cfg.blocks[b]
            copies = []
            for p in preds:
                eid = 0x70000000 + 0x100000 + len(getattr(self, 'edge_blocks', {})) + len(copies) + \
                    sum(len(v) for v in self.dup_of.values())
                pst = self.out_state[p].copy()
                for r in self.phis.get(b, ()):
                    if r not in addr_regs:
                        pst.regs[r] = Var('reg', '%s@%x' % (r, b), 4)
                self.lift_block(blk, pst, key=eid)
                if not hasattr(self, 'edge_blocks'):
                    self.edge_blocks = {}
                self.edge_blocks[eid] = b
                t = self.block_term[p]
                self.block_term[p] = redirect(t, b, eid)
                copies.append(eid)
            self.dup_of[b] = copies

    def is_diamond(self, preds, b):
        blocks = self.cfg.blocks
        heads = set()
        for p in preds:
            pp = [q for q in blocks[p].preds] if p in blocks else []
            if blocks[p].term == 'jcc':
                heads.add(p)
            elif len(pp) == 1:
                heads.add(pp[0])
            else:
                return False
        return len(heads) == 1

    def fixpoint(self, order, init):
        cfg = self.cfg
        changed = True
        rounds = 0
        while changed and rounds < 8:
            changed = False
            rounds += 1
            for b in order:
                blk = cfg.blocks[b]
                preds = [p for p in blk.preds if p in self.out_state]
                if b == cfg.entry:
                    st = init.copy()
                elif b in cfg.extra and not preds:
                    st = self.handler_state()
                else:
                    if len(preds) >= 2:
                        # registers live here with disagreeing known incoming values become variables now
                        # (deciding inside the iteration keeps it monotonic)
                        for r in self.live_in.get(b, ()):
                            if r in self.phis.get(b, ()):
                                continue
                            known = [self.out_state[p].regs.get(r) for p in preds]
                            known = [v for v in known if v is not None and not isinstance(v, Unknown)]
                            if len(known) >= 2 and not all(same(v, known[0]) for v in known) and \
                                    not any(isinstance(v, ImportFn) for v in known):
                                self.phis.setdefault(b, set()).add(r)
                    st = merge_states([self.out_state[p] for p in preds], preds)
                    if st is not None:
                        # only outgoing-argument cells may become per-predecessor values
                        for k in list(st.mem):
                            v = st.mem[k][1]
                            if type(v).__name__ == 'PhiCell' and self.loc_kind(k) != 'cell':
                                del st.mem[k]
                    if st is None:
                        st = self.handler_state() if b in cfg.extra else State()
                        if b not in cfg.extra and preds == []:
                            st.sp = self.local_low if self.local_low is not None else 0
                for r in self.phis.get(b, ()):
                    st.regs[r] = Var('reg', '%s@%x' % (r, b), 4)
                self.in_state[b] = st.copy()
                old = self.out_state.get(b)
                self.lift_block(blk, st)
                new = self.out_state[b]
                if old is None or not self.state_eq(old, new):
                    changed = True

    def add_phis(self, order):
        """Registers live into a join block with differing incoming values become register variables."""
        added = False
        for b in order:
            blk = self.cfg.blocks[b]
            preds = [p for p in blk.preds if p in self.out_state]
            if len(preds) < 2:
                continue
            for r in self.live_in.get(b, ()):
                if r in self.phis.get(b, ()):
                    continue
                vals = [self.out_state[p].regs.get(r) for p in preds]
                known = [v for v in vals if v is not None and not isinstance(v, Unknown)]
                if not known:
                    continue
                if all(same(v, known[0]) for v in known):
                    continue
                if any(isinstance(v, ImportFn) for v in known):
                    continue
                self.phis.setdefault(b, set()).add(r)
                added = True
        return added

    def place_phi_assignments(self, order):
        pending = {}
        for b, regs in self.phis.items():
            blk = self.cfg.blocks[b]
            preds = []
            for p in blk.preds:
                preds.extend(self.dup_of.get(p, [p]))
            for p in preds:
                st = self.out_state.get(p)
                if st is None or p not in self.block_stmts:
                    continue
                for r in sorted(regs):
                    v = st.regs.get(r)
                    if v is None or isinstance(v, Unknown):
                        continue
                    name = '%s@%x' % (r, b)
                    if isinstance(v, Var) and v.kind == 'reg' and v.off == name:
                        continue
                    s = Assign(Var('reg', name, 4), v)
                    s.phi = True
                    s.addr = blk.start
                    s.phi = True
                    pending.setdefault(p, []).append(s)
        for p, assigns in pending.items():
            t = self.block_term.get(p)
            if not t or t[0] in ('goto', 'ret', 'stop'):
                self.block_stmts[p].extend(assigns)
                continue
            # p has several successors: the copies belong on the edge(s) into each phi block
            by_target = {}
            for s in assigns:
                tgt = int(s.dst.off.split('@')[1], 16)
                by_target.setdefault(tgt, []).append(s)
            new_t = list(t)
            for tgt, lst in by_target.items():
                eid = self.new_edge_block(lst, tgt)
                if t[0] == 'cond':
                    if new_t[2] == tgt:
                        new_t[2] = eid
                    if new_t[3] == tgt:
                        new_t[3] = eid
                elif t[0] == 'switch':
                    new_t[2] = [eid if x == tgt else x for x in new_t[2]]
            self.block_term[p] = tuple(new_t)

    def new_edge_block(self, stmts, target):
        if not hasattr(self, 'edge_blocks'):
            self.edge_blocks = {}
        eid = 0x70000000 + len(self.edge_blocks)
        while eid in self.block_stmts:
            eid += 1
        self.edge_blocks[eid] = target
        self.block_stmts[eid] = list(stmts)
        self.block_term[eid] = ('goto', target)
        return eid

    def handler_state(self):
        """State at an error-handler label: registers unknown, esp at frame bottom."""
        st = State()
        st.sp = self.local_low if self.local_low is not None else 0
        for r in ('ebx', 'esi', 'edi'):
            st.regs[r] = Unknown('handler_' + r)
        st.regs['ebp'] = SAddr(-4)
        for r in self.ctx_me_regs():
            st.regs[r] = Me()
        return st

    def ctx_me_regs(self):
        return []

    def state_eq(self, a, b):
        if a.sp != b.sp or len(a.fpu) != len(b.fpu):
            return False
        if set(a.regs) != set(b.regs):
            return False
        return all(same(a.regs[r], b.regs[r]) for r in a.regs)

    def rpo(self):
        cfg = self.cfg
        seen = set()
        out = []
        roots = [cfg.entry] + [h for h in cfg.extra if h in cfg.blocks]

        for root in roots:
            stack = [(root, 0)]
            while stack:
                v, i = stack.pop()
                if v not in cfg.blocks or v in cfg.error_blocks:
                    continue
                if i == 0:
                    if v in seen:
                        continue
                    seen.add(v)
                succs = cfg.blocks[v].succs
                if i < len(succs):
                    stack.append((v, i + 1))
                    stack.append((succs[i], 0))
                else:
                    out.append(v)
        out.reverse()
        return out

    def lift_block(self, blk, st, key=None):
        key = blk.start if key is None else key
        self.st = st
        self.stmts = []
        for ins in blk.insns:
            self._tcount.pop(ins.addr, None)
        self.term = None
        for ins in blk.insns:
            self.cur = ins
            try:
                self.execute(ins)
            except Exception as e:  # keep going; record the problem in the output
                import traceback
                self.warn('lift error %s: %s' % (ins.text, e))
                self.emit(Comment('[decompiler] could not lift %x: %s (%s)' % (ins.addr, ins.text, e)))
                if self.ctx.debug:
                    traceback.print_exc()
        # terminator
        self.block_term[key] = self.make_term(blk)
        self.block_stmts[key] = self.stmts
        self.out_state[key] = self.st

    def make_term(self, blk):
        if blk.term == 'jcc':
            cond = self.cond_for(blk.cond_insn.mn)
            if isinstance(cond, Const):
                return ('goto', blk.taken if cond.v else blk.fall)
            return ('cond', cond, blk.taken, blk.fall)
        if blk.term in ('jmp', 'fall'):
            return ('goto', blk.succs[0]) if blk.succs else ('stop',)
        if blk.term == 'ret':
            return ('ret',)
        if blk.term == 'switch':
            idx_reg, targets = blk.switch
            idx = self.reg(idx_reg)
            bt = getattr(blk, 'byte_table', None)
            if bt is not None and isinstance(idx, Deref) and idx.size == 1:
                # two-level table: idx = byte[x + table1]; show the cases in terms of x
                base = idx.base
                if (idx.off & 0xffffffff) == bt[0]:
                    return ('switch', base, bt[1])
                if isinstance(base, Bin) and base.op == '+' and isinstance(base.b, Const) and \
                        (base.b.v & 0xffffffff) == bt[0] and idx.off == 0:
                    return ('switch', base.a, bt[1])
            return ('switch', idx, targets)
        return ('stop',)

    # ================================================================== conditions
    def cond_for(self, mn):
        f = self.st.flags
        if f is None:
            return Unknown('cond:%s' % mn)
        kind = f[0]
        if kind == 'fah':
            # per-outcome evaluation
            fl = f[1]
            taken = frozenset(o for o in OUTCOMES if jcc_taken(mn, fl[o]))
            a, b = f[2], f[3]
            if not taken:
                return Const(0, 'Boolean')
            if len(taken) == 3:
                return Const(-1, 'Boolean')
            return Bin(REL_BY_SET[taken], a, b, ty='Boolean')
        if kind == 'cmp':
            _, a, b, size = f
            if mn in ('ja', 'jae', 'jb', 'jbe') and isinstance(b, Const) and b.v >= 0 and size > 1 and \
                    not isinstance(a, (SetCC, BoolVal)):
                # unsigned range check (VB `Case lo To hi` / jump-table bounds): a negative value is "above"
                neg = self.mk_rel('<', a, Const(0))
                nonneg = self.mk_rel('>=', a, Const(0))
                if mn == 'ja':
                    return Bin('Or', neg, self.mk_rel('>', a, b), ty='Boolean')
                if mn == 'jae':
                    return Bin('Or', neg, self.mk_rel('>=', a, b), ty='Boolean')
                if mn == 'jb':
                    return Bin('And', nonneg, self.mk_rel('<', a, b), ty='Boolean')
                return Bin('And', nonneg, self.mk_rel('<=', a, b), ty='Boolean')
            ex = self.exact_rel(a, b, size)
            if ex is not None:
                a, b = ex
            return self.int_cond(mn, a, b)
        if kind == 'test':
            _, a, b, size = f
            if same(a, b):
                return self.zero_cond(mn, a)
            if isinstance(b, Const) and isinstance(a, Expr):
                return self.zero_cond(mn, Bin('And', a, b))
            return self.zero_cond(mn, Bin('And', a, b))
        if kind == 'res':
            _, r, size = f
            ex = self.exact_rel(r, Const(0), size)
            if ex is not None and mn in JREL_SIGNED:
                return self.mk_rel(JREL_SIGNED[mn], ex[0], ex[1])
            return self.zero_cond(mn, r)
        if kind == 'cf':
            # carry flag set from a condition (e.g. after neg / bt): jb -> cond
            c = f[1]
            if mn in ('jb', 'jc'):
                return c
            if mn in ('jae', 'jnc'):
                return negate(c)
        return Unknown('cond:%s' % mn)

    @staticmethod
    def exact_rel(a, b, size):
        """`x + c rel k` computed in 32 bits on a sign/zero-extended 16-bit (or 8-bit) value x cannot wrap
        around: it is exactly `x rel k - c`. (A 16-bit operation or compare could wrap: left as it is.)
        Returns (x, Const(k - c)) or None."""
        if size != 4 or not isinstance(b, Const) or not isinstance(a, Bin):
            return None
        c = 0
        x = a
        while isinstance(x, Bin) and x.op in ('+', '-') and getattr(x, 'w', None) == 4 and isinstance(x.b, Const):
            c += x.b.v if x.op == '+' else -x.b.v
            x = x.a
        if isinstance(x, Bin) or not isinstance(x, (Var, Deref, Member)) or getattr(x, 'size', 4) > 2:
            return None
        if abs(c) > 0x10000 or abs(b.v) > 0x10000:
            return None
        return x, Const(b.v - c)

    def int_cond(self, mn, a, b):
        rel = JREL_SIGNED.get(mn)
        if rel is None:
            if mn == 'js':
                return Bin('<', Bin('-', a, b), Const(0), ty='Boolean')
            if mn == 'jns':
                return Bin('>=', Bin('-', a, b), Const(0), ty='Boolean')
            return Unknown('cond:%s' % mn)
        return self.mk_rel(rel, a, b)

    def zero_cond(self, mn, a):
        rel = {'je': '=', 'jne': '<>', 'js': '<', 'jns': '>=', 'jg': '>', 'jle': '<=', 'jl': '<', 'jge': '>=',
               'ja': '<>', 'jbe': '=', 'jb': 'never', 'jae': 'always'}.get(mn)
        if rel is None:
            return Unknown('cond:%s' % mn)
        if rel == 'never':
            return Const(0, 'Boolean')
        if rel == 'always':
            return Const(-1, 'Boolean')
        return self.mk_rel(rel, a, Const(0))

    def mk_rel(self, rel, a, b):
        """Build `a rel b`, simplifying booleans / string compares / setcc values."""
        if isinstance(a, Const) and not isinstance(b, Const):
            a, b, rel = b, a, SWAP[rel]
        if is_const(b, 0):
            if isinstance(a, StrCmpResult):
                return Bin(rel, a.a, a.b, ty='Boolean')
            if isinstance(a, (SetCC, BoolVal)):
                if rel in ('<>', '<', '>'):
                    return a.cond
                if rel == '=':
                    return negate(a.cond)
            if isinstance(a, Bin) and a.op in VBCMP and a.ty == 'Boolean':
                if rel in ('<>', '<'):
                    return a
                if rel == '=':
                    return negate(a)
            if isinstance(a, Un) and a.op == 'Not' and a.ty == 'Boolean':
                if rel in ('<>', '<'):
                    return a
                if rel == '=':
                    return negate(a)
            if isinstance(a, Bin) and a.op in ('And', 'Or', 'Xor') and a.ty == 'Boolean':
                c = as_cond(a)
                if rel in ('<>', '<'):
                    return c
                if rel == '=':
                    return negate(c)
            if isinstance(a, Call) and a.ty == 'Boolean':
                if rel in ('<>', '<'):
                    return a
                if rel == '=':
                    return Un('Not', a, ty='Boolean')
        if isinstance(a, StrCmpResult) and isinstance(b, Const):
            # StrComp result compared with -1/1
            if b.v == -1 and rel == '=':
                return Bin('<', a.a, a.b, ty='Boolean')
            if b.v == 1 and rel == '=':
                return Bin('>', a.a, a.b, ty='Boolean')
        if isinstance(a, Hresult) and isinstance(b, Const):
            # COM calls are assumed to succeed (S_OK = 0): resolve the error-check branch statically
            x, y = 0, b.v
            truth = {'=': x == y, '<>': x != y, '<': x < y, '<=': x <= y, '>': x > y, '>=': x >= y}[rel]
            return Const(-1 if truth else 0, 'Boolean')
        return Bin(rel, a, b, ty='Boolean')

    # ================================================================== execute
    def execute(self, ins):
        mn = ins.mn
        f = getattr(self, 'op_' + mn, None)
        if f is None:
            if mn.startswith('set'):
                return self.op_setcc(ins)
            if mn.startswith('f'):
                return self.fpu_op(ins)
            if mn.startswith('rep') or mn in ('movsd', 'movsb', 'stosd', 'stosb', 'movsw', 'stosw'):
                return self.op_rep(ins)
            self.warn('unhandled %s' % ins.text)
            self.emit(Comment('[decompiler] unhandled instruction %x: %s' % (ins.addr, ins.text)))
            return
        return f(ins)

    # ---- data movement
    def op_mov(self, ins):
        d, s = ins.ops
        if d.kind == 'reg' and d.reg == 'ebp' and s.kind == 'reg' and s.reg == 'esp':
            self.set_reg('ebp', SAddr(self.st.sp))
            return
        if d.kind == 'reg' and d.reg == 'esp':
            v = self.load(s)
            if isinstance(v, SAddr):
                self.st.sp = v.o
            return
        if s.kind == 'mem' and s.seg == 'fs':
            self.set_reg(d.reg, Unknown('fs0')) if d.kind == 'reg' else None
            return
        if d.kind == 'mem' and d.seg == 'fs':
            return
        if d.kind == 'mem' and s.kind == 'reg' and s.reg == 'esp' and self.is_frame_slot(d):
            # mov [ebp-R], esp  (prologue: saved stack pointer)
            return
        val = self.load(s, d.size)
        if d.kind == 'reg':
            self.set_reg(d.reg, val)
        else:
            if isinstance(val, CodePtr) and self.frame_R is None:
                pass
            self.store(d, val, d.size)

    def is_frame_slot(self, op):
        return op.base == 'ebp' and op.index is None and op.disp < 0 and (
            self.frame_R is None or -op.disp <= self.frame_R + 8)

    def op_movsx(self, ins):
        d, s = ins.ops
        v = self.load(s, s.size)
        if isinstance(v, Const):
            if s.size == 2:
                v = Const(s16(v.v), v.ty)
            elif s.size == 1:
                x = v.v & 0xff
                v = Const(x - 256 if x & 0x80 else x, v.ty)
        if isinstance(v, Var) and s.size == 2 and v.ty is None:
            self.note_type(v, 'Integer')
        if s.size == 2 and isinstance(v, Bin) and v.op in ('+', '-', '*'):
            # only the low 16 bits of this sum are kept: it is Integer arithmetic, so the register
            # variables in it hold Integers (as Longs the sum would not wrap around like this)
            for x in v.walk():
                if isinstance(x, Var) and x.kind == 'reg':
                    self.note_type(x, 'Integer', 50)
        self.set_reg(d.reg, v)

    def op_movzx(self, ins):
        d, s = ins.ops
        v = self.load(s, s.size)
        if isinstance(v, Const):
            v = Const(v.v & ((1 << (8 * s.size)) - 1), v.ty)
        if isinstance(v, Var) and s.size == 1:
            self.note_type(v, 'Byte')
        self.set_reg(d.reg, v)

    def op_lea(self, ins):
        d, s = ins.ops
        a = self.addr_of_mem(s)
        if a[0] == 'stack':
            self.set_reg(d.reg, SAddr(a[1]))
            return
        if a[0] == 'abs':
            self.set_reg(d.reg, self.imm_value(a[1]))
            return
        _, base, idx, scale, disp = a
        if isinstance(base, Me) and idx is None:
            self.set_reg(d.reg, self.field_addr(disp))
            return
        # arithmetic lea
        e = None
        if base is not None:
            e = base
        if idx is not None:
            t = idx if scale == 1 else self.with_width(self.arith('*', idx, Const(scale)), d.size, idx, None)
            e = t if e is None else self.with_width(self.arith('+', e, t), d.size, e, t)
        if disp:
            e = Const(disp) if e is None else self.with_width(self.arith('+', e, Const(disp)), d.size, e, None)
        if isinstance(base, AddrOf) and idx is None:
            e = AddrOf(Member(base.e, disp, 4))
        self.set_reg(d.reg, e if e is not None else Const(0))

    def op_push(self, ins):
        s = ins.ops[0]
        if s.kind == 'imm':
            v = self.imm_value(s.imm)
        else:
            v = self.load(s, 4)
        if s.kind == 'reg' and s.reg in ('ebx', 'esi', 'edi', 'ebp') and isinstance(v, Unknown) and \
                v.text == s.reg:
            # saving a callee-saved register (its value on entry): not an argument
            self.saved_cells.add(self.st.sp - 4)
        if s.kind == 'reg' and s.reg in ('ebx', 'esi', 'edi', 'ebp') and self.frame_R is not None \
                and self.local_low is None:
            pass
        self.push(v, 4)

    def op_pop(self, ins):
        d = ins.ops[0]
        v = self.pop(4)
        if d.kind == 'reg':
            self.set_reg(d.reg, v)
        else:
            self.store(d, v, 4)

    def op_xchg(self, ins):
        a, b = ins.ops
        va, vb = self.load(a), self.load(b)
        self.store(a, vb)
        self.store(b, va)

    def op_leave(self, ins):
        ebp = self.reg('ebp')
        if isinstance(ebp, SAddr):
            self.st.sp = ebp.o
        self.set_reg('ebp', self.pop(4))

    def op_nop(self, ins):
        pass

    op_wait = op_nop
    op_fnclex = op_nop
    op_fwait = op_nop
    op_cld = op_nop
    op_std = op_nop
    op_int3 = op_nop

    def op_cdq(self, ins):
        self.set_reg('edx', CdqOf(self.reg('eax')))

    def op_cwde(self, ins):
        pass

    def op_cwd(self, ins):
        self.set_reg('edx', Unknown('cwd'))

    # ---- arithmetic
    def arith(self, op, a, b):
        if isinstance(a, Const) and isinstance(b, Const):
            x, y = a.v, b.v
            r = None
            if op == '+':
                r = x + y
            elif op == '-':
                r = x - y
            elif op == '*':
                r = x * y
            elif op == 'And':
                r = x & y
            elif op == 'Or':
                r = x | y
            elif op == 'Xor':
                r = x ^ y
            if r is not None:
                return Const(s32(r))
        if op == '+' and is_const(b, 0):
            return a
        if op == '+' and is_const(a, 0):
            return b
        if op == '-' and is_const(b, 0):
            return a
        if op == '*' and is_const(b, 1):
            return a
        if op == '+' and isinstance(b, Const) and b.v < 0:
            return Bin('-', a, Const(-b.v))
        if op == '+' and isinstance(a, SAddr) and isinstance(b, Const):
            return SAddr(a.o + b.v)
        if op == '+' and isinstance(a, Me) and isinstance(b, Const):
            return AddrOf(self.field(b.v, 4))
        if op == '-' and isinstance(a, SAddr) and isinstance(b, Const):
            return SAddr(a.o - b.v)
        return Bin(op, a, b)

    def binop(self, ins, op):
        d, s = ins.ops
        if op in ('And', 'Or', 'Xor') and d.kind == 'reg' and d.size in (1, 2) and s.kind == 'imm' and \
                (d.reg in SUB8L_REGS or d.size == 2):
            # a bitwise op on al / ax leaves the rest of the register alone: do it on the whole register
            # with the immediate widened accordingly (and al, 0xfe = and eax, 0xfffffffe)
            full = FULL_REG[d.reg]
            mask = (1 << (8 * d.size)) - 1
            imm = s.imm & mask
            if op == 'And':
                imm |= 0xffffffff & ~mask
            a = self.reg(full)
            b = Const(s32(imm))
            r = self.bool_algebra(op, a, b)
            if r is None:
                r = self.arith(op, a, b)
            if op == 'And' and is_const(b, 0):
                r = Const(0)
            self.set_reg(full, r)
            self.st.flags = ('res', r, d.size)
            return
        a = self.load(d)
        b = self.load(s, d.size)
        if d.kind == 'reg' and d.reg == 'esp' and not isinstance(b, Const):
            self.warn('non-constant stack adjustment: %s' % ins.text)
            return
        if d.kind == 'reg' and d.reg == 'esp' and isinstance(b, Const):
            delta = b.v if op == '+' else -b.v
            if op in ('+', '-'):
                if delta > 0:
                    # releasing argument space (cdecl cleanup)
                    for k in list(self.st.mem):
                        if self.st.sp <= k < self.st.sp + delta and self.loc_kind(k) == 'cell':
                            del self.st.mem[k]
                self.st.sp += delta
                if delta < 0 and self.local_low is None and self.frame_R is not None:
                    pass
                return
        # Me & ~1 -> Me ; boolean algebra
        if isinstance(a, Me) and op == 'And' and isinstance(b, Const) and (b.v & 0xffffffff) == 0xfffffffe:
            self.st.flags = None
            return
        if isinstance(a, Me) and op == 'And' and is_const(b, 1):
            self.set_reg(d.reg, Const(0)) if d.kind == 'reg' else None
            return
        r = self.bool_algebra(op, a, b)
        if r is None:
            r = self.arith(op, a, b)
        if op == 'Xor' and same(a, b):
            r = Const(0)
        if op == 'Or' and is_const(b, -1):
            r = Const(-1)
        if op == 'And' and is_const(b, 0):
            r = Const(0)
        if isinstance(r, Bin) and r is not a and r is not b:
            r.w = d.size            # width of the machine operation (see exact_rel, held_type)
        self.store(d, r, d.size)
        if op == '-':
            self.st.flags = ('cmp', a, b, d.size)
        else:
            self.st.flags = ('res', r, d.size)

    def bool_algebra(self, op, a, b):
        """And/Or/Xor on VB booleans and setcc values."""
        def cond(x):
            if isinstance(x, (SetCC, BoolVal)):
                return x.cond
            if isinstance(x, Bin) and x.ty == 'Boolean':
                return as_cond(x)
            if isinstance(x, Un) and x.op == 'Not' and x.ty == 'Boolean':
                return x
            if isinstance(x, Call) and x.ty == 'Boolean':
                return x
            if isinstance(x, Var) and x.ty == 'Boolean':
                return x
            return None
        if op in ('And', 'Or'):
            ca, cb = cond(a), cond(b)
            if ca is not None and cb is not None:
                cls = BoolVal if isinstance(a, BoolVal) or isinstance(b, BoolVal) else SetCC
                return cls(Bin(op, ca, cb, ty='Boolean'))
            if ca is not None and isinstance(b, (Var, Deref, Member, Call, Temp)):
                return BoolVal(Bin(op, ca, b, ty='Boolean'))
            if cb is not None and isinstance(a, (Var, Deref, Member, Call, Temp)):
                return BoolVal(Bin(op, a, cb, ty='Boolean'))
        if op == 'Xor':
            ca = cond(a)
            if ca is not None and isinstance(b, Const) and b.v in (-1, 1, 0xffff):
                return (BoolVal if isinstance(a, BoolVal) else SetCC)(negate(ca))
        return None

    def op_add(self, ins):
        self.binop(ins, '+')

    def op_sub(self, ins):
        d, s = ins.ops
        a = self.load(d)
        if isinstance(a, AbsPending):
            b = self.load(s)
            if isinstance(b, CdqOf) and same(b.a, a.a):
                r = Call('Abs', [a.a], pure=True, ty='Long')
                self.store(d, r, d.size)
                self.st.flags = ('res', r, d.size)
                return
        self.binop(ins, '-')

    def op_and(self, ins):
        d, s = ins.ops
        a = self.load(d)
        if isinstance(a, (FpuAH,)) and s.kind == 'imm':
            vals = {o: a.vals[o] & s.imm for o in OUTCOMES}
            r = FpuAH(a.a, a.b, vals)
            self.store(d, r, d.size)
            self.st.flags = ('fah', {o: flags_of(vals[o], 1) for o in OUTCOMES}, a.a, a.b)
            return
        self.binop(ins, 'And')

    def op_or(self, ins):
        d, s = ins.ops
        a = self.load(d)
        if isinstance(a, FpuAH) and s.kind == 'imm':
            vals = {o: a.vals[o] | s.imm for o in OUTCOMES}
            if len({vals[o] & 0xffffffff for o in OUTCOMES}) == 1:
                # `or eax, -1` after fnstsw: the register no longer depends on the comparison
                c = Const(s32(vals[OUTCOMES[0]]))
                self.store(d, c, d.size)
                self.st.flags = ('res', c, d.size)
                return
            self.store(d, FpuAH(a.a, a.b, vals), d.size)
            self.st.flags = ('fah', {o: flags_of(vals[o], 1) for o in OUTCOMES}, a.a, a.b)
            return
        self.binop(ins, 'Or')

    def op_xor(self, ins):
        d, s = ins.ops
        a = self.load(d)
        if isinstance(a, FpuAH) and s.kind == 'imm':
            vals = {o: a.vals[o] ^ s.imm for o in OUTCOMES}
            self.store(d, FpuAH(a.a, a.b, vals), d.size)
            self.st.flags = ('fah', {o: flags_of(vals[o], 1) for o in OUTCOMES}, a.a, a.b)
            return
        if d.kind == 'reg' and s.kind == 'reg' and d.reg == s.reg:
            self.set_reg(d.reg, Const(0))
            self.st.flags = ('res', Const(0), d.size)
            return
        b = self.load(s)
        if isinstance(b, CdqOf) and same(b.a, a):
            self.store(d, AbsPending(a), d.size)
            return
        self.binop(ins, 'Xor')

    def op_adc(self, ins):
        d, s = ins.ops
        f = self.st.flags
        a = self.load(d)
        b = self.load(s, d.size)
        if f and f[0] == 'cf' and is_const(b, 0):
            r = self.arith('+', a, BoolVal(f[1]) if False else Un('neg', BoolVal(f[1])))
            self.store(d, r, d.size)
            return
        self.store(d, self.arith('+', a, b), d.size)

    def op_sbb(self, ins):
        d, s = ins.ops
        f = self.st.flags
        a = self.load(d)
        b = self.load(s, d.size)
        if same(a, b) and f is not None:
            # result = -CF : a VB boolean from the carry flag
            if f[0] == 'cf':
                self.store(d, BoolVal(f[1]), d.size)
            elif f[0] == 'cmp':
                self.store(d, BoolVal(self.mk_rel('<', f[1], f[2])), d.size)
            else:
                self.store(d, Unknown('sbb'), d.size)
            self.st.flags = ('res', self.load(d), d.size)
            return
        self.store(d, self.arith('-', a, b), d.size)

    def op_neg(self, ins):
        d = ins.ops[0]
        a = self.load(d)
        if isinstance(a, SetCC):
            r = BoolVal(a.cond)
            self.st.flags = ('cf', a.cond)
        elif isinstance(a, BoolVal):
            r = SetCC(a.cond)
            self.st.flags = ('cf', a.cond)
        elif isinstance(a, Const):
            r = Const(-a.v)
            self.st.flags = ('cf', Const(-1 if a.v else 0, 'Boolean'))
        else:
            r = Un('neg', a)
            r.w = d.size
            self.st.flags = ('cf', self.mk_rel('<>', a, Const(0)))
        self.store(d, r, d.size)

    def op_not(self, ins):
        d = ins.ops[0]
        a = self.load(d)
        if isinstance(a, (BoolVal,)):
            r = BoolVal(negate(a.cond))
        elif isinstance(a, Const):
            r = Const(~a.v)
        elif isinstance(a, Bin) and a.ty == 'Boolean':
            r = BoolVal(negate(as_cond(a)))
        else:
            r = Un('Not', a)
        self.store(d, r, d.size)

    def op_inc(self, ins):
        d = ins.ops[0]
        a = self.load(d)
        if isinstance(a, BoolVal):
            # -1/0 + 1 -> 0/1  (Not cond as 0/1)
            r = SetCC(negate(a.cond))
        else:
            r = self.arith('+', a, Const(1))
            if isinstance(r, Bin):
                r.w = d.size
        self.store(d, r, d.size)
        self.st.flags = ('res', r, d.size)

    def op_dec(self, ins):
        d = ins.ops[0]
        a = self.load(d)
        if isinstance(a, SetCC):
            r = BoolVal(negate(a.cond))
        else:
            r = self.arith('-', a, Const(1))
            if isinstance(r, Bin):
                r.w = d.size
        self.store(d, r, d.size)
        self.st.flags = ('res', r, d.size)

    def op_imul(self, ins):
        ops = ins.ops
        if len(ops) == 1:
            a = self.reg('eax')
            b = self.load(ops[0])
            self.set_reg('eax', self.with_width(self.arith('*', a, b), 4, a, b))
            self.set_reg('edx', Unknown('imul_hi'))
            return
        if len(ops) == 2:
            a = self.load(ops[0])
            b = self.load(ops[1], ops[0].size)
            self.store(ops[0], self.with_width(self.arith('*', a, b), ops[0].size, a, b))
            return
        a = self.load(ops[1], ops[0].size)
        b = self.load(ops[2], ops[0].size)
        self.store(ops[0], self.with_width(self.arith('*', a, b), ops[0].size, a, b))

    @staticmethod
    def with_width(r, size, a, b):
        if isinstance(r, Bin) and r is not a and r is not b:
            r.w = size
        return r

    def op_mul(self, ins):
        a = self.reg('eax')
        b = self.load(ins.ops[0])
        self.set_reg('eax', self.arith('*', a, b))
        self.set_reg('edx', Unknown('mul_hi'))

    def op_idiv(self, ins):
        s = ins.ops[0]
        a = self.reg('eax') if s.size == 4 else self.reg('ax')
        b = self.load(s)
        self.set_reg('eax', Bin('\\', a, b))
        self.set_reg('edx', Bin('Mod', a, b))

    op_div = op_idiv

    def shift(self, ins, kind):
        d, s = ins.ops[0], ins.ops[1] if len(ins.ops) > 1 else None
        a = self.load(d)
        n = self.load(s) if s is not None else Const(1)
        if isinstance(a, Const) and isinstance(n, Const):
            if kind == 'shl':
                r = Const(s32(a.v << n.v))
            elif kind == 'sar':
                r = Const(s32(a.v) >> n.v)
            else:
                r = Const((a.v & 0xffffffff) >> n.v)
        elif isinstance(n, Const):
            if kind == 'shl':
                r = self.with_width(self.arith('*', a, Const(1 << n.v)), d.size, a, None)
            else:
                r = Bin('\\', a, Const(1 << n.v))
        else:
            r = Raw('(%s %s %s)' % (a, kind, n))
        self.store(d, r, d.size)
        self.st.flags = ('res', r, d.size)

    def op_shl(self, ins):
        self.shift(ins, 'shl')

    op_sal = op_shl

    def op_sar(self, ins):
        self.shift(ins, 'sar')

    def op_shr(self, ins):
        self.shift(ins, 'shr')

    def op_cmp(self, ins):
        a, b = ins.ops
        va = self.load(a)
        vb = self.load(b, a.size)
        if isinstance(va, FpuAH) and isinstance(vb, Const):
            fl = {}
            for o in OUTCOMES:
                x = va.vals[o] & 0xff
                y = vb.v & 0xff
                r = (x - y) & 0xff
                f = flags_of(r, 1)
                f['CF'] = x < y
                sx = x - 256 if x & 0x80 else x
                sy = y - 256 if y & 0x80 else y
                f['OF'] = not (-128 <= sx - sy <= 127)
                fl[o] = f
            self.st.flags = ('fah', fl, va.a, va.b)
            return
        if isinstance(va, Var) and a.size == 2:
            self.note_type(va, 'Integer')
        self.st.flags = ('cmp', va, vb, a.size)

    def op_test(self, ins):
        a, b = ins.ops
        va = self.load(a)
        vb = self.load(b, a.size)
        if isinstance(va, FpuAH) and isinstance(vb, Const):
            fl = {o: flags_of(va.vals[o] & vb.v, 1) for o in OUTCOMES}
            self.st.flags = ('fah', fl, va.a, va.b)
            return
        self.st.flags = ('test', va, vb, a.size)

    def op_sahf(self, ins):
        ah = self.reg('ah')
        if isinstance(ah, FpuAH):
            fl = {}
            for o in OUTCOMES:
                v = ah.vals[o]
                fl[o] = {'CF': bool(v & 1), 'PF': bool(v & 4), 'ZF': bool(v & 0x40), 'SF': bool(v & 0x80),
                         'OF': False}
            self.st.flags = ('fah', fl, ah.a, ah.b)

    def op_setcc(self, ins):
        mn = SET_TO_J.get(ins.mn)
        c = self.cond_for(mn) if mn else Unknown(ins.mn)
        self.store(ins.ops[0], SetCC(c), 1)

    def op_bt(self, ins):
        a, b = ins.ops
        self.st.flags = ('cf', Bin('<>', Bin('And', self.load(a), Raw('2 ^ %s' % self.load(b))), Const(0),
                                   ty='Boolean'))

    # ---- string ops (UDT / variant copies)
    def op_rep(self, ins):
        t = ins.text
        n = self.reg('ecx')
        if 'movs' in t:
            src = self.reg('esi')
            dst = self.reg('edi')
            unit = 4 if 'dword' in t or t.endswith('movsd') else (2 if 'word' in t else 1)
            size = n.v * unit if isinstance(n, Const) else 0
            if not t.startswith('rep'):
                size = unit
            if isinstance(dst, SAddr) and self.loc_kind(dst.o) == 'cell':
                # by-value struct argument copied onto the stack
                val = self.deref_ptr(src, size or 4)
                if isinstance(val, Var) and size:
                    val = Var(val.kind, val.off, size, val.ty, val.name)
                self.mem_store(dst.o, size or 4, val, None)
            else:
                val = self.deref_ptr(src, size or 4)
                self.store_ptr(dst, val, size or 4)
            if t.startswith('rep'):
                self.set_reg('ecx', Const(0))
            if isinstance(src, SAddr):
                self.set_reg('esi', SAddr(src.o + size))
            if isinstance(dst, SAddr):
                self.set_reg('edi', SAddr(dst.o + size))
            return
        if 'stos' in t:
            dst = self.reg('edi')
            val = self.reg('eax')
            unit = 4 if 'dword' in t or t.endswith('stosd') else (2 if 'word' in t else 1)
            size = n.v * unit if isinstance(n, Const) and t.startswith('rep') else unit
            if isinstance(dst, SAddr) and self.loc_kind(dst.o) in ('local', 'cell') and is_const(val, 0):
                # zero-initialisation of a local block (prologue) - not source code
                for k in range(0, size, 4):
                    self.mem_store(dst.o + k, 4, Const(0), None)
                if t.startswith('rep'):
                    self.set_reg('ecx', Const(0))
                self.set_reg('edi', SAddr(dst.o + size))
                return
            self.emit(Comment('[decompiler] memory fill %s' % ins.text))
            if t.startswith('rep'):
                self.set_reg('ecx', Const(0))
            return
        self.emit(Comment('[decompiler] string op %s' % ins.text))

    # ================================================================== FPU
    def fpush(self, v):
        self.st.fpu.append(self.cap(v))

    def fpop(self):
        if self.st.fpu:
            return self.st.fpu.pop()
        return Unknown('st0')

    def fget(self, i):
        f = self.st.fpu
        if len(f) > i:
            return f[-1 - i]
        return Unknown('st%d' % i)

    def fset(self, i, v):
        v = self.cap(v)
        f = self.st.fpu
        if len(f) > i:
            f[-1 - i] = v
        else:
            while len(f) <= i:
                f.insert(0, Unknown('st'))
            f[-1 - i] = v

    def fsrc(self, op, int_=False):
        if op.kind == 'reg' and op.reg.startswith('st'):
            return self.fget(int(op.reg[3]) if '(' in op.reg else 0)
        if op.kind == 'mem':
            ty = None
            if not int_:
                ty = 'Single' if op.size == 4 else 'Double'
            v = self.load(op, op.size, ty)
            if type(v).__name__ == 'VTbl':
                v = Deref(v.obj, 0, op.size, ty)       # a number through a pointer, not a vtable
            # a value fild converted and fstp parked in a temporary is that integer variable as a number,
            # not a float variable: loading the temporary back says nothing about the variable's type
            converted = getattr(v, 'fpu_int', False)
            if isinstance(v, Deref) and not int_ and not converted:
                self.note_type(v, 'Single' if op.size == 4 else 'Double')
            if isinstance(v, Deref) and int_ and v.ty is None and v.size in (2, 4):
                # the value's own size: a word widened (movsx) into a dword temporary is still an Integer
                v.ty = 'Integer' if v.size == 2 else 'Long'
            if isinstance(v, Var):
                if int_:
                    if v.kind != 'reg':     # a register parked in a dword temporary: its size says nothing
                        self.note_type(v, 'Integer' if v.size == 2 else 'Long')
                elif not converted:
                    self.note_type(v, 'Single' if op.size == 4 else ('Double' if op.size == 8 else None))
            if int_ and isinstance(v, (Var, Deref)):
                v.fpu_int = True
            if isinstance(v, Const) and not int_ and op.size == 4:
                v = FConst(struct.unpack('<f', struct.pack('<I', v.v & 0xffffffff))[0], 'Single')
            return v
        return Unknown(str(op))

    def streg(self, op):
        if op.kind == 'reg' and op.reg.startswith('st'):
            return int(op.reg[3]) if '(' in op.reg else 0
        return None

    def fpu_op(self, ins):
        mn = ins.mn
        ops = ins.ops
        if mn in ('fld', 'fild'):
            v = self.fsrc(ops[0], int_=(mn == 'fild'))
            self.fpush(v)
            return
        if mn == 'fld1':
            self.fpush(FConst(1.0))
            return
        if mn == 'fldz':
            self.fpush(FConst(0.0))
            return
        if mn == 'fldpi':
            self.fpush(Raw('3.14159265358979', 'Double'))
            return
        if mn in ('fst', 'fstp', 'fist', 'fistp'):
            v = self.fget(0)
            d = ops[0]
            if d.kind == 'reg' and d.reg.startswith('st'):
                i = self.streg(d)
                self.fset(i, v)
            else:
                if mn.startswith('fist'):
                    v = Call('CLng' if d.size >= 4 else 'CInt', [v], pure=True, ty='Long' if d.size >= 4 else 'Integer')
                # what kind of store this is (integer / floating point) tells the type of the memory
                self.store_ty = ('Long' if d.size >= 4 else 'Integer') if mn.startswith('fist') else \
                    ('Single' if d.size == 4 else 'Double' if d.size == 8 else None)
                if d.size == 4 and not mn.startswith('fist') and d.kind == 'mem' and \
                        self.addr_of_mem(d)[0] == 'stack' and isinstance(v, (Bin, Un, Call)) and \
                        getattr(v, 'ty', None) != 'Single':
                    # a value parked in a Single stack slot is rounded to single precision there; where it
                    # is read back (often inlined), that rounding must show
                    v = Call('CSng', [v], pure=True, ty='Single')
                self.store(d, v, d.size)
                self.store_ty = None
                tgt = self.mem_var(d, d.size) if d.kind == 'mem' else None
                if isinstance(tgt, Var) and not mn.startswith('fist'):
                    if getattr(v, 'date_hint', False) or getattr(v, 'ty', None) == 'Date':
                        self.note_type(tgt, 'Date')
                    else:
                        self.note_type(tgt, 'Single' if d.size == 4 else 'Double')
            if mn.endswith('p'):
                self.fpop()
            return
        if mn == 'fxch':
            i = self.streg(ops[0]) if ops else 1
            if i is None and len(ops) == 2:
                i = self.streg(ops[1])
            a, b = self.fget(0), self.fget(i or 1)
            self.fset(0, b)
            self.fset(i or 1, a)
            return
        if mn == 'fchs':
            v = self.fget(0)
            self.fset(0, FConst(-v.v, v.ty) if isinstance(v, FConst) else Un('neg', v))
            return
        if mn == 'fabs':
            self.fset(0, Call('Abs', [self.fget(0)], pure=True))
            return
        if mn == 'fsqrt':
            self.fset(0, Call('Sqr', [self.fget(0)], pure=True))
            return
        if mn == 'frndint':
            self.fset(0, Call('CLng', [self.fget(0)], pure=True))
            return
        if mn in ('fsin', 'fcos'):
            self.fset(0, Call('Sin' if mn == 'fsin' else 'Cos', [self.fget(0)], pure=True))
            return
        base = {'fadd': '+', 'fsub': '-', 'fsubr': 'r-', 'fmul': '*', 'fdiv': '/', 'fdivr': 'r/',
                'fiadd': '+', 'fisub': '-', 'fisubr': 'r-', 'fimul': '*', 'fidiv': '/', 'fidivr': 'r/'}
        name = mn[:-1] if mn.endswith('p') and mn[:-1] in base else mn
        if name in base:
            op = base[name]
            pop = mn.endswith('p') and name != mn
            rev = op.startswith('r')
            bop = op[-1]
            if len(ops) == 0:
                # faddp / fsubp etc. = st(1) op= st(0), pop
                a, b = self.fget(1), self.fget(0)
                r = Bin(bop, b, a) if rev else Bin(bop, a, b)
                self.fset(1, r)
                self.fpop()
                return
            if len(ops) == 1:
                if ops[0].kind == 'reg' and ops[0].reg.startswith('st'):
                    i = self.streg(ops[0])
                    if pop:
                        a, b = self.fget(i), self.fget(0)
                        r = Bin(bop, b, a) if rev else Bin(bop, a, b)
                        self.fset(i, r)
                        self.fpop()
                    else:
                        a, b = self.fget(0), self.fget(i)
                        r = Bin(bop, b, a) if rev else Bin(bop, a, b)
                        self.fset(0, r)
                    return
                src = self.fsrc(ops[0], int_=name.startswith('fi'))
                a = self.fget(0)
                r = Bin(bop, src, a) if rev else Bin(bop, a, src)
                self.fset(0, r)
                return
            # two register operands
            di, si = self.streg(ops[0]), self.streg(ops[1])
            a, b = self.fget(di), self.fget(si)
            r = Bin(bop, b, a) if rev else Bin(bop, a, b)
            self.fset(di, r)
            if pop:
                self.fpop()
            return
        if mn in ('fcom', 'fcomp', 'fcompp', 'fucom', 'fucomp', 'fucompp', 'ficom', 'ficomp', 'ftst',
                  'fcomi', 'fcomip', 'fucomi', 'fucomip'):
            a = self.fget(0)
            if mn == 'ftst':
                b = FConst(0.0)
            elif mn.endswith('pp') or not ops:
                b = self.fget(1)
            else:
                o = ops[-1]
                b = self.fget(self.streg(o)) if o.kind == 'reg' else self.fsrc(o, int_=mn.startswith('fi'))
            self.fcmp = (a, b)
            if mn.startswith('fcomi') or mn.startswith('fucomi'):
                fl = {o: {'CF': o == 'lt', 'ZF': o == 'eq', 'PF': False, 'SF': False, 'OF': False} for o in OUTCOMES}
                self.st.flags = ('fah', fl, a, b)
            if mn.endswith('pp'):
                self.fpop()
                self.fpop()
            elif mn.endswith('p'):
                self.fpop()
            return
        if mn in ('fnstsw', 'fstsw'):
            a, b = getattr(self, 'fcmp', (Unknown('st0'), Unknown('?')))
            vals = dict(FSW)
            v = FpuAH(a, b, vals)
            d = ops[0]
            if d.kind == 'reg':
                self.set_reg('eax', v)
            return
        if mn in ('fninit', 'finit', 'fnclex', 'fclex', 'fwait', 'fincstp', 'fdecstp', 'ffree', 'fnop'):
            return
        if mn in ('fldcw', 'fnstcw', 'fstcw'):
            return
        if mn == 'fpatan':
            y, x = self.fget(1), self.fget(0)
            self.fpop()
            self.fset(0, Call('Atn', [Bin('/', y, x)], pure=True))
            return
        if mn == 'fscale' or mn == 'fyl2x' or mn == 'f2xm1' or mn == 'fprem' or mn == 'fprem1':
            self.emit(Comment('[decompiler] fpu %s' % ins.text))
            if mn == 'fyl2x':
                self.fpop()
            return
        self.warn('unhandled fpu %s' % ins.text)
        self.emit(Comment('[decompiler] unhandled fpu instruction %x: %s' % (ins.addr, ins.text)))

    # ================================================================== calls
    def op_jcc(self, ins):
        pass

    op_je = op_jne = op_jg = op_jge = op_jl = op_jle = op_ja = op_jae = op_jb = op_jbe = op_jcc
    op_js = op_jns = op_jo = op_jno = op_jp = op_jnp = op_jecxz = op_jcc

    def op_ret(self, ins):
        self.ret_values.append((self.reg('eax'), self.fget(0) if self.st.fpu else None, self.st.sp))

    def op_jmp(self, ins):
        pass

    # runtime functions that can run other code (events, the user) or change the program's variables
    OUTSIDE_RT = {'rtcDoEvents', 'rtcMsgBox', 'rtcInputBox', 'rtcShell', 'rtcSendKeys'}

    def protect_before_call(self, ins):
        """Values kept in callee-saved registers (or on the FPU stack) across a call that can change the
        program's variables: when they were computed from those variables, the computation happens here,
        before the call - later uses must not re-read changed memory. (In a loop the register becomes a
        loop variable assigned before the loop, which is what the machine does.)"""
        o = ins.ops[0] if ins.ops else None
        n = None
        if o is not None and o.kind == 'mem' and o.base is None and o.index is None:
            n = self.im.imports.get(o.disp & 0xffffffff)
        elif o is not None and o.kind == 'reg':
            f = self.reg(o.reg)         # `call edi` with edi = an import loaded earlier
            n = f.name if isinstance(f, ImportFn) else None
        runtime = bool(n and n not in self.OUTSIDE_RT and not n.startswith('__vbaLate'))
        for r in ('ebx', 'esi', 'edi', 'ebp'):
            v = self.st.regs.get(r)
            if runtime and not self.fallible_value(v):
                continue
            if self.volatile_value(v) or self.fallible_value(v):
                # (a value that can raise an error - checked integer arithmetic, a division - happens
                # here, before any call: Timer / Randomize / a file read come after its overflow, as in
                # the machine code, which computed it first)
                t = self.new_temp(held_type(v))
                self.emit(Assign(t, v))
                self.st.regs[r] = t
        for i, v in enumerate(self.st.fpu):
            if not runtime and self.volatile_value(v):
                t = self.new_temp(getattr(v, 'ty', None) or 'Double')
                self.emit(Assign(t, v))
                self.st.fpu[i] = t

    def fallible_value(self, v):
        """Integer arithmetic that can raise a run-time error where it is computed: + - * with overflow
        checks compiled in, integer division (by zero) always. Addresses (base + offset) are not."""
        if not isinstance(v, (Bin, Un)) or not self.volatile_value(v, reads_only=False):
            return False
        checks = self.ctx.compile_options().get('overflow_checks')
        for x in v.walk():
            if isinstance(x, Bin) and x.op in ('\\', 'Mod'):
                return True
            if checks and isinstance(x, Bin) and x.op in ('+', '-', '*') and \
                    getattr(x, 'ty', None) not in ('Single', 'Double', 'Currency', 'Date', 'String', 'Variant'):
                return True
            if checks and isinstance(x, Un) and x.op == 'neg' and getattr(x, 'ty', None) not in ('Single', 'Double'):
                return True
        return False

    @staticmethod
    def volatile_value(v, reads_only=True):
        """A computed value that reads program memory (not a pointer kept for later use)."""
        if not isinstance(v, Expr) or isinstance(v, (Temp, Const, FConst, Str, Me, SAddr, Unknown)):
            return False

        def reads(e):
            for x in e.walk():
                if isinstance(x, Var) and x.kind in ('field', 'global'):
                    return True
                if isinstance(x, (Deref, Member)) and not isinstance(x, VTbl):
                    return True
            return False
        if isinstance(v, Bin) and v.op in ('+', '-'):
            from simplify import linear
            lin = linear(v)
            if lin is None or any(c == 1 and isinstance(t, (Var, Deref)) and getattr(t, 'size', 4) == 4 and
                                  not (isinstance(t, Var) and t.kind in ('local', 'reg', 'param'))
                                  for t, c in lin[0].values()):
                return False        # base + offset: an address (an array element), kept as it is
        if not reads_only:
            return True             # (not an address: the caller wants to know only that)
        if isinstance(v, (Bin, Un, BoolVal, SetCC)):
            return reads(v)
        if isinstance(v, (Var, Deref, Member)) and getattr(v, 'size', 4) <= 2:
            return reads(v)
        return False

    def op_call(self, ins):
        before = (self.st.regs.get('ecx'), self.st.regs.get('edx'))
        self.protect_before_call(ins)
        self.dispatch_call(ins)
        # ecx/edx are caller-saved: forget them unless the handler produced a value there
        if self.st.regs.get('ecx') is before[0]:
            self.st.regs['ecx'] = Unknown('ecx')
        if self.st.regs.get('edx') is before[1]:
            self.st.regs['edx'] = Unknown('edx')

    def dispatch_call(self, ins):
        o = ins.ops[0]
        if o.kind == 'imm':
            return self.call_direct(o.imm)
        if o.kind == 'mem' and o.base is None and o.index is None:
            name = self.im.imports.get(o.disp & 0xffffffff)
            if name:
                return self.rt.call(self, name)
            return self.call_unknown('[%x]' % o.disp)
        if o.kind == 'mem':
            base = self.reg(o.base) if o.base else None
            if isinstance(base, VTbl) and o.index is None:
                return self.com_call(base.obj, o.disp)
            if isinstance(base, (Me,)) and o.index is None:
                return self.com_call(base, o.disp)
            if o.index is None and o.base and self.pending_cells() >= 4 and (o.disp & 3) == 0 \
                    and 0 < o.disp < 0x1000:
                # vtable pointer kept in a register: the object is the `this` argument (last push)
                this = self.cell(0)
                if not isinstance(this, (Unknown, Const)):
                    return self.com_call(this, o.disp)
            return self.call_unknown(ins.text)
        if o.kind == 'reg':
            v = self.reg(o.reg)
            if isinstance(v, ImportFn):
                return self.rt.call(self, v.name)
            if isinstance(v, CodePtr):
                return self.call_direct(v.va)
            if isinstance(v, Const) and self.im.imports.get(v.v & 0xffffffff):
                return self.rt.call(self, self.im.imports[v.v & 0xffffffff])
            return self.call_unknown(ins.text)
        return self.call_unknown(ins.text)

    def call_direct(self, va):
        im = self.im
        # import thunk:  jmp dword ptr [IAT]
        if im.valid(va, 6) and im.read(va, 2) == b'\xff\x25':
            slot = im.u32(va + 2)
            if slot in im.imports:
                return self.rt.call(self, im.imports[slot])
        d = self.proj.declare_at(va)
        if d is not None:
            return self.api_call(d)
        p = self.proj.procs.get(va)
        if p is not None:
            return self.proc_call(p)
        # small thunk (e.g. FDIV adjust helper) or unknown code
        return self.call_unknown('sub_%X' % va, va=va)

    def call_unknown(self, desc, va=None):
        n = self.pending_cells()
        args, _ = self.args_by_sizes([4] * (n // 4))
        self.consume(n)
        c = Call('Call_%s' % re.sub(r'[^A-Za-z0-9_]', '_', desc) if va is None else 'sub_%X' % va, args, kind='raw')
        t = self.call_value(c, None)
        self.set_reg('eax', t)
        self.invalidate_locals()
        self.warn('unknown call %s' % desc)

    def api_call(self, d):
        """Call to a Declare'd API stub: all pending cells are its arguments (stdcall)."""
        sig = self.ctx.api_signature(d)
        n = self.pending_cells() if sig is None else sig
        args, _ = self.args_by_sizes([4] * (n // 4))
        self.consume(n)
        c = Call(self.ctx.declare_name(d), [self.api_arg(a) for a in args], kind='api')
        c.decl = d
        t = self.call_value(c, 'Long')
        self.set_reg('eax', t)
        for a in args:
            if isinstance(a, (SAddr, AddrOf)):
                self.invalidate_locals()
                break
        self.ctx.note_api_args(d, args)

    def api_arg(self, a):
        """API argument: ANSI string temps show as the original string; pointers as ByRef vars."""
        if isinstance(a, SAddr):
            return self.ptr_target(a)
        if isinstance(a, AddrOf):
            return a.e
        return a

    def proc_call(self, p):
        nbytes = p.ret_n if p.ret_n is not None else self.pending_cells()
        args, _ = self.args_by_sizes([4] * (nbytes // 4))
        self.consume(nbytes)
        sig = self.ctx.proc_signature(p)
        args = [self.proc_arg(a, sig, i) for i, a in enumerate(args)]
        is_method = p.obj is not None and p.obj.kind in ('form', 'class')
        if is_method and args:
            args = args[1:]             # Me
        name = self.ctx.proc_name(p)
        retptr = None
        if sig and sig.get('retptr') and args:
            retptr = args[-1]
            args = args[:-1]
        addr_args = [a for a in args if isinstance(a, (SAddr, AddrOf))]
        args = [self.byref_arg(a) for a in args]
        c = Call(name, args, kind='proc')
        c.proc = p
        if retptr is not None:
            t = self.call_value(c, sig.get('rettype'))
            self.store_ptr(retptr, t, 4, sig.get('rettype'))
            self.set_reg('eax', Hresult(c))
        elif sig and sig.get('ret') == 'fpu':
            t = self.call_value(c, sig.get('rettype'))
            self.fpush(t)
        elif sig and sig.get('ret') == 'eax' and not is_method:
            t = self.call_value(c, sig.get('rettype'))
            self.set_reg('eax', t)
        elif is_method:
            self.call_stmt(c)
            self.set_reg('eax', Hresult(c))
        else:
            # unknown return: bind eax in case it is used
            t = self.call_value(c, None)
            t.maybe_unused = True
            self.set_reg('eax', t)
        self.invalidate_after_call(addr_args)

    def byref_arg(self, a):
        """A pointer to a variable passed to a user procedure is a ByRef argument: show the variable."""
        v = a
        if isinstance(a, SAddr):
            v = self.slot_var(a.o, 4)
        elif isinstance(a, AddrOf):
            v = a.e
        if v is not a and isinstance(v, Var):
            v = Var(v.kind, v.off, v.size, v.ty, v.name)
            v.byref_arg = True      # passed by address: the procedure may change it (see simplify)
        return v

    def proc_arg(self, a, sig, i):
        return a

    # ---------------------------------------------------------------- COM
    def find_check(self, call_ins_next, vtoff=None):
        """Look ahead for the __vbaHresultCheckObj(hr, obj, IID, vtoff) that follows a COM call.
        The check block may re-evaluate the object expression (nested calls), so scan past calls
        until `push vtoff ; push IID` is found."""
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
                if prev_imm is not None and (vtoff is None or prev_imm == vtoff) and self.im.valid(v, 16) \
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
                if o.kind == 'mem' and o.base is None and \
                        self.im.imports.get(o.disp & 0xffffffff) == '__vbaHresultCheckObj':
                    break
            cur = ins.next
        return None

    def com_call(self, obj, off):
        if off in (4, 8) and not isinstance(obj, Unknown):
            # IUnknown AddRef / Release
            self.consume(4)
            self.set_reg('eax', Const(0))
            return
        chk = self.find_check(self.cur.next, off)
        iface = None
        if chk is not None and chk[0] == off:
            iid_va = chk[1]
            if self.im.valid(iid_va, 16):
                guid = self.im.guid(iid_va)
                iface = self.ctx.iface_for_guid(guid)
                if iface is None:
                    self.ctx.note_unknown_iid(guid)
        if iface is None:
            iface = self.ctx.object_iface(obj, self)
        member = None
        if iface is not None:
            member = self.ctx.member_at(iface, off, obj, self)
        if member is None:
            return self.com_call_unknown(obj, off, iface)
        if isinstance(member, dict):
            return self.self_method_call(obj, off, member)
        return self.com_call_member(obj, member)

    def com_call_unknown(self, obj, off, iface):
        n = self.pending_cells()
        args, _ = self.args_by_sizes([4] * (n // 4))
        self.consume(n)
        nm = '%s_Method%X' % (iface.name if iface else 'Unknown', off)
        c = Call(nm, args[1:], obj=obj, kind='com')
        self.call_stmt(c)
        self.set_reg('eax', Hresult(c))
        self.invalidate_locals()
        self.warn('unresolved COM call %s+%x' % (iface.name if iface else '?', off))

    def param_size(self, ptype):
        t = ptype
        if t.endswith('*') or t.endswith('()'):
            return 4
        if t in ('Variant',):
            return 16
        if t in ('Double', 'Currency', 'Date'):
            return 8
        return self.tlb.type_size(t) if t in self.tlb.records else 4

    def com_call_member(self, obj, m):
        params = m.params
        sizes = [4] + [self.param_size(p['type']) for p in params]
        args, total = self.args_by_sizes(sizes)
        this = args[0]
        vals = args[1:]
        self.consume(total)
        out_args = []
        retptr = None
        for p, v in zip(params, vals):
            if p['flags'] & 8:
                retptr = v
                continue
            if p['type'] == 'Void*' and m.name in ('SetProperty', 'GetProperty') and out_args and \
                    isinstance(out_args[0], Str) and isinstance(v, (SAddr, AddrOf, DataPtr)):
                # DirectInput property blocks (ByRef As Any): the property name says which record it is
                rec = DIPROP_RECORDS.get(out_args[0].s.upper(), 'DIPROPLONG')
                if rec in self.tlb.records:
                    p = dict(p, type=rec + '*')
            out_args.append(self.com_arg(p, v))
        ty = self.ctx.vb_type(m.ret if m.ret not in ('HRESULT', 'Void') else
                              (params[-1]['type'][:-1] if retptr is not None else None))
        if retptr is None and m.ret not in ('HRESULT', 'Void', None):
            # non-HRESULT method (e.g. dx8vb returns directly)
            pass
        if m.invkind in ('let', 'set'):
            val = out_args[-1] if out_args else Unknown('value')
            target = Call(m.name, out_args[:-1], obj=obj, kind='com', prop=m.invkind)
            target.member = m
            self.emit(Assign(target, val, kind='set' if m.invkind == 'set' else 'let'))
            self.set_reg('eax', Hresult(target))
            self.invalidate_locals_for(out_args)
            return
        c = Call(m.name, out_args, obj=obj, kind='com', prop=m.invkind if m.invkind == 'get' else None)
        c.member = m
        c.ty = ty
        if retptr is not None:
            rtype = params[-1]['type']
            base_t = rtype[:-1] if rtype.endswith('*') else rtype
            t = self.call_value(c, self.ctx.vb_type(base_t))
            obj_ret = self.is_iface_type(base_t.rstrip('*'))
            self.store_ptr(retptr, t, self.tlb.type_size(base_t) if base_t in self.tlb.records else
                           {'Variant': 16, 'Double': 8, 'Currency': 8, 'Date': 8, 'Integer': 2, 'Boolean': 2,
                            'Byte': 1}.get(self.ctx.vb_type(base_t), 4), self.ctx.vb_type(base_t),
                           kind='set' if obj_ret else 'let')
            self.set_reg('eax', Hresult(c))
        elif m.ret not in ('HRESULT', 'Void', None):
            t = self.call_value(c, self.ctx.vb_type(m.ret))
            if self.ctx.vb_type(m.ret) in ('Single', 'Double'):
                self.fpush(t)
            else:
                self.set_reg('eax', t)
        else:
            self.call_stmt(c)
            self.set_reg('eax', Hresult(c))
        self.invalidate_locals_for(out_args)

    def invalidate_locals_for(self, args):
        for a in args:
            if isinstance(a, Var) and a.kind == 'local':
                self.invalidate_var(a, a.size)

    def com_arg(self, p, v):
        t = p['type']
        if t == 'Variant':
            return v
        if t == 'Variant*':
            return self.variant_at(v)
        if t.endswith('*') and self.is_iface_type(t[:-1]) and not t.endswith('**'):
            return v
        if t.endswith('*'):
            base = t[:-1]
            if isinstance(v, (SAddr, AddrOf, DataPtr)):
                size = {'Integer': 2, 'Boolean': 2, 'Byte': 1, 'Double': 8, 'Currency': 8, 'Date': 8}.get(base)
                if base in self.tlb.records:
                    size = self.tlb.layout(base)[1]
                tgt = self.ptr_target(v, size or 4, self.ctx.vb_type(base))
                if isinstance(tgt, Var) and base in self.tlb.records:
                    tgt = Var(tgt.kind, tgt.off, size, tgt.ty, tgt.name)
                    self.ctx.note_record(self, tgt, base)
                return tgt
            return Deref(v, 0, 4) if not isinstance(v, (Var, Temp)) else v
        if t == 'Boolean' and isinstance(v, Const):
            return Const(-1 if v.v & 0xffff else 0, 'Boolean')
        if t == 'String' and isinstance(v, Const) and v.v == 0:
            return Str('')          # a null BSTR: the empty string
        if t in ('Integer',) and isinstance(v, Const):
            return Const(s16(v.v), 'Integer')
        if t in ('Single',) and isinstance(v, Const):
            return FConst(struct.unpack('<f', struct.pack('<I', v.v & 0xffffffff))[0], 'Single')
        en = self.tlb.enums.get(t)
        if en is not None and isinstance(v, Const):
            nm = self.ctx.enum_const(t, v.v)
            if nm:
                return Raw(nm)
        return v

    def is_iface_type(self, t):
        if t in ('IUnknown', 'IDispatch', 'Object'):
            return True
        it = self.tlb.by_name.get(t)
        return it is not None and it.kind in ('interface', 'dispatch', 'coclass')

    def self_method_call(self, obj, off, info):
        """Call through Me's vtable to one of the object's own procedures / control accessors."""
        kind = info['kind']
        if kind == 'control':
            # accessor: Me.<control> - the control object is returned in eax
            self.consume(4)
            ctl = Raw(info['name'], 'Object')
            if not isinstance(obj, Me):
                ctl = Raw('%s.%s' % (render_obj(obj), info['name']), 'Object')
            ctl.control = info['name']
            self.set_reg('eax', ctl)
            return
        p = info['proc']
        return self.proc_call(p)
