"""Interpreter for the decompiled VB6 source (one procedure at a time, against a World).

The procedure body is flattened into a list of steps (conditional jumps instead of nested blocks)
so that GoTo between any two labels works as in VB. Every effect outside the procedure goes through
the World (see world.py), which is what the machine-code side uses too.

Things the interpreter refuses (raising Unsupported) are reported by the checker as decompilation
defects: an undeclared name, a call to something that does not exist, a statement it cannot place.
"""
import math

import vbparse as P
from vbvalues import (V, VBError, ObjRef, VBArray, VBRecord, UdtLayout, EMPTY, NULL, MISSING, NOTHING,
                      TRUE, FALSE, boolv, convert, to_str, to_bool, to_int, to_float, str_v, binop, unop,
                      canon, default_value, copy_value, num_to_str, val_of, str_to_num, f32, round_half_even,
                      date_parts, INTEGRAL, NUMERIC, VT, str_compare)
from world import StopRun, print_text, write_text, input_value


class OutOfBounds(Exception):
    """An array index outside the bounds in a program compiled without bounds checks: the machine code
    reads/writes whatever memory is there, so the run cannot be compared from this point."""


class Unsupported(Exception):
    """The source does something the checker cannot give a meaning to (a decompilation defect)."""

    def __init__(self, msg, line=None):
        self.msg = msg
        self.line = line
        super().__init__('line %s: %s' % (line, msg))


VB_CONSTANTS = {
    'vbcrlf': V('String', '\r\n'), 'vbcr': V('String', '\r'), 'vblf': V('String', '\n'),
    'vbtab': V('String', '\t'), 'vbnullchar': V('String', '\0'), 'vbnullstring': V('String', ''),
    'vbnewline': V('String', '\r\n'),
    'vbcritical': V('Long', 16), 'vbquestion': V('Long', 32), 'vbexclamation': V('Long', 48),
    'vbinformation': V('Long', 64), 'vbokonly': V('Long', 0), 'vbokcancel': V('Long', 1),
    'vbyesno': V('Long', 4), 'vbyesnocancel': V('Long', 3), 'vbdefaultbutton2': V('Long', 256),
    'vbtextcompare': V('Long', 1), 'vbbinarycompare': V('Long', 0),
    'vbyes': V('Long', 6), 'vbno': V('Long', 7), 'vbok': V('Long', 1), 'vbcancel': V('Long', 2),
}
GLOBAL_OBJECTS = {'app': '_App', 'clipboard': '_Clipboard', 'screen': '_Screen', 'printer': '_Printer'}


# ----------------------------------------------------------------------------- references (lvalues)
class Ref:
    def get(self):
        raise NotImplementedError

    def set(self, v):
        raise NotImplementedError


class Cell(Ref):
    """A variable: declared type t (None = take any), current value."""
    __slots__ = ('t', 'val', 'name', 'layout', 'is_array')

    def __init__(self, t, val, name='', layout=None, is_array=False):
        self.t = t
        self.val = val
        self.name = name
        self.layout = layout
        self.is_array = is_array

    def get(self):
        return self.val

    def set(self, v):
        self.val = store_convert(v, self.t, self.layout, self.is_array)


class ElemRef(Ref):
    __slots__ = ('arr', 'k')

    def __init__(self, arr, k):
        self.arr = arr
        self.k = k

    def get(self):
        return self.arr.data[self.k]

    def set(self, v):
        self.arr.data[self.k] = store_convert(v, self.arr.etype, self.arr.rec, False)


class FieldRef(Ref):
    __slots__ = ('rec', 'key', 't', 'layout')

    def __init__(self, rec, key, t, layout):
        self.rec = rec
        self.key = key
        self.t = t
        self.layout = layout

    def get(self):
        return self.rec.vals[self.key]

    def set(self, v):
        self.rec.vals[self.key] = store_convert(v, self.t, self.layout, False)


# the program's compile options (set by Interp): without overflow checks, assigning an integer value
# to a narrower integer variable keeps the low bits (the compiled code just stores them)
OPTIONS = {'overflow_checks': True, 'fp_checks': True, 'bounds_checks': True}


def store_convert(v, t, layout, is_array):
    """Value stored into a variable of declared type t."""
    # without overflow checks, an integer value stored into a narrower integer variable is cut to size
    # ("values assigned to variables of these types" are not checked either); an explicit CInt still
    # calls the runtime's __vbaI2I4, which checks
    if not OPTIONS['overflow_checks'] and t in INTEGRAL and t != 'Boolean' and v.t in INTEGRAL and \
            not v.variant:
        from vbvalues import wrap_int
        return V(t, wrap_int(t, v.v))
    if is_array:
        if v.t != 'Array':
            raise VBError(13, 'assigning a non-array to an array')
        return V('Array', v.v)
    if t is None or t == 'Variant':
        if v.t == 'Missing':
            raise VBError(448)
        if v.t == 'Record':
            return V('Record', v.v.copy(), True)
        if v.t == 'Single' and not v.variant:
            return V('Single', f32(v.v), True)
        return v if v.variant else V(v.t, v.v, True)
    if layout is not None:
        if v.t != 'Record':
            raise VBError(13, 'assigning %s to a record' % v.t)
        return V('Record', v.v.copy())
    return convert(v, t)


# ----------------------------------------------------------------------------- program knowledge
# DirectInput SetProperty / GetProperty take their value `As Any` (Void* in the type library): the property
# name says which record is passed (DIPROPLONG unless listed)
DIPROP_RECORDS = {'DIPROP_RANGE': 'DIPROPRANGE', 'DIPROP_GUIDANDPATH': 'DIPROPGUIDANDPATH',
                  'DIPROP_INSTANCENAME': 'DIPROPSTRING', 'DIPROP_PRODUCTNAME': 'DIPROPSTRING',
                  'DIPROP_KEYNAME': 'DIPROPSTRING', 'DIPROP_CPOINTS': 'DIPROPCPOINTS'}


def any_param_type(member, args):
    """Concrete type for a `ByRef As Any` (Void*) parameter, from the arguments before it."""
    if member in ('SetProperty', 'GetProperty') and args and args[0].t == 'String':
        return DIPROP_RECORDS.get(str(args[0].v).upper(), 'DIPROPLONG') + '*'
    return None


