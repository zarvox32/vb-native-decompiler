"""Project-level decompiler: context (names, types, signatures), per-procedure pipeline."""
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ir import (Expr, Const, FConst, Str, Var, Member, Deref, AddrOf, Bin, Un, Call, Temp, Me, Missing, Unknown,
                Raw, Assign, ExprStmt, Text, Comment, FmtStmt, ekey, same, render, render_stmt, NAMER)
from vbmeta import Project
from tlb import TypeLibDB
from cfg import CFG, Decoder
from lift import SAddr, VTbl, ImportFn, CodePtr, DataPtr, StrCmpResult, Hresult, FpuStatus, SetCC, BoolVal, s32
from x86sem import ProcLifter
from runtime import Runtime
import simplify
import structure

VBTYPES = {'Integer', 'Long', 'Single', 'Double', 'Currency', 'Date', 'String', 'Object', 'Boolean', 'Variant',
           'Byte'}
FORM_VTSIZE = 0x2F8          # _Form dual interface size (32-bit) - form-specific members start here
EVENT_PARAM_TYPES = {'Integer*': 'Integer', 'Long*': 'Long', 'Single*': 'Single', 'Double*': 'Double',
                     'String*': 'String', 'Boolean*': 'Boolean', 'Variant*': 'Variant'}


class FormIface:
    """Pseudo interface for an object's own vtable (form / class)."""

    def __init__(self, obj, base_iface):
        self.obj = obj
        self.base = base_iface
        self.name = obj.name
        self.guid = None


