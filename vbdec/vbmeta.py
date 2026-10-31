"""VB5/6 project metadata: header, objects (forms/modules/classes), controls, events,
API declares, procedure discovery for native-code executables."""
import re
import struct
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'lib'))
sys.path.insert(0, HERE)
from capstone import Cs, CS_ARCH_X86, CS_MODE_32  # noqa: E402

from pe import Image  # noqa: E402

_md = Cs(CS_ARCH_X86, CS_MODE_32)
_mdd = Cs(CS_ARCH_X86, CS_MODE_32)
_mdd.detail = True


class Control:
    def __init__(self):
        self.name = ''
        self.guid = None          # event interface IID
        self.index = 0
        self.events = {}          # event index -> handler proc VA
        self.flags = 0
        self.storage = 0          # dword at +4 (instance storage offset?)


class Proc:
    def __init__(self, va, obj=None, name=None):
        self.va = va
        self.obj = obj            # VBObject
        self.name = name
        self.kind = None          # 'event' | 'method' | 'proc'
        self.event = None         # (control, event typelib func)
        self.method_index = None  # index in the object's method table
        self.ret_n = None         # bytes popped by `ret N`
        self.public = False

    def __repr__(self):
        return '<Proc %s %x>' % (self.name, self.va)


class VBObject:
    def __init__(self):
        self.index = 0
        self.name = ''
        self.flags = 0
        self.kind = 'module'
        self.objinfo = 0
        self.opt = None
        self.controls = []
        self.methods = []         # proc VAs in method-table order (forms/classes)
        self.method_names = {}    # index -> public name (when stored)
        self.procs = []           # Proc objects belonging to this object (filled later)
        self.module_public = 0
        self.module_static = 0
        self.public_bytes = 0
        self.static_bytes = 0
        self.gui = None           # form designer data (bytes) for forms


class Declare:
    def __init__(self, lib, func, stub=None, cache=None):
        self.lib = lib
        self.func = func
        self.stub = stub          # VA of the call stub in code
        self.cache = cache        # VA of the resolved-address cache dword
        self.name = func


