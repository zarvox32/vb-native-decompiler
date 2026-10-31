"""The scripted outside world shared by both sides of a check run.

Everything a procedure does that leaves the procedure goes through a World method: calls to other
procedures, COM/DirectX calls, Declare'd APIs, DoEvents, timers, random numbers, files, the
registry, MsgBox, End. Each call is logged as an event together with a snapshot of the observable
state (form fields and globals). The answer to an event depends only on the run seed, the event's
position and the event itself, so two sides that behave the same get the same answers and produce
identical logs; the first difference in the logs is a behavioural difference.

The world also plays the rest of the program: at DoEvents and after calls to other procedures it
may change form fields the procedure reads (what key handlers and other procedures would do), which
checks that the procedure re-reads state where the machine code does.
"""
import hashlib
import random

from vbvalues import (V, VBError, ObjRef, VBArray, VBRecord, canon, convert, to_str, str_to_num, f32,
                      EMPTY, NOTHING, INTEGRAL)


class StopRun(Exception):
    """The run reached its event budget (or ended with End / Unload): stop both sides here."""

    def __init__(self, why):
        self.why = why
        super().__init__(why)


# what the project file tells about its programs (check.py fills this in): the folder App.Path returns,
# and strings the programs work with (added to the general test strings)
SETTINGS = {'app_path': 'C:\\PROGRAM', 'strings': []}
STRING_POOL = ['', 'a', 'n', '0', '1', '12', '-3', 'left', 'right', '0,0', 'hello world']
TIMER_POOL = [1000000, 1000500, 1006000, 2000000]
INT_POOL = [0, 0, 0, 1, 1, -1, -1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 20, 24, 30, 99, 100, 500,
            1000, 32767, -32768]


class Event:
    __slots__ = ('kind', 'key', 'state', 'resp')

    def __init__(self, kind, key, state, resp=None):
        self.kind = kind
        self.key = key
        self.state = state
        self.resp = resp

    def __repr__(self):
        return '%s %r' % (self.kind, self.key)


