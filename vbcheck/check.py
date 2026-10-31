"""Check that decompiled VB6 source behaves like the machine code it came from.

    python -I vbcheck/check.py "Troopanum 1.6" [--runs 40] [--proc Proc_418550] [--seed 1]
                               [--project NAME_OR_FILE]

The program is looked up in the library (apps/*/project/project.json; see project.py): where its executable
or unpacked image is, the omission rules, the program's folder and data files for the scripted world.

For every procedure of the program: decompile the executable (without the usual omissions, so that the
whole output is checked), then run the procedure many times from random starting states, once by
interpreting the decompiled source and once by emulating the original machine code, both against the
same scripted world. Any difference in what the two do (events, arguments, form state at each event,
final state, return value, run-time errors) is reported with the first point where they part ways.

Coverage: the machine-code blocks each procedure reached are counted ("blocks 40/45"). After --runs runs,
further runs draw values from the constants and strings the machine code compares with, re-run starting
states that reached new code with one variable changed, and aim at branches not taken yet (setting the
variable a branch tests to the value it compares with), until nothing new is reached (at most 10 x runs).
--show lists the blocks never reached: code that was not exercised is not verified.

Statuses: pass; MISMATCH (a difference: a decompiler bug, or a gap in this checker); omitted (left out of
the published source on purpose); unsupported-source / invalid-source (the interpreter cannot run or parse
that source); machine-problem (the emulation could not run the original code). Runs in which the source
indexes outside an array (undefined in programs compiled without bounds checks) are counted as
out-of-bounds, not compared. (In a program compiled with bounds checks - Troopanum 1.5, for example - an
index outside the array is error 9 on both sides and is compared like any other error.)
"""
import argparse
import os
import random
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(TOOLS, 'vbdec'))
sys.path.insert(0, os.path.join(TOOLS, 'lib'))

import vbparse as P  # noqa: E402
from vbvalues import V, VBError, ObjRef, VBArray, VBRecord, canon, convert, copy_value, default_value  # noqa: E402
from vbinterp import Program, Interp, State, Cell, Unsupported, OutOfBounds  # noqa: E402
import world  # noqa: E402
from world import World, StopRun  # noqa: E402
import project as projects  # noqa: E402

FILES = {}      # path -> contents: the program's data files the starting states may have (from the project)


def use_project(proj):
    """The project's knowledge of its programs for the scripted world: the program folder (App.Path), the
    data files found there, strings the programs work with."""
    ck = proj.data.get('checker', {})
    world.SETTINGS['app_path'] = ck.get('app_path', world.SETTINGS['app_path'])
    world.SETTINGS['strings'] = list(ck.get('strings', []))
    FILES.clear()
    for name, text in ck.get('files', {}).items():
        FILES[world.SETTINGS['app_path'].rstrip('\\') + '\\' + name] = text


def load_decompiler(proj, game):
    from decompiler import Decompiler
    image = proj.image_path(game)
    if image:
        from pe import Image
        g, info = proj.game(game)
        img = Image(path=image, mapped=open(image, 'rb').read(), base=int(info.get('image_base', '400000'), 16))
        return Decompiler(image=img)
    return Decompiler(proj.exe_path(game))


def decompile(proj, game, outdir):
    """Decompile with no omissions (the omitted parts are checked too); returns (decompiler, Program)."""
    os.environ['VBDEC_NOEXCLUDE'] = '1'
    import main as vbmain
    dc = load_decompiler(proj, game)
    lifted = dc.run_all()
    vbmain.project_files(dc, lifted, outdir, proj)
    mods = []
    for f in sorted(os.listdir(outdir)):
        if f.endswith(('.frm', '.bas', '.cls')):
            mods.append(P.parse_file(os.path.join(outdir, f)))
    from tlb import TypeLibDB
    prog = Program(mods, dc.tlb)
    return dc, prog


# ----------------------------------------------------------------------------- analysis of a procedure
REG32 = {'ax': 'eax', 'al': 'eax', 'ah': 'eax', 'bx': 'ebx', 'bl': 'ebx', 'bh': 'ebx', 'cx': 'ecx', 'cl': 'ecx',
         'ch': 'ecx', 'dx': 'edx', 'dl': 'edx', 'dh': 'edx', 'si': 'esi', 'di': 'edi', 'bp': 'ebp', 'sp': 'esp'}