class Project:
    def __init__(self, path=None, image=None):
        self.im = image or Image(path)
        im = self.im
        self.hdr = im.find(b'VB5!')
        if self.hdr < 0:
            raise ValueError('not a VB5/6 executable')
        h = self.hdr
        self.proj = im.u32(h + 0x30)
        self.sub_main = im.u32(h + 0x2C)
        self.name = im.cstr(h + im.u32(h + 0x64)) or 'Project1'
        self.exe_name = im.cstr(h + im.u32(h + 0x5C)) or ''
        self.description = im.cstr(h + im.u32(h + 0x58)) or ''
        self.help_file = im.cstr(h + im.u32(h + 0x60)) or ''
        self.form_count = im.u16(h + 0x44)
        self.gui_table = im.u32(h + 0x4C)
        self.ext_comp_table = im.u32(h + 0x50)
        p = self.proj
        self.code_start = im.u32(p + 0x0C)
        self.code_end = im.u32(p + 0x10)
        self.native_code = im.u32(p + 0x20)     # also the FDIV-bug flag dword
        self.is_native = self.native_code != 0
        self.objects = []
        self.declares = []
        self.ext_objects = []                   # (class desc, global var)
        self.procs = {}                         # va -> Proc
        self._load_objects()
        self._load_externals()
        self._load_gui()
        self._discover_procs()

    # ------------------------------------------------------------------
    def _load_objects(self):
        im = self.im
        otab = im.u32(self.proj + 0x04)
        self.obj_table = otab
        total = im.u16(otab + 0x2A)
        arr = im.u32(otab + 0x30)
        for i in range(total):
            d = arr + i * 0x30
            o = VBObject()
            o.index = i
            o.desc = d
            o.objinfo = im.u32(d + 0x00)
            o.public_bytes = im.u32(d + 0x08)
            o.static_bytes = im.u32(d + 0x0C)
            o.module_public = im.u32(d + 0x10)
            o.module_static = im.u32(d + 0x14)
            o.name = im.cstr(im.u32(d + 0x18)) or 'Object%d' % i
            o.method_count = im.u32(d + 0x1C)
            o.method_names_ptr = im.u32(d + 0x20)
            o.flags = im.u32(d + 0x28)
            if o.flags & 0x80:
                o.kind = 'form'
            elif o.flags & 0x02:
                o.kind = 'class'
            else:
                o.kind = 'module'
            if o.method_names_ptr and im.valid(o.method_names_ptr):
                for k in range(min(o.method_count, 4096)):
                    pn = im.u32(o.method_names_ptr + 4 * k)
                    if pn and im.valid(pn) and not (im.base <= pn < im.base + 0x1000):
                        s = im.cstr(pn, 256)
                        if s and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', s):
                            o.method_names[k] = s
            if o.flags & 0x02:
                self._load_optional(o)
            self.objects.append(o)

    def _load_optional(self, o):
        im = self.im
        opt = o.objinfo + 0x38
        o.opt = opt
        ccount = im.u32(opt + 0x20)
        lctl = im.u32(opt + 0x24)
        for k in range(min(ccount, 1024)):
            c = lctl + k * 0x28
            ctl = Control()
            ctl.flags = im.u16(c + 0x00)
            nev = im.u16(c + 0x02)
            ctl.storage = im.u32(c + 0x04)
            g = im.u32(c + 0x08)
            ctl.guid = im.guid(g) if g and im.valid(g, 16) else None
            ctl.index = im.u16(c + 0x0C)
            ctl.name = im.cstr(im.u32(c + 0x20)) or 'Control%d' % k
            evt = im.u32(c + 0x18)
            if evt and im.valid(evt):
                for e in range(nev):
                    stub = im.u32(evt + 0x18 + 4 * e)
                    if stub:
                        tgt = self._stub_target(stub)
                        if tgt:
                            ctl.events[e] = tgt
            o.controls.append(ctl)
        # method table: pointers to `jmp proc` thunks, starting at lpEvents+4 (indexes match
        # the public method-name table; the first slots are reserved/null)
        ev = im.u32(opt + 0x30)
        if ev and im.valid(ev):
            k = 0
            while k < 4096:
                p = im.u32(ev + 0x4 + 4 * k)
                if p == 0:
                    o.methods.append(None)
                    k += 1
                    continue
                if not im.valid(p, 5) or im.u8(p) != 0xE9:
                    break
                o.methods.append(p + 5 + im.i32(p + 1))
                k += 1
            while o.methods and o.methods[-1] is None:
                o.methods.pop()

    def _stub_target(self, va):
        """Event thunk: `sub dword [esp+4], N ; jmp proc` (or a bare jmp)."""
        im = self.im
        if not im.valid(va, 13):
            return None
        b = im.read(va, 13)
        if b[0] == 0xE9:
            return va + 5 + im.i32(va + 1)
        if b[0:4] == b'\x81\x6c\x24\x04' and b[8] == 0xE9:
            return va + 13 + im.i32(va + 9)
        if b[0:4] == b'\x83\x6c\x24\x04' and b[5] == 0xE9:
            return va + 10 + im.i32(va + 6)
        return None

    def _load_externals(self):
        im = self.im
        ext = im.u32(self.proj + 0x234)
        n = im.u32(self.proj + 0x238)
        by_cache = {}
        for k in range(min(n, 4096)):
            t = im.u32(ext + 8 * k)
            p = im.u32(ext + 8 * k + 4)
            if t == 7 and im.valid(p, 16):
                lib = im.cstr(im.u32(p)) or '?'
                func = im.cstr(im.u32(p + 4)) or '?'
                if func.startswith('#'):
                    pass
                d = Declare(lib, func, cache=im.u32(p + 0xC))
                d.desc = p
                self.declares.append(d)
                by_cache[p] = d
            elif t == 6:
                self.ext_objects.append((im.u32(p), im.u32(p + 4)))
        # find the call stubs:  A1 cache / 0B C0 / 74 02 / FF E0 / 68 desc / B8 DllFunctionCall / FF D0 / FF E0
        mem = im.mem
        for m in re.finditer(re.compile(rb'\xA1(....)\x0B\xC0\x74\x02\xFF\xE0\x68(....)\xB8', re.S), mem):
            desc = struct.unpack('<I', m.group(2))[0]
            if desc in by_cache:
                by_cache[desc].stub = im.base + m.start()
        # disambiguate duplicate declare names (same function from different modules)
        seen = {}
        for d in self.declares:
            seen.setdefault(d.func, []).append(d)

    def _load_gui(self):
        im = self.im
        g = self.gui_table
        forms = [o for o in self.objects if o.kind == 'form']
        if not g or not im.valid(g):
            return
        for i in range(self.form_count):
            e = g + i * 0x50
            if not im.valid(e, 0x50):
                break
            size = im.u32(e + 0x40)
            data = im.u32(e + 0x48)
            if data and im.valid(data, size) and i < len(forms):
                forms[i].gui = bytes(im.read(data, size))

    # ------------------------------------------------------------------
    def _discover_procs(self):
        im = self.im
        # 1) object methods and event handlers
        for o in self.objects:
            for k, va in enumerate(o.methods):
                if va is None:
                    continue
                p = self.procs.get(va) or Proc(va, o)
                p.obj = o
                p.method_index = k
                p.kind = p.kind or 'method'
                if k in o.method_names:
                    p.name = o.method_names[k]
                    p.public = True
                self.procs[va] = p
            for c in o.controls:
                for e, va in c.events.items():
                    p = self.procs.get(va) or Proc(va, o)
                    p.obj = o
                    p.kind = 'event'
                    p.event = (c, e)
                    self.procs[va] = p
        if self.sub_main:
            p = self.procs.get(self.sub_main) or Proc(self.sub_main, None, 'Main')
            p.name = 'Main'
            self.procs[self.sub_main] = p
        # 2) direct call targets inside the user code range, and prologue scan
        lo, hi = self.code_start, self.code_end
        code = im.read(lo, hi - lo)
        targets = set()
        for m in re.finditer(rb'\xE8', code):
            at = lo + m.start()
            if at + 5 > hi:
                continue
            t = at + 5 + im.i32(at + 1)
            if lo <= t < hi:
                targets.add(t)
        for m in re.finditer(rb'\x55\x8B\xEC', code):
            targets.add(lo + m.start())
        # keep only plausible procedure starts
        for t in sorted(targets):
            if t in self.procs:
                continue
            b = im.read(t, 3)
            if b == b'\x55\x8B\xEC':
                self.procs[t] = Proc(t, None)
        # 2b) unreferenced procedures: 16-byte aligned starts after NOP padding with a VB prologue
        PROLOGUES = (b'\x55\x8B\xEC', b'\x83\xEC', b'\x81\xEC', b'\x51', b'\x53', b'\x56', b'\x57',
                     b'\x8B\x44\x24', b'\x8B\x4C\x24', b'\x8B\x54\x24', b'\x33\xC0', b'\x6A')
        a = (lo + 15) & ~15
        while a < hi:
            if a not in self.procs and im.valid(a - 1, 8) and im.u8(a - 1) == 0x90 and \
                    self._after_padding(a) and any(im.read(a, len(pp)) == pp for pp in PROLOGUES):
                first = im.u32(a)
                if not (lo <= first < hi) and self._looks_like_proc(a):
                    self.procs[a] = Proc(a, None)
            a += 16
        # 3) assign owning objects to free procedures by address ranges
        self._assign_owners()
        # 4) `ret N`
        for p in self.procs.values():
            p.ret_n = self.find_ret_n(p.va)

    def _after_padding(self, a):
        """The NOP run before `a` must follow a ret / jmp (end of the previous procedure)."""
        im = self.im
        s = a - 1
        while im.valid(s - 1, 1) and im.u8(s - 1) == 0x90 and a - s < 16:
            s -= 1
        if im.u8(s - 1) == 0xC3:
            return True
        if im.valid(s - 3, 3) and im.u8(s - 3) == 0xC2:
            return True
        if im.valid(s - 5, 5) and im.u8(s - 5) == 0xE9:
            return True
        if im.valid(s - 2, 2) and im.u8(s - 2) == 0xEB:
            return True
        return False

    def _looks_like_proc(self, va):
        """Decode forward: plausible straight code ending in a ret within a short distance."""
        n = 0
        for i in _md.disasm(self.im.read(va, 0x4000), va):
            n += 1
            if i.mnemonic in ('ret', 'retn'):
                return n >= 3
            if i.mnemonic in ('int3', 'hlt', 'in', 'out', 'insb', 'insd', 'outsb', 'outsd', 'arpl', 'bound',
                              'popal', 'pushal', 'das', 'aas', 'daa', 'aaa', 'into', 'iretd', 'retf'):
                return False
            if n > 4000:
                return False
        return False

    def _assign_owners(self):
        procs = sorted(self.procs)
        owned = [(va, self.procs[va].obj) for va in procs if self.procs[va].obj is not None]
        # objects with known procs give address spans; free procs between spans belong to the
        # object whose span precedes them, unless that object is a form and a module exists after.
        mods = [o for o in self.objects if o.kind == 'module']
        # a free procedure inside a form's code range belongs to that form
        for o in self.objects:
            if o.kind not in ('form', 'class'):
                continue
            vas = [va for va, ob in owned if ob is o]
            if not vas:
                continue
            lo_, hi_ = min(vas), max(vas)
            for va in procs:
                if self.procs[va].obj is None and lo_ < va < hi_:
                    self.procs[va].obj = o
        owned = [(va, self.procs[va].obj) for va in procs if self.procs[va].obj is not None]
        spans = {}
        for va, o in owned:
            lo, hi = spans.get(o.index, (va, va))
            spans[o.index] = (min(lo, va), max(hi, va))
        free = [va for va in procs if self.procs[va].obj is None]
        if not free:
            return
        # modules have no method table: split remaining procs among modules in object order,
        # using the module's static-data hints when possible (procs referencing globals in the
        # module's static/public block).
        if len(mods) == 1:
            for va in free:
                self.procs[va].obj = mods[0]
            return
        if not mods:
            for va in free:
                self.procs[va].obj = max(((v, ob) for v, ob in owned if v < va), default=(0, self.objects[0]))[1]
            return
        # module code is emitted in object order: split the (address-sorted) free procedures into
        # contiguous groups, choosing split points that agree best with the data each one touches
        guess = self._module_by_data_refs(free, mods)
        n, m = len(free), len(mods)
        INF = -10 ** 9
        best = [[INF] * (n + 1) for _ in range(m + 1)]
        back = [[0] * (n + 1) for _ in range(m + 1)]
        best[0][0] = 0
        for j in range(1, m + 1):
            for i in range(n + 1):
                for k in range(i + 1):
                    if best[j - 1][k] == INF:
                        continue
                    score = best[j - 1][k] + sum(1 for va in free[k:i] if guess.get(va) is mods[j - 1]) + \
                        (0.5 if i > k else 0)
                    if score > best[j][i]:
                        best[j][i] = score
                        back[j][i] = k
        i = n
        for j in range(m, 0, -1):
            k = back[j][i]
            for va in free[k:i]:
                self.procs[va].obj = mods[j - 1]
            i = k

    def _module_by_data_refs(self, free, mods):
        """Guess the module of each free proc from the module data blocks it touches."""
        im = self.im
        ranges = []
        for o in mods:
            for a in (o.module_public, o.module_static):
                if a:
                    ranges.append((a, o))
        ranges.sort(key=lambda x: x[0])
        bounds = {}
        for i, (a, o) in enumerate(ranges):
            bounds[a] = ranges[i + 1][0] if i + 1 < len(ranges) else a + 0x1000
        res = {}
        allp = sorted(self.procs)
        for va in free:
            idx = allp.index(va)
            end = allp[idx + 1] if idx + 1 < len(allp) else self.code_end
            code = im.read(va, min(end - va, 0x20000))
            votes = {}
            values = []
            for ins in _mdd.disasm(code, va):
                for op in ins.operands:
                    if op.type == 3 and op.mem.base == 0 and op.mem.index == 0:      # [abs]
                        values.append(op.mem.disp & 0xffffffff)
                    elif op.type == 2:                                               # imm (push offset)
                        values.append(op.imm & 0xffffffff)
            for v in values:
                best = None
                for a, o in ranges:
                    if a <= v < bounds[a]:
                        best = (a, o)
                if best:
                    votes[best[1].index] = votes.get(best[1].index, 0) + 1
            if votes:
                k = max(votes, key=votes.get)
                res[va] = self.objects[k]
        # procedures without data votes follow their callers / callees (call-graph affinity)
        calls = {va: set() for va in free}
        fs = set(free)
        for va in free:
            idx = allp.index(va)
            end = allp[idx + 1] if idx + 1 < len(allp) else self.code_end
            for ins in _md.disasm(im.read(va, min(end - va, 0x20000)), va):
                if ins.mnemonic == 'call' and ins.op_str.startswith('0x'):
                    t = int(ins.op_str, 16)
                    if t in fs and t != va:
                        calls[va].add(t)
                        calls[t].add(va)
        for _ in range(len(free)):
            changed = False
            for va in free:
                if va in res:
                    continue
                c = {}
                for o in calls[va]:
                    if o in res:
                        c[res[o].index] = c.get(res[o].index, 0) + 1
                if c:
                    res[va] = self.objects[max(c, key=c.get)]
                    changed = True
            if not changed:
                break
        return res

    def find_ret_n(self, va, limit=0x20000):
        """Bytes popped by the procedure's final `ret N` (the first `ret` preceded by pop ebp/leave)."""
        im = self.im
        prev = []
        for i in _md.disasm(im.read(va, limit), va):
            if i.mnemonic == 'ret':
                if prev and (prev[-1] in ('pop ebp', 'leave')):
                    return int(i.op_str, 16) if i.op_str else 0
                if i.op_str:
                    return int(i.op_str, 16)
            if i.mnemonic == 'int3':
                pass
            prev.append('%s %s' % (i.mnemonic, i.op_str) if i.op_str else i.mnemonic)
            if len(prev) > 4:
                prev.pop(0)
        return None

    # ------------------------------------------------------------------
    def rtmi(self, va):
        """Procedure descriptor ([ebp-8]/[ebp-0x10] in the prologue): error handler labels."""
        im = self.im
        if not im.valid(va, 0x14):
            return None
        info = {'va': va, 'handlers': {}}
        tab = im.u32(va + 0x10)
        if tab and im.valid(tab, 8):
            n = im.u32(tab)
            if 0 < n < 256:
                for k in range(n):
                    a = im.u32(tab + 8 + 4 * k)
                    if im.is_code(a):
                        info['handlers'][k + 1] = a
        # code built without optimisation: where each numbered statement starts (the number is kept at
        # [ebp - 4]); On Error Resume Next carries on at entry number + 1 - the last entry is the exit
        info['statements'] = {}
        tab = im.u32(va + 0x14)
        if tab and im.valid(tab, 4):
            n = im.u32(tab)
            if 0 < n < 100000 and im.valid(tab + 4, 4 * n):
                addrs = [im.u32(tab + 4 + 4 * k) for k in range(n)]
                if all(im.is_code(a) for a in addrs):
                    info['statements'] = {k + 1: a for k, a in enumerate(addrs)}
        return info

    def declare_at(self, va):
        for d in self.declares:
            if d.stub == va:
                return d
        return None


if __name__ == '__main__':
    pr = Project(sys.argv[1])
    print(pr.name, pr.exe_name, 'native' if pr.is_native else 'pcode', 'code %x-%x' % (pr.code_start, pr.code_end))
    for o in pr.objects:
        print('object', o.index, o.name, o.kind, hex(o.flags), 'methods', len(o.methods),
              'controls', [(c.name, c.guid, {k: hex(v) for k, v in c.events.items()}) for c in o.controls],
              'gui', len(o.gui) if o.gui else None)
    for d in pr.declares:
        print('declare', d.lib, d.func, hex(d.stub or 0))
    print('procs', len(pr.procs))
    for va in sorted(pr.procs):
        p = pr.procs[va]
        print('  %x %-10s %-12s %s ret=%s' % (va, p.obj.name if p.obj else '?', p.kind, p.name, p.ret_n))
