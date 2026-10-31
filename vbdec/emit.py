"""Write a decompiled VB6 project (.vbp/.frm/.bas) from lifted procedures."""
import os
import re
from collections import Counter, defaultdict

from ir import (Expr, Const, FConst, Str, Var, Member, Deref, Index, AddrOf, Bin, Un, Call, Temp, Me, Missing,
                Unknown, Raw, Assign, ExprStmt, Text, Comment, FmtStmt, ekey, same, render, NAMER)
from lift import SAddr, BoolVal, SetCC
import simplify
from simplify import rewrite, stmt_rewrite, term_rewrite, term_exprs, iter_exprs_in
import structure

VB_INT_TYPES = ('Integer', 'Long', 'Byte', 'Boolean')


class Exclusion:
    """Code to leave out of the published output, from a project file's omission rules (see project.py).

    A statement is left out when it uses one of `controls`, contains a string matching one of `patterns`,
    calls one of `apis`, or calls a procedure left out for the same reason. A whole procedure is left out
    when a string in it matches one of `strong` (or it uses one of the controls and `strong` is given), or
    when 30% or more of its statements are left out. `whole_module`: a module all of whose procedures
    touch this code is left out entirely (a licensing module, say)."""

    def __init__(self, controls=(), patterns=(), label='', strong=(), apis=(), whole_module=False):
        self.apis = set(apis)
        self.controls = {c.lower() for c in controls}
        self.patterns = [re.compile(p, re.I) for p in patterns]
        self.strong = [re.compile(p, re.I) for p in strong]
        self.label = label
        self.whole_module = whole_module


class ProcOut:
    def __init__(self):
        self.header = ''
        self.footer = ''
        self.lines = []
        self.locals = {}
        self.omitted = None


