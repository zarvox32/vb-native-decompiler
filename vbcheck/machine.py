"""Machine-code side of the check: runs one procedure of the real executable in Unicorn.

The program image is mapped at its base address. Every import (MSVBVM60 runtime function) points to a
hook stub handled in Python (mxruntime.py); the form object and every COM object the procedure sees
are fake objects whose vtables also lead to hook stubs; calls to the program's other procedures are
intercepted at their entry (direct calls) or in the form's vtable (method calls). Nothing outside
the procedure under test runs: its effects come from the World, exactly as for the interpreter.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TOOLS, 'emu'))
sys.path.insert(0, os.path.join(TOOLS, 'vbdec'))
sys.path.insert(0, os.path.join(TOOLS, 'lib'))

from unicorn import Uc, UcError, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE, UC_HOOK_MEM_UNMAPPED, \
    UC_HOOK_MEM_FETCH_UNMAPPED, UC_PROT_ALL  # noqa: E402
from unicorn.x86_const import (UC_X86_REG_EAX, UC_X86_REG_EBX, UC_X86_REG_ECX, UC_X86_REG_EDX,  # noqa: E402
                               UC_X86_REG_ESI, UC_X86_REG_EDI, UC_X86_REG_EBP, UC_X86_REG_ESP,
                               UC_X86_REG_EIP, UC_X86_REG_GDTR, UC_X86_REG_FS, UC_X86_REG_DS,
                               UC_X86_REG_ES, UC_X86_REG_SS, UC_X86_REG_EFLAGS)

from vbvalues import V, VBError, ObjRef, canon  # noqa: E402
from mxmem import Mem  # noqa: E402
from world import StopRun  # noqa: E402
from mxruntime import RunStopError  # noqa: E402

GDT = 0x00001000
TEB = 0x00002000
SCRATCH = 0x00003000          # SCR_IN (8) SCR_OUT (8) RET_SLOT (4) ... trampolines at +0x100
SCR_IN = SCRATCH
SCR_OUT = SCRATCH + 8
RET_SLOT = SCRATCH + 16
TRAMP_FPUSH = SCRATCH + 0x100   # fld qword [SCR_OUT] ; jmp [RET_SLOT]
TRAMP_FPUSH32 = SCRATCH + 0x120  # fld dword [SCR_OUT] ; jmp [RET_SLOT]
STACK_LO = 0x00100000
STACK_HI = 0x00400000 - 0x1000
STUBS = 0x70000000
STUB_SIZE = 16
NSTUBS = 0x8000

REGS = {'eax': UC_X86_REG_EAX, 'ebx': UC_X86_REG_EBX, 'ecx': UC_X86_REG_ECX, 'edx': UC_X86_REG_EDX,
        'esi': UC_X86_REG_ESI, 'edi': UC_X86_REG_EDI, 'ebp': UC_X86_REG_EBP, 'esp': UC_X86_REG_ESP,
        'eip': UC_X86_REG_EIP}

FORM_VTSIZE = 0x2F8


def gdt_entry(base, limit, access, flags):
    e = limit & 0xffff
    e |= (base & 0xffffff) << 16
    e |= (access & 0xff) << 40
    e |= ((limit >> 16) & 0xf) << 48
    e |= (flags & 0xf) << 52
    e |= ((base >> 24) & 0xff) << 56
    return struct.pack('<Q', e)


class RunStop(Exception):
    pass


class Machine:
    def __init__(self, dc, prog, label=''):
        """dc: a vbdec Decompiler after run_all() (metadata, signatures, vtable layout);
        prog: the vbinterp.Program of the decompiled source (declared types)."""
        from mxruntime import Runtime
        self.dc = dc
        self.prog = prog
        self.im = dc.im
        self.label = label
        uc = Uc(UC_ARCH_X86, UC_MODE_32)
        self.uc = uc
        self.mem = Mem(uc, prog)
        self.mem.make_vtable = self.vtable_for
        self.world = None
        self.pending = None
        self.stubs = {}            # address -> (kind, data)
        self.nstub = 0
        self.vtables = {}
        im = self.im
        # image
        size = (len(im.mem) + 0xfff) & ~0xfff
        uc.mem_map(im.base, size, UC_PROT_ALL)
        self.image_bytes = bytes(im.mem)
        self.image_size = size
        # low memory: GDT, TEB, scratch
        uc.mem_map(0x1000, 0x10000, UC_PROT_ALL)
        uc.mem_map(STACK_LO, STACK_HI + 0x1000 - STACK_LO, UC_PROT_ALL)
        from mxmem import HEAP_BASE, HEAP_SIZE
        uc.mem_map(HEAP_BASE, HEAP_SIZE, UC_PROT_ALL)
        uc.mem_map(STUBS, NSTUBS * STUB_SIZE, UC_PROT_ALL)
        uc.mem_write(STUBS, b'\x90' * (NSTUBS * STUB_SIZE))
        g = b'\0' * 8 + gdt_entry(0, 0xfffff, 0x9a | 0x60, 0xc) + gdt_entry(0, 0xfffff, 0x92 | 0x60, 0xc) + \
            gdt_entry(TEB, 0xfff, 0x92 | 0x60, 0x4) + gdt_entry(0, 0xfffff, 0x92, 0xc)
        uc.mem_write(GDT, g)
        uc.reg_write(UC_X86_REG_GDTR, (0, GDT, len(g) - 1, 0x0))
        uc.reg_write(UC_X86_REG_FS, (3 << 3) | 3)
        uc.reg_write(UC_X86_REG_DS, (2 << 3) | 3)
        uc.reg_write(UC_X86_REG_ES, (2 << 3) | 3)
        uc.reg_write(UC_X86_REG_SS, (4 << 3) | 0)
        # trampolines
        uc.mem_write(TRAMP_FPUSH, b'\xDD\x05' + struct.pack('<I', SCR_OUT) + b'\xFF\x25' + struct.pack('<I', RET_SLOT))
        uc.mem_write(TRAMP_FPUSH32, b'\xD9\x05' + struct.pack('<I', SCR_OUT) + b'\xFF\x25' +
                     struct.pack('<I', RET_SLOT))
        # FPU: 53-bit precision, round to nearest, all exceptions masked (Windows default 0x27F)
        self.fpu_init = STUBS + (NSTUBS - 1) * STUB_SIZE
        uc.mem_write(self.fpu_init, b'\xDB\xE3' + b'\xD9\x2D' + struct.pack('<I', SCRATCH + 0x200) + b'\x90')
        uc.mem_write(SCRATCH + 0x200, struct.pack('<H', 0x27F))
        # exit stub: fst qword [SCR_IN] (keeps a floating-point function result), then the end address
        self.exit_stub = self.new_stub('exit', None, pre=bytes([0xDD, 0x15]) + struct.pack('<I', SCR_IN))
        self.exit_end = self.exit_stub + 6
        self.rt = Runtime(self)
        self.import_stubs = {}
        for slot, name in im.imports.items():
            self.import_stubs[slot] = self.import_stub(name)
        self.declare_stubs = {}     # declare descriptor -> (address cache, stub)
        for d in dc.proj.declares:
            self.declare_stubs[getattr(d, 'desc', id(d))] = (d.cache, self.new_stub('api', d))
        # procedures: entry stubs for direct calls
        self.proc_stub = {}
        for va, p in dc.proj.procs.items():
            self.proc_stub[va] = self.new_stub('proc', p)
        uc.hook_add(UC_HOOK_CODE, self.on_stub, begin=STUBS, end=STUBS + NSTUBS * STUB_SIZE - 1)
        uc.hook_add(UC_HOOK_MEM_UNMAPPED | UC_HOOK_MEM_FETCH_UNMAPPED, self.on_unmapped)
        # coverage: which of the watched addresses (block starts) ran; see watch_blocks()
        self.cov = None
        self.cov_hooks = []
        self.me_obj = ObjRef(1, 'Form', 'me')
        self.form = None
        for o in dc.proj.objects:
            if o.kind == 'form':
                self.form = o
                break
        self.me_ptr = None

    # ------------------------------------------------------------------ stubs
    def new_stub(self, kind, data, pre=b''):
        a = STUBS + self.nstub * STUB_SIZE
        self.nstub += 1
        if self.nstub >= NSTUBS - 1:
            raise RuntimeError('out of hook stubs')
        if pre:
            self.uc.mem_write(a, pre)
        self.stubs[a + len(pre)] = (kind, data)
        return a

    def import_stub(self, name):
        pre = b''
        if name in self.rt.FPU_IN:
            pre = b'\xDD\x1D' + struct.pack('<I', SCR_IN)      # fstp qword [SCR_IN]
        return self.new_stub('rt', name, pre)

    def vtable_for(self, o):
        """vtable address for object o (one per interface; the object id is in the object)."""
        key = 'me' if o.kind == 'me' else o.iface
        vt = self.vtables.get(key)
        if vt is None:
            n = 0x400
            vt = self.mem_static_alloc(4 * n)
            for k in range(n):
                s = self.new_stub('com', (key, 4 * k))
                self.uc.mem_write(vt + 4 * k, struct.pack('<I', s))
            self.vtables[key] = vt
        return vt

    def mem_static_alloc(self, n):
        """Memory that survives runs (vtables): carved from the top of the heap region."""
        if not hasattr(self, '_static'):
            from mxmem import HEAP_BASE, HEAP_SIZE
            self._static = HEAP_BASE + HEAP_SIZE - 0x10000
        self._static -= (n + 15) & ~15
        return self._static

    # ------------------------------------------------------------------ registers / stack
    def reg(self, r):
        return self.uc.reg_read(REGS[r])

    def set_reg(self, r, v):
        self.uc.reg_write(REGS[r], v & 0xffffffff)

    def arg(self, i):
        """i-th dword argument (0 = first, just above the return address)."""
        return self.mem.u32(self.reg('esp') + 4 + 4 * i)

    def arg_at(self, off):
        return self.reg('esp') + 4 + off

    def ret(self, eax=None, pop=0, edx=None):
        esp = self.reg('esp')
        ra = self.mem.u32(esp)
        if eax is not None:
            self.set_reg('eax', eax)
        if edx is not None:
            self.set_reg('edx', edx)
        self.set_reg('esp', esp + 4 + pop)
        self.set_reg('eip', ra)

    def ret_fpu(self, x, pop=0, single=False):
        esp = self.reg('esp')
        ra = self.mem.u32(esp)
        if single:
            self.mem.wf32(SCR_OUT, x)
        else:
            self.mem.wf64(SCR_OUT, x)
        self.mem.w32(RET_SLOT, ra)
        self.set_reg('esp', esp + 4 + pop)
        self.set_reg('eip', TRAMP_FPUSH32 if single else TRAMP_FPUSH)

    def fpu_arg(self):
        return self.mem.f64(SCR_IN)

    # ------------------------------------------------------------------ hook dispatch
    def on_unmapped(self, uc, access, address, size, value, ud):
        if address < 0x10000:
            # a method call / member access through Nothing: VB reports error 91
            self.pending = VBError(91, 'object variable not set (read of %x at eip %x)' % (address, self.reg('eip')))
        else:
            self.pending = RunStop('access to unmapped memory %x (eip %x)' % (address, self.reg('eip')))
        uc.emu_stop()
        return False

    def watch_blocks(self, addrs):
        """Record in self.cov (a set, when not None) each of these code addresses that runs. (Unicorn's
        block hook misses chained blocks, so each address gets its own one-instruction code hook; the
        image is rewritten before every run, so no translated code predates the hooks.)"""
        for h in self.cov_hooks:
            self.uc.hook_del(h)
        self.cov_hooks = [self.uc.hook_add(UC_HOOK_CODE, self.on_cov, begin=a, end=a) for a in addrs]

    def on_cov(self, uc, address, size, ud):
        if self.cov is not None:
            self.cov.add(address)

    def on_stub(self, uc, address, size, ud):
        ent = self.stubs.get(address)
        if ent is None:
            return
        kind, data = ent
        try:
            if kind == 'exit':
                uc.emu_stop()
                self.finished = True
                return
            if kind == 'rt':
                self.rt.call(data)
            elif kind == 'com':
                self.com_call(*data)
            elif kind == 'proc':
                self.proc_call(data)
            elif kind == 'api':
                self.api_call(data)
            else:
                raise RunStop('unknown stub kind %s' % kind)
        except VBError as e:
            if os.environ.get('VBCHECK_TRACE'):
                import traceback
                traceback.print_exc()
            if getattr(e, 'where', None) is None:
                e.where = 'machine: %s called from %x' % (data if isinstance(data, str) else kind,
                                                          self.mem.u32(self.reg('esp')))
            self.pending = e
            uc.emu_stop()
        except (StopRun, RunStop, RunStopError) as e:
            self.pending = e
            uc.emu_stop()
        except Exception as e:      # a bug in a model: stop and report it as such
            import traceback
            self.pending = RunStop('checker error in %s %s: %s\n%s' % (kind, data, e, traceback.format_exc()))
            uc.emu_stop()

    # ------------------------------------------------------------------ errors (On Error)
    def handle_error(self, e):
        """Run-time error inside the procedure under test: jump to its active handler (if any)."""
        self.err_number = e.num
        h = self.on_error
        if h is None or self.in_handler:
            return False
        if h == -1:
            # On Error Resume Next: the code keeps the number of the statement it is in at [ebp - 4];
            # carry on at the start of the next statement (statement numbers follow the code order)
            ebp = self.reg('ebp')
            if ebp != self.frame_ebp:
                return False
            addr = self.statement_starts.get(self.mem.i32(ebp - 4) + 1)
            if addr is None:
                return False
            self.set_reg('esp', self.mem.u32(ebp - self.esp_slot))
            self.set_reg('eip', addr)
            return True
        addr = self.handlers.get(h)
        if addr is None:
            return False
        ebp = self.reg('ebp')
        if ebp != self.frame_ebp:
            return False
        self.in_handler = True
        self.set_reg('esp', self.mem.u32(ebp - self.esp_slot))
        self.set_reg('eip', addr)
        return True

    # ------------------------------------------------------------------ calls out of the procedure
    def proc_by_vtoff(self, off):
        o = self.form
        base = self.dc.method_base.get(o.index)
        if base is None or off < base:
            return None
        k = (off - base) // 4
        if 0 <= k < len(o.methods) and o.methods[k] is not None:
            return self.dc.proj.procs.get(o.methods[k])
        return None

    def com_call(self, key, off):
        esp = self.reg('esp')
        this = self.mem.u32(esp + 4)
        if key == 'me':
            if off == 0:      # QueryInterface(riid, ppv)
                ppv = self.mem.u32(esp + 12)
                if ppv:
                    self.mem.w32(ppv, this)
                return self.ret(0, 12)
            if off in (4, 8):     # AddRef / Release
                return self.ret(1, 4)
            if off < FORM_VTSIZE:
                return self.object_member(self.me_obj, '_Form', off)
            p = self.proc_by_vtoff(off)
            if p is not None:
                return self.user_call(p, method=True)
            # control accessor
            ctls = [c for c in self.form.controls if c.name != 'Form' and not self.dc.is_impl_iface(c)]
            ci = (off - FORM_VTSIZE) // 4
            if 0 <= ci < len(ctls):
                name = ctls[ci].name.lower()
                oid, cls = self.prog.controls.get(name, (None, None))
                if oid is None:
                    raise RunStop('control %s not declared in the source' % name)
                return self.ret(self.mem.obj_ptr(ObjRef(oid, cls, 'control')), 4)
            raise RunStop('call through the form vtable at +%x: no procedure or control there' % off)
        o = self.mem.obj_by_ptr.get(this)
        if o is None:
            raise RunStop('COM call on %x, not an object the checker created' % this)
        if off == 0:      # QueryInterface(riid, ppv)
            ppv = self.mem.u32(esp + 12)
            if ppv:
                self.mem.w32(ppv, this)
            return self.ret(0, 12)
        if off in (4, 8):
            return self.ret(1, 4)
        return self.object_member(o, o.iface, off)

    def object_member(self, o, iface_name, off):
        if o.kind == 'err':
            return self.rt.err_member(off)
        it = self.prog.iface_of(iface_name) if iface_name != '_Form' else self.prog.tlb.by_name['_Form']
        if it is None:
            raise RunStop('object %s of unknown interface %s' % (o, iface_name))
        m = it.member(off)
        if m is None:
            raise RunStop('%s has no member at vtable offset %x' % (it.name, off))
        esp = self.reg('esp')
        pos = esp + 8
        args = []
        outs = {}
        out_ptrs = {}
        retptr = None
        rtype = None
        rlay = None
        vis = 0
        for p in m.params:
            pt = p['type']
            flags = p['flags']
            if flags & 8:
                retptr = self.mem.u32(pos)
                pos += 4
                base = pt[:-1]
                rtype = self.prog.canon_type(base.rstrip('*')) if not self.is_iface(base) else base.rstrip('*')
                rlay = self.prog.layout(base)
                continue
            if pt == 'Void*':
                from vbinterp import any_param_type
                pt = any_param_type(m.name, args) or pt
            if pt == 'Variant':
                args.append(self.mem.read_variant(pos))
                pos += 16
            elif pt in ('Double', 'Date', 'Currency'):
                args.append(self.mem.read_value(pos, pt))
                pos += 8
            elif pt.endswith('*') and not self.is_iface(pt[:-1]):
                ptr = self.mem.u32(pos)
                pos += 4
                base = pt[:-1]
                lay = self.prog.layout(base)
                t = self.prog.canon_type(base)
                if t == 'Variant':
                    args.append(self.mem.read_variant(ptr))
                else:
                    args.append(self.mem.read_value(ptr, t, lay))
                if flags & 2:
                    outs[vis] = (t, lay)
                    out_ptrs[vis] = (ptr, t, lay)
            elif pt.endswith('**') and self.is_iface(pt):
                # a ByRef object parameter (InitAudio's DirectSound): the address of an object variable
                ptr = self.mem.u32(pos)
                args.append(V('Object', self.mem.read_obj(self.mem.u32(ptr) if ptr else 0)))
                pos += 4
            elif self.is_iface(pt):
                args.append(V('Object', self.mem.read_obj(self.mem.u32(pos))))
                pos += 4
            else:
                t = self.prog.canon_type(pt)
                if t == 'Single':
                    args.append(V('Single', self.mem.f32(pos)))
                else:
                    args.append(self.mem.read_value(pos, t))
                pos += 4
            vis += 1
        if retptr is None and m.ret not in ('HRESULT', 'Void', None):
            rtype = self.prog.canon_type(m.ret)
        if rtype is not None and rlay is None and self.is_iface(rtype):
            pass
        ret, outv = self.world.com_call(o, m.name, m.invkind, args, outs, rtype, rlay)
        for i, v in outv.items():
            ptr, t, lay = out_ptrs[i]
            self.mem.write_value(ptr, t, v, lay)
        pop = pos - esp - 4
        if retptr is not None:
            if ret is None:
                raise RunStop('no value for %s.%s' % (it.name, m.name))
            self.mem.write_value(retptr, rtype if not self.is_iface(rtype) else 'Object', ret, rlay)
            return self.ret(0, pop)
        if rtype is not None:
            if rtype in ('Single', 'Double'):
                return self.ret_fpu(ret.v, pop)
            return self.ret(self.encode_scalar(ret), pop)
        return self.ret(0, pop)

    def is_iface(self, t):
        t = t.rstrip('*')
        if t in ('IUnknown', 'IDispatch', 'Object'):
            return True
        it = self.prog.tlb.by_name.get(t)
        return it is not None and it.kind in ('interface', 'dispatch', 'coclass')

    def encode_scalar(self, v):
        if v.t == 'Object':
            return self.mem.obj_ptr(v.v)
        if v.t == 'String':
            return self.mem.bstr(v.v)
        if v.t in ('Integer', 'Long', 'Byte', 'Boolean'):
            return v.v & 0xffffffff
        raise RunStop('cannot return %s in a register' % v.t)

    def user_call(self, p, method):
        """Call to another procedure of the program: arguments per its decompiled declaration."""
        name = self.dc.proc_name(p)
        vp = self.prog.procs.get(name.lower())
        esp = self.reg('esp')
        sig = self.dc.signatures.get(p.va) or {}
        pos = esp + 4 + (4 if method else 0)
        args = []
        if vp is None:
            raise RunStop('call to %s, which the source does not define' % name)
        out_types, out_ptrs = {}, {}
        for i, prm in enumerate(vp.params):
            t = self.prog.canon_type(prm.type)
            lay = self.prog.layout(t)
            if prm.byval and not prm.is_array:
                if t == 'Variant':
                    args.append(self.mem.read_variant(pos))
                    pos += 16
                elif t in ('Double', 'Date', 'Currency'):
                    args.append(self.mem.read_value(pos, t))
                    pos += 8
                elif lay is not None:
                    raise RunStop('record passed ByVal')
                else:
                    args.append(self.mem.read_value(pos, t))
                    pos += 4
            else:
                ptr = self.mem.u32(pos)
                pos += 4
                if not prm.is_array and self.is_iface(t):
                    out_types[i], out_ptrs[i] = t, ptr       # the procedure may Set an object there
                if t == 'Variant' and not prm.is_array:
                    args.append(self.mem.read_variant(ptr))
                else:
                    args.append(self.mem.read_value(ptr, t, lay, prm.is_array))
        rt = None
        retptr = None
        if vp.kind == 'function':
            rt = self.prog.canon_type(vp.rettype)
            if method or sig.get('retptr'):
                retptr = self.mem.u32(pos)
                pos += 4
        pop = pos - esp - 4
        if p.ret_n is not None and p.ret_n != pop:
            raise RunStop('%s pops %d bytes but its decompiled declaration accounts for %d' % (name, p.ret_n, pop))
        ret, outs = self.world.call_proc(name, args, rt, self.prog.layout(rt) if rt else None, out_types)
        for i, v in outs.items():
            self.mem.write_value(out_ptrs[i], out_types[i], v, None)
        if rt is not None:
            lay = self.prog.layout(rt)
            if retptr is not None:
                self.mem.write_value(retptr, rt, ret, lay)
                return self.ret(0, pop)
            if rt in ('Single', 'Double', 'Date'):
                return self.ret_fpu(ret.v, pop)
            return self.ret(self.encode_scalar(ret), pop)
        return self.ret(0, pop)

    def proc_call(self, p):
        """Direct call to a procedure's entry (module procedures; recursion into the tested one)."""
        method = p.obj is not None and p.obj.kind in ('form', 'class')
        return self.user_call(p, method)

    def api_call(self, d):
        """Declare'd API (reached through the declare's call stub)."""
        name = d.func.lower()
        if name == 'gettickcount':
            v = self.world.api('GetTickCount', [], 'Long')
            return self.ret(v.v, 0)
        if name == 'sndplaysounda':
            s = self.mem.read_ansi(self.arg(0)) if self.arg(0) else ''
            v = self.world.api('sndPlaySound', [V('String', s), V('Long', self.arg(1))], 'Long')
            return self.ret(v.v, 8)
        if name == 'timegettime':
            v = self.world.api('timeGetTime', [], 'Long')
            return self.ret(v.v, 0)
        nbytes = self.dc.API_ARG_BYTES.get(d.func)
        if nbytes is None:
            raise RunStop('API %s not modelled' % d.func)
        args = [V('Long', self.mem.i32(self.arg_at(4 * k))) for k in range(nbytes // 4)]
        v = self.world.api(d.name, args, 'Long')
        return self.ret(v.v if v else 0, nbytes)

    # ------------------------------------------------------------------ running a procedure
    def reset(self, test_va):
        uc = self.uc
        uc.mem_write(self.im.base, self.image_bytes)
        self.mem.reset()
        self.pending = None
        self.finished = False
        self.on_error = None
        self.in_handler = False
        self.err_number = 0
        self.frame_ebp = None
        # imports -> stubs
        for slot, stub in self.import_stubs.items():
            uc.mem_write(slot, struct.pack('<I', stub))
        for cache, stub in self.declare_stubs.values():
            if cache and self.im.valid(cache, 4):
                uc.mem_write(cache, struct.pack('<I', stub))
        # the FDIV-bug flag: a modern CPU (no adjustment calls)
        if self.dc.proj.native_code and self.im.valid(self.dc.proj.native_code, 4):
            uc.mem_write(self.dc.proj.native_code, b'\0\0\0\0')
        # direct calls to other procedures land in their stubs
        for va, stub in self.proc_stub.items():
            if va == test_va:
                continue
            rel = stub - (va + 5)
            uc.mem_write(va, b'\xE9' + struct.pack('<i', rel))
        uc.mem_write(TEB, struct.pack('<I', 0xFFFFFFFF))
        self.me_ptr = None
        self.mem.ptr_by_oid = {}
        self.mem.obj_by_ptr = {}

    def run(self, p, args_setup, max_insns=3000000):
        """Run procedure p. args_setup(machine) -> list of dwords/bytes to push (first argument first)
        and the retptr address (or None). Returns (eax, ret_kind)."""
        uc = self.uc
        cells = args_setup(self)
        esp = STACK_HI - 0x100
        blob = b''.join(cells)
        esp -= len(blob)
        uc.mem_write(esp, blob)
        esp -= 4
        uc.mem_write(esp, struct.pack('<I', self.exit_stub))
        # init FPU
        self.set_reg('esp', esp - 64)
        self.set_reg('eip', self.fpu_init)
        uc.emu_start(self.fpu_init, self.fpu_init + 8)
        for r in ('eax', 'ebx', 'ecx', 'edx', 'esi', 'edi', 'ebp'):
            self.set_reg(r, 0xDEAD0000 + hash(r) % 0x1000)
        self.set_reg('esp', esp)
        self.watch_frame(p)
        start = p.va
        for _ in range(1000):
            try:
                uc.emu_start(start, self.exit_end, count=max_insns)
            except UcError as e:
                if self.pending is None:
                    self.pending = RunStop('CPU fault: %s at %x' % (e, self.reg('eip')))
            # a VB error with an active On Error GoTo handler: continue there
            if isinstance(self.pending, VBError) and self.handle_error(self.pending):
                self.pending = None
                start = self.reg('eip')
                continue
            break
        if self.pending is not None:
            raise self.pending
        if self.reg('eip') != self.exit_end:
            raise StopRun('instruction budget')
        return self.reg('eax')

    def watch_frame(self, p):
        """Remember the procedure's error handlers (On Error GoTo)."""
        fi = self.dc.frame_info(p)
        self.esp_slot = fi.get('esp_slot') or 8
        # statement numbers (`mov dword ptr [ebp - 4], n` at the start of each statement, in code built
        # without optimisation): where each statement begins, for On Error Resume Next
        self.statement_starts = {}
        try:
            cfg = self.dc.lift(p).cfg
            for b in cfg.blocks.values():
                for ins in b.insns:
                    o = ins.ops
                    if ins.mn == 'mov' and len(o) == 2 and o[0].kind == 'mem' and o[0].base == 'ebp' and \
                            o[0].index is None and (o[0].disp & 0xffffffff) == 0xfffffffc and o[1].kind == 'imm' \
                            and 0 < o[1].imm < 100000:
                        self.statement_starts.setdefault(o[1].imm, ins.addr)
        except Exception:
            self.statement_starts = {}
        self.handlers = {}
        if fi.get('rtmi'):
            r = self.dc.proj.rtmi(fi['rtmi'])
            if r:
                self.handlers = dict(r['handlers'])
                if r.get('statements'):
                    # the compiler's own table (it has the exit after the last statement, too)
                    self.statement_starts = dict(r['statements'])
        self.frame_ebp = STACK_HI - 0x100 - 4 - 0      # updated by __vbaOnError (ebp of the frame)