class World:
    def __init__(self, seed, snapshot=None, max_events=300, candidates=(), constants=(),
                 files=None, registry=None, side='', focus=False, strings=(), floats=()):
        self.seed = seed
        self.strings = sorted(set(strings))       # string literals of the procedure
        self.floats = sorted(set(floats))         # floating-point constants of the procedure
        self.focus = focus                # integers mostly from `constants` (to reach rarely taken branches)
        self.side = side
        self.snapshot = snapshot          # () -> dict name -> canon value
        self.apply_writes = None          # [(name, V)] -> None  (set by the side)
        self.max_events = max_events
        self.candidates = list(candidates)    # [(field name, type name, layout or None)]
        self.constants = sorted(set(int(c) for c in constants if isinstance(c, int) and -100000 < c < 100000))
        self.log = []
        self.next_oid = 1000
        self.objects = {}
        self.files = {k.lower(): v for k, v in (files or {}).items()}
        self.open_files = {}
        self.registry = dict(registry or {})
        self.ticks = 1000000
        self.timer_val = 40000.0
        self.now_val = 37500.5
        self.tlb = None
        self.layout_of = None             # type name -> UdtLayout or None (nested records)

    # ------------------------------------------------------------------ plumbing
    def rng(self, key):
        h = hashlib.md5(('%s|%d|%r' % (self.seed, len(self.log), key)).encode('utf-8', 'replace')).digest()
        return random.Random(int.from_bytes(h[:8], 'little'))

    def event(self, kind, key, resp=None):
        if len(self.log) >= self.max_events:
            raise StopRun('event budget')
        st = self.snapshot() if self.snapshot else None
        ev = Event(kind, key, st, resp)
        self.log.append(ev)
        return ev

    def new_obj(self, iface, kind='com'):
        self.next_oid += 1
        o = ObjRef(self.next_oid, iface, kind)
        self.objects[o.oid] = o
        return o

    # ------------------------------------------------------------------ value generators
    def gen_int(self, r, t='Long'):
        pool = (self.constants or INT_POOL) if self.focus else INT_POOL + self.constants * 3
        if t == 'Long' and not self.focus:
            pool = pool + TIMER_POOL        # timestamps like the GetTickCount values the world hands out
        n = r.choice(pool) if r.random() < 0.85 else r.randint(-50, 2000)
        if t == 'Byte':
            n &= 0xff
        elif t == 'Integer':
            n = max(-32768, min(32767, n))
        elif t == 'Boolean':
            n = r.choice((0, -1))
        return V(t, n)

    def string_pool(self):
        if self.focus and self.strings:
            return self.strings + [s.upper() for s in self.strings] + ['']
        return STRING_POOL + SETTINGS['strings'] + self.strings * 2

    def gen_value(self, r, t, layout=None, depth=0):
        if layout is None and self.layout_of is not None and t not in INTEGRAL:
            layout = self.layout_of(t)
        if t in INTEGRAL:
            return self.gen_int(r, t)
        if t == 'Single':
            return V('Single', f32(r.choice([0.0, 0.5, 1.0, 2.0, -1.5, 100.25, r.uniform(-1000, 1000)] +
                                            [float(x) for x in TIMER_POOL])))
        if t in ('Double', 'Date'):
            if t == 'Date':
                return V('Date', 37000.0 + r.randint(0, 1000) + r.randint(0, 86399) / 86400.0)
            if self.floats and r.random() < (0.7 if self.focus else 0.3):
                return V('Double', r.choice(self.floats) + r.choice((0.0, 0.0, 0.5, -0.5)))
            return V('Double', r.choice([0.0, 0.5, 1.0, 3.0, -2.25, 1e6, r.uniform(-1e4, 1e4)] +
                                        [float(x) for x in TIMER_POOL]))
        if t == 'Currency':
            return V('Currency', r.randint(-100000, 100000))
        if t == 'String':
            return V('String', r.choice(self.string_pool()))
        if t == 'Variant':
            k = r.random()
            if k < 0.2:
                return EMPTY
            if k < 0.6:
                return self.gen_int(r, 'Integer').as_variant()
            if k < 0.8:
                return self.gen_int(r, 'Long').as_variant()
            return V('String', r.choice(self.string_pool()), True)
        if layout is not None:
            rec = VBRecord(layout)
            if depth < 2:
                for f in layout.fields:
                    name, ft, off, size, count = f
                    if count:
                        continue
                    rec.vals[name.lower()] = self.gen_value(r, ft, None, depth + 1)
            elif self.layout_of is not None:
                pass
            return V('Record', rec)
        # an object type
        return V('Object', self.new_obj(t))

    # ------------------------------------------------------------------ the rest of the program
    def maybe_writes(self, r, p):
        """Random changes to fields the procedure reads (other code running in between)."""
        if not self.candidates or r.random() >= p or not self.apply_writes:
            return []
        writes = []
        for _ in range(r.choice((1, 1, 2))):
            name, t, layout = r.choice(self.candidates)
            if t == 'Object' or (t not in INTEGRAL and t not in ('Single', 'Double', 'String', 'Variant',
                                                                   'Date', 'Currency')):
                continue
            writes.append((name, self.gen_value(r, t, layout)))
        if writes:
            self.apply_writes(writes)
        return writes

    # ------------------------------------------------------------------ events
    def call_proc(self, name, args, ret_type, ret_layout=None, out_types=None):
        """A call to another user procedure (not executed: its effects are scripted). out_types:
        {param index: class} for ByRef object parameters - the procedure may Set a new object there
        (a loader: `Set buffer = ...`); then returns (return value, {index: V})."""
        key = ('proc', name.lower(), tuple(canon(a) for a in args))
        r = self.rng(key)
        ev = self.event('proc', key)
        ret = None
        if ret_type:
            ret = self.gen_value(r, ret_type, ret_layout)
        outs = {}
        for i, t in sorted((out_types or {}).items()):
            if r.random() < 0.85:
                outs[i] = V('Object', self.new_obj(t))
        ev.resp = (canon(ret), tuple((i, canon(v)) for i, v in sorted(outs.items()))) if outs else canon(ret)
        self.maybe_writes(r, 0.35)
        return ret if out_types is None else (ret, outs)

    def com_call(self, obj, member, kind, args, out_types, ret_type, ret_layout=None):
        """obj.member(args) on a COM object. out_types: {param index: (type, layout)} for ByRef
        outputs the callee fills; returns (return value V or None, {index: V})."""
        oid = obj.oid if obj is not None else 0
        key = ('com', oid, member.lower(), kind, tuple(canon(a) for a in args))
        r = self.rng(key)
        ev = self.event('com', key)
        outs = {}
        for i, (t, lay) in sorted(out_types.items()):
            outs[i] = self.gen_out(r, t, lay, member)
        ret = None
        if ret_type:
            ret = self.gen_ret(r, ret_type, ret_layout, member, obj)
        ev.resp = (canon(ret), tuple((i, canon(v)) for i, v in sorted(outs.items())))
        return ret, outs

    def gen_out(self, r, t, layout, member):
        if member.lower() == 'getdevicestatekeyboard' and layout is not None:
            rec = VBRecord(layout)
            arr = rec.vals.get('key')
            if arr is not None and arr.t == 'Array':
                for k in range(len(arr.v.data)):
                    arr.v.data[k] = V('Byte', 0)
                for _ in range(r.choice((0, 0, 1, 1, 2, 3))):
                    k = r.choice([0x1d, 0x9d, 0x38, 0xb8, 0xcb, 0xcd, 0xc8, 0xd0, 0x2a, 0x36, 0x1c, 0x01,
                                  0x39, 0x23, 0x02, 0x03, 0x04, 0x19, 0x26, 0x14, 0xc9, 0xd1, 0x52, 0x9c,
                                  r.randint(0, 255)])
                    arr.v.data[k] = V('Byte', 0x80)
            return V('Record', rec)
        return self.gen_value(r, t, layout)

    def gen_ret(self, r, t, layout, member, obj):
        m = member.lower()
        if m == 'getstatus':
            return V('Long', r.choice((0, 0, 1, 1, 1, 4, 5)))
        if m in ('path',):
            return V('String', SETTINGS['app_path'])
        if m == 'hwnd':
            return V('Long', 0x1234)
        if t == 'Object' or (t not in INTEGRAL and t not in ('Single', 'Double', 'String', 'Variant', 'Date',
                                                              'Currency') and layout is None):
            return V('Object', self.new_obj(t))
        return self.gen_value(r, t, layout)

    def new_object(self, cls):
        key = ('new', cls.lower())
        self.event('new', key)
        o = self.new_obj(cls)
        return V('Object', o)

    def api(self, name, args, ret_type='Long'):
        key = ('api', name.lower(), tuple(canon(a) for a in args))
        r = self.rng(key)
        ev = self.event('api', key)
        n = name.lower()
        if n == 'gettickcount':
            self.ticks += r.choice((0, 1, 15, 16, 100, 250, 1000, 5000, 30000))
            ret = V('Long', self.ticks)
        elif n == 'timegettime':
            self.ticks += r.choice((0, 1, 16, 100, 1000))
            ret = V('Long', self.ticks)
        else:
            ret = self.gen_int(r, ret_type or 'Long') if ret_type else None
        ev.resp = canon(ret)
        return ret

    def doevents(self):
        key = ('doevents',)
        r = self.rng(key)
        ev = self.event('doevents', key)
        w = self.maybe_writes(r, 0.5)
        ev.resp = tuple((n, canon(v)) for n, v in w)

    def rnd(self, arg):
        key = ('rnd', canon(arg) if arg is not None else None)
        r = self.rng(key)
        ev = self.event('rnd', key)
        v = V('Single', f32(r.choice([0.0, 0.1, 0.25, 0.5, 0.75, 0.999, r.random()])))
        ev.resp = canon(v)
        return v

    def randomize(self, arg):
        self.event('randomize', ('randomize', canon(arg) if arg is not None else None))

    def timer(self):
        key = ('timer',)
        r = self.rng(key)
        ev = self.event('timer', key)
        self.timer_val += r.choice((0.0, 0.05, 1.0, 10.0))
        v = V('Single', f32(self.timer_val))
        ev.resp = canon(v)
        return v

    def now(self, what='now'):
        key = (what,)
        r = self.rng(key)
        ev = self.event(what, key)
        self.now_val += r.choice((0.0, 1.0 / 86400, 60.0 / 86400, 3700.0 / 86400))
        if what == 'date':
            v = V('Date', float(int(self.now_val)))
        elif what == 'time':
            v = V('Date', self.now_val - int(self.now_val))
        else:
            v = V('Date', self.now_val)
        ev.resp = canon(v)
        return v

    def msgbox(self, prompt, buttons, title):
        key = ('msgbox', canon(prompt), canon(buttons), canon(title))
        r = self.rng(key)
        ev = self.event('msgbox', key)
        v = V('Integer', r.choice((1, 2, 6, 7)))
        ev.resp = canon(v)
        return v

    def simple(self, kind, *args):
        """Events without an answer: Beep, End, Unload, Randomize, Kill, SaveSetting..."""
        self.event(kind, (kind,) + tuple(canon(a) if isinstance(a, V) else a for a in args))
        if kind == 'end':
            raise StopRun('End')

    def error(self, num):
        self.event('error', ('error', num))

    # ------------------------------------------------------------------ registry
    def savesetting(self, app, sec, key, val):
        k = (app.lower(), sec.lower(), key.lower())
        self.event('savesetting', ('savesetting',) + k + (val,))
        self.registry[k] = val

    def getsetting(self, app, sec, key, default):
        k = (app.lower(), sec.lower(), key.lower())
        ev = self.event('getsetting', ('getsetting',) + k + (default,))
        v = self.registry.get(k, default)
        ev.resp = v
        return v

    # ------------------------------------------------------------------ files
    def norm_path(self, p):
        return p.replace('/', '\\').lower()

    def file_open(self, path, mode, num):
        p = self.norm_path(path)
        self.event('open', ('open', p, mode, num))
        if num in self.open_files:
            raise VBError(55, 'file already open')
        if mode == 'input':
            if p not in self.files:
                raise VBError(53, 'file not found: %s' % path)
            self.open_files[num] = {'path': p, 'mode': mode, 'pos': 0}
        elif mode == 'output':
            self.files[p] = ''
            self.open_files[num] = {'path': p, 'mode': mode, 'pos': 0}
        elif mode == 'append':
            self.files.setdefault(p, '')
            self.open_files[num] = {'path': p, 'mode': mode, 'pos': len(self.files[p])}
        else:
            self.files.setdefault(p, '')
            self.open_files[num] = {'path': p, 'mode': mode, 'pos': 0}

    def file_close(self, num):
        self.event('close', ('close', num))
        self.open_files.pop(num, None)

    def _f(self, num, want):
        f = self.open_files.get(num)
        if f is None:
            raise VBError(52, 'file #%d not open' % num)
        if want and f['mode'] not in want:
            raise VBError(54, 'bad file mode')
        return f

    def file_write_text(self, num, text, kind):
        f = self._f(num, ('output', 'append'))
        self.event(kind, (kind, num, text))
        self.files[f['path']] += text

    def file_input_field(self, num):
        """Next comma / newline separated field for Input # (raw text, quotes removed)."""
        f = self._f(num, ('input',))
        data = self.files[f['path']]
        i = f['pos']
        while i < len(data) and data[i] in ' \t':
            i += 1
        if i >= len(data):
            raise VBError(62, 'input past end of file')
        if data[i] == '"':
            j = data.find('"', i + 1)
            if j < 0:
                j = len(data)
            field = data[i + 1:j]
            i = j + 1
            while i < len(data) and data[i] in ' \t':
                i += 1
        else:
            j = i
            while j < len(data) and data[j] not in ',\r\n':
                j += 1
            field = data[i:j].strip()
            i = j
        if i < len(data) and data[i] == ',':
            i += 1
        elif i < len(data) and data[i] in '\r\n':
            if data[i] == '\r' and i + 1 < len(data) and data[i + 1] == '\n':
                i += 2
            else:
                i += 1
        f['pos'] = i
        self.event('input', ('input', num, field))
        return field

    def file_line_input(self, num):
        f = self._f(num, ('input',))
        data = self.files[f['path']]
        i = f['pos']
        if i >= len(data):
            raise VBError(62, 'input past end of file')
        j = i
        while j < len(data) and data[j] not in '\r\n':
            j += 1
        line = data[i:j]
        if j < len(data) and data[j] == '\r' and j + 1 < len(data) and data[j + 1] == '\n':
            j += 2
        elif j < len(data):
            j += 1
        f['pos'] = j
        self.event('lineinput', ('lineinput', num, line))
        return line

    def file_eof(self, num):
        f = self._f(num, None)
        data = self.files.get(f['path'], '')
        r = f['pos'] >= len(data)
        self.event('eof', ('eof', num, r))
        return r

    def file_lof(self, num):
        f = self._f(num, None)
        n = len(self.files.get(f['path'], '').encode('latin-1', 'replace'))
        self.event('lof', ('lof', num, n))
        return n

    def kill(self, path):
        p = self.norm_path(path)
        self.event('kill', ('kill', p))
        if p not in self.files:
            raise VBError(53, 'file not found')
        del self.files[p]

    def dir(self, path, attrs):
        p = self.norm_path(path) if path is not None else None
        ev = self.event('dir', ('dir', p, attrs))
        if p is not None and p in self.files:
            name = p.split('\\')[-1]
            ev.resp = name
            return name
        ev.resp = ''
        return ''