class ProjectWriter:
    def __init__(self, dc, lifted, exclusions=()):
        self.dc = dc
        self.pr = dc.proj
        self.tlb = dc.tlb
        self.lifted = lifted
        self.exclusions = exclusions
        self.field_hints = defaultdict(Counter)     # (obj index, field off) -> Counter(type)
        self.field_sizes = defaultdict(int)
        self.global_hints = defaultdict(Counter)
        self.global_sizes = defaultdict(int)
        self.control_by_storage = {}
        self.excluded_fields = {}
        for o in self.pr.objects:
            for c in o.controls:
                if c.storage:
                    self.control_by_storage[(o.index, c.storage)] = c.name
                    for ex in exclusions:
                        if c.name.lower() in ex.controls:
                            self.excluded_fields[(o.index, c.storage)] = ex
        self.cur_obj = None
        self.cur_names = {}
        self.proc_excluded = {}    # proc va -> Exclusion (whole procedure omitted)
        self.forced_exclusions = {}  # proc va -> Exclusion (e.g. a whole licensing module)
        self.would_exclude = {}     # proc va -> label: omitted from the normal output (VBDEC_NOEXCLUDE runs)
        self.would_drop = {}        # proc va -> Counter of exclusions its code touches (VBDEC_NOEXCLUDE runs)
        self.proc_param_types = {}  # proc va -> {param offset: VB type}
        self.proc_param_known = {}  # proc va -> {param offset: the type is from evidence}
        self.ret_hints = defaultdict(Counter)  # proc va -> type evidence for the function result
        self.excluded_writes = {}   # var key -> labels of omitted code that assigns it
        self.normal_writes = set()  # var keys assigned by code that is kept
        self.last_dropped = Counter()
        self.arrays = {}           # array base var -> {(stride, element size)}
        self.array_kind = {}       # 'pv' (data pointer) / 'psa' (SAFEARRAY*)
        self.reg_names = {}
        self.elem_hints = defaultdict(Counter)
        # record arrays: array key -> byte offset of a field -> Counter of type hints (incl. 'sizeN')
        self.rec_fields = defaultdict(lambda: defaultdict(Counter))
        self.pending_types = {}    # object index -> [(type name, array key, stride)]
        self.cur_proc = None
        self.param_types = {}
        self.records = {}          # ekey(var) -> record name (from dc.records)
        self.notes = []

    # ================================================================ naming
    def name_var(self, v):
        if v.kind == 'local':
            n = self.cur_names.get(ekey(v))
            if not n and not v.name and v.off >= 0x1000000:
                # a separate use of a reused stack slot (simplify.split_locals): <slot name>_<n>
                return '%s_%d' % (self.name_var(Var('local', v.off & 0xffffff, v.size)), v.off >> 24)
            if not n and 0x10000 <= v.off < 0x10100:
                start = 1 if self.cur_obj is not None and self.cur_obj.kind in ('form', 'class') else 0
                return 'var_Scratch%d' % ((v.off - 0x10000) // 4 - start + 1)
            return n or v.name or 'var_%X' % v.off
        if v.kind == 'param':
            n = self.cur_names.get(('param', v.off))
            return n or 'p%d' % (v.off // 4)
        if v.kind == 'field':
            o = self.cur_obj
            if o is not None:
                c = self.control_by_storage.get((o.index, v.off))
                if c:
                    return c
            return 'm_%X' % v.off
        if v.kind == 'global':
            obj = self.dc.global_objects.get(v.off)
            if obj and obj not in ('Global', 'VBGlobal', 'Object'):
                return obj
            return 'g_%X' % (v.off & 0xffff)
        if v.kind == 'internal':
            return 'frame_%X' % v.off
        if v.kind == 'reg':
            n = self.reg_names.get(v.off)
            if n is None:
                base = 'r' + v.off.split('@')[0][1:].upper()
                used = set(self.reg_names.values())
                n = base
                k = 2
                while n in used:
                    n = '%s%d' % (base, k)
                    k += 1
                self.reg_names[v.off] = n
            return n
        return None

    # ================================================================ per procedure
    def proc_header(self, p, sig):
        dc = self.dc
        name = dc.proc_name(p)
        is_method = p.obj is not None and p.obj.kind in ('form', 'class')
        params = []
        self.header_param_types = {}
        self.header_param_known = {}
        names = {}
        nargs = sig.get('nargs', 0) if sig else 0
        start = 1 if is_method else 0
        nparams = nargs - start - (1 if sig and sig.get('retptr') else 0)
        evm = None
        if p.kind == 'event' and p.event:
            evm = dc.event_member(*p.event)
        for i in range(max(nparams, 0)):
            off = 4 * (start + i)
            byref = sig.get('byref', {}).get(off) if sig else False
            size = sig.get('sizes', {}).get(off, 4) if sig else 4
            pname = 'p%d' % (i + 1)
            ptype = {2: 'Integer', 4: 'Long', 8: 'Double', 16: 'Variant', 1: 'Byte'}.get(size, 'Long')
            if evm is not None and i < len(evm.params):
                ep = evm.params[i]
                pname = ep['name'] or pname
                t = ep['type']
                byref = t.endswith('*')
                ptype = dc.vb_type(t.rstrip('*')) or ptype
            hint = self.param_types.get(off)
            if hint and evm is None:
                ptype = hint
            arr = ''
            if off in getattr(self, 'cur_param_arrays', {}) and evm is None:
                # arrays are always passed ByRef
                arr, byref = '()', True
                ptype = {2: 'Integer', 4: 'Long', 8: 'Double', 16: 'Variant', 1: 'Byte'}.get(
                    self.cur_param_arrays[off], 'Variant')
            self.header_param_types[off] = ptype
            # a type from evidence (use in the code, the access size, the event's declaration), not a default
            self.header_param_known[off] = bool(hint) or evm is not None or bool(arr) or \
                off in (sig.get('sizes', {}) if sig else {})
            names[('param', off)] = pname
            params.append('%s%s%s As %s' % ('' if byref else 'ByVal ', pname, arr, ptype))
        kind = 'Sub'
        rettype = None
        if sig and (sig.get('retptr') or sig.get('ret')):
            kind = 'Function'
            rettype = sig.get('rettype') or 'Variant'
        scope = 'Private' if (is_method and not p.public) else 'Public'
        if p.kind == 'event':
            scope = 'Private'
        head = '%s %s %s(%s)' % (scope, kind, name, ', '.join(params))
        if rettype:
            head += ' As %s' % rettype
        return head, kind, names

    def prepare(self, p, L):
        """Copy the lifted blocks (reachable, non-error) for post-processing."""
        cfg = L.cfg
        reach = set(cfg.reachable()) | set(getattr(L, 'edge_blocks', {}))
        blocks = {}
        terms = {}
        for b in reach:
            if (b in cfg.error_blocks and b < 0x70000000) or b not in L.block_stmts:
                continue
            blocks[b] = list(L.block_stmts[b])
            terms[b] = L.block_term[b]
        for b in list(terms):
            t = terms[b]
            if t[0] == 'goto' and t[1] not in blocks:
                terms[b] = ('stop',)
            elif t[0] == 'cond':
                a_ok, b_ok = t[2] in blocks, t[3] in blocks
                if not a_ok and not b_ok:
                    terms[b] = ('stop',)
                elif not a_ok:
                    terms[b] = ('goto', t[3])
                elif not b_ok:
                    terms[b] = ('goto', t[2])
            elif t[0] == 'switch':
                terms[b] = ('switch', t[1], [x if x in blocks else None for x in t[2]])
        self.resolve_phicells(blocks, terms)
        drop_unread_regvars(blocks, terms)
        # a branch whose two arms are empty and rejoin (left over from tail-merged pushes) is a no-op
        for b in list(terms):
            t = terms[b]
            if t[0] == 'cond':
                ends = []
                for x in (t[2], t[3]):
                    while x in blocks and not blocks[x] and terms[x][0] == 'goto' and x != b:
                        x = terms[x][1]
                    ends.append(x)
                if ends[0] == ends[1]:
                    terms[b] = ('goto', ends[0])
        # drop blocks that became unreachable once error-check branches were resolved
        live = set()
        work = [L.cfg.entry] + [h for h in L.cfg.extra if h in blocks]
        while work:
            v = work.pop()
            if v in live or v not in blocks:
                continue
            live.add(v)
            work.extend(succs_of(terms[v]))
        for b in list(blocks):
            if b not in live:
                del blocks[b]
                del terms[b]
        self.merge_chains(blocks, terms, L.cfg.entry, set(L.cfg.extra))
        return blocks, terms

    def resolve_phicells(self, blocks, terms):
        from lift import PhiCell
        succ = {b: succs_of(t) for b, t in terms.items()}

        def reach(start, avoid):
            seen = set()
            st = [start]
            while st:
                v = st.pop()
                if v in seen or v == avoid or v not in succ:
                    continue
                seen.add(v)
                st.extend(succ[v])
            return seen

        def side_of(c, p, merge):
            t = terms[c]
            if p == c:
                return t[2] == merge
            rt, rf = reach(t[2], c), reach(t[3], c)
            if p in rt and p not in rf:
                return True
            if p in rf and p not in rt:
                return False
            return None

        def resolve(x, merge):
            preds = [p for p, v in x.pairs]
            for c, t in terms.items():
                if t[0] != 'cond':
                    continue
                sides = [side_of(c, p, merge) for p in preds]
                if None in sides or len(set(sides)) != 2:
                    continue
                tv = [v for (p, v), s in zip(x.pairs, sides) if s]
                fv = [v for (p, v), s in zip(x.pairs, sides) if not s]
                if all(same(v, tv[0]) for v in tv) and all(same(v, fv[0]) for v in fv):
                    return Call('IIf', [t[1], tv[0], fv[0]], pure=True, ty=getattr(tv[0], 'ty', None))
            return Raw('[one of: %s]' % ' | '.join(render(v) for p, v in x.pairs))

        for b, stmts in blocks.items():
            def fn(x, b=b):
                if isinstance(x, PhiCell):
                    return resolve(x, b)
                return None
            for s in stmts:
                stmt_rewrite(s, fn)
            terms[b] = term_rewrite(terms[b], fn)

    def add_return_assignments(self, L, blocks, terms, fname):
        """Module Function returning a computed value in EAX: assign it to the function name."""
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

    def merge_chains(self, blocks, terms, entry, roots):
        """Merge b -> t when b ends in an unconditional jump and t has no other predecessor."""
        changed = True
        while changed:
            changed = False
            preds = defaultdict(set)
            for b, t in terms.items():
                for x in succs_of(t):
                    preds[x].add(b)
            for b in list(blocks):
                t = terms[b]
                if t[0] != 'goto':
                    continue
                n = t[1]
                if n == b or n == entry or n in roots or n not in blocks or preds[n] != {b}:
                    continue
                blocks[b] = blocks[b] + blocks[n]
                terms[b] = terms[n]
                del blocks[n]
                del terms[n]
                changed = True
                break

    def params_rewrite(self, p, sig, blocks, terms, fname):
        """arg pointers -> parameters; function result plumbing -> function name."""
        is_method = p.obj is not None and p.obj.kind in ('form', 'class')
        last_off = 4 * ((sig or {}).get('nargs', 0) - 1)
        retptr = bool(sig and sig.get('retptr'))
        resvar = None
        # find the local copied to the result pointer at exit
        if retptr:
            for b, stmts in blocks.items():
                for s in stmts:
                    if isinstance(s, Assign) and isinstance(s.dst, Deref) and isinstance(s.dst.base, Var) and \
                            s.dst.base.kind == 'arg' and s.dst.base.off == last_off and isinstance(s.src, Var) and \
                            s.src.kind == 'local':
                        resvar = ekey(s.src)
        if sig and sig.get('retvar'):
            resvar = sig['retvar']
        result = Var('result', 0, 4, name=fname)
        byref = (sig or {}).get('byref', {})
        # writes into the stack slot of a ByRef parameter are compiler scratch reuse, not the parameter
        for b, stmts in blocks.items():
            for s in stmts:
                if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'arg' and \
                        byref.get(s.dst.off) and not (retptr and s.dst.off == last_off):
                    s.dst = Var('local', 0x10000 + s.dst.off, s.dst.size)

        def fn(x):
            if isinstance(x, Deref) and isinstance(x.base, Var) and x.base.kind in ('arg', 'param') and \
                    x.off == 0 and not getattr(x.base, 'name', None):
                if retptr and x.base.off == last_off:
                    return Var('result', 0, x.size, name=fname)
                return Var('param', x.base.off, x.size, x.ty)
            if isinstance(x, Deref) and isinstance(x.base, Var) and x.base.kind == 'result' and x.off == 0:
                return result
            if type(x).__name__ == 'VTbl' and isinstance(x.obj, Var) and x.obj.kind in ('arg', 'param') and \
                    not getattr(x.obj, 'name', None):
                if retptr and x.obj.off == last_off:
                    return result
                return Var('param', x.obj.off, 4)
            if isinstance(x, Var) and x.kind == 'arg':
                if retptr and x.off == last_off:
                    return result
                if byref.get(x.off):
                    return AddrOf(Var('param', x.off, 4))     # the pointer value of a ByRef parameter
                return Var('param', x.off, x.size)
            if isinstance(x, (Deref,)) and isinstance(x.base, AddrOf) and x.off == 0 and isinstance(x.base.e, Var):
                v = x.base.e
                return Var(v.kind, v.off, x.size, v.ty or x.ty, v.name)
            if type(x).__name__ == 'VTbl' and isinstance(x.obj, AddrOf) and isinstance(x.obj.e, Var):
                return x.obj.e
            if resvar is not None and isinstance(x, Var) and ekey(x) == resvar:
                return result
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)
            terms[b] = term_rewrite(terms[b], fn)
            blocks[b] = [s for s in stmts if not (isinstance(s, Assign) and same(s.dst, s.src))]

    def objects_rewrite(self, blocks, terms):
        """Global VB objects (App, Screen...) and record members."""
        dc = self.dc

        def fn(x):
            if isinstance(x, Call) and isinstance(x.obj, Var) and x.obj.kind == 'global':
                cls = dc.global_objects.get(x.obj.off)
                if cls in ('Global', 'VBGlobal'):
                    n = Call(x.fn, x.args, None, x.kind, x.ty, x.prop, not x.side, x.paren)
                    if hasattr(x, 'member'):
                        n.member = x.member
                    return n
            return self.record_member(x)
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)
            terms[b] = term_rewrite(terms[b], fn)

    def record_member(self, x):
        """Variables that fall inside a record-typed variable -> rec.field."""
        if not isinstance(x, Var) or x.kind not in ('local', 'field', 'global'):
            if isinstance(x, Member) and x.name is None and ekey(x.base) in self.cur_records:
                rec = self.cur_records[ekey(x.base)]
                path = self.field_path(rec, x.off, x.size)
                if path:
                    return Member(x.base, x.off, x.size, name=path)
            return None
        for k, rec in self.cur_records.items():
            if k[0] != 'v' or k[1] != x.kind:
                continue
            base_off = k[2]
            fields, size, align = self.tlb.layout(rec)
            if x.kind == 'local':
                rel = base_off - x.off
            else:
                rel = x.off - base_off
            if 0 <= rel < size and not (rel == 0 and x.size >= size):
                path = self.field_path(rec, rel, x.size)
                if path:
                    return Member(Var(x.kind, base_off, size), rel, x.size, name=path)
        return None

    def record_array_element(self, x):
        """`m_528(i)` where m_528 is an array member of a record variable (DIJOYSTATE.buttons...) ->
        `m_4F8.buttons(i)`."""
        if not (isinstance(x, Index) and isinstance(x.arr, Var) and x.arr.kind in ('local', 'field', 'global')
                and len(x.idx) == 1):
            return None
        v = x.arr
        for k, rec in self.cur_records.items():
            if k[0] != 'v' or k[1] != v.kind or rec not in self.tlb.records:
                continue
            fields, size, align = self.tlb.layout(rec)
            rel = (k[2] - v.off) if v.kind == 'local' else (v.off - k[2])
            if not 0 < rel < size:
                continue
            for name, foff, t in fields:
                if foff == rel and t.startswith('CArray:'):
                    _, elem, n, lb = t.split(':')
                    lb = int(lb)
                    i = x.idx[0]
                    if lb:
                        i = Const(i.v + lb) if isinstance(i, Const) else Bin('+', i, Const(lb))
                    return Index(Member(Var(v.kind, k[2], size), foff, self.tlb.type_size(elem), name=name),
                                 [i], x.ty)
        return None

    def field_path(self, rec, off, access=None):
        """Member path of byte `off` in record `rec`; `access`: the size read or written there (a nested
        record at that offset is entered when the access is smaller than the record)."""
        fields, size, align = self.tlb.layout(rec)
        for name, foff, t in reversed(fields):
            if foff <= off:
                rel = off - foff
                if t.startswith('CArray:'):
                    _, elem, n, lb = t.split(':')
                    es = self.tlb.type_size(elem)
                    k, r = divmod(rel, es)
                    if k < int(n) and r == 0:
                        if rec == 'DIKEYBOARDSTATE':
                            nm = self.tlb.enum_name('CONST_DIKEYFLAGS', k + int(lb))
                            if nm:
                                return '%s(%s)' % (name, nm)
                        return '%s(%d)' % (name, k + int(lb))
                    if k < int(n) and elem in self.tlb.records:
                        sub = self.field_path(elem, r, access)
                        return '%s(%d).%s' % (name, k + int(lb), sub) if sub else None
                    return None
                if rel == 0 and not (t in self.tlb.records and access and access < self.tlb.layout(t)[1]):
                    return name
                if t in self.tlb.records:
                    sub = self.field_path(t, rel, access)
                    return '%s.%s' % (name, sub) if sub else None
                return None
        return None

    # ---------------------------------------------------------------- exclusions
    def excluded_by(self, s):
        for e in iter_exprs_in(s):
            for x in e.walk():
                for ex in self.exclusions:
                    if isinstance(x, Var) and x.kind == 'field' and self.cur_obj is not None:
                        if self.excluded_fields.get((self.cur_obj.index, x.off)) is ex:
                            return ex
                    if isinstance(x, Raw) and x.text.lower() in ex.controls:
                        return ex
                    if isinstance(x, Str) and any(p.search(x.s) for p in ex.patterns):
                        return ex
                    if isinstance(x, Call) and x.kind == 'proc' and getattr(x, 'proc', None) is not None:
                        if self.proc_excluded.get(x.proc.va) is ex:
                            return ex
                    if isinstance(x, Call) and x.kind == 'api' and getattr(x, 'decl', None) is not None and \
                            x.decl.func in ex.apis:
                        return ex
        return None

    def note_excluded_writes(self, s, ex):
        if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind in ('field', 'global'):
            self.excluded_writes.setdefault(ekey(s.dst), set()).add(ex.label)

    def apply_exclusions(self, p, blocks, terms):
        if os.environ.get('VBDEC_NOEXCLUDE'):
            # keep everything (the checker compares all code) but remember what the normal output omits
            saved = (self.last_dropped, dict(self.excluded_writes))
            ex = self._apply_exclusions(p, {b: list(s) for b, s in blocks.items()}, dict(terms))
            self.would_drop[p.va] = Counter(self.last_dropped) if ex is None else Counter({ex: 1})
            self.last_dropped, self.excluded_writes = saved
            if ex is not None:
                self.would_exclude[p.va] = ex.label
            return None
        return self._apply_exclusions(p, blocks, terms)

    def _apply_exclusions(self, p, blocks, terms):
        total = 0
        dropped = Counter()
        for b, stmts in blocks.items():
            for s in stmts:
                for e in iter_exprs_in(s):
                    for x in e.walk():
                        for ex in self.exclusions:
                            # a procedure that builds a score URL / uses the excluded control is dedicated to it
                            if isinstance(x, Str) and any(p.search(x.s) for p in ex.strong):
                                return ex
                            if ex.strong and (getattr(x, 'control', None) or '').lower() in ex.controls:
                                return ex
        self.last_dropped = dropped
        for b, stmts in blocks.items():
            out = []
            for s in stmts:
                total += 1
                ex = self.excluded_by(s)
                if ex is not None:
                    self.note_excluded_writes(s, ex)
                    dropped[ex] += 1
                    if out and isinstance(out[-1], Comment) and out[-1].text.startswith('[omitted'):
                        continue
                    out.append(Comment('[omitted: %s]' % ex.label, s.addr))
                    continue
                out.append(s)
            blocks[b] = out
            t = terms[b]
            if t[0] == 'cond':
                fake = ExprStmt(t[1])
                ex = self.excluded_by(fake)
                if ex is not None:
                    dropped[ex] += 1
        if total and dropped:
            ex, n = dropped.most_common(1)[0]
            if n * 10 >= total * 3:
                return ex
        return None

    def new_index_var(self):
        """A fresh local for an array index recovered from a pointer/offset loop."""
        self.index_vars = getattr(self, 'index_vars', 0) + 1
        n = self.index_vars
        return Var('local', 0x7e00 + 4 * n, 4, name='idx' if n == 1 else 'idx%d' % n)

    # ---------------------------------------------------------------- main per-proc pipeline
    def proc_text(self, p, L):
        dc = self.dc
        sig = dc.signatures.get(p.va) or {}
        fname = dc.proc_name(p)
        self.cur_obj = p.obj
        self.cur_proc = p.va
        self.reg_names = {}
        self.index_vars = 0
        self.cur_param_arrays = {}
        self.param_types = {}
        self.cur_records = dict(self.dc.records)
        self.cur_records.update(getattr(L, 'records', {}))
        blocks, terms = self.prepare(p, L)
        simplify.merge_variant_copies(blocks)
        self.params_rewrite(p, sig, blocks, terms, fname)
        if sig.get('retexpr'):
            self.add_return_assignments(L, blocks, terms, fname)
        self.objects_rewrite(blocks, terms)
        simplify.remove_zero_inits(blocks, L.cfg.entry)
        simplify.propagate_constant_regvars(blocks, terms)
        simplify.propagate_addresses(blocks, terms, L.cfg.entry)
        if simplify.capture_with_indices(blocks, terms, self.new_index_var):
            simplify.propagate_addresses(blocks, terms, L.cfg.entry)
        simplify.fold_boolean_phis(blocks, terms)
        self.merge_chains(blocks, terms, L.cfg.entry, set(L.cfg.extra))
        simplify.fold_phi_copies(blocks, terms)
        simplify.resolve_register_aliases(blocks, terms)
        for rk, xk in simplify.fold_mirrored_registers(blocks, terms, L.cfg.entry):
            # the local (often a dword temporary for a conversion) now carries the register's value: a
            # register used as a 16-bit Integer makes it an Integer, or its arithmetic would not wrap
            # around (or overflow) as the machine code's does
            h = L.vartypes.get(rk, {})
            if h.get('Integer') and not h.get('Long'):
                d = L.vartypes.setdefault(xk, {})
                d['Integer'] = d.get('Integer', 0) + 100
        simplify.propagate_base_copies(blocks)
        for _ in range(4):
            simplify.lower_all(blocks, terms)
            simplify.remove_dead_stores_liveness(blocks, terms)
            simplify.remove_dead_local_stores(blocks, terms)
            simplify.inline_temps(blocks, terms, lambda d, k: False)
            simplify.inline_locals(blocks, terms)
            simplify.clean_statements(blocks)
        sig = dc.signatures.get(p.va) or {}
        byref = sig.get('byref', {})
        last_off = 4 * (sig.get('nargs', 0) - 1)

        def byval_param(x):
            if isinstance(x, Var) and x.kind == 'local' and 0x10000 <= x.off < 0x10100 and \
                    not byref.get(x.off - 0x10000) and not (sig.get('retptr') and x.off - 0x10000 == last_off):
                return Var('param', x.off - 0x10000, x.size)      # a ByVal parameter assigned by the code
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, byval_param)
            terms[b] = term_rewrite(terms[b], byval_param)
        if simplify.undo_strength_reduction(blocks, terms, self.new_index_var):
            simplify.remove_dead_stores_liveness(blocks, terms)
            simplify.inline_temps(blocks, terms, lambda d, k: False)
            simplify.clean_statements(blocks)
        simplify.lower_all(blocks, terms)
        fn = simplify.array_rewrite_fn(self.note_array, self.known_array_stride)

        def byref_elems(x):
            # user procedure arguments that are addresses of array elements (ByRef element)
            if isinstance(x, Call) and x.kind == 'proc':
                new = []
                changed = False
                q = getattr(x, 'proc', None)
                start = 4 if (q is not None and q.obj is not None and q.obj.kind in ('form', 'class')) else 0
                for i, a in enumerate(x.args):
                    if isinstance(a, AddrOf) and isinstance(a.e, (Var, Member, Index)):
                        new.append(a.e)        # passing a variable ByRef
                        changed = True
                        continue
                    if isinstance(a, Bin) and a.op == '+' and not getattr(a, 'ty', None):
                        d = fn(Deref(a, 0, 4))
                        if isinstance(d, Member) and d.off == 0 and isinstance(d.base, Index) and \
                                isinstance(d.base.arr, Var):
                            # the address of a whole element (its size is the array's, not a guess)
                            fa = self.fixed_array(d.base.arr)
                            st = getattr(d.base, 'stride', None)
                            if fa is not None and st == fa[0]:
                                pt = self.callee_param_type(q, start + 4 * i)
                                if pt and self.record_elements(d.base.arr) and not self.is_record_type(pt):
                                    # a record's first member, not the whole record: the parameter says
                                    d = Member(d.base, 0, TYPE_SIZES.get(pt, 4), name='f0')
                                else:
                                    d = d.base
                        if d is not None and not isinstance(d, Deref):
                            new.append(d)
                            changed = True
                            continue
                    new.append(a)
                if changed:
                    n = Call(x.fn, new, x.obj, x.kind, x.ty, x.prop, not x.side, x.paren)
                    n.proc = x.proc
                    return n
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)
                stmt_rewrite(s, byref_elems)
            terms[b] = term_rewrite(terms[b], fn)
            terms[b] = term_rewrite(terms[b], byref_elems)
        simplify.collapse_array_literals(blocks, terms)
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, self.fixed_array_index)
                stmt_rewrite(s, self.fixed_array_member)
                stmt_rewrite(s, self.record_array_element)
            terms[b] = term_rewrite(terms[b], self.fixed_array_index)
            terms[b] = term_rewrite(terms[b], self.fixed_array_member)
            terms[b] = term_rewrite(terms[b], self.record_array_element)
        # stack slots reused for unrelated temporaries become separate variables (each its own type)
        split = simplify.split_locals(blocks, terms, L.cfg.entry, [h for h in L.cfg.extra if h in blocks],
                                      self.split_value_type)
        self.collect_types(p, L, blocks, terms)
        for nk, ok in split.items():
            # the lifter's type facts were gathered per stack slot: the parts share the size evidence only
            for t, n in self.cur_local_types.get(ok, {}).items():
                if t.startswith('size'):
                    self.cur_local_types[nk][t] += n
        self.boolean_rewrite(blocks, terms)
        self.drop_store_conversions(blocks, sig)
        self.widen_int_arith(blocks, terms, narrow_read_locals(blocks, terms))
        self.float_args(blocks, terms)
        simplify.float_const_stores(blocks, self.var_type, self.float_evidence)
        simplify.strip_condition_negs(terms)
        head, kind, pnames = self.proc_header(p, sig)
        self.proc_param_types[p.va] = dict(self.header_param_types)
        self.proc_param_known[p.va] = dict(self.header_param_known)
        out = ProcOut()
        out.header = head
        out.footer = 'End %s' % kind
        if p.va in self.forced_exclusions:
            ex = self.forced_exclusions[p.va]
            for b, stmts in blocks.items():
                for s in stmts:
                    self.note_excluded_writes(s, ex)
        else:
            ex = self.apply_exclusions(p, blocks, terms)
            for b, stmts in blocks.items():
                for s in stmts:
                    if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind in ('field', 'global'):
                        self.normal_writes.add(ekey(s.dst))
        out.excl = Counter(self.last_dropped) if ex is None else Counter({ex: 1})
        self.cur_names = dict(pnames)
        if ex is not None:
            if p.va not in self.forced_exclusions:
                for b, stmts in blocks.items():
                    for s in stmts:
                        self.note_excluded_writes(s, ex)
            out.omitted = ex.label
            reason = self.project.data.get('omission_reason') if getattr(self, 'project', None) else None
            out.lines = ["    ' [omitted by request: %s code%s]" % (ex.label, ' is ' + reason if reason else '')]
            return out
        NAMER['fn'] = self.name_var
        bypass_empty_blocks(blocks, terms, L.cfg.entry, set(L.cfg.extra))
        simplify.merge_condition_chains(blocks, terms, L.cfg.entry, set(L.cfg.extra), dc.compile_options())
        simplify.strip_condition_negs(terms)
        renumber_temps(blocks, terms)
        if os.environ.get('VBDEC_DUMP') and int(os.environ['VBDEC_DUMP'], 16) == p.va:
            from ir import render_stmt as _rs
            for b in sorted(blocks):
                t = terms[b]
                print('-- %x %s' % (b, (t[0], render(t[1]), hex(t[2]), hex(t[3])) if t[0] == 'cond' else t))
                for s in blocks[b]:
                    print('     ', _rs(s))
        try:
            lines = structure.structure(blocks, terms, L.cfg.entry, [h for h in L.cfg.extra if h in blocks],
                                        exit_kw=kind)
        except RecursionError:
            lines = ["    ' [decompiler] structuring failed (recursion)"]
        out.lines = lines
        out.locals = self.local_decls(p, L, blocks, terms, fname)
        self.rename_loop_registers(out)
        return out

    def rename_loop_registers(self, out):
        """For-loop counters that only lived in a CPU register (rBX...) -> i, j, k..."""
        text = '\n'.join(out.lines)
        regs = []
        for m in re.finditer(r'^\s*For (r[A-Z]{2}\d*) = ', text, re.M):
            if m.group(1) not in regs:
                regs.append(m.group(1))
        if not regs:
            return
        taken = set(re.findall(r'\b[A-Za-z_]\w*\b', text)) | set(out.locals)
        pool = [n for n in ('i', 'j', 'k', 'n', 'm', 'q') if n not in taken]
        mapping = {}
        for r in regs:
            if not pool:
                break
            mapping[r] = pool.pop(0)
        if not mapping:
            return
        pat = re.compile(r'\b(%s)\b' % '|'.join(map(re.escape, mapping)))
        out.lines = [pat.sub(lambda m: mapping[m.group(1)], l) for l in out.lines]
        out.locals = {mapping.get(k, k): v for k, v in out.locals.items()}

    def note_array(self, base, stride, size, kind='pv'):
        if isinstance(base, Var) and base.kind == 'param':
            self.cur_param_arrays[base.off] = stride
        k = ekey(base)
        if base.kind == 'field' and self.cur_obj is not None:
            k = ('field', self.cur_obj.index, base.off)
        elif base.kind == 'local':
            k = ('local', self.cur_proc, base.off)
        self.arrays.setdefault(k, set()).add((stride, size))
        self.array_kind[k] = kind

    def fixed_array(self, v):
        """(element size, bounds) of a fixed-size form/class array (from the instance-data table) or of a
        fixed-size local array (from its __vbaAryConstruct2 template)."""
        if isinstance(v, Var) and v.kind == 'field' and self.cur_obj is not None:
            return self.dc.fixed_arrays.get(self.cur_obj.index, {}).get(v.off)
        if isinstance(v, Var) and v.kind == 'local':
            la = self.dc.local_fixed_arrays.get((self.cur_proc,) + tuple(ekey(v)))
            if la is not None:
                return la[0], la[1]
        return None

    def fixed_array_index(self, x):
        """Element number (counted from the data pointer) -> VB indices of a fixed-size array: adds the
        lower bounds and splits a flat number over the dimensions (first index varies fastest)."""
        if isinstance(x, Index) and len(x.idx) > 1 and getattr(x, 'flat', None) is not None:
            return self.fixed_array_multi(x)
        if not (isinstance(x, Index) and len(x.idx) == 1):
            return None
        fa = self.fixed_array(x.arr)
        if fa is None:
            return None
        cb, bounds = fa
        if getattr(x, 'stride', cb) != cb:
            return None         # indexed by whole rows: see fixed_array_member
        e = x.idx[0]
        var, c = e, 0
        if isinstance(e, Const):
            var, c = None, e.v
        elif isinstance(e, Bin) and e.op in ('+', '-') and isinstance(e.b, Const):
            var, c = e.a, e.b.v if e.op == '+' else -e.b.v
        if len(bounds) == 1 and bounds[0][0] == 0:
            return None
        idx = []
        rest = c
        for k, (lb, ub) in enumerate(bounds):
            n = ub - lb + 1
            if k == len(bounds) - 1:
                part = rest
            else:
                part = rest % n
                rest //= n
            v = lb + part
            if k == 0 and var is not None:
                idx.append(var if v == 0 else Bin('+', var, Const(v)) if v > 0 else Bin('-', var, Const(-v)))
            else:
                idx.append(Const(v))
        return Index(x.arr, idx, x.ty)

    def fixed_array_multi(self, x):
        """A fixed-size array indexed with several variables (element number = i + n1 * j + ...): the
        multipliers say which dimension each index belongs to; a constant part is spread over the
        dimensions like a mixed-radix number; the lower bounds are added."""
        fa = self.fixed_array(x.arr)
        if fa is None:
            return None
        idxs, (mults, k) = x.flat
        # the constant was folded into the first index: take it off again
        terms = list(idxs)
        if k:
            t0 = terms[0]
            if isinstance(t0, Bin) and t0.op in ('+', '-') and isinstance(t0.b, Const) and \
                    (t0.b.v if t0.op == '+' else -t0.b.v) == k:
                terms[0] = t0.a
            else:
                return None
        return self.fixed_array_element(x.arr, terms, mults, k, x.ty)

    def fixed_array_member(self, x):
        """`m_D4(i).f5C` where m_D4 is a fixed-size array of plain elements accessed with a larger stride
        (a whole row at a time): element number i * (stride / element size) + offset / element size."""
        if not (isinstance(x, Member) and isinstance(x.base, Index) and len(x.base.idx) == 1 and
                isinstance(x.base.arr, Var)):
            return None
        fa = self.fixed_array(x.base.arr)
        if fa is None:
            return None
        cb, bounds = fa
        stride = getattr(x.base, 'stride', None)
        if not stride or x.size != cb or x.off % cb or stride % cb or stride == cb:
            return None
        i = x.base.idx[0]
        k = 0
        if isinstance(i, Bin) and i.op in ('+', '-') and isinstance(i.b, Const):
            k = (i.b.v if i.op == '+' else -i.b.v) * (stride // cb)
            i = i.a
        elif isinstance(i, Const):
            k, i = i.v * (stride // cb), None
        k += x.off // cb
        if i is None:
            return self.fixed_array_element(x.base.arr, [], [], k, x.ty)
        return self.fixed_array_element(x.base.arr, [i], [stride // cb], k, x.ty)

    def fixed_array_element(self, arr, terms, mults, k, ty):
        """Index of a fixed-size array from its element number sum(terms * mults) + k."""
        cb, bounds = self.fixed_array(arr)
        sizes = [ub - lb + 1 for lb, ub in bounds]
        place = [1]
        for n in sizes[:-1]:
            place.append(place[-1] * n)
        dim_of = {}
        for t, m in zip(terms, mults):
            if m not in place or place.index(m) in dim_of:
                return None
            dim_of[place.index(m)] = t
        out = []
        rest = k
        for d, (lb, ub) in enumerate(bounds):
            if d == len(bounds) - 1:
                digit = rest
            else:
                digit = rest % sizes[d]
                rest //= sizes[d]
            c = lb + digit
            t = dim_of.get(d)
            if t is None:
                out.append(Const(c))
            else:
                out.append(t if c == 0 else Bin('+', t, Const(c)) if c > 0 else Bin('-', t, Const(-c)))
        return Index(arr, out, ty)

    SMALL_INT = ('Integer', 'Byte', 'Boolean')

    def widen_int_arith(self, blocks, terms, narrow):
        """Integer values the machine combines in 32-bit registers (`movsx` then `lea` chains, `neg eax`,
        32-bit adds - code built without overflow checks): that is Long arithmetic. Written as VB
        Integer arithmetic it would wrap around (or overflow) where the machine's does not, so one
        operand becomes CLng(...). Not needed where only the low 16 bits of the result are used (stored
        into an Integer): those are the same either way."""
        def small(e):
            if isinstance(e, Const):
                return -32768 <= e.v <= 32767 and not isinstance(e.v, float)
            if isinstance(e, Var):
                if e.kind in ('field', 'global'):
                    return self.var_type(e) in self.SMALL_INT
                if e.kind == 'param':
                    t = self.param_types.get(e.off) or self.proc_param_types.get(self.cur_proc, {}).get(e.off)
                    return t in self.SMALL_INT
                if e.kind in ('local', 'reg'):
                    # as local_decls will declare it
                    k = ekey(e)
                    return k in narrow or \
                        choose_type(self.cur_local_types.get(k, Counter()), e.size) in self.SMALL_INT
                return False
            if isinstance(e, Member):
                return e.size in (1, 2) and e.ty not in ('Single', 'Double', 'String')
            if isinstance(e, Index) and isinstance(e.arr, Var):
                if e.ty is not None:
                    return e.ty in self.SMALL_INT
                s = self.arrays.get(self.array_key(e.arr))
                return bool(s) and max(s)[1] in (1, 2) and \
                    choose_type(self.elem_hints.get(self.array_key(e.arr), Counter()), 2) in self.SMALL_INT
            if isinstance(e, Call):
                t = e.ty
                if e.kind == 'proc' and getattr(e, 'proc', None) is not None:
                    t = (self.dc.signatures.get(e.proc.va) or {}).get('rettype')
                return t in self.SMALL_INT
            if isinstance(e, Temp):
                return e.ty in self.SMALL_INT
            if isinstance(e, Bin) and e.op in ('+', '-', '*', '\\', 'Mod', 'And', 'Or', 'Xor'):
                return small(e.a) and small(e.b)
            if isinstance(e, Un) and e.op in ('neg', 'Not'):
                return small(e.a)
            return False

        low16 = set()

        def mark(e):
            # the consumer keeps only the low 16 bits: so do + - * And Or Xor Not of the operands
            if isinstance(e, Bin) and e.op in ('+', '-', '*', 'And', 'Or', 'Xor'):
                low16.add(id(e))
                mark(e.a)
                mark(e.b)
            elif isinstance(e, Un) and e.op in ('neg', 'Not'):
                low16.add(id(e))
                mark(e.a)

        def clng(x):
            c = Call('CLng', [x], pure=True, ty='Long')
            c.widened = True
            return c

        def fn(x):
            if isinstance(x, Bin) and x.op in ('<', '<=', '>', '>=', '=', '<>') and isinstance(x.b, Const) and \
                    isinstance(x.a, Bin) and x.a.op in ('+', '-') and isinstance(x.a.b, Const) and \
                    isinstance(x.a.a, Call) and x.a.a.fn == 'CLng' and getattr(x.a.a, 'widened', False) and \
                    abs(x.a.b.v) <= 0x10000 and abs(x.b.v) <= 0x10000:
                # CLng(x) - c < k: 32-bit arithmetic on a 16-bit value cannot wrap - exactly x < k + c
                c = x.a.b.v if x.a.op == '+' else -x.a.b.v
                return Bin(x.op, x.a.a.args[0], Const(x.b.v - c), x.ty)
            if id(x) in low16 or getattr(x, 'w', None) != 4:
                return None
            if isinstance(x, Bin) and x.op in ('+', '-', '*') and small(x.a) and small(x.b):
                if isinstance(x.a, Const):
                    n = Bin(x.op, x.a, clng(x.b), x.ty)
                else:
                    n = Bin(x.op, clng(x.a), x.b, x.ty)
                n.w = x.w
                return n
            if isinstance(x, Un) and x.op == 'neg' and small(x.a):
                n = Un('neg', clng(x.a), x.ty)
                n.w = x.w
                return n
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                low16.clear()
                if isinstance(s, Assign) and isinstance(s.dst, Var) and small(s.dst):
                    mark(s.src)         # (a variable declared Integer keeps the low 16 bits, whatever its slot)
                elif isinstance(s, Assign) and getattr(s.dst, 'size', 4) in (1, 2) and \
                        isinstance(s.dst, (Member, Deref)) and small(s.dst):
                    mark(s.src)
                elif isinstance(s, Assign) and isinstance(s.dst, Index) and small(s.dst):
                    mark(s.src)
                stmt_rewrite(s, fn)
            low16.clear()
            if terms[b][0] != 'switch':     # (a jump table's index arithmetic selects a Case: not evaluated)
                terms[b] = term_rewrite(terms[b], fn)

    def callee_param_type(self, q, off):
        """The (known) type of a program procedure's parameter at `off`, from the earlier pass."""
        if q is None or not self.proc_param_known.get(q.va, {}).get(off):
            return None
        return self.proc_param_types.get(q.va, {}).get(off)

    def is_record_type(self, t):
        return t.startswith('udt_') or t in self.tlb.records

    def record_elements(self, arr):
        """Are the elements of this array records (accessed in parts smaller than an element)?"""
        if arr.kind == 'field' and self.cur_obj is not None:
            k = ('field', self.cur_obj.index, arr.off)
        elif arr.kind == 'local':
            k = ('local', self.cur_proc, arr.off)
        else:
            k = ekey(arr)
        fa = self.fixed_array(arr)
        cb = fa[0] if fa else None
        return any(sz < (cb or st) or st % (cb or st) for st, sz in self.arrays.get(k, ()))

    def known_array_stride(self, v):
        fa = self.fixed_array(v)
        if fa is not None:
            return fa[0]
        if v.kind == 'field' and self.cur_obj is not None:
            k = ('field', self.cur_obj.index, v.off)
        elif v.kind == 'local':
            k = ('local', self.cur_proc, v.off)
        else:
            k = ekey(v)
        s = self.arrays.get(k)
        if not s or self.array_kind.get(k) == 'psa':
            return None
        return max(s)[0]

    def is_array(self, v):
        if v.kind == 'field' and self.cur_obj is not None:
            return ('field', self.cur_obj.index, v.off) in self.arrays
        if v.kind == 'local':
            return ('local', self.cur_proc, v.off) in self.arrays
        return ekey(v) in self.arrays

    # ---------------------------------------------------------------- types
    def var_type(self, v):
        if not isinstance(v, Var):
            return None
        if v.kind == 'field' and self.cur_obj is not None:
            k = (self.cur_obj.index, v.off)
            return choose_type(self.field_hints.get(k, Counter()), self.field_sizes.get(k) or v.size)
        if v.kind == 'global':
            return choose_type(self.global_hints.get(v.off, Counter()), self.global_sizes.get(v.off, v.size))
        if v.kind == 'local':
            return choose_type(self.cur_local_types.get(ekey(v), Counter()), v.size)
        return None

    def float_evidence(self, v):
        """Floating-point type of variable v from real evidence (a float access or a declared type), not
        merely from its size."""
        if not isinstance(v, Var):
            return None
        if v.kind == 'field' and self.cur_obj is not None:
            h = self.field_hints.get((self.cur_obj.index, v.off), Counter())
        elif v.kind == 'global':
            h = self.global_hints.get(v.off, Counter())
        elif v.kind == 'local':
            h = self.cur_local_types.get(ekey(v), Counter())
        else:
            return None
        for t in ('Double', 'Date', 'Single'):
            if h.get(t):
                return t
        return None

    def float_args(self, blocks, terms):
        """Integer bit patterns passed to Single/Double parameters are float constants."""
        import struct as _st

        def fn(x):
            if isinstance(x, Call) and x.kind == 'proc' and getattr(x, 'proc', None) is not None:
                pt = self.proc_param_types.get(x.proc.va)
                if not pt:
                    return None
                start = 4 if (x.proc.obj is not None and x.proc.obj.kind in ('form', 'class')) else 0
                new = list(x.args)
                ch = False
                for i, a in enumerate(new):
                    t = pt.get(start + 4 * i)
                    if t == 'Single' and isinstance(a, Const):
                        new[i] = FConst(_st.unpack('<f', _st.pack('<I', a.v & 0xffffffff))[0], 'Single')
                        ch = True
                    elif t and isinstance(a, Const) and a.v == 0 and (t == 'Object' or self.is_class_name(t)):
                        new[i] = Raw('Nothing', 'Object')       # a null object reference
                        ch = True
                if ch:
                    n = Call(x.fn, new, x.obj, x.kind, x.ty, x.prop, not x.side, x.paren)
                    n.proc = x.proc
                    return n
            if isinstance(x, Call) and x.kind == 'com' and getattr(x, 'member', None) is not None:
                # 0 for an object parameter of a type library method (ByVal or ByRef): Nothing
                new = list(x.args)
                ch = False
                for i, (prm, a) in enumerate(zip(x.member.visible_params, x.args)):
                    t = prm['type'].rstrip('*')
                    it = self.tlb.by_name.get(t)
                    if isinstance(a, Const) and a.v == 0 and \
                            (t in ('IUnknown', 'IDispatch') or
                             (it is not None and it.kind in ('interface', 'dispatch', 'coclass'))):
                        new[i] = Raw('Nothing', 'Object')
                        ch = True
                if ch:
                    n = Call(x.fn, new, x.obj, x.kind, x.ty, x.prop, not x.side, x.paren)
                    n.member = x.member
                    return n
            return None
        hp = self.proc_param_types.get(self.cur_proc, {})     # this procedure's header (earlier pass)
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)
                # a dword constant stored into a Single parameter is the float's bit pattern
                if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'param' and \
                        isinstance(s.src, Const) and s.dst.size == 4 and \
                        (hp.get(s.dst.off) or self.param_types.get(s.dst.off)) == 'Single':
                    s.src = FConst(_st.unpack('<f', _st.pack('<I', s.src.v & 0xffffffff))[0], 'Single')

    def drop_store_conversions(self, blocks, sig):
        """`x = CInt(e)` with x an Integer is `x = e` when storing converts the same way: a floating-point e
        (rounded and range-checked by the runtime either way), or any e when the program checks integer
        overflow. Without those checks, storing an integer into a narrower variable just cuts it, while an
        explicit CInt checks the range: then the CInt stays."""
        conv = {'CInt': 'Integer', 'CLng': 'Long', 'CByte': 'Byte', 'CSng': 'Single', 'CDbl': 'Double'}
        checked = self.dc.compile_options().get('overflow_checks', True)
        for b, stmts in blocks.items():
            for s in stmts:
                if not (isinstance(s, Assign) and s.kind == 'let' and isinstance(s.src, Call) and
                        s.src.kind == 'rt' and s.src.fn in conv and len(s.src.args) == 1 and
                        s.src.args[0] is not None):
                    continue
                d = s.dst
                t = sig.get('rettype') if isinstance(d, Var) and d.kind == 'result' else self.var_type(d)
                a = s.src.args[0]
                floating = expr_type(a) in ('Single', 'Double', 'Currency', 'Date') or isinstance(a, FConst) or \
                    isinstance(a, Call) and a.fn in ('Int', 'Fix') and a.ty in ('Single', 'Double', None)
                if t == conv[s.src.fn] and (floating or checked or t in ('Single', 'Double')):
                    s.src = a

    def boolean_rewrite(self, blocks, terms):
        """Constants -1/0 stored into / compared with Boolean variables -> True/False."""
        def fn(x):
            if isinstance(x, Bin) and x.op in ('=', '<>') and isinstance(x.b, Const) and x.b.v in (0, -1) and \
                    self.var_type(x.a) == 'Boolean':
                truth = (x.b.v == -1) == (x.op == '=')
                return x.a if truth else Un('Not', x.a, ty='Boolean')
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                if isinstance(s, Assign) and isinstance(s.src, Const) and s.src.v in (0, -1) and \
                        self.var_type(s.dst) == 'Boolean':
                    s.src = Const(s.src.v, 'Boolean')
                stmt_rewrite(s, fn)
            terms[b] = term_rewrite(terms[b], fn)

    def collect_types(self, p, L, blocks, terms):
        """Accumulate type evidence for fields / globals / locals."""
        self.cur_local_types = defaultdict(Counter)
        self.param_types = {}
        for k, d in L.vartypes.items():
            for t, n in d.items():
                self.add_hint(k, t, n)
        for k, sz in L.var_sizes.items():
            self.add_size(k, sz)
        for b, stmts in blocks.items():
            for s in stmts:
                if isinstance(s, Assign) and isinstance(s.dst, Var) and isinstance(s.src, Const):
                    if s.src.v == -1:
                        self.add_hint(ekey(s.dst), 'BoolConst', 1)
                    elif s.src.v != 0:
                        self.add_hint(ekey(s.dst), 'IntConst', 1)
                if isinstance(s, Assign) and isinstance(s.dst, Index) and isinstance(s.dst.arr, Var):
                    if s.dst.ty in TYPE_SET:
                        self.add_hint(('elem',) + ekey(s.dst.arr), s.dst.ty, 1)    # how the store was done
                    t = expr_type(s.src)
                    if s.kind == 'set':
                        t = t if t and t not in TYPE_SET else 'Object'
                    if t:
                        self.add_hint(('elem',) + ekey(s.dst.arr), t, 1)
                if isinstance(s, Assign):
                    d = s.dst
                    t = expr_type(s.src)
                    if isinstance(d, Var) and t:
                        self.add_hint(ekey(d), t, 2 if s.kind == 'set' else 1)
                    if s.kind == 'set' and isinstance(d, Var):
                        self.add_hint(ekey(d), 'Object', 2)
                for e in iter_exprs_in(s):
                    self.hints_from_expr(e)
            for e in term_exprs(terms[b]):
                self.hints_from_expr(e)
        self.byref_arg_hints(blocks, terms)
        for k, c in self.dc.obj_types.items():
            if k[0] == 'proc':          # a variable of one procedure
                if k[1] != p.va:
                    continue
                k = tuple(k[2:])
            for t, n in c.items():
                self.add_hint(k, t, n * 3)
        for k, rec in self.cur_records.items():
            self.add_hint(k, rec, 100)
        self.collect_record_field_types(blocks, terms)
        self.collect_return_hints(p, blocks)

    def byref_arg_hints(self, blocks, terms):
        """A variable passed ByRef to one of the program's procedures is passed by address, unconverted:
        it has exactly the parameter's type (VB refuses anything else)."""
        def scan(e):
            for x in e.walk():
                if not (isinstance(x, Call) and x.kind == 'proc' and getattr(x, 'proc', None) is not None):
                    continue
                q = x.proc
                sig = self.dc.signatures.get(q.va) or {}
                pt = self.proc_param_types.get(q.va, {})
                known = self.proc_param_known.get(q.va, {})
                start = 4 if (q.obj is not None and q.obj.kind in ('form', 'class')) else 0
                for i, a in enumerate(x.args):
                    off = start + 4 * i
                    t = pt.get(off)
                    if not (sig.get('byref', {}).get(off) and known.get(off) and t and t not in ('Variant', 'Object') and
                            (t in TYPE_SET or self.is_class_name(t))):
                        continue
                    if isinstance(a, Var) and a.kind in ('param', 'local', 'field', 'global'):
                        self.add_hint(ekey(a), t, 50)
                    elif isinstance(a, Index) and isinstance(a.arr, Var):
                        self.add_hint(('elem',) + ekey(a.arr), t, 50)     # an array element passed ByRef
        for b, stmts in blocks.items():
            for s in stmts:
                for e in iter_exprs_in(s):
                    scan(e)
            for e in term_exprs(terms[b]):
                scan(e)

    def split_value_type(self, e):
        """Kind of value an expression gives, for deciding whether a reused stack slot holds different
        things (None when unknown)."""
        if isinstance(e, Const):
            return None
        if isinstance(e, Var) and e.kind in ('field', 'global'):
            t = self.var_type(e)
        elif isinstance(e, Var) and e.kind == 'param':
            t = self.proc_param_types.get(self.cur_proc, {}).get(e.off)
        elif isinstance(e, Call) and e.kind == 'proc' and getattr(e, 'proc', None) is not None:
            t = (self.dc.signatures.get(e.proc.va) or {}).get('rettype')
        else:
            t = expr_type(e)
        if t in ('Integer', 'Long', 'Byte', 'Boolean'):
            return 'integer'            # integer kinds convert exactly into each other's variable
        return t

    def collect_return_hints(self, p, blocks):
        """Evidence for function result types. The machine code copies a result without converting it,
        so the function's type is the type of what it assigns to its result and of the variables its
        callers store the result in (a 2-byte result may be Integer or Boolean, a 4-byte one Long,
        Single, String or an object)."""
        def value_type(e):
            if isinstance(e, Var) and e.kind == 'param':
                return self.param_types.get(e.off) or self.proc_param_types.get(p.va, {}).get(e.off)
            if isinstance(e, Var) and e.kind in ('field', 'global', 'local'):
                return self.var_type(e)
            if isinstance(e, Const):
                return 'BoolConst' if e.v == -1 else 'IntConst' if e.v else None
            if isinstance(e, Index) and isinstance(e.arr, Var) and e.ty not in TYPE_SET:
                h = self.elem_hints.get(self.array_key(e.arr))
                if h:
                    return choose_type(h, 4)        # the array's element type
            return expr_type(e)

        def usable(t, s):
            # object types only from Set assignments: a Let of an object is a default-property call
            return t and (s.kind == 'set') == (t not in TYPE_SET or t == 'Object')
        for b, stmts in blocks.items():
            for s in stmts:
                if not isinstance(s, Assign):
                    continue
                if isinstance(s.dst, Var) and s.dst.kind == 'result':
                    t = value_type(s.src)
                    if usable(t, s) or t in ('IntConst', 'BoolConst'):
                        self.ret_hints[p.va][t] += 2
                elif isinstance(s.src, Call) and s.src.kind == 'proc' and getattr(s.src, 'proc', None) is not None:
                    t = value_type(s.dst)
                    if usable(t, s):
                        self.ret_hints[s.src.proc.va][t] += 1

    def refine_return_types(self):
        """Settle Integer/Boolean (and Long/Single/String/object) function results from the evidence."""
        for va, sig in self.dc.signatures.items():
            t = sig.get('rettype')
            h = self.ret_hints.get(va)
            if not h or t not in ('Integer', 'Long'):
                continue
            if t == 'Integer':
                new = choose_field_type(h, 2)
            else:
                # the best supported 4-byte type (constants count for Long); a tie keeps Long
                score = Counter()
                for k, n in h.items():
                    if k in ('Long', 'IntConst', 'BoolConst'):
                        score['Long'] += n
                    elif k in ('Single', 'String') or k == 'Object' or k not in TYPE_SET and k[0].isupper() \
                            and k not in ('Ptr', 'Hex', 'Cond'):
                        score[k] += n
                new = max(score, key=lambda k: (score[k], k == 'Long')) if score else t
            if new != t:
                sig['rettype'] = new

    # ---------------------------------------------------------------- record arrays (UDT elements)
    def array_key(self, v):
        """Key of an array variable, as used by note_array."""
        if v.kind == 'field' and self.cur_obj is not None:
            return ('field', self.cur_obj.index, v.off)
        if v.kind == 'local':
            return ('local', self.cur_proc, v.off)
        return ekey(v)

    @staticmethod
    def rec_member(x):
        """`arr(i).fNN`: a field of a record-array element (named after its byte offset)."""
        return isinstance(x, Member) and isinstance(x.base, Index) and isinstance(x.base.arr, Var) and \
            bool(x.name) and re.fullmatch(r'f[0-9A-F]+', x.name) is not None

    def rec_hint(self, m, t, n=1):
        d = self.rec_fields[self.array_key(m.base.arr)][m.off]
        d['size%d' % m.size] += 1
        if t:
            d[t] += n

    def collect_record_field_types(self, blocks, terms):
        """Type evidence for the fields of record arrays: access sizes, values stored, objects used."""
        def scan(e):
            for x in e.walk():
                if self.rec_member(x):
                    self.rec_hint(x, x.ty if x.ty in TYPE_SET else None)
                if isinstance(x, Call) and x.kind == 'com' and self.rec_member(x.obj):
                    m = getattr(x, 'member', None)
                    if m is not None:
                        self.rec_hint(x.obj, self.tlb.class_name_for(m.iface.name), 3)
                    else:
                        self.rec_hint(x.obj, 'Object', 1)
                if isinstance(x, Bin) and x.op in ('=', '<>') and self.rec_member(x.a) and isinstance(x.b, Const):
                    self.rec_hint(x.a, 'BoolConst' if x.b.v == -1 else 'IntConst' if x.b.v else None)
                if isinstance(x, Bin) and x.op == '&':
                    for y in (x.a, x.b):
                        if self.rec_member(y):
                            self.rec_hint(y, 'String')
                if isinstance(x, Call) and x.kind == 'proc':
                    pt = self.proc_param_types.get(getattr(x.proc, 'va', None), {})
                    start = 4 if (x.proc.obj is not None and x.proc.obj.kind in ('form', 'class')) else 0
                    for i, a in enumerate(x.args):
                        if self.rec_member(a) and pt.get(start + 4 * i):
                            self.rec_hint(a, pt[start + 4 * i])
        for b, stmts in blocks.items():
            for s in stmts:
                if isinstance(s, Assign) and self.rec_member(s.dst):
                    if s.dst.ty in TYPE_SET:
                        self.rec_hint(s.dst, s.dst.ty, 3)     # the kind of store (e.g. fstp dword: Single)
                    t = expr_type(s.src)
                    if s.kind == 'set':
                        t = t if t and t not in TYPE_SET else 'Object'
                        self.rec_hint(s.dst, 'Object', 2)
                    if isinstance(s.src, Const):
                        t = 'BoolConst' if s.src.v == -1 else 'IntConst' if s.src.v else t
                    self.rec_hint(s.dst, t)
                for e in iter_exprs_in(s):
                    scan(e)
            for e in term_exprs(terms[b]):
                scan(e)

    def record_type_name(self, arr_name):
        return 'udt_%s' % arr_name

    def type_decl(self, name, ak, stride, scope='Private'):
        """`Type` declaration for the elements of record array `ak` (stride bytes each). Fields keep the
        decompiler's fNN names (NN = byte offset); bytes the code never touches become unused_NN fillers,
        so every field sits at the same offset as in the original record."""
        sizes = {'Integer': 2, 'Boolean': 2, 'Long': 4, 'Single': 4, 'String': 4, 'Object': 4, 'Double': 8,
                 'Date': 8, 'Currency': 8, 'Byte': 1, 'Variant': 16}
        fields = self.rec_fields.get(ak, {})
        lines = ["%s Type %s   ' %d-byte record; fields are named f<byte offset>" % (scope, name, stride)]
        cur = 0
        for off in sorted(fields):
            hints = fields[off]
            size = max([int(k[4:]) for k in hints if k.startswith('size')] or [4])
            t = choose_field_type(hints, size)
            tsize = sizes.get(t, 4)
            if off < cur:
                lines.append("    ' f%X (%d bytes) overlaps the previous field" % (off, size))
                continue
            if off % min(tsize, 4):
                lines.append("    ' f%X is not aligned for %s: kept as bytes" % (off, t))
                t, tsize = None, size
            if off > cur:
                lines.append('    unused_%X(0 To %d) As Byte' % (cur, off - cur - 1))
            if t is None:
                lines.append('    f%X(0 To %d) As Byte' % (off, tsize - 1))
            else:
                lines.append('    f%X As %s' % (off, t))
            cur = off + tsize
        if cur < stride:
            lines.append('    unused_%X(0 To %d) As Byte' % (cur, stride - cur - 1))
        lines.append('End Type')
        return lines

    def hints_from_expr(self, e):
        for x in e.walk():
            if isinstance(x, Index) and isinstance(x.arr, Var) and x.ty in TYPE_SET:
                self.add_hint(('elem',) + ekey(x.arr), x.ty, 1)
            if isinstance(x, Call) and x.kind == 'com' and isinstance(x.obj, Index) and \
                    isinstance(x.obj.arr, Var) and getattr(x, 'member', None) is not None:
                # a method called on an array element: the elements are of that class
                self.add_hint(('elem',) + ekey(x.obj.arr), self.tlb.class_name_for(x.member.iface.name), 3)
            if isinstance(x, Call) and x.kind == 'com' and getattr(x, 'member', None) is not None:
                # an object argument of a type library method (DuplicateSoundBuffer(original As
                # DirectSoundSecondaryBuffer8)): what is passed there holds that class (VB refuses others)
                for prm, a in zip(x.member.visible_params, x.args):
                    t = prm['type']
                    it = self.tlb.by_name.get(t[:-1]) if t.endswith('*') and not t.endswith('**') else None
                    if it is None or it.kind not in ('interface', 'dispatch', 'coclass'):
                        continue
                    cls = self.tlb.class_name_for(it.name)
                    if isinstance(a, Index) and isinstance(a.arr, Var):
                        self.add_hint(('elem',) + ekey(a.arr), cls, 3)
                    elif isinstance(a, Var) and a.kind in ('field', 'global', 'local', 'param'):
                        self.add_hint(ekey(a), cls, 3)
            if isinstance(x, Var) and x.kind == 'param' and x.ty:
                self.add_hint(ekey(x), x.ty, 1)
            if isinstance(x, Call) and x.obj is not None and isinstance(x.obj, Var):
                if x.kind == 'com':
                    m = getattr(x, 'member', None)
                    if m is not None:
                        self.add_hint(ekey(x.obj), self.tlb.class_name_for(m.iface.name), 3)
                    else:
                        self.add_hint(ekey(x.obj), 'Object', 1)
            if isinstance(x, Bin) and x.op in ('=', '<>') and isinstance(x.a, Var) and isinstance(x.b, Const):
                if x.b.v == -1:
                    self.add_hint(ekey(x.a), 'BoolConst', 1)
                elif x.b.v != 0:
                    self.add_hint(ekey(x.a), 'IntConst', 1)
            if isinstance(x, Bin) and x.op == '&':
                for y in (x.a, x.b):
                    if isinstance(y, Var):
                        self.add_hint(ekey(y), 'String', 1)
            if isinstance(x, Bin) and x.op in ('=', '<>', '<', '>', '<=', '>=') and \
                    (isinstance(x.a, Str) or isinstance(x.b, Str)):
                for y in (x.a, x.b):
                    if isinstance(y, Var):
                        self.add_hint(ekey(y), 'String', 1)

    def add_hint(self, k, t, n=1):
        if k is None or t is None:
            return
        if k[0] == 'elem':
            kk = k[1:]
            if kk[1] == 'field' and self.cur_obj is not None:
                self.elem_hints[('field', self.cur_obj.index, kk[2])][t] += n
            elif kk[1] == 'global':
                self.elem_hints[('global', kk[2])][t] += n
            return
        if k[0] == 'v' and k[1] == 'field' and self.cur_obj is not None:
            self.field_hints[(self.cur_obj.index, k[2])][t] += n
        elif k[0] == 'v' and k[1] == 'global':
            self.global_hints[k[2]][t] += n
        elif k[0] == 'v' and k[1] in ('local', 'reg'):
            self.cur_local_types[k][t] += n
        elif k[0] == 'v' and k[1] == 'param' and t in TYPE_SET:
            # (a generic Object - e.g. from `Set p1 = ...` - says less than a class name already known)
            if t != 'Object' or self.param_types.get(k[2]) is None:
                self.param_types[k[2]] = t
        elif k[0] == 'v' and k[1] == 'param' and t and self.is_class_name(t):
            self.param_types[k[2]] = t      # an object parameter whose members the code calls

    def is_class_name(self, t):
        """A COM class / interface name of the type libraries (as VB code writes it)."""
        tl = self.tlb
        return t in tl.by_name or ('_' + t) in tl.by_name or any(cc.name == t for cc in tl.coclass.values())

    def add_size(self, k, sz):
        if k[0] == 'v' and k[1] == 'field' and self.cur_obj is not None:
            key = (self.cur_obj.index, k[2])
            self.field_sizes[key] = max(self.field_sizes[key], sz)
        elif k[0] == 'v' and k[1] == 'global':
            self.global_sizes[k[2]] = max(self.global_sizes[k[2]], sz)
        elif k[0] == 'v' and k[1] in ('local', 'reg'):
            self.cur_local_types[k]['size%d' % sz] += 1

    def local_decls(self, p, L, blocks, terms, fname):
        used = {}
        for b, stmts in blocks.items():
            for s in stmts:
                for e in iter_exprs_in(s):
                    for x in e.walk():
                        if isinstance(x, Var) and x.kind == 'local':
                            used[ekey(x)] = x
                        if isinstance(x, Temp):
                            used[ekey(x)] = x
                if isinstance(s, Assign) and isinstance(s.dst, (Var, Temp)) and \
                        (not isinstance(s.dst, Var) or s.dst.kind == 'local'):
                    used[ekey(s.dst)] = s.dst
                if isinstance(s, Assign) and isinstance(s.dst, Member):
                    d = s.dst
                    while isinstance(d, Member):
                        d = d.base
                    if isinstance(d, Var) and d.kind == 'local':
                        used[ekey(d)] = d
            for e in term_exprs(terms[b]):
                for x in e.walk():
                    if isinstance(x, (Var, Temp)) and (not isinstance(x, Var) or x.kind in ('local', 'reg')):
                        used[ekey(x)] = x
        for b, stmts in blocks.items():
            for s in stmts:
                for e in list(iter_exprs_in(s)) + ([s.dst] if isinstance(s, Assign) else []):
                    for x in e.walk():
                        if isinstance(x, Var) and x.kind == 'reg':
                            used[ekey(x)] = x
        narrow = narrow_read_locals(blocks, terms)
        decls = {}
        for k, v in used.items():
            if isinstance(v, Temp):
                decls[v.name] = v.ty if v.ty in TYPE_SET else 'Variant'
                continue
            name = self.name_var(v)
            ak = ('local', self.cur_proc, v.off) if isinstance(v, Var) and v.kind == 'local' else None
            fa = self.fixed_array(v) if ak else None
            if fa is not None:
                # a fixed-size local array: declared with its bounds
                cb, bounds = fa
                vt = self.dc.local_fixed_arrays[(self.cur_proc,) + tuple(ekey(v))][2]
                et = VT_TYPES.get(vt) or choose_type(self.elem_hints.get(ak, Counter()), cb)
                decls['%s(%s)' % (name, ', '.join('%d To %d' % b for b in bounds))] = et
                continue
            if ak is not None and ak in self.arrays:
                # a dynamic local array (ReDim): the element type from the ReDim's type code when known
                stride, esize = max(self.arrays[ak])
                rd = self.dc.arrays.get(('proc', self.cur_proc) + tuple(ekey(v)))
                decls['%s()' % name] = VT_TYPES.get(rd[0] if rd else None) or \
                    choose_type(self.elem_hints.get(ak, Counter()), esize)
                continue
            if k in narrow:
                # every read takes the low 16 bits: an Integer (or Boolean) that is assigned 32-bit values
                # unchecked (overflow checks off: the machine stores the whole register, VB wraps)
                decls[name] = choose_field_type(self.cur_local_types.get(k, Counter()), 2)
                continue
            decls[name] = choose_type(self.cur_local_types.get(k, Counter()), v.size)
        return decls

    # ================================================================ module level
    def field_decls(self, o, used_names=None):
        """Dim lines of the form's variables. used_names: the m_XX names the emitted code uses (a variable
        only ever touched as part of another one, e.g. the upper half of a Double cleared with two dword
        stores, does not limit the size of the variable before it)."""
        out = []
        keys = sorted(k for k in self.field_hints if k[0] == o.index)
        keys = sorted(set(keys) | {k for k in self.field_sizes if k[0] == o.index} |
                      {(ak[1], ak[2]) for ak in self.arrays if ak[0] == 'field' and ak[1] == o.index})
        bounds_keys = [k for k in keys if used_names is None or 'm_%X' % k[1] in used_names or
                       (o.index, k[1]) in self.control_by_storage]
        for k in keys:
            off = k[1]
            if (o.index, off) in self.control_by_storage:
                continue
            if off < 0x30:
                continue        # object header (vtable, reference count...), not a variable
            if self.inside_record_field(o, off):
                continue
            ak = ('field', o.index, off)
            fa = self.dc.fixed_arrays.get(o.index, {}).get(off)
            if fa is not None:
                cb, bounds = fa
                strides = self.arrays.get(ak, set())
                esize = max(strides)[1] if strides else cb
                # a record element type only when accesses inside an element are smaller than an
                # element (a stride of whole rows - a multiple of the element size - is just indexing)
                if any(sz < cb or st % cb for st, sz in strides):
                    et = self.record_type_name('m_%X' % off)
                    self.pending_types.setdefault(o.index, []).append((et, ak, cb))
                else:
                    et = choose_type(self.elem_hints.get(ak, Counter()), cb)
                    if et in TYPE_SIZES and TYPE_SIZES[et] != cb:
                        # the descriptor's element size is a fact: the hints only choose among that size
                        et = choose_field_type(self.elem_hints.get(ak, Counter()), cb)
                dims = ', '.join('%d To %d' % b for b in bounds)
                out.append("Dim m_%X(%s) As %s   ' fixed-size array (bounds from the form's instance data)"
                           % (off, dims, et))
                continue
            if ak in self.arrays and self.array_kind.get(ak) == 'fixed':
                stride, esize = max(self.arrays[ak])
                et = choose_type(self.elem_hints.get(ak, Counter()), esize)
                if stride != esize:
                    et = self.record_type_name('m_%X' % off)
                    self.pending_types.setdefault(o.index, []).append((et, ak, stride))
                later = [k2[1] for k2 in bounds_keys if k2[1] > off]
                if later:
                    n = (min(later) - off) // stride
                    out.append("Dim m_%X(%d) As %s   ' fixed-size array inside the form (%d elements before "
                               "the next variable; lower bound not recorded)" % (off, n - 1, et, n))
                else:
                    out.append("Dim m_%X() As %s   ' fixed-size array inside the form (size not recorded)"
                               % (off, et))
                continue
            if ak in self.arrays:
                strides = self.arrays[ak]
                stride, esize = max(strides)
                eh = self.elem_hints.get(ak, Counter())
                if stride == esize:
                    et = choose_type(eh, esize)
                else:
                    et = self.record_type_name('m_%X' % off)
                    self.pending_types.setdefault(o.index, []).append((et, ak, stride))
                out.append("Dim m_%X() As %s   ' array (element size %d bytes)" % (off, et, stride))
                continue
            size = self.field_sizes.get(k) or 4     # 0: only its address is ever taken
            later = [k2[1] for k2 in bounds_keys if k2[1] > off]
            if later and size > min(later) - off:
                # a variable cannot run into the next one: a larger size came from an address passed on
                # (Input #/Write #, a ByRef argument), not from an access of that size
                size = min(later) - off
            t = choose_type(self.field_hints.get(k, Counter()), size)
            if later and TYPE_SIZES.get(t, 4) > min(later) - off:
                # it would overlap the next variable: the size decides, hints only choose among that size
                t = choose_field_type(self.field_hints.get(k, Counter()), min(later) - off)
            if off % 4 and TYPE_SIZES.get(t, 4) >= 4:
                # VB places 4- and 8-byte variables on a multiple of 4: one at another offset is smaller
                t = choose_field_type(self.field_hints.get(k, Counter()), 2 if off % 2 == 0 else 1)
            out.append('Dim m_%X As %s%s' % (off, t, self.omitted_writer_note(('v', 'field', off))))
        return out

    def type_decls(self, o):
        """Type declarations for the record arrays declared by field_decls(o) (call it first)."""
        out = []
        for name, ak, stride in self.pending_types.get(o.index, []):
            out += self.type_decl(name, ak, stride, 'Private' if o.kind in ('form', 'class') else 'Public')
            out.append('')
        return out

    def inside_record_field(self, o, off):
        """Is this form field part of a record-typed field declared elsewhere?"""
        for k, rec in self.dc.records.items():
            if k[0] == 'v' and k[1] == 'field':
                base = k[2]
                size = self.tlb.layout(rec)[1]
                if base < off < base + size:
                    return True
        return False

    def global_decls(self, o, globals_used):
        out = []
        for va in sorted(globals_used):
            if self.dc.global_objects.get(va):
                continue
            t = choose_type(self.global_hints.get(va, Counter()), self.global_sizes.get(va, 4))
            out.append('Public g_%X As %s%s' % (va & 0xffff, t, self.omitted_writer_note(('v', 'global', va))))
        return out

    def omitted_writer_note(self, k):
        labels = self.excluded_writes.get(k)
        if labels and k not in self.normal_writes:
            return "   ' only assigned by omitted %s code" % ' / '.join(sorted(labels))
        return ''


def narrow_read_locals(blocks, terms):
    """Keys of local variables written with 4 bytes but only ever read as their low 2 bytes."""
    reads, writes = defaultdict(set), defaultdict(set)

    def scan(e):
        for x in e.walk():
            if isinstance(x, Var) and x.kind == 'local':
                reads[ekey(x)].add(x.size)
            elif isinstance(x, AddrOf):
                for y in x.walk():
                    if isinstance(y, Var) and y.kind == 'local':
                        reads[ekey(y)].add(0)       # address taken: anything may read it
    for b, stmts in blocks.items():
        for s in stmts:
            if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'local':
                writes[ekey(s.dst)].add(s.dst.size)
                scan(s.src)
                continue
            for e in iter_exprs_in(s):
                scan(e)
            if isinstance(s, Assign):
                scan(s.dst)
        for e in term_exprs(terms[b]):
            scan(e)
    return {k for k, w in writes.items() if reads.get(k) == {2} and w <= {2, 4} and 4 in w}


def renumber_temps(blocks, terms):
    names = {}
    produced = set()

    def fn(x):
        if isinstance(x, Temp) and x.name not in produced:
            n = names.get(x.name)
            if n is None:
                n = 'tmp%d' % (len(names) + 1)
                names[x.name] = n
                produced.add(n)
            return Temp(n, x.ty)
        return None
    for b in sorted(blocks):
        for s in blocks[b]:
            stmt_rewrite(s, fn)
        terms[b] = term_rewrite(terms[b], fn)


def drop_unread_regvars(blocks, terms):
    """Register variables (the decompiler's, for values a CPU register carried between blocks) whose value
    never reaches anything that matters: not a condition, a call, a store to a real variable, only other
    such register variables (e.g. a leftover object pointer passed around a loop). Their assignments go
    when the value computed has no effect of its own."""
    def regs_in(e):
        return {ekey(x) for x in e.walk() if isinstance(x, Var) and x.kind == 'reg'}
    useful = set()
    feeds = {}          # register key -> register keys its assignments read
    for b, stmts in blocks.items():
        for s in stmts:
            if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind == 'reg':
                feeds.setdefault(ekey(s.dst), set()).update(regs_in(s.src))
                continue
            for e in iter_exprs_in(s):
                useful |= regs_in(e)
        for e in term_exprs(terms[b]):
            useful |= regs_in(e)
    work = list(useful)
    while work:
        k = work.pop()
        for k2 in feeds.get(k, ()):
            if k2 not in useful:
                useful.add(k2)
                work.append(k2)
    for b in blocks:
        blocks[b] = [s for s in blocks[b] if not (isinstance(s, Assign) and isinstance(s.dst, Var) and
                                                   s.dst.kind == 'reg' and ekey(s.dst) not in useful and
                                                   not any(isinstance(x, Call) and x.side for x in s.src.walk()))]


def bypass_empty_blocks(blocks, terms, entry, roots):
    """Redirect edges around blocks that contain nothing and just jump on."""
    fwd = {}
    for b, t in terms.items():
        if b != entry and b not in roots and not blocks.get(b) and t[0] == 'goto' and t[1] != b:
            fwd[b] = t[1]

    def final(x):
        seen = set()
        while x in fwd and x not in seen:
            seen.add(x)
            x = fwd[x]
        return x
    if not fwd:
        return
    for b, t in list(terms.items()):
        if t[0] == 'goto':
            terms[b] = ('goto', final(t[1]))
        elif t[0] == 'cond':
            a, c = final(t[2]), final(t[3])
            terms[b] = ('goto', a) if a == c else ('cond', t[1], a, c)
        elif t[0] == 'switch':
            terms[b] = ('switch', t[1], [final(x) if x is not None else None for x in t[2]])
    live = set()
    work = [entry] + [r for r in roots if r in blocks]
    while work:
        v = work.pop()
        if v in live or v not in blocks:
            continue
        live.add(v)
        work.extend(succs_of(terms[v]))
    for b in list(blocks):
        if b not in live:
            del blocks[b]
            del terms[b]


def succs_of(t):
    if t[0] == 'goto':
        return [t[1]]
    if t[0] == 'cond':
        return [t[2], t[3]]
    if t[0] == 'switch':
        return [x for x in t[2] if x is not None]
    return []


TYPE_SET = {'Integer', 'Long', 'Single', 'Double', 'Currency', 'Date', 'String', 'Object', 'Boolean', 'Variant',
            'Byte'}
VT_TYPES = {2: 'Integer', 3: 'Long', 4: 'Single', 5: 'Double', 6: 'Currency', 7: 'Date', 8: 'String',
            9: 'Object', 11: 'Boolean', 12: 'Variant', 17: 'Byte'}
TYPE_SIZES = {'Integer': 2, 'Boolean': 2, 'Long': 4, 'Single': 4, 'String': 4, 'Object': 4, 'Double': 8,
              'Date': 8, 'Currency': 8, 'Byte': 1, 'Variant': 16}


def expr_type(e):
    if isinstance(e, Str):
        return 'String'
    if isinstance(e, FConst):
        return e.ty or 'Double'
    if isinstance(e, Bin) and e.op in ('=', '<>', '<', '>', '<=', '>=', 'Is'):
        return 'Boolean'
    if isinstance(e, Raw) and e.text.startswith('New '):
        return e.text[4:]
    t = getattr(e, 'ty', None)
    if t and t.startswith('_') and t[1:2].isupper():
        t = t[1:]                       # an interface name as VB code writes the class (_Foo -> Foo)
    if t in TYPE_SET or (t and t[0].isupper() and t not in ('Ptr', 'Hex', 'Cond')):
        return t
    return None


def choose_field_type(hints, size):
    """Type of a record field: the machine access size decides; hints only choose among the types of
    that size (a value merely assigned from a Double expression does not make a 4-byte field Double)."""
    h = Counter({k: v for k, v in hints.items() if not k.startswith('size')})
    if size == 1:
        return 'Byte'
    if size == 2:
        if (h.get('Boolean', 0) or h.get('BoolConst', 0)) and not h.get('IntConst') and \
                h.get('Integer', 0) <= h.get('Boolean', 0) + h.get('BoolConst', 0):
            return 'Boolean'
        return 'Integer'
    if size == 4:
        specific = [(n, t) for t, n in h.items() if t not in TYPE_SET and
                    t not in ('Array', 'Ptr', 'Hex', 'Cond', 'BoolConst', 'IntConst')]
        if specific:
            return max(specific)[1]
        if h.get('Object'):
            return 'Object'
        if h.get('String'):
            return 'String'
        if h.get('Single', 0) > h.get('Long', 0):
            return 'Single'
        return 'Long'
    if size == 8:
        if h.get('Date'):
            return 'Date'
        if h.get('Currency'):
            return 'Currency'
        return 'Double'
    if size >= 16:
        return 'Variant'
    return 'Long'


def choose_type(hints, size):
    sizes = [int(k[4:]) for k in hints if k.startswith('size')]
    if sizes:
        size = max([size] + sizes)
    h = Counter({k: v for k, v in hints.items() if not k.startswith('size')})
    recs = [(n, t) for t, n in h.items() if n >= 100]
    if recs:
        return max(recs)[1]
    if size >= 16 or h.get('Variant', 0) > 0 and not h.get('String'):
        if 'Variant' in h or size >= 16:
            return 'Variant'
    # specific record / class names
    specific = [(n, t) for t, n in h.items() if t not in TYPE_SET and
                t not in ('Array', 'Ptr', 'Hex', 'Cond', 'BoolConst', 'IntConst')]
    if specific:
        specific.sort(reverse=True)
        return specific[0][1]
    if h.get('Date') and size in (4, 8):
        return 'Date'
    for t in ('String', 'Object', 'Double', 'Single', 'Date', 'Currency'):
        if h.get(t):
            if t == 'Single' and size == 8:
                return 'Double'
            return t
    if size == 8:
        return 'Double'
    if size == 2:
        if (h.get('Boolean', 0) or h.get('BoolConst', 0)) and not h.get('IntConst') and \
                h.get('Integer', 0) <= h.get('Boolean', 0) + h.get('BoolConst', 0):
            return 'Boolean'
        return 'Integer'
    if size == 1:
        return 'Byte'
    if h.get('Boolean') and not h.get('Long') and not h.get('Integer'):
        return 'Boolean'
    return 'Long'