class Program:
    """Everything the interpreter knows about the decompiled project."""

    def __init__(self, modules, tlb, form_name=None):
        self.modules = modules
        self.tlb = tlb
        self.form = None
        for m in modules:
            if m.kind == 'form' and (form_name is None or m.name == form_name):
                self.form = m
        self.procs = {}
        self.proc_module = {}
        for m in modules:
            for k, p in m.procs.items():
                self.procs.setdefault(k, p)
                self.proc_module.setdefault(k, m)
        self.declares = {}
        for m in modules:
            self.declares.update(m.declares)
        self.types = {}
        for m in modules:
            for k, t in m.types.items():
                self.types[k] = t
        self.layouts = {}
        self.enum_values = {}
        for name, e in tlb.enums.items():
            for v in e.vars:
                self.enum_values.setdefault(v['name'].lower(), v['value'])
        self.option_compare_text = any(m.option_compare_text for m in modules)
        self.controls = {}
        if self.form is not None:
            for i, (name, cls) in enumerate(self.form.controls):
                self.controls[name.lower()] = (2 + i, cls)

    # ------------------------------------------------------------ types
    def layout(self, tname):
        """UdtLayout for a record type name (source Type or typelib record), else None."""
        if tname is None:
            return None
        k = tname.lower()
        if k in self.layouts:
            return self.layouts[k]
        lay = None
        if k in self.types:
            lay = self.source_layout(self.types[k])
        else:
            for rn in self.tlb.records:
                if rn.lower() == k:
                    lay = self.tlb_layout(rn)
                    break
        self.layouts[k] = lay
        return lay

    def type_size(self, t):
        sizes = {'Integer': 2, 'Boolean': 2, 'Long': 4, 'Single': 4, 'Double': 8, 'Date': 8, 'Currency': 8,
                 'String': 4, 'Byte': 1, 'Variant': 16}
        if t in sizes:
            return sizes[t], min(sizes[t], 4) if t != 'Variant' else 4
        lay = self.layout(t)
        if lay is not None:
            return lay.size, 4
        return 4, 4

    def source_layout(self, ut):
        off = 0
        fields = []
        for name, tr in ut.fields:
            t = self.canon_type(tr.name)
            size, align = self.type_size(t)
            count = 0
            if tr.bounds:
                lo = to_int(self.const_expr(tr.bounds[0][0])) if tr.bounds[0][0] is not None else 0
                hi = to_int(self.const_expr(tr.bounds[0][1]))
                count = hi - lo + 1
            off = (off + align - 1) // align * align
            fields.append((name, t, off, size, count))
            off += size * (count or 1)
        size = (off + 3) // 4 * 4
        return UdtLayout(ut.name, fields, size)

    def tlb_layout(self, rn):
        fields, size, align = self.tlb.layout(rn)
        out = []
        for name, off, t in fields:
            count = 0
            if t.startswith('CArray:'):
                _, elem, n, lb = t.split(':')
                t = elem
                count = int(n)
            vt = self.canon_type(t)
            out.append((name, vt, off, self.tlb.type_size(t), count))
        return UdtLayout(rn, out, size)

    def canon_type(self, t):
        base = {'integer': 'Integer', 'long': 'Long', 'single': 'Single', 'double': 'Double', 'string': 'String',
                'boolean': 'Boolean', 'byte': 'Byte', 'variant': 'Variant', 'date': 'Date',
                'currency': 'Currency', 'object': 'Object'}
        if t is None:
            return 'Variant'
        lt = t.lower()
        if lt in base:
            return base[lt]
        it = self.tlb.by_name.get(t)
        if it is not None and it.kind == 'enum':
            return 'Long'
        if it is not None and it.kind == 'alias' and it.alias:
            return self.canon_type(it.alias)
        return t

    def const_expr(self, e):
        if isinstance(e, P.Num):
            return V('Long', e.v) if not e.is_float else V('Double', e.v)
        if isinstance(e, P.UnOp) and e.op == 'neg':
            return unop('neg', self.const_expr(e.a))
        raise Unsupported('array bound is not a constant', getattr(e, 'line', None))

    def iface_of(self, tname):
        """typelib interface for an object type name (coclass -> its default interface)."""
        if tname is None:
            return None
        it = self.tlb.by_name.get(tname) or self.tlb.by_name.get('_' + tname)
        if it is not None and it.kind == 'coclass':
            it = self.tlb.default_iface(it.guid) or it
        return it


# ----------------------------------------------------------------------------- flattening
class Op:
    __slots__ = ('kind', 'a', 'b', 'c', 'line')

    def __init__(self, kind, a=None, b=None, c=None, line=None):
        self.kind, self.a, self.b, self.c, self.line = kind, a, b, c, line


class Flattener:
    def __init__(self, proc):
        self.proc = proc
        self.ops = []
        self.labels = {}
        self.fixups = []
        self.nfor = 0
        self.nsel = 0

    def here(self):
        return len(self.ops)

    def emit(self, kind, a=None, b=None, c=None, line=None):
        self.ops.append(Op(kind, a, b, c, line))
        return len(self.ops) - 1

    def run(self):
        self.block(self.proc.body, None, None)
        self.emit('return')
        for i, lab in self.fixups:
            if lab.lower() not in self.labels:
                raise Unsupported('GoTo to unknown label %s' % lab, self.ops[i].line)
            self.ops[i].a = self.labels[lab.lower()]
        return self.ops, self.labels

    def block(self, stmts, do_exit, for_exit):
        for s in stmts:
            self.stmt(s, do_exit, for_exit)

    def stmt(self, s, do_exit, for_exit):
        if isinstance(s, P.Label):
            if s.name.lower() in self.labels:
                raise Unsupported('label %s defined twice' % s.name, s.line)
            self.labels[s.name.lower()] = self.here()
            return
        if isinstance(s, P.GoTo):
            i = self.emit('goto', None, line=s.line)
            self.fixups.append((i, s.label))
            return
        if isinstance(s, P.Exit):
            if s.what in ('sub', 'function'):
                self.emit('return', line=s.line)
            elif s.what == 'do':
                if do_exit is None:
                    raise Unsupported('Exit Do outside a Do loop', s.line)
                do_exit.append(self.emit('goto', None, line=s.line))
            elif s.what == 'for':
                if for_exit is None:
                    raise Unsupported('Exit For outside a For loop', s.line)
                for_exit.append(self.emit('goto', None, line=s.line))
            return
        if isinstance(s, P.If):
            ends = []
            for cond, body in s.arms:
                j = self.emit('jf', cond, None, line=s.line)
                self.block(body, do_exit, for_exit)
                ends.append(self.emit('goto', None, line=s.line))
                self.ops[j].b = self.here()
            if s.els is not None:
                self.block(s.els, do_exit, for_exit)
            for e in ends:
                self.ops[e].a = self.here()
            return
        if isinstance(s, P.DoLoop):
            exits = []
            top = self.here()
            if s.pre is not None:
                kind, cond = s.pre
                j = self.emit('jf' if kind == 'while' else 'jt', cond, None, line=s.line)
                exits.append(('b', j))
            self.block(s.body, exits, for_exit)
            if s.post is not None:
                kind, cond = s.post
                # Loop While c: jump back while true; Loop Until c: jump back while false
                self.emit('jt' if kind == 'while' else 'jf', cond, top, line=s.line)
            else:
                self.emit('goto', top, line=s.line)
            end = self.here()
            for x in exits:
                if isinstance(x, tuple):
                    self.ops[x[1]].b = end
                else:
                    self.ops[x].a = end
            return
        if isinstance(s, P.For):
            self.nfor += 1
            fid = self.nfor
            exits = []
            init = self.emit('for_init', s, fid, None, line=s.line)
            top = self.here()
            test = self.emit('for_test', s, fid, None, line=s.line)
            self.block(s.body, do_exit, exits)
            self.emit('for_next', s, fid, top, line=s.line)
            end = self.here()
            self.ops[test].c = end
            for x in exits:
                self.ops[x].a = end
            return
        if isinstance(s, P.Select):
            self.nsel += 1
            sid = self.nsel
            self.emit('sel_init', s.subject, sid, line=s.line)
            ends = []
            for vals, body in s.cases:
                j = self.emit('case', vals, sid, None, line=s.line)
                self.block(body, do_exit, for_exit)
                ends.append(self.emit('goto', None, line=s.line))
                self.ops[j].c = self.here()
            if s.els is not None:
                self.block(s.els, do_exit, for_exit)
            for e in ends:
                self.ops[e].a = self.here()
            return
        self.emit('stmt', s, line=s.line)


# ----------------------------------------------------------------------------- the interpreter
class State:
    """Form-level and global variables (shared by all procedures of the run)."""

    def __init__(self):
        self.fields = {}      # lower name -> Cell
        self.globals = {}     # lower name -> Cell
        self.err_number = 0
        self.err_desc = ''