# ---------------------------------------------------------------------------- shared text formats
def print_text(items, newline=True):
    """Print # formatting: items [(V or None, sep)], sep ';' (no space), ',' (next 14-column zone)."""
    out = ''
    for v, sep in items:
        if v is not None:
            if v.t in ('Integer', 'Long', 'Byte', 'Single', 'Double', 'Currency'):
                s = to_str(v)
                out += (s if s.startswith('-') else ' ' + s) + ' '
            elif v.t == 'Boolean':
                out += to_str(v)
            elif v.t == 'Date':
                out += to_str(v)
            elif v.t in ('Empty',):
                pass
            elif v.t == 'Null':
                out += 'Null'
            else:
                out += to_str(v)
        if sep == ',':
            out += ' ' * (14 - len(out) % 14)
    end_sep = items[-1][1] if items else None
    if newline and end_sep not in (';', ','):
        out += '\r\n'
    return out


def write_text(values):
    """Write # formatting: comma separated, strings quoted, Booleans as #TRUE#."""
    parts = []
    for v in values:
        if v.t == 'String':
            parts.append('"%s"' % v.v)
        elif v.t == 'Boolean':
            parts.append('#TRUE#' if v.v else '#FALSE#')
        elif v.t == 'Empty':
            parts.append('')
        elif v.t == 'Null':
            parts.append('#NULL#')
        elif v.t == 'Date':
            parts.append('#%s#' % to_str(v))
        else:
            parts.append(to_str(v))
    return ','.join(parts) + '\r\n'


def input_value(text, t):
    """Value read by Input # into a variable of type t."""
    if t == 'String':
        return V('String', text)
    if t == 'Variant':
        n = str_to_num(text, err=False)
        if text.upper() in ('#TRUE#', '#FALSE#'):
            return V('Boolean', -1 if text.upper() == '#TRUE#' else 0, True)
        if n is None:
            return V('String', text, True)
        if n == int(n) and -32768 <= n <= 32767:
            return V('Integer', int(n), True)
        if n == int(n) and -2147483648 <= n <= 2147483647:
            return V('Long', int(n), True)
        return V('Double', n, True)
    if t == 'Boolean':
        u = text.upper()
        if u in ('#TRUE#', 'TRUE'):
            return V('Boolean', -1)
        if u in ('#FALSE#', 'FALSE'):
            return V('Boolean', 0)
    n = str_to_num(text, err=False)
    if n is None:
        n = 0.0
    return convert(V('Double', n), t)