def walk(node):
    """All AST nodes below node (statements and expressions)."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            stack.extend(n)
            continue
        if isinstance(n, tuple):
            stack.extend(x for x in n if isinstance(x, (P.Node, list, tuple)))
            continue
        if not isinstance(n, P.Node):
            continue
        yield n
        for s in n.__slots__:
            v = getattr(n, s, None)
            if isinstance(v, (P.Node, list, tuple)):
                stack.append(v)


def referenced_names(proc):
    out = set()
    for n in walk(proc.body):
        if isinstance(n, P.Name):
            out.add(n.name.lower())
    return out


def constants_of(proc):
    out = set()
    for n in walk(proc.body):
        if isinstance(n, P.Num) and not n.is_float:
            out.add(n.v)
            out.add(n.v + 1)
            out.add(n.v - 1)
    return out


def source_hints(proc):
    """(path, value) pairs for the coverage search: array elements with constant indexes and record
    fields (`m_230(6).fC`, `m_554.lHow`) that the decompiled source compares with constants. Paths are
    (variable, indexes or None, field or None). Only used to choose test inputs."""
    def const(n):
        if isinstance(n, P.Num):
            return n.v
        if isinstance(n, P.UnOp) and n.op in ('-', 'neg') and isinstance(n.a, P.Num):
            return -n.a.v
        if isinstance(n, P.StrLit):
            return n.s
        return None

    def path(n):
        member = None
        if isinstance(n, P.Member):
            member = n.name.lower()
            n = n.obj
        if isinstance(n, P.Apply) and isinstance(n.target, P.Name):
            idxs = [const(a) for a in n.args]
            if not idxs:
                return None
            if any(not isinstance(i, int) for i in idxs):
                # a variable index (a loop over the elements): every element gets the value
                return (n.target.name.lower(), '*', member)
            return (n.target.name.lower(), tuple(idxs), member)
        if isinstance(n, P.Name) and member is not None:
            return (n.name.lower(), None, member)
        return None

    out = []

    def add(pth, v):
        vals = [v] if isinstance(v, str) else [v, v + 1, v - 1]
        for x in vals + [0]:
            if (pth, x) not in out:
                out.append((pth, x))
    for n in walk(proc.body):
        if isinstance(n, P.BinOp) and n.op in ('=', '<>', '<', '>', '<=', '>='):
            for a, b in ((n.a, n.b), (n.b, n.a)):
                pth, v = path(a), const(b)
                if pth is not None and v is not None:
                    add(pth, v)
        elif isinstance(n, P.Select):
            pth = path(n.subject)
            if pth is not None:
                for exprs, body in n.cases:
                    for e in exprs:
                        v = const(e)
                        if v is not None:
                            add(pth, v)
    return out


def array_usage(mod):
    """name -> (max number of indices, max constant index) over the whole module."""
    use = {}
    for p in mod.proc_order:
        for n in walk(p.body):
            if isinstance(n, P.Apply) and isinstance(n.target, P.Name):
                k = n.target.name.lower()
                dims, mx = use.get(k, (0, 0))
                dims = max(dims, len(n.args))
                for a in n.args:
                    if isinstance(a, P.Num) and not a.is_float:
                        mx = max(mx, a.v)
                use[k] = (dims, mx)
    return use


def write_var(mem, prog, addr, d, v, inline=False):
    """inline: a fixed-size array whose elements sit in the object itself (no descriptor), as the
    machine code shows for arrays the instance-data table does not list as kind 5."""
    t = prog.canon_type(d.type.name)
    lay = prog.layout(t)
    if d.type.is_array and d.type.bounds and inline:
        if v.v is None:
            raise ValueError('fixed-size array without data')
        size = mem.slot_size(t, lay)
        for i, e in enumerate(v.v.data):
            mem.write_value(addr + i * size, t, e, lay)
        return
    if d.type.is_array and d.type.bounds:
        # fixed-size array inside the object: descriptor embedded at addr - 0x0C, data pointer at addr
        if v.v is None:
            raise ValueError('fixed-size array without data')
        mem.write_array(v.v, at=addr - 0xC)
        return
    mem.write_value(addr, t, v, lay, d.type.is_array)


def read_var(mem, prog, addr, d, inline=False):
    t = prog.canon_type(d.type.name)
    lay = prog.layout(t)
    if d.type.is_array and d.type.bounds and inline:
        bounds = [(prog.const_expr(lo).v if lo is not None else 0, prog.const_expr(hi).v)
                  for lo, hi in d.type.bounds]
        n = 1
        for lb, ub in bounds:
            n *= ub - lb + 1
        size = mem.slot_size(t, lay)
        return V('Array', VBArray(t, bounds, data=[mem.read_value(addr + i * size, t, lay) for i in range(n)],
                                  rec=lay))
    if d.type.is_array and d.type.bounds:
        return V('Array', mem.read_array(addr - 0xC, t, lay))
    return mem.read_value(addr, t, lay, d.type.is_array)


# ----------------------------------------------------------------------------- one procedure
class Checker:
    def __init__(self, game, dc, prog, runs=30, seed=0, max_events=200, verbose=False):
        from machine import Machine
        self.game = game
        self.dc = dc
        self.prog = prog
        self.runs = runs
        self.seed = seed
        self.max_events = max_events
        self.verbose = verbose
        self.focus = False          # draw integers only from the procedure's constants (see check_proc)
        self.inline_arrays = set()  # fixed-size form arrays stored inline (no descriptor); set below
        self.consts, self.strings, self.floats = set(), [], []
        self.m = Machine(dc, prog, game)
        co = dc.compile_options()
        self.options = {k: co[k] for k in ('overflow_checks', 'fp_checks', 'bounds_checks')}
        self.form = prog.form
        if self.form is not None and self.m.form is not None:
            described = self.dc.fixed_arrays.get(self.m.form.index, {})
            self.inline_arrays = {k for k, d, off in self.fields()
                                  if d.type.is_array and d.type.bounds and off not in described}
        self.usage = {}
        for mod in prog.modules:
            for k, v in array_usage(mod).items():
                d, mx = self.usage.get(k, (0, 0))
                self.usage[k] = (max(d, v[0]), max(mx, v[1]))
        # globals: g_XXXX -> address in the data section
        self.global_addr = {}
        sec = None
        dc.is_data_va(0)
        sec = getattr(dc, '_data_sec', None)
        for mod in prog.modules:
            for k, d in list(mod.globals.items()) + list(mod.fields.items()):
                mm = re.fullmatch(r'g_([0-9a-f]+)', k)
                if mm and sec:
                    low = int(mm.group(1), 16)
                    for va in range(sec[0] & ~0xffff, sec[1], 0x10000):
                        cand = (va & ~0xffff) | low
                        if sec[0] <= cand < sec[1]:
                            self.global_addr[k] = cand
                            break

    # ------------------------------------------------------------ declarations
    def fields(self):
        """[(lower name, VarDecl, offset)] for the form's m_XXX variables."""
        out = []
        if self.form is None:
            return out
        for k, d in self.form.fields.items():
            mm = re.fullmatch(r'm_([0-9a-f]+)', k)
            if mm:
                out.append((k, d, int(mm.group(1), 16)))
        return out

    def globals(self):
        out = []
        for mod in self.prog.modules:
            for k, d in list(mod.globals.items()) + ([] if mod is self.form else list(mod.fields.items())):
                if k in self.global_addr:
                    out.append((k, d, self.global_addr[k]))
        return out

    # ------------------------------------------------------------ random starting states
    def gen_var(self, r, w, name, d):
        """Random initial value for a declared variable."""
        t = self.prog.canon_type(d.type.name)
        lay = self.prog.layout(t)
        if d.type.is_array:
            if d.type.bounds:
                bounds = []
                for lo, hi in d.type.bounds:
                    lb = self.prog.const_expr(lo).v if lo is not None else 0
                    bounds.append((lb, self.prog.const_expr(hi).v))
            else:
                dims, mx = self.usage.get(name, (1, 0))
                dims = max(dims, 1)
                ub = max(12, mx + 1) if dims == 1 else max(6, mx + 1)
                if r.random() < 0.05:
                    return V('Array', None)
                bounds = [(0, ub)] * dims
            arr = VBArray(t, bounds, data=[], rec=lay)
            n = 1
            for lb, ub in bounds:
                n *= ub - lb + 1
            arr.data = [w.gen_value(r, t, lay) for _ in range(n)]
            return V('Array', arr)
        if lay is not None:
            return w.gen_value(r, t, lay)
        v = w.gen_value(r, t, None)
        if v.t == 'Object' and r.random() < 0.08:
            return V('Object', None)
        return convert(v, t) if t not in ('Variant',) and v.t != 'Object' else v

    def make_start(self, seed, vp):
        """Starting state shared by both sides: field/global values, arguments, files."""
        r = random.Random(seed)
        w = World(seed, constants=self.consts, focus=self.focus, strings=self.strings, floats=self.floats)
        w.layout_of = self.prog.layout
        w.next_oid = 100
        fields = {k: self.gen_var(r, w, k, d) for k, d, off in self.fields()}
        gl = {k: self.gen_var(r, w, k, d) for k, d, a in self.globals()}
        args = []
        for prm in vp.params:
            t = self.prog.canon_type(prm.type)
            d = P.VarDecl(prm.name, P.TypeRef(prm.type, prm.is_array), 0, 'param')
            args.append(self.gen_var(r, w, prm.name.lower(), d))
        files = {}
        for k, v in FILES.items():
            if r.random() < 0.85:
                files[k] = v
        return fields, gl, args, files, w.objects

    # ------------------------------------------------------------ interpreter side
    def run_source(self, name, vp, start, seed):
        fields, gl, args, files, objs = start
        st = State()
        decls = {k: d for k, d, off in self.fields()}
        for k, v in fields.items():
            d = decls[k]
            t = self.prog.canon_type(d.type.name)
            st.fields[k] = Cell(t, copy_value(v), k, self.prog.layout(t), d.type.is_array)
        gdecl = {k: d for k, d, a in self.globals()}
        for k, v in gl.items():
            d = gdecl[k]
            t = self.prog.canon_type(d.type.name)
            st.globals[k] = Cell(t, copy_value(v), k, self.prog.layout(t), d.type.is_array)
        snap = self.snapshot_source(st)
        w = World(seed, snapshot=snap, max_events=self.max_events, candidates=self.cands,
                  constants=self.consts, files=files, side='source', focus=self.focus, strings=self.strings,
                  floats=self.floats)
        w.layout_of = self.prog.layout

        def apply(writes):
            for n, v in writes:
                c = st.fields.get(n) or st.globals.get(n)
                c.set(v)
        w.apply_writes = apply
        it = Interp(self.prog, st, w, options=self.options)
        cells = []
        call_args = []
        for prm, v in zip(vp.params, args):
            t = self.prog.canon_type(prm.type)
            c = Cell(t, copy_value(v), prm.name, self.prog.layout(t), prm.is_array)
            cells.append(c)
            call_args.append(c if not prm.byval else copy_value(v))
        outcome = None
        ret = None
        where = None
        try:
            ret = it.run_proc(name, call_args)
            outcome = ('return',)
        except VBError as e:
            outcome = ('error', e.num)
            where = getattr(e, 'where', None)
            if os.environ.get('VBCHECK_TRACE'):
                import traceback
                traceback.print_exc()
        except StopRun as e:
            outcome = ('stop', e.why)
        except OutOfBounds as e:
            outcome = ('out-of-bounds', str(e))
        final = snap()
        params = [canon(c.get()) for c in cells]
        return {'log': w.log, 'outcome': outcome, 'ret': canon(ret) if ret is not None else None,
                'final': final, 'params': params, 'world': w, 'where': where}

    def snapshot_source(self, st):
        names = self.watch

        def snap():
            out = {}
            for n in names:
                c = st.fields.get(n) or st.globals.get(n)
                if c is not None:
                    out[n] = canon(c.get())
            return out
        return snap

    # ------------------------------------------------------------ machine side
    def run_machine(self, p, vp, start, seed):
        from machine import RunStop
        from mxruntime import RunStopError
        m = self.m
        fields, gl, args, files, objs = start
        m.reset(p.va)
        mem = m.mem
        method = p.obj is not None and p.obj.kind in ('form', 'class')
        me = None
        decls = {k: (d, off) for k, d, off in self.fields()}
        if self.form is not None:
            me = mem.alloc(0x2000, fill=0xCC)
            mem.w32(me, m.vtable_for(m.me_obj))
            mem.ptr_by_oid[1] = me
            mem.obj_by_ptr[me] = m.me_obj
            m.me_ptr = me
            for k, v in fields.items():
                d, off = decls[k]
                write_var(mem, self.prog, me + off, d, v, k in self.inline_arrays)
            for c in m.form.controls:
                if c.storage and c.name.lower() in self.prog.controls:
                    oid, cls = self.prog.controls[c.name.lower()]
                    mem.w32(me + c.storage, mem.obj_ptr(ObjRef(oid, cls, 'control')))
        gdecl = {k: (d, a) for k, d, a in self.globals()}
        for k, v in gl.items():
            d, a = gdecl[k]
            write_var(mem, self.prog, a, d, v)
        snap = self.snapshot_machine(me)
        w = World(seed, snapshot=snap, max_events=self.max_events, candidates=self.cands,
                  constants=self.consts, files=files, side='machine', focus=self.focus, strings=self.strings,
                  floats=self.floats)
        w.layout_of = self.prog.layout

        def apply(writes):
            for n, v in writes:
                if n in decls:
                    d, off = decls[n]
                    write_var(mem, self.prog, me + off, d, v, n in self.inline_arrays)
                else:
                    d, a = gdecl[n]
                    write_var(mem, self.prog, a, d, v)
        w.apply_writes = apply
        m.world = w
        sig = self.dc.signatures.get(p.va) or {}
        cells = []
        retcell = None
        import struct

        def setup(mm):
            nonlocal retcell
            blob = []
            if method:
                blob.append(struct.pack('<I', me))
            for prm, v in zip(vp.params, args):
                t = self.prog.canon_type(prm.type)
                lay = self.prog.layout(t)
                if prm.byval and not prm.is_array:
                    if t == 'Variant':
                        a = mem.alloc(16)
                        mem.write_variant(a, v)
                        blob.append(mem.read(a, 16))
                    elif t in ('Double', 'Date', 'Currency'):
                        a = mem.alloc(8)
                        mem.write_value(a, t, v)
                        blob.append(mem.read(a, 8))
                    else:
                        a = mem.alloc(8, fill=0)
                        mem.write_value(a, t, v)
                        blob.append(mem.read(a, 4))
                    cells.append(None)
                else:
                    a = mem.alloc(32, fill=0xAB)
                    if t == 'Variant' and not prm.is_array:
                        mem.write_variant(a, v)
                    else:
                        mem.write_value(a, t, v, lay, prm.is_array)
                    blob.append(struct.pack('<I', a))
                    cells.append((a, t, lay, prm.is_array))
            if vp.kind == 'function' and (method or sig.get('retptr')):
                retcell = mem.alloc(32, fill=0xCD)
                t = self.prog.canon_type(vp.rettype)
                if t in ('String', 'Object', 'Variant'):
                    mem.write(retcell, b'\0' * 16)
                blob.append(struct.pack('<I', retcell))
            return blob
        outcome = None
        ret = None
        where = None
        try:
            eax = m.run(p, setup)
            outcome = ('return',)
            if vp.kind == 'function':
                t = self.prog.canon_type(vp.rettype)
                if retcell is not None:
                    ret = mem.read_variant(retcell) if t == 'Variant' else \
                        mem.read_value(retcell, t, self.prog.layout(t))
                elif t in ('Single', 'Double', 'Date'):
                    from machine import SCR_IN
                    ret = V(t, mem.f64(SCR_IN))
                else:
                    import struct as _s
                    if t in ('Integer', 'Boolean'):
                        ret = V(t, _s.unpack('<h', _s.pack('<H', eax & 0xffff))[0])
                    elif t == 'Long':
                        ret = V('Long', _s.unpack('<i', _s.pack('<I', eax))[0])
                    elif t == 'String':
                        ret = V('String', mem.read_bstr(eax))
                    else:
                        ret = V(t, eax)
        except VBError as e:
            outcome = ('error', e.num)
            where = getattr(e, 'where', None)
        except StopRun as e:
            outcome = ('stop', e.why)
        except (RunStop, RunStopError) as e:
            outcome = ('machine-problem', str(e))
        final = snap()
        params = []
        for c in cells:
            if c is None:
                params.append(None)
                continue
            a, t, lay, isarr = c
            try:
                v = mem.read_variant(a) if t == 'Variant' and not isarr else mem.read_value(a, t, lay, isarr)
                params.append(canon(v))
            except VBError as e:
                params.append(('unreadable', str(e)))
        return {'log': w.log, 'outcome': outcome, 'ret': canon(ret) if ret is not None else None,
                'final': final, 'params': params, 'world': w, 'where': where}

    def snapshot_machine(self, me):
        decls = {k: (d, off) for k, d, off in self.fields()}
        gdecl = {k: (d, a) for k, d, a in self.globals()}
        names = self.watch
        mem = self.m.mem

        def snap():
            out = {}
            for n in names:
                if n in decls and me is not None:
                    d, off = decls[n]
                    a = me + off
                elif n in gdecl:
                    d, a = gdecl[n]
                else:
                    continue
                try:
                    out[n] = canon(read_var(mem, self.prog, a, d, n in self.inline_arrays and n in decls))
                except VBError as e:
                    out[n] = ('unreadable', e.msg)
                except Exception:       # unicorn: the pointer leads nowhere
                    out[n] = ('unreadable', 'bad pointer %08X' % mem.u32(a))
            return out
        return snap

    # ------------------------------------------------------------ compare
    def check_proc(self, p):
        name = self.dc.proc_name(p)
        vp = self.prog.procs.get(name.lower())
        res = {'name': name, 'va': p.va, 'runs': 0, 'status': 'pass', 'detail': None}
        if vp is None:
            res['status'] = 'missing'
            res['detail'] = 'no procedure %s in the decompiled source' % name
            return res
        omitted = getattr(self.dc, 'would_exclude', {}).get(p.va)
        if omitted:
            # left out of the published source on purpose (licensing, online scores...): not compared
            res['status'] = 'omitted'
            res['detail'] = 'not in the published source: %s' % omitted
            return res
        if vp.error is not None:
            res['status'] = 'invalid-source'
            res['detail'] = 'line %s: %s' % (vp.error.line, vp.error.msg)
            return res
        refs = referenced_names(vp)
        field_names = {k for k, d, off in self.fields()}
        global_names = {k for k, d, a in self.globals()}
        self.watch = sorted((refs & (field_names | global_names)))
        self.consts = constants_of(vp)
        self.cands = []
        fdecl = {k: d for k, d, off in self.fields()}
        for k in self.watch:
            d = fdecl.get(k)
            if d is None:
                continue
            if d.type.is_array:
                continue
            t = self.prog.canon_type(d.type.name)
            self.cands.append((k, t, self.prog.layout(t)))
        stats = {}
        # coverage of the original code: run until every block was reached, or until `runs` runs in a row
        # found nothing new (at most 10 x runs)
        targets = self.coverage_targets(p)
        self.m.watch_blocks(sorted(targets))
        # values worth trying: constants of the decompiled source and of the machine code itself
        self.consts = constants_of(vp) | self.machine_constants(p)
        self.strings, self.floats = self.machine_literals(p)
        self.focus = False
        self.m_cov = set()
        self.cur_vp = vp
        hints = self.branch_hints(p, targets)
        for b, prs in self.ir_hints(p).items():
            hints.setdefault(b, []).extend(prs)
        general = source_hints(vp)      # array elements / record fields the source compares with constants
        before = self.watched_before(p, targets)
        corpus = []         # (starting state, the blocks its run reached) for states that reached new code
        tried = set()       # (state number, variable, value) already tried for a branch not taken
        best, last_new = -1, 0
        prev_start = prev_cov = None
        i = -1
        while True:
            i += 1
            covered = self.covered_blocks(targets)
            if len(covered) > best:
                if prev_start is not None:
                    corpus.append((prev_start, prev_cov))
                best, last_new = len(covered), i
            todo = []           # (state number, (variable, value))
            if corpus:
                per = []
                for b in sorted(set(targets) - covered):
                    # a state whose run reached the branch that leads to b (the newest such); none yet: b
                    # waits until a run gets there (the entry block's successors have no such branch)
                    pre = before.get(b, set())
                    j = next((k for k in range(len(corpus) - 1, -1, -1) if corpus[k][1] & pre),
                             None if pre else len(corpus) - 1)
                    if j is None:
                        continue
                    per.append([(j, pr) for pr in hints.get(b, []) if (j,) + pr not in tried])
                # round robin: one try for every block not reached yet before a second one for any
                for k in range(max((len(x) for x in per), default=0)):
                    for x in per:
                        if k < len(x) and x[k] not in todo:
                            todo.append(x[k])
                if covered != set(targets):
                    j = len(corpus) - 1
                    for pr in general:
                        if (j,) + pr not in tried and (j, pr) not in todo:
                            todo.append((j, pr))
            if i >= self.runs and best == len(targets) or \
                    i >= 2 * self.runs and i - last_new >= self.runs and not todo or i >= 10 * self.runs:
                break
            res['coverage'] = (best, len(targets))
            res['uncovered'] = sorted(set(targets) - covered)
            seed = '%s:%x:%d' % (self.game, p.va, self.seed * 100000 + i)
            # after the first runs: a third draw integers only from the procedure's constants, a third
            # re-run a state that reached new code with one variable changed, a third aim at a branch
            # not taken yet (setting the variable it tests to the value it compares with)
            mode = (i - self.runs) % 3 if i >= self.runs else None
            self.focus = mode == 0
            start = self.make_start(seed, vp)
            rr = random.Random(seed + ':mutate')
            if mode in (1, 2) and todo:
                # a state that reached the branch, with the variable it tests set as the branch wants
                j, pr = todo[0]
                tried.add((j,) + pr)
                start = self.mutate_start(corpus[j][0], [pr])
            elif mode in (1, 2) and corpus:
                names = [k for k, t, lay in self.cands if lay is None]
                pool = sorted(self.consts, key=str) + list(self.strings)
                if names and pool:
                    start = self.mutate_start(rr.choice(corpus)[0], [(rr.choice(names), rr.choice(pool))
                                                                      for _ in range(rr.choice((1, 1, 2)))])
            prev_start = start
            try:
                src = self.run_source(name, vp, start, seed)
            except Unsupported as e:
                res['status'] = 'unsupported-source'
                res['detail'] = 'line %s: %s' % (e.line, e.msg)
                res['runs'] = i
                return res
            prev_cov = set()
            self.m.cov = prev_cov
            try:
                mx = self.run_machine(p, vp, start, seed)
            finally:
                self.m.cov = None
                self.m_cov |= prev_cov
            res['runs'] = i + 1
            if src['outcome'][0] == 'out-of-bounds':
                stats['out-of-bounds'] = stats.get('out-of-bounds', 0) + 1
                res.setdefault('oob', src['outcome'][1])
                continue
            o = mx['outcome'][0]
            stats[o] = stats.get(o, 0) + 1
            if mx['outcome'][0] == 'machine-problem':
                res['status'] = 'machine-problem'
                res['detail'] = '%s (source outcome %r)' % (mx['outcome'][1], src['outcome'])
                res['seed'] = seed
                if self.verbose:
                    res['detail'] += '\n' + self.show_logs(src, mx)
                return res
            diff = self.compare(src, mx)
            if diff:
                res['status'] = 'MISMATCH'
                res['detail'] = diff
                res['seed'] = seed
                if self.verbose:
                    res['detail'] += '\n' + self.show_logs(src, mx)
                return res
        covered = self.covered_blocks(targets)
        res['coverage'] = (len(covered), len(targets))
        res['uncovered'] = sorted(set(targets) - covered)
        res['stats'] = stats
        return res

    def branch_hints(self, p, targets):
        """{block: [(variable, value)]}: for each branch that tests a form variable against a constant (also
        through a register it was loaded into, after add/sub/dec, or as a jump-table index), the values
        that send it either way; both successors get them."""
        try:
            cfg = self.dc.lift(p).cfg
        except Exception:
            return {}
        names = {off: k for k, d, off in self.fields()}
        im = self.dc.im
        out = {}
        state_in = {}

        def r32(reg):
            return REG32.get(reg, reg)

        def sval(o, size):
            v = o.imm & 0xffffffff
            if size == 2:
                return ((v & 0xffff) ^ 0x8000) - 0x8000
            if size == 1:
                return ((v & 0xff) ^ 0x80) - 0x80
            return v - (1 << 32) if v & 0x80000000 else v

        def field_of(o):
            if o.kind == 'mem' and o.base not in (None, 'esp', 'ebp') and o.index is None and o.disp in names:
                return names[o.disp]
            return None

        for start in sorted(cfg.blocks):
            b = cfg.blocks[start]
            st = dict(state_in.get(start, {}))     # register -> (variable, value added to it)
            pairs = []
            fpu_field = None
            loaded, strings = set(), set()
            for ins in b.insns:
                mn, ops = ins.mn, ins.ops
                for o in ops:
                    f = field_of(o)
                    if f:
                        loaded.add(f)
                    if o.kind == 'imm' and 0x400000 <= (o.imm & 0xffffffff) < 0x10000000:
                        lit = im.bstr(o.imm & 0xffffffff)
                        if lit is not None:
                            strings.add(lit)
                if mn in ('mov', 'movsx', 'movzx') and ops[0].kind == 'reg':
                    d, src = r32(ops[0].reg), ops[1]
                    f = field_of(src)
                    if f:
                        st[d] = (f, 0)
                    elif src.kind == 'reg' and r32(src.reg) in st:
                        st[d] = st[r32(src.reg)]
                    else:
                        st.pop(d, None)
                    continue
                if mn in ('add', 'sub') and ops[0].kind == 'reg' and r32(ops[0].reg) in st and ops[1].kind == 'imm':
                    f, k = st[r32(ops[0].reg)]
                    v = sval(ops[1], ops[0].size)
                    k = k + v if mn == 'add' else k - v
                    st[r32(ops[0].reg)] = (f, k)
                    pairs += [(f, -k), (f, -k + 1)]
                    continue
                if mn in ('inc', 'dec') and ops and ops[0].kind == 'reg' and r32(ops[0].reg) in st:
                    f, k = st[r32(ops[0].reg)]
                    k += 1 if mn == 'inc' else -1
                    st[r32(ops[0].reg)] = (f, k)
                    pairs += [(f, -k), (f, -k + 1), (f, -k - 1)]
                    continue
                if mn == 'cmp' and len(ops) == 2 and ops[1].kind == 'imm':
                    a = ops[0]
                    if a.kind == 'reg' and r32(a.reg) in st:
                        f, k = st[r32(a.reg)]
                    else:
                        f, k = field_of(a), 0
                    if f:
                        v = sval(ops[1], a.size) - k
                        pairs += [(f, v), (f, v + 1), (f, v - 1)]
                    continue
                if mn == 'test' and ops[0].kind == 'reg' and r32(ops[0].reg) in st:
                    f, k = st[r32(ops[0].reg)]
                    pairs += [(f, -k), (f, 1 - k), (f, -1 - k)]
                    continue
                if mn in ('fld', 'fild'):
                    fpu_field = field_of(ops[0]) or fpu_field
                    continue
                if mn.startswith('fcom') and fpu_field and ops and ops[0].kind == 'mem' and ops[0].base is None \
                        and ops[0].index is None and ops[0].size in (4, 8) and im.valid(ops[0].disp & 0xffffffff, 4):
                    a = ops[0].disp & 0xffffffff
                    fv = im.f32(a) if ops[0].size == 4 else im.f64(a)
                    if fv == fv and abs(fv) < 1e12:
                        pairs += [(fpu_field, float(fv)), (fpu_field, float(fv) + 0.5), (fpu_field, float(fv) - 0.5)]
                    continue
                if mn == 'call':
                    for r in ('eax', 'ecx', 'edx'):
                        st.pop(r, None)
                    continue
                if ops and ops[0].kind == 'reg':
                    st.pop(r32(ops[0].reg), None)
            pairs += [(f, lit) for f in loaded for lit in strings]
            if b.term == 'switch' and b.switch:
                idx, tlist = b.switch
                idx = getattr(idx, 'reg', idx)
                ent = st.get(r32(idx)) if isinstance(idx, str) else None
                if ent:
                    f, k = ent
                    for i, tg in enumerate(tlist):
                        out.setdefault(tg, []).append((f, i - k))
            if pairs:
                for s2 in b.succs:
                    out.setdefault(s2, []).extend(pairs)
            for s2 in b.succs:
                if s2 in cfg.blocks and len(cfg.blocks[s2].preds) <= 1 and s2 not in state_in:
                    state_in[s2] = dict(st)
        return out

    def ir_hints(self, p):
        """Like branch_hints, from the lifted conditions (they also see the procedure's parameters, named
        '#<index>'). Only used to choose test inputs: a mistake here costs coverage, not correctness."""
        from ir import Var as IVar, Deref as IDeref, Const as IConst, Str as IStr, FConst as IFConst, Bin as IBin
        try:
            L = self.dc.lift(p)
        except Exception:
            return {}
        first = 4 if (p.obj is not None and p.obj.kind in ('form', 'class')) else 0
        out = {}

        def leaves_of(e):
            names, values = set(), set()
            for x in e.walk():
                if isinstance(x, IVar) and x.kind == 'field':
                    names.add('m_%x' % x.off)
                elif isinstance(x, IDeref) and isinstance(x.base, IVar) and x.base.kind == 'arg' and x.off == 0:
                    names.add('#%d' % ((x.base.off - first) // 4))
                elif isinstance(x, IVar) and x.kind == 'arg' and x.off >= first:
                    names.add('#%d' % ((x.off - first) // 4))
                elif isinstance(x, IConst) and isinstance(x.v, int) and abs(x.v) < 100000:
                    values |= {x.v, x.v + 1, x.v - 1}
                elif isinstance(x, IStr):
                    values.add(x.s)
                elif isinstance(x, IFConst):
                    values |= {float(x.v), float(x.v) + 0.5, float(x.v) - 0.5}
            return names, values
        for b, t in L.block_term.items():
            if t[0] == 'cond':
                names, values = leaves_of(t[1])
                values |= {0, 1, -1}
                prs = [(n, v) for n in names for v in values]
                for s2 in (t[2], t[3]):
                    out.setdefault(s2, []).extend(prs)
            elif t[0] == 'switch':
                idx, tl = t[1], t[2]
                k = 0
                if isinstance(idx, IBin) and idx.op in ('+', '-') and isinstance(idx.b, IConst):
                    k = idx.b.v if idx.op == '+' else -idx.b.v
                    idx = idx.a
                names, _ = leaves_of(idx)
                for i, tg in enumerate(tl):
                    if tg is not None:
                        out.setdefault(tg, []).extend((n, i - k) for n in names)
        return out

    def mutate_start(self, base, assignments):
        """A copy of a starting state with some form variables (or '#<index>' parameters) set:
        [(name, value)]."""
        fields, gl, args, files, objs = base
        fields = {k: copy_value(v) for k, v in fields.items()}
        args = [copy_value(a) for a in args]
        decls = {k: d for k, d, off in self.fields()}
        params = getattr(self, 'cur_vp', None).params if getattr(self, 'cur_vp', None) else []
        for name, v in assignments:
            if isinstance(name, tuple):
                self.set_path(fields, name, v)
                continue
            if name.startswith('#'):
                k = int(name[1:])
                if not (0 <= k < len(params)) or params[k].is_array:
                    continue
                t = self.prog.canon_type(params[k].type)
                nv = self.value_for(t, v)
                if nv is not None:
                    args[k] = nv
                continue
            d = decls.get(name)
            if d is None or d.type.is_array:
                continue
            t = self.prog.canon_type(d.type.name)
            nv = self.value_for(t, v)
            if nv is not None:
                fields[name] = nv
        return fields, gl, args, files, objs

    def set_path(self, fields, path, v):
        """Set m_XX(constant indexes).field (or m_XX.field) of a starting state to test value v."""
        base, idxs, member = path
        cur = fields.get(base)
        if cur is None:
            return
        if idxs == '*':
            if cur.t != 'Array' or cur.v is None:
                return
            for k in range(len(cur.v.data)):
                self.set_element(cur.v, k, member, v)
            return
        if idxs is not None:
            if cur.t != 'Array' or cur.v is None:
                return
            arr = cur.v
            try:
                k = arr.offset(list(idxs))
            except VBError:
                return
            if member is None:
                nv = self.value_for(arr.etype, v)
                if nv is not None:
                    arr.data[k] = nv
                return
            cur = arr.data[k]
        if member is None or cur.t != 'Record':
            return
        f = cur.v.layout.by_name.get(member)
        if f is None or f[4]:
            return
        nv = self.value_for(self.prog.canon_type(f[1]), v)
        if nv is not None:
            cur.v.vals[member] = nv

    def set_element(self, arr, k, member, v):
        if member is None:
            nv = self.value_for(arr.etype, v)
            if nv is not None:
                arr.data[k] = nv
            return
        el = arr.data[k]
        if el.t != 'Record':
            return
        f = el.v.layout.by_name.get(member)
        if f is None or f[4]:
            return
        nv = self.value_for(self.prog.canon_type(f[1]), v)
        if nv is not None:
            el.v.vals[member] = nv

    def value_for(self, t, v):
        """A test value v (int, float or str) as a value of VB type t (None if it does not fit)."""
        if t not in ('Byte', 'Boolean', 'Integer', 'Long', 'Single', 'Double', 'Currency', 'Date', 'String',
                     'Variant'):
            return None
        try:
            if isinstance(v, str):
                if t not in ('String', 'Variant'):
                    return None
                return V('String', v, t == 'Variant')
            if isinstance(v, float):
                return convert(V('Double', v), t) if t != 'Variant' else V('Double', v, True)
            return convert(V('Long', v), t) if t != 'Variant' else V('Long', v, True)
        except VBError:
            return None

    def machine_literals(self, p):
        """String literals and floating-point constants the machine code uses."""
        strings, floats = set(), set()
        try:
            cfg = self.dc.lift(p).cfg
        except Exception:
            return [], []
        im = self.dc.im
        for b in cfg.blocks.values():
            for ins in b.insns:
                for o in ins.ops:
                    if o.kind == 'imm' and 0x400000 <= (o.imm & 0xffffffff) < 0x10000000:
                        s = im.bstr(o.imm & 0xffffffff)
                        if s is not None and len(s) < 200:
                            strings.add(s)
                    if o.kind == 'mem' and o.base is None and o.index is None and ins.mn.startswith('f') and \
                            o.size in (4, 8) and im.valid(o.disp & 0xffffffff, o.size):
                        a = o.disp & 0xffffffff
                        v = im.f32(a) if o.size == 4 else im.f64(a)
                        if v == v and abs(v) < 1e12:
                            floats.add(float(v))
        return sorted(strings), sorted(floats)

    def machine_constants(self, p):
        """Integers the machine code compares with or offsets by (and the case range of jump tables)."""
        out = set()
        try:
            cfg = self.dc.lift(p).cfg
        except Exception:
            return out
        for b in cfg.blocks.values():
            offset = None
            for ins in b.insns:
                if len(ins.ops) == 2 and ins.ops[1].kind == 'imm' and ins.mn in ('cmp', 'add', 'sub'):
                    v = ins.ops[1].imm & 0xffffffff
                    v = v - (1 << 32) if v & 0x80000000 else v
                    if ins.ops[0].kind == 'mem' and ins.ops[0].size == 2 or getattr(ins.ops[0], 'size', 4) == 2:
                        v = ((v & 0xffff) ^ 0x8000) - 0x8000
                    if abs(v) > 100000:
                        continue
                    if ins.mn == 'cmp':
                        out |= {v - 1, v, v + 1}
                        if offset is not None:
                            out |= {v - offset, v - offset + 1, v - offset - 1}
                        if offset is not None and 0 <= v <= 64:
                            out |= {k - offset for k in range(v + 1)}      # x + offset in 0..v
                    else:
                        offset = (offset or 0) + (v if ins.mn == 'add' else -v)
                        out |= {-offset, -offset + 1}
                elif ins.mn in ('dec', 'inc') and len(ins.ops) == 1 and ins.ops[0].kind == 'reg':
                    # dec/je chains: Select Case 1, 2, 3...
                    offset = (offset or 0) + (1 if ins.mn == 'inc' else -1)
                    out |= {-offset, -offset + 1}
                elif ins.mn == 'mov' and ins.ops[0].kind == 'reg':
                    offset = None
        return out

    def coverage_targets(self, p):
        """Start addresses of the procedure's basic blocks, except those that only raise a runtime error and
        the failure branches of COM calls (__vbaHresultCheckObj: the world's objects never fail)."""
        try:
            cfg = self.dc.lift(p).cfg
        except Exception:
            return {}
        out = {}
        for b in cfg.reachable():
            if b not in cfg.blocks or b >= 0x70000000:
                continue
            ins = cfg.blocks[b].insns
            if any(x.mn == 'call' and cfg._import_name(x) == '__vbaHresultCheckObj' for x in ins):
                continue
            # the same check through a register: push vtable offset / push IID / push obj / push hr / call reg
            calls = [k for k, x in enumerate(ins) if x.mn == 'call']
            if calls and ins[calls[-1]].ops[0].kind == 'reg' and calls[-1] >= 4 and \
                    all(x.mn == 'push' for x in ins[calls[-1] - 4:calls[-1]]) and \
                    ins[calls[-1] - 4].ops[0].kind == 'imm' and ins[calls[-1] - 4].ops[0].imm < 0x1000 and \
                    ins[calls[-1] - 3].ops[0].kind == 'imm':
                continue
            out[b] = cfg.blocks[b].end
        return out

    def watched_before(self, p, targets):
        """block -> the watched blocks (coverage targets) a run passes just before reaching it: its
        predecessors, looking further back through blocks that are not watched."""
        try:
            cfg = self.dc.lift(p).cfg
        except Exception:
            return {}
        out = {}
        for b in targets:
            found, seen = set(), {b}
            work = list(cfg.blocks[b].preds) if b in cfg.blocks else []
            while work:
                x = work.pop()
                if x in seen:
                    continue
                seen.add(x)
                if x in targets:
                    found.add(x)
                elif x in cfg.blocks:
                    work.extend(cfg.blocks[x].preds)
            out[b] = found
        return out

    def covered_blocks(self, targets):
        return set(targets) & self.m_cov

    def compare(self, s, m):
        ls, lm = s['log'], m['log']
        for i in range(min(len(ls), len(lm))):
            a, b = ls[i], lm[i]
            if a.key != b.key:
                return 'event %d differs:\n    source:  %r\n    machine: %r%s' % (
                    i, a.key, b.key, self.context(ls, lm, i))
            if a.state != b.state:
                return 'event %d (%r): state differs before it:\n%s' % (i, a.key, self.state_diff(a.state, b.state))
        if len(ls) != len(lm):
            k = min(len(ls), len(lm))
            more = ls[k:k + 3] if len(ls) > len(lm) else lm[k:k + 3]
            side = 'source' if len(ls) > len(lm) else 'machine'
            return 'the %s side does more: %r (outcomes: source %r, machine %r)' % (
                side, more, s['outcome'], m['outcome'])
        so, mo = s['outcome'], m['outcome']
        if so != mo:
            if so[0] == 'stop' and mo[0] == 'stop':
                return None
            return 'different ending: source %r, machine %r' % (so, mo)
        if so[0] == 'stop' and so[1] == 'event budget':
            return None
        if s['final'] != m['final']:
            return 'final state differs:\n%s' % self.state_diff(s['final'], m['final'])
        if s['ret'] != m['ret']:
            return 'return value differs: source %r, machine %r' % (s['ret'], m['ret'])
        if s['params'] != m['params']:
            for k, (a, b) in enumerate(zip(s['params'], m['params'])):
                if b is not None and a != b:
                    return 'ByRef parameter %d differs at the end: source %r, machine %r' % (k + 1, a, b)
        return None

    def show_logs(self, s, m):
        out = ['source outcome %r, machine outcome %r' % (s['outcome'], m['outcome'])]
        for side, r in (('source', s), ('machine', m)):
            if r.get('where'):
                out.append('    %s error raised at %s' % (side, r['where']))
        for i in range(max(len(s['log']), len(m['log']))):
            a = (s['log'][i].key, s['log'][i].resp) if i < len(s['log']) else '-'
            b = (m['log'][i].key, m['log'][i].resp) if i < len(m['log']) else '-'
            out.append('  %3d %s %r\n      %s %r' % (i, ' ' if a == b else '*', a, ' ', b))
        return '\n'.join(out)

    def context(self, ls, lm, i):
        lo = max(0, i - 3)
        out = '\n    before: ' + ' | '.join(repr(e.key) for e in ls[lo:i])
        return out

    def state_diff(self, a, b):
        lines = []
        for k in sorted(set(a or {}) | set(b or {})):
            x, y = (a or {}).get(k), (b or {}).get(k)
            if x != y:
                leaves = []
                leaf_diffs(x, y, k, leaves)
                if len(leaves) == 1 and leaves[0][0] == k:
                    lines.append('    %s: source %s, machine %s' % (k, short(x), short(y)))
                else:
                    for path, u, w in leaves[:6]:
                        lines.append('    %s: source %s, machine %s' % (path, short(u), short(w)))
                    if len(leaves) > 6:
                        lines.append('    (%d more differences in %s)' % (len(leaves) - 6, k))
        return '\n'.join(lines[:24])


def leaf_diffs(x, y, path, out):
    """The innermost places where two canonical values differ (array elements, record fields)."""
    if x == y:
        return
    if isinstance(x, tuple) and isinstance(y, tuple) and len(x) == len(y) and len(x) > 0:
        if x[0] == 'Record' and len(x) == 3 and x[1] == y[1]:
            fx, fy = dict(x[2]), dict(y[2])
            for f in fx:
                leaf_diffs(fx[f], fy.get(f), '%s.%s' % (path, f), out)
            return
        if x[0] == 'Array' and len(x) == 4 and x[2] == y[2] and len(x[3]) == len(y[3]):
            for i, (u, w) in enumerate(zip(x[3], y[3])):
                leaf_diffs(u, w, '%s[%d]' % (path, i), out)
            return
    out.append((path, x, y))


def short(v, n=200):
    s = repr(v)
    return s if len(s) <= n else s[:n] + '...'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('game')
    ap.add_argument('--runs', type=int, default=30)
    ap.add_argument('--proc', action='append')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--events', type=int, default=200)
    ap.add_argument('--out')
    ap.add_argument('--show', action='store_true', help='print both event logs of a mismatch')
    ap.add_argument('--project', help='app name in the library (apps/), or a project file (default: the one '
                                      'that has this program)')
    a = ap.parse_args()
    t0 = time.time()
    proj = projects.find(a.project, a.game)
    game, _ = proj.game(a.game)
    use_project(proj)
    outdir = a.out or tempfile.mkdtemp(prefix='vbcheck_')
    dc, prog = decompile(proj, game, outdir)
    print('decompiled %s into %s (%.0fs)' % (game, outdir, time.time() - t0))
    recorded = proj.recorded_omissions(game)
    if recorded and set(recorded.get('procedures', {})) != set(dc.omissions['procedures']):
        print('NOTE: the procedures the omission rules leave out differ from the record in %s (see '
              'build_sourcecode.py / main.py --game for the details)' % os.path.basename(proj.path))
    ck = Checker(game, dc, prog, runs=a.runs, seed=a.seed, max_events=a.events, verbose=a.show)
    want = {x.lower() for x in a.proc} if a.proc else None
    counts = {}
    for va in sorted(dc.proj.procs):
        p = dc.proj.procs[va]
        name = dc.proc_name(p)
        if want and name.lower() not in want:
            continue
        r = ck.check_proc(p)
        counts[r['status']] = counts.get(r['status'], 0) + 1
        line = '%-28s %-18s runs %-3d' % (name, r['status'], r['runs'])
        if r.get('coverage'):
            line += ' blocks %d/%d' % r['coverage']
        if r.get('stats'):
            line += ' ' + ' '.join('%s:%d' % kv for kv in sorted(r['stats'].items()))
        print(line)
        if r['detail']:
            print('    ' + r['detail'].replace('\n', '\n    '))
        elif r.get('uncovered') and a.show:
            print('    never reached: ' + ' '.join('%x' % b for b in r['uncovered']))
        sys.stdout.flush()
    print('summary:', counts, '(%.0fs)' % (time.time() - t0))


if __name__ == '__main__':
    main()