class Decompiler:
    def __init__(self, path=None, image=None, tlb=None, debug=False):
        self.proj = Project(path, image)
        self.im = self.proj.im
        self.tlb = tlb or TypeLibDB()
        self.debug = debug
        self.rt = Runtime(self)
        self.dec = Decoder(self.im)
        self.option_compare_text = False
        self.signatures = {}          # proc va -> dict
        self.me_calls = defaultdict(list)   # obj index -> [(off, pending bytes)]
        self.method_base = {}         # obj index -> vtable offset of method index 0
        self.global_objects = {}      # global var va -> class name
        self.global_object_iface = {}
        self.obj_types = defaultdict(Counter)    # var key -> Counter(type name)
        self.records = {}             # var key -> record name
        self.arrays = {}              # var key -> (vt, cb, ndims)
        self.unknown_iids = Counter()
        self.api_args = defaultdict(list)
        self.warnings = []
        self.pass_no = 1
        self.form_iid = {}            # guid -> VBObject
        self.me_slot_written = set()  # procedures that reuse Me's stack slot as scratch
        self.arg_slot_written = set() # (procedure, stack offset) of parameter slots that are stored to
        self._find_form_iids()
        # fixed-size arrays of forms/classes: {object index: {data pointer offset: (element size, bounds)}}
        import instdata
        self.local_fixed_arrays = {}     # (proc va, local key of the data pointer) -> (cb, bounds, vt)
        self.fixed_arrays = {o.index: instdata.fixed_arrays(self.im, o) for o in self.proj.objects
                             if o.kind in ('form', 'class')}
        self.vb_form = self.tlb.by_name.get('_Form')
        self.proc_names = {}
        self._name_procs()

    # ================================================================== naming
    def _name_procs(self):
        pr = self.proj
        used = set()
        for va in sorted(pr.procs):
            p = pr.procs[va]
            name = None
            if p.kind == 'event' and p.event:
                ctl, ev = p.event
                ename = self.event_name(ctl, ev)
                name = '%s_%s' % (ctl.name, ename)
            elif p.name:
                name = p.name
            else:
                name = 'Proc_%X' % va
            base = name
            k = 2
            while name in used:
                name = '%s_%d' % (base, k)
                k += 1
            used.add(name)
            self.proc_names[va] = name

    def event_name(self, ctl, ev):
        it = self.tlb.iface(ctl.guid)
        if it is not None:
            m = it.event_by_index(ev)
            if m is not None:
                return m.name
        return 'Event%d' % ev

    def event_member(self, ctl, ev):
        it = self.tlb.iface(ctl.guid)
        if it is not None:
            return it.event_by_index(ev)
        return None

    def proc_name(self, p):
        return self.proc_names.get(p.va, 'Proc_%X' % p.va)

    def declare_name(self, d):
        return d.name

    def handler_label(self, L, n):
        a = L.handlers.get(n) if hasattr(L, 'handlers') else None
        if a is None:
            return 'ErrHandler%d' % n
        return 'L_%X' % a

    # ================================================================== objects / interfaces
    def _find_form_iids(self):
        im = self.im
        for o in self.proj.objects:
            if o.opt is None:
                continue
            g = im.u32(o.opt + 0x04)
            # the object's interface IID is stored 16 bytes before its GUID list
            for cand in (g - 0x10, g):
                if im.valid(cand, 16):
                    self.form_iid[im.guid(cand)] = o

    def iface_for_guid(self, guid):
        if guid in self.form_iid:
            o = self.form_iid[guid]
            return FormIface(o, self.vb_form)
        return self.tlb.iface(guid)

    def note_unknown_iid(self, guid):
        self.unknown_iids[guid] += 1

    def object_iface(self, obj, L):
        if isinstance(obj, Me):
            o = L.proc.obj
            if o is not None and o.kind == 'form':
                return FormIface(o, self.vb_form)
            return None
        k = ekey(obj)
        if isinstance(obj, Var) and obj.kind == 'global' and obj.off in self.global_object_iface:
            return self.global_object_iface[obj.off]
        if k in self.records:
            return None
        c = self.obj_types.get(self.obj_key(obj, L))
        if c:
            nm = c.most_common(1)[0][0]
            it = self.tlb.by_name.get(nm) or self.tlb.by_name.get('_' + nm)
            if it is not None and it.kind in ('interface', 'dispatch'):
                return it
        return None

    def member_at(self, iface, off, obj, L):
        if isinstance(iface, FormIface):
            if off < FORM_VTSIZE:
                return iface.base.member(off) if iface.base else None
            return self.self_member(iface.obj, off, obj, L)
        m = iface.member(off)
        if m is not None and isinstance(obj, (Var, Temp)):
            self.note_obj_type(obj, iface.name, L)
        return m

    def self_member(self, o, off, obj, L):
        if self.pass_no == 1:
            self.me_calls[o.index].append((off, L.pending_cells()))
            return None
        base = self.method_base.get(o.index)
        ci = (off - FORM_VTSIZE) // 4
        ctls = [c for c in o.controls if c.name != 'Form' and not self.is_impl_iface(c)]
        if base is None or off < base:
            if 0 <= ci < len(ctls) and L.pending_cells() == 4:
                return {'kind': 'control', 'name': ctls[ci].name}
            if base is None:
                return None
        k = (off - base) // 4
        if 0 <= k < len(o.methods) and o.methods[k] is not None:
            p = self.proj.procs.get(o.methods[k])
            if p is not None:
                return {'kind': 'method', 'proc': p}
        # control accessors at the start of the object's area
        ci = (off - FORM_VTSIZE) // 4
        ctls = [c for c in o.controls if c.name != 'Form' and not self.is_impl_iface(c)]
        if 0 <= ci < len(ctls):
            return {'kind': 'control', 'name': ctls[ci].name}
        return None

    def is_impl_iface(self, c):
        it = self.tlb.iface(c.guid)
        return it is not None and it.kind == 'interface' and not it.name.endswith('Events')

    def solve_method_bases(self):
        for o in self.proj.objects:
            obs = self.me_calls.get(o.index)
            if not obs or not o.methods:
                continue
            rets = []
            for va in o.methods:
                p = self.proj.procs.get(va) if va else None
                rets.append(p.ret_n if p else None)
            best = None
            hi = max(off for off, _ in obs)
            for B in range(FORM_VTSIZE, hi + 4, 4):
                score = 0
                for off, pend in obs:
                    k = (off - B) // 4
                    if 0 <= k < len(rets) and rets[k] is not None:
                        score += 2 if rets[k] == pend else -1
                if best is None or score > best[0]:
                    best = (score, B)
            if best:
                self.method_base[o.index] = best[1]

    def note_obj_type(self, obj, name, L):
        name = self.tlb.class_name_for(name)
        self.obj_types[self.obj_key(obj, L)][name] += 1

    @staticmethod
    def obj_key(obj, L):
        """Key of an object variable for obj_types: stack variables, registers and temporaries belong to
        their procedure (var_14 of one procedure is not var_14 of another)."""
        k = ekey(obj)
        if isinstance(obj, Var) and obj.kind in ('field', 'global'):
            return k
        return ('proc', L.proc.va) + tuple(k)

    def class_name(self, desc):
        if isinstance(desc, DataPtr) and self.im.valid(desc.va, 12):
            clsid_p = self.im.u32(desc.va + 4)
            iid_p = self.im.u32(desc.va + 8)
            if self.im.valid(clsid_p, 16):
                g = self.im.guid(clsid_p)
                cc = self.tlb.coclass.get(g)
                if cc is not None:
                    return cc.name
            if self.im.valid(iid_p, 16):
                g = self.im.guid(iid_p)
                it = self.tlb.iface(g)
                if it is not None:
                    return it.name.lstrip('_')
                if g in self.form_iid:
                    return self.form_iid[g].name
        return 'Object'

    def note_global_object(self, pvar, desc):
        if isinstance(pvar, AddrOf) and isinstance(pvar.e, Var):
            va = pvar.e.off
            name = self.class_name(desc)
            self.global_objects[va] = name
            if isinstance(desc, DataPtr) and self.im.valid(desc.va, 12):
                iid_p = self.im.u32(desc.va + 8)
                if self.im.valid(iid_p, 16):
                    it = self.iface_for_guid(self.im.guid(iid_p))
                    if it is not None:
                        self.global_object_iface[va] = it

    def dispid_name(self, obj, dispid):
        d = dispid.v if isinstance(dispid, Const) else None
        cname = getattr(obj, 'control', None) or (obj.text if isinstance(obj, Raw) else None)
        if d is not None and cname:
            for o in self.proj.objects:
                for c in o.controls:
                    if c.name == cname:
                        cls = self.control_typelib_class(c)
                        if cls is not None:
                            for m in cls.members:
                                if m.memid == d:
                                    return m.name
        return 'Dispid%s' % (d if d is not None else dispid)

    def control_typelib_class(self, c):
        """Dispatch interface of a control from the referenced OCX type libraries (by ProgID)."""
        progs = self.ext_progids()
        for prog in progs:
            lib, cls = prog.split('.', 1)
            if cls.lower().startswith(c.name.lower()[:3]) or c.name.lower() in cls.lower():
                cc = self.tlb.by_name.get(cls)
                if cc is not None and cc.kind == 'coclass':
                    for imp in cc.impl:
                        if (imp['flags'] & 1) and not (imp['flags'] & 2):
                            return self.tlb.iface(imp['guid'])
        return None

    def ext_progids(self):
        if not hasattr(self, '_progids'):
            import re as _re
            self._progids = []
            t = self.proj.ext_comp_table
            if t and self.im.valid(t, 0x400):
                blob = self.im.read(t, 0x2000 if self.im.valid(t, 0x2000) else 0x400)
                for m in _re.finditer(rb'([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\x00', blob):
                    s = m.group(1).decode()
                    if s not in self._progids and not s.lower().endswith(('.ocx', '.dll', '.oca')):
                        self._progids.append(s)
        return self._progids

    def vb_type(self, t):
        if t is None:
            return None
        t = t.rstrip('*')
        if t in VBTYPES:
            return t
        if t in ('IUnknown', 'IDispatch'):
            return 'Object'
        it = self.tlb.by_name.get(t)
        if it is not None:
            if it.kind == 'enum':
                return t
            if it.kind == 'alias' and it.alias:
                return self.vb_type(it.alias)
            if it.kind in ('interface', 'dispatch', 'coclass'):
                return t.lstrip('_')
            if it.kind == 'record':
                return t
        return t

    def enum_const(self, enum, value):
        return self.tlb.enum_name(enum, value)

    def record_type_at(self, L, v, size):
        """Record type of the `size`-byte location v: a record variable, or a record-typed member at that
        offset inside one (None if not known)."""
        if not isinstance(v, Var):
            return None
        recs = dict(self.records)
        recs.update(getattr(L, 'records', {}))
        t = recs.get(ekey(v))
        if t and self.tlb.layout(t)[1] == size:
            return t
        for k, rec in recs.items():
            if k[0] != 'v' or k[1] != v.kind or rec not in self.tlb.records:
                continue
            rel = (k[2] - v.off) if v.kind == 'local' else (v.off - k[2])
            fields, rsize, _ = self.tlb.layout(rec)
            if 0 < rel < rsize:
                for name, foff, ft in fields:
                    if foff == rel and ft in self.tlb.records and self.tlb.layout(ft)[1] == size:
                        return ft
        return None

    def note_record(self, L, var, rec):
        if isinstance(var, Var) and var.kind == 'local':
            if not hasattr(L, 'records'):
                L.records = {}
            L.records[ekey(var)] = rec
        else:
            self.records[ekey(var)] = rec

    def note_array(self, L, arr, vt, cb, ndims):
        if isinstance(arr, Var):
            k = ekey(arr) if arr.kind in ('field', 'global') else ('proc', L.proc.va) + tuple(ekey(arr))
            self.arrays[k] = (vt.v if isinstance(vt, Const) else None,
                            cb.v if isinstance(cb, Const) else None, ndims)

    def array_ref(self, L, psa):
        """UBound(x): psa is the SAFEARRAY* value (usually loaded from the array variable)."""
        return psa

    def msgbox_flags(self, v):
        parts = []
        btn = v & 0xf
        icon = v & 0xf0
        dflt = v & 0xf00
        names = {0: None, 1: 'vbOKCancel', 2: 'vbAbortRetryIgnore', 3: 'vbYesNoCancel', 4: 'vbYesNo',
                 5: 'vbRetryCancel'}
        if names.get(btn):
            parts.append(names[btn])
        ic = {16: 'vbCritical', 32: 'vbQuestion', 48: 'vbExclamation', 64: 'vbInformation'}.get(icon)
        if ic:
            parts.append(ic)
        d = {0x100: 'vbDefaultButton2', 0x200: 'vbDefaultButton3'}.get(dflt)
        if d:
            parts.append(d)
        rest = v & ~0xfff
        if rest:
            parts.append(str(rest))
        return ' + '.join(parts) if parts else '0'

    # ================================================================== compile options
    def compile_options(self):
        """VB 'Advanced Optimizations' visible in the code: overflow checks are `jo` jumps after integer
        arithmetic, array bounds checks call __vbaGenerateBoundsError, floating-point error checks call
        __vbaFPException. Without them the compiled program wraps around / reads out of range silently."""
        if hasattr(self, '_copts'):
            return self._copts
        from capstone import Cs, CS_ARCH_X86, CS_MODE_32
        md = Cs(CS_ARCH_X86, CS_MODE_32)
        slots = {name: va for va, name in self.im.imports.items()}
        want = {'bounds': slots.get('__vbaGenerateBoundsError'), 'fp': slots.get('__vbaFPException')}
        counts = {'jo': 0, 'bounds': 0, 'fp': 0}
        starts = sorted(self.proj.procs)
        for a, b in zip(starts, starts[1:] + [self.proj.code_end]):
            for ins in md.disasm_lite(self.im.read(a, b - a), a):
                if ins[2] == 'jo':
                    counts['jo'] += 1
                elif ins[2] == 'call':
                    for k, slot in want.items():
                        if slot and ('[0x%x]' % slot) in ins[3]:
                            counts[k] += 1
        self._copts = {'overflow_checks': counts['jo'] > 0, 'bounds_checks': counts['bounds'] > 0,
                       'fp_checks': counts['fp'] > 0, 'counts': counts}
        return self._copts

    # ================================================================== signatures
    API_ARG_BYTES = {'GetTickCount': 0, 'sndPlaySoundA': 8, 'GetSystemDirectoryA': 8,
                     'InternetGetConnectedState': 8, 'ShellExecuteA': 24, 'PathMatchSpecW': 8, 'lstrlenW': 4,
                     'FindNextFileA': 8, 'FindFirstFileA': 8, 'FindClose': 4, 'timeGetTime': 0, 'Sleep': 4,
                     'GetAsyncKeyState': 4, 'GetKeyState': 4, 'mciSendStringA': 16, 'PlaySoundA': 12}

    def is_data_va(self, va):
        """Is va inside VB's data section (module/global variables)?"""
        if not hasattr(self, '_data_sec'):
            self._data_sec = None
            anchor = self.proj.native_code
            for lo, hi, name, ch in self.im.sections:
                if anchor and lo <= anchor < hi and not (lo <= self.proj.code_start < hi):
                    self._data_sec = (lo, hi)
            if self._data_sec is None:
                for lo, hi, name, ch in self.im.sections:
                    if (ch & 0x80000000) and not (ch & 0x20000000) and name.lower() in ('.data', ''):
                        self._data_sec = (lo, hi)
                        break
        if self._data_sec is None:
            return False
        lo, hi = self._data_sec
        return lo <= va < hi

    def api_signature(self, d):
        return self.API_ARG_BYTES.get(d.func)

    def proc_end(self, p):
        if not hasattr(self, '_proc_ends'):
            starts = sorted(self.proj.procs)
            self._proc_ends = {a: b for a, b in zip(starts, starts[1:] + [self.proj.code_end])}
        return self._proc_ends.get(p.va, self.proj.code_end)

    def note_api_args(self, d, args):
        self.api_args[d.func].append(len(args))

    def proc_signature(self, p):
        return self.signatures.get(p.va)

    # ================================================================== frame analysis
    def frame_info(self, p):
        fi = self._frame_info(p)
        return fi

    def _frame_info(self, p):
        """Parse the prologue: frame-internal size R, locals bottom, stack pointer after saves."""
        dec = self.dec
        sp = 0
        R = None
        local_low = None
        base_sp = None
        rtmi = None
        seh = False
        esp_slot = None      # [ebp - esp_slot] keeps the stack pointer for the error handlers
        cur = p.va
        saw_ebp = False
        for _ in range(40):
            ins = dec.at(cur)
            if ins is None:
                break
            t = ins.text
            if t == 'push ebp':
                sp -= 4
            elif t == 'mov ebp, esp':
                saw_ebp = True
                ebp = sp
            elif ins.mn == 'sub' and ins.ops[0].kind == 'reg' and ins.ops[0].reg == 'esp' and ins.ops[1].kind == 'imm':
                if saw_ebp and R is None and not seh:
                    R = ins.ops[1].imm
                    sp -= R
                else:
                    sp -= ins.ops[1].imm
                    local_low = sp
            elif ins.mn == 'push' and ins.ops[0].kind == 'imm' and R is not None and not seh:
                seh = True
                sp -= 4
            elif t.startswith('mov eax, dword ptr fs:'):
                pass
            elif t == 'push eax' and seh and local_low is None:
                sp -= 4
            elif t.startswith('mov dword ptr fs:'):
                pass
            elif ins.mn == 'mov' and ins.ops[0].kind == 'reg' and ins.ops[0].reg == 'eax' and ins.ops[1].kind == 'imm':
                chk = dec.at(ins.next)
                if chk and chk.mn == 'call':
                    n = None
                    o = chk.ops[0]
                    if o.kind == 'mem' and o.base is None:
                        n = self.im.imports.get(o.disp & 0xffffffff)
                    elif o.kind == 'imm' and self.im.valid(o.imm, 6) and self.im.read(o.imm, 2) == b'\xff\x25':
                        n = self.im.imports.get(self.im.u32(o.imm + 2))     # call thunk -> jmp [IAT]
                    if n == '__vbaChkstk':
                        sp -= ins.ops[1].imm
                        local_low = sp
                        cur = chk.next
                        continue
                break
            elif ins.mn == 'push' and ins.ops[0].kind == 'reg' and ins.ops[0].reg in ('ebx', 'esi', 'edi'):
                if local_low is None:
                    local_low = sp
                sp -= 4
            elif ins.mn == 'mov' and ins.ops[0].kind == 'mem' and ins.ops[0].base == 'ebp' and \
                    ins.ops[1].kind == 'imm' and seh and rtmi is None:
                v = ins.ops[1].imm
                if self.im.valid(v, 0x14) and not (self.proj.code_start <= v < self.proj.code_end):
                    rtmi = v
            elif ins.mn == 'mov' and ins.ops[0].kind == 'mem' and ins.ops[0].base == 'ebp' and \
                    ins.ops[1].kind == 'reg' and ins.ops[1].reg == 'esp':
                esp_slot = -((ins.ops[0].disp + 0x80000000) % 0x100000000 - 0x80000000)
            else:
                break
            cur = ins.next
        if not saw_ebp:
            # frameless: optional `sub esp, N` followed by register saves
            sp = 0
            low = 0
            cur = p.va
            for _ in range(8):
                ins = dec.at(cur)
                if ins is None:
                    break
                if ins.mn == 'sub' and ins.ops[0].kind == 'reg' and ins.ops[0].reg == 'esp' and \
                        ins.ops[1].kind == 'imm' and sp == 0:
                    sp -= ins.ops[1].imm
                    low = sp
                elif ins.mn == 'push' and ins.ops[0].kind == 'reg' and ins.ops[0].reg in ('eax', 'ecx', 'edx')                         and sp == low:
                    sp -= 4
                    low = sp
                elif ins.mn == 'push' and ins.ops[0].kind == 'reg' and ins.ops[0].reg in ('ebx', 'esi', 'edi', 'ebp'):
                    sp -= 4
                else:
                    break
                cur = ins.next
            return {'R': None, 'local_low': low, 'base_sp': sp, 'rtmi': None, 'seh': False, 'frameless': True}
        if local_low is None:
            local_low = sp
        base_sp = sp
        if not seh:
            R_int = None
        else:
            R_int = R
        return {'R': R_int, 'local_low': local_low, 'base_sp': base_sp, 'rtmi': rtmi, 'seh': seh,
                'esp_slot': esp_slot}

    # ================================================================== per procedure
    def lift(self, p):
        fi = self.frame_info(p)
        handlers = {}
        if fi['rtmi']:
            r = self.proj.rtmi(fi['rtmi'])
            if r:
                end = self.proc_end(p)
                handlers = {k: a for k, a in r['handlers'].items() if p.va < a < end}
        cfg = CFG(self.proj, p.va, list(handlers.values()), dec=self.dec)
        L = ProcLifter(self.proj, cfg, p, self.rt, self.tlb, self)
        L.frame_R = fi['R']
        L.local_low = fi['base_sp']
        L.frameless = fi.get('frameless', False)
        L.locals_top = fi['local_low']
        L.handlers = handlers
        L.fi = fi
        L.run()
        return L

    def analyze_signature(self, p, L):
        """Derive parameters / return kind from a lifted procedure."""
        sig = {'nargs': (p.ret_n or 0) // 4, 'params': {}, 'retptr': False, 'ret': None, 'rettype': None}
        is_method = p.obj is not None and p.obj.kind in ('form', 'class')
        first = 1 if is_method else 0
        nparams = sig['nargs'] - first
        # parameter usage: ByRef (dereferenced) vs ByVal
        byref = {}
        sizes = {}

        class _Term:
            def __init__(self, t):
                self.t = t

            def exprs(self):
                return (self.t[1],) if self.t[0] in ('cond', 'switch') else ()
        for b, stmts in L.block_stmts.items():
            for s in list(stmts) + [_Term(L.block_term[b])] if b in L.block_term else stmts:
                for e in s.exprs():
                    if e is None:
                        continue
                    for x in e.walk():
                        if isinstance(x, Deref) and isinstance(x.base, Var) and x.base.kind == 'arg' and x.off == 0:
                            byref[x.base.off] = True
                            sizes[x.base.off] = max(sizes.get(x.base.off, 0), x.size)
                        elif type(x).__name__ == 'VTbl' and isinstance(x.obj, Var) and x.obj.kind == 'arg':
                            byref[x.obj.off] = True
                            sizes[x.obj.off] = max(sizes.get(x.obj.off, 0), 4)
                        elif isinstance(x, Var) and x.kind == 'arg':
                            sizes.setdefault(x.off, x.size)
        # method functions: last param written through at exit
        last_off = 4 * (sig['nargs'] - 1)
        read_last = False
        for b, stmts in L.block_stmts.items():
            for s in stmts:
                exprs = [s.src] if isinstance(s, Assign) else list(s.exprs())
                if isinstance(s, Assign) and isinstance(s.dst, Deref):
                    exprs.append(s.dst.base if not (isinstance(s.dst.base, Var) and s.dst.base.kind == 'arg') else None)
                for e in exprs:
                    if e is None:
                        continue
                    for x in e.walk():
                        if (isinstance(x, Deref) or type(x).__name__ == 'VTbl') and \
                                isinstance(getattr(x, 'base', getattr(x, 'obj', None)), Var) and \
                                getattr(x, 'base', getattr(x, 'obj', None)).kind == 'arg' and \
                                getattr(x, 'base', getattr(x, 'obj', None)).off == last_off:
                            read_last = True
        if is_method and nparams >= 1 and not read_last:
            for b, stmts in L.block_stmts.items():
                for s in stmts:
                    if isinstance(s, Assign) and isinstance(s.dst, Deref) and isinstance(s.dst.base, Var) and \
                            s.dst.base.kind == 'arg' and s.dst.base.off == last_off and \
                            self._exit_path(L, b):
                        sig['retptr'] = True
                        sig['rettype'] = self._size_type(s.dst.size, s.src)
            for b, stmts in L.block_stmts.items():
                for s in stmts:
                    if sig.get('retptr') and isinstance(s, Assign) and isinstance(s.dst, Deref) and \
                            isinstance(s.dst.base, Var) and s.dst.base.kind == 'arg' and \
                            s.dst.base.off == last_off and s.dst.off == 12:
                        sig['rettype'] = 'Variant'
        if not is_method and L.ret_values:
            eaxs = [r[0] for r in L.ret_values]
            fpus = [r[1] for r in L.ret_values]
            if any(f is not None and not isinstance(f, Unknown) for f in fpus):
                sig['ret'] = 'fpu'
                sig['rettype'] = 'Double'
            elif all(isinstance(e, Var) and e.kind == 'local' for e in eaxs):
                sig['ret'] = 'eax'
                sig['retvar'] = ekey(eaxs[0])
                sig['rettype'] = self._size_type(eaxs[0].size, eaxs[0])
            elif eaxs and all(isinstance(e, (BoolVal, SetCC)) or (isinstance(e, Bin) and e.ty == 'Boolean')
                              for e in eaxs):
                sig['ret'] = 'eax'
                sig['retexpr'] = True
                sig['rettype'] = 'Boolean'
        sig['byref'] = byref
        sig['sizes'] = sizes
        return sig

    def _exit_path(self, L, b):
        t = L.block_term.get(b)
        return t is not None and (t[0] == 'ret' or (t[0] == 'goto' and L.block_term.get(t[1], ('',))[0] == 'ret'))

    VB_SIZES = {'Byte': 1, 'Integer': 2, 'Boolean': 2, 'Long': 4, 'Single': 4, 'String': 4, 'Object': 4,
                'Double': 8, 'Date': 8, 'Currency': 8, 'Variant': 16}

    def _size_type(self, size, e):
        ty = getattr(e, 'conv', None) or getattr(e, 'ty', None)
        if ty in VBTYPES and self.VB_SIZES[ty] == size:
            return ty           # (the size stored is a fact: a 2-byte result is no Variant)
        return {2: 'Integer', 4: 'Long', 8: 'Double', 16: 'Variant', 1: 'Byte'}.get(size, 'Long')

    def run_all(self):
        """Pass 1 (signatures, vtable layout) and pass 2 (final)."""
        pr = self.proj
        self.pass_no = 1
        lifted = {}
        for va in sorted(pr.procs):
            p = pr.procs[va]
            try:
                L = self.lift(p)
                lifted[va] = L
                self.signatures[va] = self.analyze_signature(p, L)
            except Exception as e:
                self.warnings.append('pass1 %s: %s' % (self.proc_name(p), e))
                if self.debug:
                    import traceback
                    traceback.print_exc()
        self.solve_method_bases()
        self.pass_no = 2
        self.obj_types_pass1 = self.obj_types
        results = {}
        for va in sorted(pr.procs):
            p = pr.procs[va]
            try:
                L = self.lift(p)
                results[va] = L
            except Exception as e:
                self.warnings.append('pass2 %s: %s' % (self.proc_name(p), e))
                if self.debug:
                    import traceback
                    traceback.print_exc()
        self.byref_from_callers(results)     # (calls to form methods are known from pass 2 on)
        self.propagate_byref(results)
        return results

    def byref_from_callers(self, lifted):
        """A parameter its procedure never touches gets its passing mode from the callers: when every call
        passes an address (of a temporary or a variable), it is ByRef - declared ByVal, the source would
        pass a value where the machine passes a pointer. (Its type: the size most of the procedure's
        other ByRef parameters have; nothing reads it.)"""
        seen = {}
        for va, L in lifted.items():
            for stmts in L.block_stmts.values():
                for s in stmts:
                    for e in s.exprs():
                        if e is None:
                            continue
                        for x in e.walk():
                            t = getattr(x, 'proc', None) if isinstance(x, Call) and x.kind == 'proc' else None
                            if t is None:
                                continue
                            first = 1 if t.obj is not None and t.obj.kind in ('form', 'class') else 0
                            for i, a in enumerate(x.args):
                                seen.setdefault((t.va, 4 * (i + first)), []).append(
                                    bool(getattr(a, 'byref_arg', False)))
        for (tva, off), flags in sorted(seen.items()):
            sig = self.signatures.get(tva)
            if not sig or off in sig.get('sizes', {}) or sig.get('byref', {}).get(off) or not all(flags):
                continue
            if off >= 4 * sig.get('nargs', 0) or (sig.get('retptr') and off == 4 * (sig['nargs'] - 1)):
                continue
            others = [sz for o, sz in sig.get('sizes', {}).items() if sig.get('byref', {}).get(o)]
            sig.setdefault('byref', {})[off] = True
            sig.setdefault('sizes', {})[off] = 2 if others and 2 * others.count(2) >= len(others) else 4

    def propagate_byref(self, lifted):
        edges = []
        for va, L in lifted.items():
            for stmts in L.block_stmts.values():
                for s in stmts:
                    for e in s.exprs():
                        if e is None:
                            continue
                        for x in e.walk():
                            t = getattr(x, 'proc', None) if isinstance(x, Call) and x.kind == 'proc' else None
                            if t is None:
                                continue
                            first = 1 if t.obj is not None and t.obj.kind in ('form', 'class') else 0
                            for i, a in enumerate(x.args):
                                if isinstance(a, Var) and a.kind == 'arg':
                                    edges.append((va, a.off, t.va, 4 * (i + first)))
        changed = True
        while changed:
            changed = False
            for cva, coff, tva, toff in edges:
                ts = self.signatures.get(tva) or {}
                cs = self.signatures.get(cva)
                if cs is None or not ts.get('byref', {}).get(toff):
                    continue
                if not cs.setdefault('byref', {}).get(coff):
                    cs['byref'][coff] = True
                    sz = ts.get('sizes', {}).get(toff)
                    if sz:
                        cs.setdefault('sizes', {})[coff] = sz
                    changed = True