class Frame:
    def __init__(self, proc):
        self.proc = proc
        self.vars = {}        # lower name -> Ref (locals, params, function result)
        self.result = None
        self.fors = {}
        self.sels = {}
        self.on_error = None  # None | ('goto', label) | 'resume_next'
        self.in_handler = False
        self.err_resume = None


class Interp:
    def __init__(self, program, state, world, max_steps=200000, options=None):
        self.prog = program
        self.opts = dict(options or {'overflow_checks': True, 'fp_checks': True, 'bounds_checks': True})
        OPTIONS.update(self.opts)
        import vbvalues
        vbvalues.FP_CHECKS[0] = bool(self.opts.get('fp_checks', True))
        self.state = state
        self.world = world
        self.max_steps = max_steps
        self.steps = 0
        self.text_cmp = program.option_compare_text
        self.objs = {}            # global object name -> V
        self.me = V('Object', ObjRef(1, 'Form', 'me'))
        self.flat_cache = {}

    # ------------------------------------------------------------ entry
    def run_proc(self, name, args):
        """Execute procedure `name` with args: list of Ref (ByRef) or V (ByVal). Returns the
        function result V (or None for a Sub)."""
        proc = self.prog.procs.get(name.lower())
        if proc is None:
            raise Unsupported('no procedure %s' % name)
        if proc.error is not None:
            raise Unsupported('procedure is not valid VB: %s' % proc.error.msg, proc.error.line)
        fr = Frame(proc)
        for prm, a in zip(proc.params, args):
            t = self.prog.canon_type(prm.type)
            lay = self.prog.layout(t)
            if isinstance(a, Ref) and not prm.byval:
                fr.vars[prm.name.lower()] = a
            else:
                v = a.get() if isinstance(a, Ref) else a
                c = Cell(t, None, prm.name, lay, prm.is_array)
                c.set(v)
                fr.vars[prm.name.lower()] = c
        for k, d in proc.locals.items():
            fr.vars[k] = self.new_cell(d.type, d.name, d.line)
        if proc.kind == 'function':
            t = self.prog.canon_type(proc.rettype)
            lay = self.prog.layout(t)
            fr.result = Cell(t, default_value(t, lay) if not lay else V('Record', VBRecord(lay)), proc.name, lay)
            fr.vars.setdefault(proc.name.lower(), fr.result)
        self.execute(fr)
        return fr.result.get() if fr.result is not None else None

    def new_cell(self, tref, name, line=None):
        t = self.prog.canon_type(tref.name)
        lay = self.prog.layout(t)
        if tref.is_array:
            if tref.bounds:
                bounds = []
                for lo, hi in tref.bounds:
                    lb = to_int(self.prog.const_expr(lo)) if lo is not None else 0
                    bounds.append((lb, to_int(self.prog.const_expr(hi))))
                return Cell(t, V('Array', VBArray(t, bounds, fixed=True, rec=lay)), name, lay, True)
            return Cell(t, V('Array', None), name, lay, True)
        if lay is not None:
            return Cell(t, V('Record', VBRecord(lay)), name, lay)
        return Cell(t, default_value(t), name)

    def flatten(self, proc):
        k = id(proc)
        if k not in self.flat_cache:
            self.flat_cache[k] = Flattener(proc).run()
        return self.flat_cache[k]

    # ------------------------------------------------------------ execution loop
    def execute(self, fr):
        ops, labels = self.flatten(fr.proc)
        pc = 0
        while True:
            if pc >= len(ops):
                return
            op = ops[pc]
            self.steps += 1
            if self.steps > self.max_steps:
                raise StopRun('step budget')
            try:
                k = op.kind
                if k == 'stmt':
                    r = self.stmt(fr, op.a)
                    if r is not None:
                        if r == 'return':
                            return
                        pc = r
                        continue
                    pc += 1
                elif k == 'goto':
                    pc = op.a
                elif k == 'jf':
                    pc = pc + 1 if self.cond(fr, op.a) else op.b
                elif k == 'jt':
                    pc = op.b if self.cond(fr, op.a) else pc + 1
                elif k == 'return':
                    return
                elif k == 'for_init':
                    s = op.a
                    start = self.eval(fr, s.start)
                    end = self.eval(fr, s.end)
                    step = self.eval(fr, s.step) if s.step is not None else V('Integer', 1)
                    ref = self.lvalue(fr, s.var)
                    ref.set(start)
                    fr.fors[op.b] = (end, step)
                    pc += 1
                elif k == 'for_test':
                    if op.b not in fr.fors:
                        raise Unsupported('jump into a For loop', op.line)
                    end, step = fr.fors[op.b]
                    v = self.lvalue(fr, op.a.var).get()
                    up = to_float(step) >= 0
                    done = binop('>' if up else '<', v, end, self.text_cmp)
                    pc = op.c if to_bool(done) else pc + 1
                elif k == 'for_next':
                    if op.b not in fr.fors:
                        raise Unsupported('jump into a For loop', op.line)
                    end, step = fr.fors[op.b]
                    ref = self.lvalue(fr, op.a.var)
                    ref.set(self.bop('+', ref.get(), step))
                    pc = op.c
                elif k == 'sel_init':
                    fr.sels[op.b] = self.eval(fr, op.a)
                    pc += 1
                elif k == 'case':
                    subj = fr.sels[op.b]
                    hit = False
                    for item in op.a:
                        if item[0] == 'eq':
                            if to_bool(binop('=', subj, self.eval(fr, item[1]), self.text_cmp)):
                                hit = True
                                break
                        else:
                            lo, hi = self.eval(fr, item[1]), self.eval(fr, item[2])
                            if to_bool(binop('>=', subj, lo, self.text_cmp)) and \
                                    to_bool(binop('<=', subj, hi, self.text_cmp)):
                                hit = True
                                break
                    pc = pc + 1 if hit else op.c
                else:
                    raise Unsupported('internal: op %s' % k, op.line)
            except VBError as e:
                pc = self.on_error(fr, e, pc, ops, labels)

    def on_error(self, fr, e, pc, ops, labels):
        """VB error raised at step pc: go to the active handler or end the run with the error."""
        self.state.err_number = e.num
        if fr.on_error == 'resume_next' and not fr.in_handler:
            return self.next_statement(ops, pc)
        if isinstance(fr.on_error, tuple) and not fr.in_handler:
            fr.in_handler = True
            fr.err_resume = pc
            return labels[fr.on_error[1].lower()]
        if getattr(e, 'where', None) is None:
            e.where = 'source line %s' % ops[pc].line
        raise e

    def next_statement(self, ops, pc):
        """Resume Next target: the step after the failing one (jumps over a failed condition)."""
        op = ops[pc]
        if op.kind in ('jf', 'jt'):
            return pc + 1
        return pc + 1

    def cond(self, fr, e):
        return to_bool(self.eval(fr, e))

    def bop(self, op, a, b):
        return binop(op, a, b, self.text_cmp, self.opts['overflow_checks'], self.opts['fp_checks'])

    # ------------------------------------------------------------ statements
    def stmt(self, fr, s):
        w = self.world
        if isinstance(s, P.Assign):
            v = self.eval(fr, s.expr)
            if s.is_set:
                if v.t != 'Object':
                    raise VBError(13, 'Set with a non-object')
            elif v.t == 'Object' and not isinstance(s.target, P.Name) and False:
                pass
            ref = self.lvalue(fr, s.target, assign_value=v, is_set=s.is_set)
            if ref is not None:
                if not s.is_set and v.t == 'Object' and self.ref_type(ref) not in ('Variant', None, 'Object') \
                        and self.ref_type(ref) in NUMERIC + ('String',):
                    v = self.default_prop(v, s.line)
                ref.set(v)
            return None
        if isinstance(s, P.CallStmt):
            self.call_stmt(fr, s)
            return None
        if isinstance(s, P.Keyword):
            n = s.name
            args = [self.eval(fr, a) for a in s.args]
            if n == 'doevents':
                w.doevents()
            elif n == 'beep':
                w.simple('beep')
            elif n == 'randomize':
                w.randomize(args[0] if args else None)
            elif n == 'kill':
                w.kill(to_str(args[0]))
            elif n == 'savesetting':
                w.savesetting(to_str(args[0]), to_str(args[1]), to_str(args[2]), to_str(args[3]))
            elif n == 'unload':
                # a method of VB's hidden Global object, as the compiled code calls it
                g = self.global_obj()
                m = self.find_member(g, 'Unload', 'method', s.line)
                self.world.com_call(g, m.name, m.invkind, [args[0]] if args else [], {}, None)
            elif n == 'error':
                raise VBError(to_int(args[0]))
            else:
                raise Unsupported('statement %s' % n, s.line)
            return None
        if isinstance(s, P.EndStmt):
            w.simple('end')
            return 'return'
        if isinstance(s, P.OnError):
            if s.mode == 'goto':
                fr.on_error = ('goto', s.label)
            elif s.mode == 'resume_next':
                fr.on_error = 'resume_next'
            else:
                fr.on_error = None
            fr.in_handler = False
            self.state.err_number = 0
            return None
        if isinstance(s, P.Resume):
            if not fr.in_handler:
                raise VBError(20, 'Resume without error')
            fr.in_handler = False
            self.state.err_number = 0
            ops, labels = self.flatten(fr.proc)
            if s.mode == 'next':
                return self.next_statement(ops, fr.err_resume)
            if s.mode == 'label':
                return labels[s.label.lower()]
            return fr.err_resume
        if isinstance(s, P.FileOpen):
            path = to_str(self.eval(fr, s.path))
            num = to_int(self.eval(fr, s.num))
            w.file_open(path, s.mode, num)
            return None
        if isinstance(s, P.FileClose):
            for n in s.nums:
                w.file_close(to_int(self.eval(fr, n)))
            return None
        if isinstance(s, P.FilePrint):
            num = to_int(self.eval(fr, s.num))
            if s.write:
                vals = [self.eval(fr, e) for e, sep in s.items if e is not None]
                w.file_write_text(num, write_text(vals), 'write')
            else:
                items = [(self.eval(fr, e) if e is not None else None, sep) for e, sep in s.items]
                w.file_write_text(num, print_text(items), 'print')
            return None
        if isinstance(s, P.FileInput):
            num = to_int(self.eval(fr, s.num))
            for tgt in s.targets:
                ref = self.lvalue(fr, tgt)
                if s.line_input:
                    ref.set(V('String', w.file_line_input(num)))
                else:
                    text = w.file_input_field(num)
                    ref.set(input_value(text, self.ref_type(ref) or 'Variant'))
            return None
        if isinstance(s, P.ReDim):
            ref = self.lvalue(fr, s.target)
            t = self.ref_type(ref)
            bounds = []
            for lo, hi in s.bounds:
                lb = to_int(self.eval(fr, lo)) if lo is not None else 0
                bounds.append((lb, to_int(self.eval(fr, hi))))
            lay = ref.layout if isinstance(ref, Cell) else None
            if isinstance(ref, Cell) and not ref.is_array and ref.t != 'Variant':
                raise Unsupported('ReDim of %s, which is declared As %s (not an array)' % (ref.name, ref.t),
                                  s.line)
            ref.val = V('Array', VBArray(t if t else 'Variant', bounds, rec=lay))
            return None
        if isinstance(s, P.Erase):
            for tgt in s.targets:
                ref = self.lvalue(fr, tgt)
                v = ref.get()
                if v.t == 'Array' and v.v is not None:
                    if v.v.fixed:
                        v.v.data = [default_value(v.v.etype, v.v.rec) for _ in v.v.data]
                    else:
                        ref.val = V('Array', None)
            return None
        raise Unsupported('statement %s' % type(s).__name__, s.line)

    def ref_type(self, ref):
        if isinstance(ref, Cell):
            return ref.t
        if isinstance(ref, ElemRef):
            return ref.arr.etype
        if isinstance(ref, FieldRef):
            return ref.t
        return None

    def default_prop(self, v, line):
        raise Unsupported('object used as a value (no default property support)', line)

    # ------------------------------------------------------------ calls
    def call_stmt(self, fr, s):
        tgt = s.target
        if isinstance(tgt, P.Name):
            n = tgt.name.lower()
            if n in fr.vars or n in self.state.fields or n in self.state.globals:
                raise Unsupported('calling variable %s' % tgt.name, s.line)
            self.call_named(fr, tgt.name, s.args, s.line, want_value=False)
            return
        if isinstance(tgt, P.Member):
            self.call_member(fr, tgt.obj, tgt.name, s.args, s.line, want_value=False, kind='method')
            return
        raise Unsupported('call of %s' % type(tgt).__name__, s.line)

    def call_named(self, fr, name, arg_nodes, line, want_value):
        n = name.lower()
        p = self.prog.procs.get(n)
        if p is not None:
            return self.call_user(fr, p, arg_nodes, line, want_value)
        d = self.prog.declares.get(n)
        if d is not None:
            return self.call_api(fr, d, arg_nodes, line)
        f = getattr(self, 'bi_' + n.rstrip('$').lower(), None)
        if f is not None:
            return f(fr, arg_nodes, line, n.endswith('$'))
        raise Unsupported('call to unknown %s' % name, line)

    def call_user(self, fr, p, arg_nodes, line, want_value):
        if len(arg_nodes) > len(p.params):
            raise Unsupported('%s called with %d arguments, takes %d' % (p.name, len(arg_nodes), len(p.params)),
                              line)
        if len(arg_nodes) < len(p.params) and not all(q.optional for q in p.params[len(arg_nodes):]):
            raise Unsupported('%s called with %d arguments, takes %d' % (p.name, len(arg_nodes), len(p.params)),
                              line)
        vals = []
        out_types, refs = {}, {}
        for i, (prm, a) in enumerate(zip(p.params, arg_nodes)):
            if isinstance(a, P.Missing):
                raise Unsupported('missing argument for %s' % p.name, line)
            t = self.prog.canon_type(prm.type)
            if not prm.byval and not prm.is_array and self.is_iface(t):
                # the procedure may Set an object into this ByRef parameter: it reaches a variable of
                # exactly the parameter's type; anything else is passed as a temporary copy (lost)
                out_types[i] = t
                ref = self.byref_target(fr, a, t)
                if ref is not None:
                    refs[i] = ref
            v = self.eval(fr, a)
            if prm.is_array:
                vals.append(v)
                continue
            lay = self.prog.layout(t)
            if lay is not None:
                if v.t != 'Record':
                    raise VBError(13, 'record argument')
                vals.append(v)
                continue
            if v.t == 'Object' and t not in ('Object', 'Variant') and t in NUMERIC + ('String',):
                raise Unsupported('object passed for %s parameter %s of %s' % (t, prm.name, p.name), line)
            vals.append(store_convert(v, t, None, False))     # passing converts like an assignment
        rt = self.prog.canon_type(p.rettype) if p.kind == 'function' else None
        ret, outs = self.world.call_proc(p.name, vals, rt, self.prog.layout(rt) if rt else None, out_types)
        for i, v in outs.items():
            if i in refs:
                refs[i].set(v)
        return ret

    def byref_target(self, fr, a, t):
        """The variable a ByRef argument passes itself (declared with exactly the parameter's type), or
        None when VB passes a temporary copy (an expression, another type). Only for arguments without
        calls in them (finding the variable must not run anything)."""
        def plain(n):
            if isinstance(n, (P.Num, P.StrLit, P.Const)):
                return True
            if isinstance(n, P.Name):
                return self.lookup(fr, n.name.lower()) is not None
            if isinstance(n, (P.BinOp,)):
                return plain(n.a) and plain(n.b)
            if isinstance(n, P.UnOp):
                return plain(n.a)
            if isinstance(n, P.Member):
                return plain(n.obj)
            if isinstance(n, P.Apply):
                return isinstance(n.target, P.Name) and plain(n.target) and all(plain(x) for x in n.args)
            return False
        if not isinstance(a, (P.Name, P.Apply, P.Member)) or not plain(a):
            return None
        try:
            ref = self.lvalue(fr, a)
        except (Unsupported, VBError, OutOfBounds):
            return None
        if isinstance(ref, ElemRef):
            rt = ref.arr.etype
        else:
            rt = getattr(ref, 't', None)
        if getattr(ref, 'is_array', False) or not isinstance(rt, str):
            return None
        return ref if self.prog.canon_type(rt).lower() == t.lower() else None

    def call_api(self, fr, d, arg_nodes, line):
        vals = []
        for prm, a in zip(d.params, arg_nodes):
            vals.append(convert(self.eval(fr, a), self.prog.canon_type(prm.type)))
        rt = self.prog.canon_type(d.rettype) if d.rettype else None
        return self.world.api(d.alias or d.name, vals, rt)

    def object_of(self, fr, node, line):
        """Evaluate the object part of obj.member: an object value, or a record."""
        if isinstance(node, P.MeRef):
            return self.me
        if isinstance(node, P.Name):
            n = node.name.lower()
            if n in GLOBAL_OBJECTS and not self.is_var(fr, n):
                return self.global_object(n)
            if n == 'err' and not self.is_var(fr, n):
                return V('Object', ObjRef(-1, 'ErrObject', 'err'))
            if n in self.prog.controls and not self.is_var(fr, n):
                oid, cls = self.prog.controls[n]
                return V('Object', ObjRef(oid, cls, 'control'))
        return self.eval(fr, node)

    def is_var(self, fr, n):
        return n in fr.vars or n in self.state.fields or n in self.state.globals

    def global_obj(self):
        """VB's hidden Global object (created once per run)."""
        if 'global' not in self.objs:
            g = self.world.new_object('Global')
            g.v.iface = 'Global'
            self.objs['global'] = g
        return self.objs['global'].v

    def global_object(self, n):
        """App / Screen / Clipboard / Printer: VB reaches them through its hidden Global object (created
        once per run), reading Global.<name> at every use, as the compiled code does."""
        g = self.global_obj()
        names = {'app': 'App', 'clipboard': 'Clipboard', 'screen': 'Screen', 'printer': 'Printer'}
        m = self.find_member(g, names[n], 'get', None)
        retp = [p for p in m.params if p['flags'] & 8]
        rt = retp[0]['type'].rstrip('*') if retp else m.ret
        ret, outs = self.world.com_call(g, m.name, m.invkind, [], {}, rt)
        return ret

    def find_member(self, obj, name, kind, line):
        it = self.prog.iface_of(obj.iface)
        if it is None:
            raise Unsupported('object of unknown type %s' % obj.iface, line)
        cands = [m for m in it.members if m.name.lower() == name.lower()]
        if not cands:
            raise Unsupported('%s has no member %s' % (it.name, name), line)
        for want in ((kind,) if kind else ()) + ('method', 'get', 'let', 'set'):
            for m in cands:
                if m.invkind == want:
                    return m
        return cands[0]

    def call_member(self, fr, obj_node, name, arg_nodes, line, want_value, kind='get', assign=None,
                    obj=None):
        if obj is None:
            obj = self.object_of(fr, obj_node, line)
        if obj.t == 'Record':
            raise Unsupported('call on a record member', line)
        if obj.t != 'Object':
            raise VBError(424, 'object required')
        o = obj.v
        if o is None:
            # the compiled code evaluates the arguments first; the call through the empty reference fails
            for a in arg_nodes:
                if a is not None and not isinstance(a, P.Missing):
                    self.eval(fr, a)
            raise VBError(91, 'object variable not set')
        if o.kind == 'err':
            return self.err_member(name, arg_nodes, fr, line)
        if o.kind == 'me' and name.lower() in self.prog.controls:
            oid, cls = self.prog.controls[name.lower()]
            return V('Object', ObjRef(oid, cls, 'control'))
        m = self.find_member(o, name, kind if kind != 'method' else 'method', line)
        params = m.params
        vis = [p for p in params if not (p['flags'] & 8)]
        retp = [p for p in params if p['flags'] & 8]
        args = []
        outs = {}
        refs = {}
        for i, p in enumerate(vis):
            if m.invkind in ('let', 'set') and i == len(vis) - 1 and assign is not None:
                args.append(self.com_in(assign, p['type'], line))
                continue
            a = arg_nodes[i] if i < len(arg_nodes) else None
            if a is None or isinstance(a, P.Missing):
                # optional; some type libraries (VB6.OLB: Form.Show) leave the flag off Variant parameters
                if p['flags'] & 0x10 or p['type'] == 'Variant':
                    args.append(MISSING)
                    continue
                raise Unsupported('missing argument %d of %s.%s' % (i + 1, o.iface, name), line)
            pt = p['type']
            if pt == 'Void*':
                pt = any_param_type(name, args) or pt
            if pt.endswith('*') and not self.is_iface(pt[:-1]):
                ref = self.lvalue(fr, a)
                refs[i] = ref
                base = pt[:-1]
                lay = self.prog.layout(base)
                if lay is not None:
                    args.append(ref.get())
                else:
                    args.append(convert(ref.get(), self.prog.canon_type(base)))
                if p['flags'] & 2:
                    outs[i] = (self.prog.canon_type(base), lay)
            else:
                args.append(self.com_in(self.eval(fr, a), pt, line))
        if len(arg_nodes) > len(vis):
            raise Unsupported('too many arguments for %s.%s' % (o.iface, name), line)
        rt = None
        rlay = None
        if retp:
            t = retp[0]['type']
            base = t[:-1] if t.endswith('*') else t
            base = base.rstrip('*')
            rt = self.prog.canon_type(base)
            rlay = self.prog.layout(base)
        elif m.ret not in ('HRESULT', 'Void', None):
            rt = self.prog.canon_type(m.ret)
            rlay = self.prog.layout(m.ret)
        if rt is not None and rlay is None and self.is_iface(rt):
            rt = self.obj_type_name(rt)
        ret, outv = self.world.com_call(o, m.name, m.invkind, args, outs, rt, rlay)
        for i, v in outv.items():
            refs[i].set(v)
        return ret

    def obj_type_name(self, t):
        return t

    def is_iface(self, t):
        t = t.rstrip('*')
        if t in ('IUnknown', 'IDispatch', 'Object'):
            return True
        it = self.prog.tlb.by_name.get(t)
        return it is not None and it.kind in ('interface', 'dispatch', 'coclass')

    def com_in(self, v, pt, line):
        if pt == 'Variant':
            return v.as_variant()
        if self.is_iface(pt):
            if v.t != 'Object':
                raise VBError(13, 'object expected')
            return v
        t = self.prog.canon_type(pt.rstrip('*'))
        if t == 'Variant':
            return v.as_variant()
        return convert(v, t)

    def err_member(self, name, arg_nodes, fr, line):
        n = name.lower()
        if n == 'number':
            return V('Long', self.state.err_number)
        if n == 'description':
            return V('String', 'error %d' % self.state.err_number)
        if n == 'clear':
            self.state.err_number = 0
            return None
        raise Unsupported('Err.%s' % name, line)

    # ------------------------------------------------------------ expressions
    def eval(self, fr, e):
        if isinstance(e, P.Num):
            if e.is_float:
                return V('Double', e.v)
            if e.suffix == '&':
                return V('Long', e.v)
            if e.suffix == 'hex':
                v = e.v
                if v <= 0xffff:
                    return V('Integer', v - 0x10000 if v >= 0x8000 else v)
                return V('Long', v - (1 << 32) if v >= 0x80000000 else v)
            v = e.v
            if -32768 <= v <= 32767:
                return V('Integer', v)
            if -2147483648 <= v <= 2147483647:
                return V('Long', v)
            return V('Double', float(v))
        if isinstance(e, P.StrLit):
            return V('String', e.s)
        if isinstance(e, P.Const):
            return {'true': TRUE, 'false': FALSE, 'nothing': NOTHING, 'empty': EMPTY, 'null': NULL}[e.name]
        if isinstance(e, P.BinOp):
            a = self.eval(fr, e.a)
            b = self.eval(fr, e.b)
            if e.op == 'Is':
                return binop('Is', a, b)
            return self.bop(e.op, a, b)
        if isinstance(e, P.UnOp):
            return unop(e.op, self.eval(fr, e.a), self.opts['overflow_checks'])
        if isinstance(e, P.MeRef):
            return self.me
        if isinstance(e, P.NewObj):
            return self.world.new_object(e.cls)
        if isinstance(e, P.Missing):
            return MISSING
        if isinstance(e, P.Name):
            return self.eval_name(fr, e)
        if isinstance(e, P.Member):
            return self.eval_member(fr, e)
        if isinstance(e, P.Apply):
            return self.eval_apply(fr, e)
        raise Unsupported('expression %s' % type(e).__name__, getattr(e, 'line', None))

    def lookup(self, fr, n):
        r = fr.vars.get(n)
        if r is not None:
            return r
        r = self.state.fields.get(n)
        if r is not None:
            return r
        return self.state.globals.get(n)

    def eval_name(self, fr, e):
        n = e.name.lower()
        r = self.lookup(fr, n)
        if r is not None:
            v = r.get()
            if v is None:
                raise Unsupported('variable %s has no value' % e.name, e.line)
            return v
        if n in self.prog.procs:
            return self.call_named(fr, e.name, [], e.line, True)
        if n in self.prog.declares:
            return self.call_named(fr, e.name, [], e.line, True)
        if n in VB_CONSTANTS:
            return VB_CONSTANTS[n]
        if n in self.prog.enum_values:
            return V('Long', self.prog.enum_values[n])
        if n in GLOBAL_OBJECTS:
            return self.global_object(n)
        if n in self.prog.controls:
            oid, cls = self.prog.controls[n]
            return V('Object', ObjRef(oid, cls, 'control'))
        f = getattr(self, 'bi_' + n.rstrip('$'), None)
        if f is not None:
            return f(fr, [], e.line, n.endswith('$'))
        raise Unsupported('undeclared name %s' % e.name, e.line)

    def eval_member(self, fr, e, obj=None):
        if obj is None:
            obj = self.object_of(fr, e.obj, e.line)
        if obj.t == 'Record':
            rec = obj.v
            f = rec.layout.by_name.get(e.name.lower())
            if f is None:
                raise Unsupported('record %s has no field %s' % (rec.layout.name, e.name), e.line)
            return rec.vals[f[0].lower()]
        return self.call_member(fr, e.obj, e.name, [], e.line, True, kind='get', obj=obj)

    def eval_apply(self, fr, e):
        tgt = e.target
        if isinstance(tgt, P.Name):
            n = tgt.name.lower()
            r = self.lookup(fr, n)
            if r is not None:
                v = r.get()
                if v.t == 'Array':
                    return self.index(v, [self.eval(fr, a) for a in e.args], e.line).get()
                if n == fr.proc.name.lower() and fr.result is r and n in self.prog.procs:
                    return self.call_named(fr, tgt.name, e.args, e.line, True)
                raise Unsupported('%s(...) but %s is not an array' % (tgt.name, tgt.name), e.line)
            return self.call_named(fr, tgt.name, e.args, e.line, True)
        if isinstance(tgt, P.Member):
            obj = self.object_of(fr, tgt.obj, e.line)
            if obj.t == 'Record':
                v = self.eval_member(fr, tgt, obj)
                if v.t != 'Array':
                    raise Unsupported('indexing a non-array record field', e.line)
                return self.index(v, [self.eval(fr, a) for a in e.args], e.line).get()
            return self.call_member(fr, tgt.obj, tgt.name, e.args, e.line, True, kind='get', obj=obj)
        if isinstance(tgt, P.Apply):
            v = self.eval_apply(fr, tgt)
            if v.t == 'Array':
                return self.index(v, [self.eval(fr, a) for a in e.args], e.line).get()
        raise Unsupported('cannot apply arguments to this expression', e.line)

    def index(self, arrv, idx, line):
        a = arrv.v
        if a is None:
            if not self.opts['bounds_checks']:
                # without bounds checks the compiled code reads through the missing array: undefined
                raise OutOfBounds('line %s: array not dimensioned' % line)
            raise VBError(9, 'array not dimensioned')
        try:
            k = a.offset([to_int(i) for i in idx])
        except VBError as e:
            if e.num == 9 and not self.opts['bounds_checks'] and len(idx) == len(a.bounds):
                raise OutOfBounds('line %s: index %s outside %s' % (line, [canon(i) for i in idx], a.bounds))
            raise
        return ElemRef(a, k)

    def lvalue(self, fr, e, assign_value=None, is_set=False):
        if isinstance(e, P.Name):
            n = e.name.lower()
            r = self.lookup(fr, n)
            if r is None:
                raise Unsupported('assignment to undeclared %s' % e.name, e.line)
            return r
        if isinstance(e, P.Apply):
            tgt = e.target
            if isinstance(tgt, P.Name):
                r = self.lookup(fr, tgt.name.lower())
                if r is None:
                    raise Unsupported('assignment to undeclared %s' % tgt.name, e.line)
                v = r.get()
                if v.t != 'Array':
                    raise Unsupported('%s is not an array' % tgt.name, e.line)
                return self.index(v, [self.eval(fr, a) for a in e.args], e.line)
            if isinstance(tgt, P.Member):
                obj = self.object_of(fr, tgt.obj, e.line)
                if obj.t == 'Record':
                    v = self.eval_member(fr, tgt, obj)
                    if v.t != 'Array':
                        raise Unsupported('indexing a non-array record field', e.line)
                    return self.index(v, [self.eval(fr, a) for a in e.args], e.line)
                if assign_value is not None:
                    self.call_member(fr, tgt.obj, tgt.name, e.args, e.line, False,
                                     kind='set' if is_set else 'let', assign=assign_value, obj=obj)
                    return None
            raise Unsupported('cannot assign to this expression', e.line)
        if isinstance(e, P.Member):
            obj = self.object_of(fr, e.obj, e.line)
            if obj.t == 'Record':
                rec = obj.v
                f = rec.layout.by_name.get(e.name.lower())
                if f is None:
                    raise Unsupported('record %s has no field %s' % (rec.layout.name, e.name), e.line)
                name, t, off, size, count = f
                return FieldRef(rec, name.lower(), t, self.prog.layout(t))
            if assign_value is not None:
                self.call_member(fr, e.obj, e.name, [], e.line, False, kind='set' if is_set else 'let',
                                 assign=assign_value, obj=obj)
                return None
        raise Unsupported('cannot assign to %s' % type(e).__name__, getattr(e, 'line', None))

    # ------------------------------------------------------------ builtins
    def args_v(self, fr, nodes):
        return [self.eval(fr, a) if not isinstance(a, P.Missing) else MISSING for a in nodes]

    def bi_doevents(self, fr, a, line, dollar):
        self.world.doevents()
        return V('Integer', 0)

    def bi_trim(self, fr, a, line, dollar):
        v = self.eval(fr, a[0])
        if v.t == 'Null':
            return NULL
        s = to_str(v).strip(' ')
        return V('String', s, not dollar)

    def bi_ltrim(self, fr, a, line, dollar):
        return V('String', to_str(self.eval(fr, a[0])).lstrip(' '), not dollar)

    def bi_rtrim(self, fr, a, line, dollar):
        return V('String', to_str(self.eval(fr, a[0])).rstrip(' '), not dollar)

    def bi_str(self, fr, a, line, dollar):
        v = self.eval(fr, a[0])
        return V('String', str_v(v), not dollar)

    def bi_cstr(self, fr, a, line, dollar):
        return V('String', to_str(self.eval(fr, a[0])))

    def bi_cint(self, fr, a, line, dollar):
        return convert(self.eval(fr, a[0]), 'Integer')

    def bi_clng(self, fr, a, line, dollar):
        return convert(self.eval(fr, a[0]), 'Long')

    def bi_csng(self, fr, a, line, dollar):
        return convert(self.eval(fr, a[0]), 'Single')

    def bi_cdbl(self, fr, a, line, dollar):
        return convert(self.eval(fr, a[0]), 'Double')

    def bi_cbool(self, fr, a, line, dollar):
        return convert(self.eval(fr, a[0]), 'Boolean')

    def bi_cbyte(self, fr, a, line, dollar):
        return convert(self.eval(fr, a[0]), 'Byte')

    def bi_cdate(self, fr, a, line, dollar):
        return convert(self.eval(fr, a[0]), 'Date')

    def _intfix(self, fr, a, f):
        v = self.eval(fr, a[0])
        if v.t == 'Null':
            return NULL
        if v.t in INTEGRAL:
            return V('Integer' if v.t == 'Boolean' else v.t, v.v, v.variant)
        if v.t == 'String':
            v = V('Double', str_to_num(v.v), True)
        if v.t == 'Empty':
            return V('Integer', 0, True)
        if v.t == 'Currency':
            return V('Currency', int(f(v.v / 10000.0)) * 10000, v.variant)
        if v.t in ('Single', 'Double', 'Date'):
            if math.isinf(v.v) or math.isnan(v.v):
                return v                # Int(Inf) = Inf (only possible without floating-point checks)
            return V(v.t, float(f(v.v)), v.variant)
        raise VBError(13)

    def bi_int(self, fr, a, line, dollar):
        return self._intfix(fr, a, math.floor)

    def bi_fix(self, fr, a, line, dollar):
        return self._intfix(fr, a, math.trunc)

    def bi_abs(self, fr, a, line, dollar):
        v = self.eval(fr, a[0])
        if v.t in NUMERIC and v.t != 'Boolean':
            n = abs(v.v)
            if v.t in INTEGRAL:
                # (without overflow checks Abs is an inline cdq / xor / sub: Abs(-32768) stays -32768;
                # a Variant widens)
                from vbvalues import _result
                return _result(v.t, n, v.variant, self.opts['overflow_checks'])
            return V(v.t, n, v.variant)
        return unop('neg', v) if to_float(v) < 0 else convert(v, 'Double')

    def bi_sgn(self, fr, a, line, dollar):
        x = to_float(self.eval(fr, a[0]))
        return V('Integer', (x > 0) - (x < 0))

    def bi_len(self, fr, a, line, dollar):
        v = self.eval(fr, a[0])
        if v.t == 'Null':
            return NULL
        if v.t != 'String' and not v.variant:
            sizes = {'Integer': 2, 'Boolean': 2, 'Long': 4, 'Single': 4, 'Double': 8, 'Date': 8, 'Byte': 1,
                     'Currency': 8}
            if v.t in sizes:
                return V('Long', sizes[v.t])
        return V('Long', len(to_str(v)))

    def bi_left(self, fr, a, line, dollar):
        s = self.eval(fr, a[0])
        n = to_int(self.eval(fr, a[1]))
        if n < 0:
            raise VBError(5)
        if s.t == 'Null':
            return NULL
        return V('String', to_str(s)[:n], not dollar)

    def bi_right(self, fr, a, line, dollar):
        s = self.eval(fr, a[0])
        n = to_int(self.eval(fr, a[1]))
        if n < 0:
            raise VBError(5)
        if s.t == 'Null':
            return NULL
        t = to_str(s)
        return V('String', t[len(t) - n:] if n else '', not dollar)

    def bi_mid(self, fr, a, line, dollar):
        s = self.eval(fr, a[0])
        start = to_int(self.eval(fr, a[1]))
        if start < 1:
            raise VBError(5)
        n = None
        if len(a) > 2 and not isinstance(a[2], P.Missing):
            n = to_int(self.eval(fr, a[2]))
            if n < 0:
                raise VBError(5)
        if s.t == 'Null':
            return NULL
        t = to_str(s)
        r = t[start - 1:] if n is None else t[start - 1:start - 1 + n]
        return V('String', r, not dollar)

    def bi_instr(self, fr, a, line, dollar):
        vals = self.args_v(fr, a)
        start = 1
        if len(vals) >= 3 and vals[0].t not in ('String',):
            start = to_int(vals[0])
            vals = vals[1:]
        s1, s2 = to_str(vals[0]), to_str(vals[1])
        text = self.text_cmp
        if len(vals) > 2 and vals[2].t != 'Missing':
            text = to_int(vals[2]) == 1
        if start < 1:
            raise VBError(5)
        if text:
            s1, s2 = s1.lower(), s2.lower()
        if start > len(s1):
            return V('Long', 0)
        if s2 == '':
            return V('Long', start)
        return V('Long', s1.find(s2, start - 1) + 1)

    def bi_strcomp(self, fr, a, line, dollar):
        vals = self.args_v(fr, a)
        if vals[0].t == 'Null' or vals[1].t == 'Null':
            return NULL
        text = self.text_cmp
        if len(vals) > 2 and vals[2].t != 'Missing':
            text = to_int(vals[2]) == 1
        return V('Integer', str_compare(to_str(vals[0]), to_str(vals[1]), text), True)

    def bi_replace(self, fr, a, line, dollar):
        vals = self.args_v(fr, a)
        s, f, r = to_str(vals[0]), to_str(vals[1]), to_str(vals[2])
        if f == '':
            return V('String', s)
        if self.text_cmp:
            out = ''
            i = 0
            lf = f.lower()
            ls = s.lower()
            while True:
                j = ls.find(lf, i)
                if j < 0:
                    out += s[i:]
                    break
                out += s[i:j] + r
                i = j + len(f)
            return V('String', out)
        return V('String', s.replace(f, r))

    def bi_chr(self, fr, a, line, dollar):
        n = to_int(self.eval(fr, a[0]))
        if n < 0 or n > 255:
            raise VBError(5)
        return V('String', bytes([n]).decode('latin-1'), not dollar)

    def bi_asc(self, fr, a, line, dollar):
        s = to_str(self.eval(fr, a[0]))
        if not s:
            raise VBError(5)
        c = s[0].encode('latin-1', 'replace')[0]
        return V('Integer', c)

    def bi_space(self, fr, a, line, dollar):
        n = to_int(self.eval(fr, a[0]))
        if n < 0:
            raise VBError(5)
        return V('String', ' ' * n, not dollar)

    def bi_ucase(self, fr, a, line, dollar):
        return V('String', to_str(self.eval(fr, a[0])).upper(), not dollar)

    def bi_lcase(self, fr, a, line, dollar):
        return V('String', to_str(self.eval(fr, a[0])).lower(), not dollar)

    def bi_val(self, fr, a, line, dollar):
        return V('Double', val_of(to_str(self.eval(fr, a[0]))))

    def bi_split(self, fr, a, line, dollar):
        vals = self.args_v(fr, a)
        s = to_str(vals[0])
        d = ' ' if len(vals) < 2 or vals[1].t == 'Missing' else to_str(vals[1])
        if s == '':
            parts = []
        elif d == '':
            parts = [s]
        else:
            parts = s.split(d) if not self.text_cmp else _split_text(s, d)
        arr = VBArray('String', [(0, len(parts) - 1)], [V('String', p) for p in parts])
        return V('Array', arr, True)

    def bi_ubound(self, fr, a, line, dollar):
        v = self.eval(fr, a[0])
        dim = to_int(self.eval(fr, a[1])) if len(a) > 1 else 1
        if v.t != 'Array' or v.v is None:
            raise VBError(9)
        if dim < 1 or dim > len(v.v.bounds):
            raise VBError(9)
        return V('Long', v.v.bounds[dim - 1][1])

    def bi_lbound(self, fr, a, line, dollar):
        v = self.eval(fr, a[0])
        dim = to_int(self.eval(fr, a[1])) if len(a) > 1 else 1
        if v.t != 'Array' or v.v is None:
            raise VBError(9)
        return V('Long', v.v.bounds[dim - 1][0])

    def bi_array(self, fr, a, line, dollar):
        vals = [x.as_variant() for x in self.args_v(fr, a)]
        return V('Array', VBArray('Variant', [(0, len(vals) - 1)], vals), True)

    def bi_rnd(self, fr, a, line, dollar):
        arg = self.eval(fr, a[0]) if a else None
        return self.world.rnd(arg)

    def bi_timer(self, fr, a, line, dollar):
        return self.world.timer()

    def bi_now(self, fr, a, line, dollar):
        return self.world.now('now').as_variant()

    def bi_date(self, fr, a, line, dollar):
        return self.world.now('date').as_variant()

    def bi_time(self, fr, a, line, dollar):
        return self.world.now('time').as_variant()

    def bi_datepart(self, fr, a, line, dollar):
        iv = to_str(self.eval(fr, a[0])).lower()
        d = convert(self.eval(fr, a[1]), 'Date').v
        y, mo, da, h, mi, s = date_parts(d)
        r = {'yyyy': y, 'm': mo, 'd': da, 'h': h, 'n': mi, 's': s}.get(iv)
        if r is None:
            raise Unsupported('DatePart interval %r' % iv, line)
        return V('Integer', r, True)

    def bi_format(self, fr, a, line, dollar):
        vals = self.args_v(fr, a)
        fmt = to_str(vals[1]) if len(vals) > 1 and vals[1].t != 'Missing' else ''
        return V('String', format_value(vals[0], fmt), not dollar)

    def bi_iif(self, fr, a, line, dollar):
        vals = self.args_v(fr, a)
        return vals[1] if to_bool(vals[0]) else vals[2]

    def bi_isnumeric(self, fr, a, line, dollar):
        v = self.eval(fr, a[0])
        if v.t in NUMERIC:
            return TRUE
        if v.t == 'String':
            return boolv(str_to_num(v.v, err=False) is not None)
        return FALSE

    def bi_msgbox(self, fr, a, line, dollar):
        vals = self.args_v(fr, a)
        while len(vals) < 3:
            vals.append(MISSING)
        btn = vals[1] if vals[1].t != 'Missing' else V('Long', 0)
        return self.world.msgbox(vals[0], convert(btn, 'Long'), vals[2])

    def bi_getsetting(self, fr, a, line, dollar):
        vals = self.args_v(fr, a)
        default = to_str(vals[3]) if len(vals) > 3 and vals[3].t != 'Missing' else ''
        return V('String', self.world.getsetting(to_str(vals[0]), to_str(vals[1]), to_str(vals[2]), default))

    def bi_dir(self, fr, a, line, dollar):
        vals = self.args_v(fr, a)
        p = to_str(vals[0]) if vals and vals[0].t != 'Missing' else None
        attrs = to_int(vals[1]) if len(vals) > 1 else 0
        return V('String', self.world.dir(p, attrs))

    def bi_eof(self, fr, a, line, dollar):
        return boolv(self.world.file_eof(to_int(self.eval(fr, a[0]))))

    def bi_lof(self, fr, a, line, dollar):
        return V('Long', self.world.file_lof(to_int(self.eval(fr, a[0]))))

    def bi_hex(self, fr, a, line, dollar):
        n = to_int(self.eval(fr, a[0]))
        return V('String', '%X' % (n & 0xffffffff if n < 0 and n < -32768 else n & 0xffff if n < 0 else n),
                 not dollar)

    def _math(self, fr, a, f):
        try:
            return V('Double', f(to_float(self.eval(fr, a[0]))))
        except (ValueError, OverflowError):
            raise VBError(5)

    def bi_sqr(self, fr, a, line, dollar):
        return self._math(fr, a, math.sqrt)

    def bi_sin(self, fr, a, line, dollar):
        return self._math(fr, a, math.sin)

    def bi_cos(self, fr, a, line, dollar):
        return self._math(fr, a, math.cos)

    def bi_tan(self, fr, a, line, dollar):
        return self._math(fr, a, math.tan)

    def bi_atn(self, fr, a, line, dollar):
        return self._math(fr, a, math.atan)

    def bi_exp(self, fr, a, line, dollar):
        return self._math(fr, a, math.exp)

    def bi_log(self, fr, a, line, dollar):
        return self._math(fr, a, math.log)


def _split_text(s, d):
    out = []
    ls, ld = s.lower(), d.lower()
    i = 0
    while True:
        j = ls.find(ld, i)
        if j < 0:
            out.append(s[i:])
            return out
        out.append(s[i:j])
        i = j + len(d)


def format_value(v, fmt):
    """Format() with the patterns supported so far (the ones the Troopanum games use)."""
    if fmt == '':
        return to_str(v)
    x = to_float(v)
    if fmt == '##0.0%':
        return '%.1f%%' % (x * 100)
    if fmt in ('0', '#'):
        return '%d' % round_half_even(x)
    if fmt == '0.00':
        return '%.2f' % x
    raise VBError(5, 'Format pattern %r not modelled' % fmt)
