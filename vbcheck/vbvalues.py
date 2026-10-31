"""VB6 value semantics shared by the source interpreter and the machine-code runtime models.

A value is V(t, v): t is the VB type name, v the Python payload:
  Integer / Long / Byte / Boolean -> int (Boolean is 0 or -1)
  Single / Double / Date          -> float (Single already rounded to 32 bits when stored)
  Currency                        -> int, scaled by 10000
  String                          -> str
  Object                          -> ObjRef or None (Nothing)
  Array                           -> VBArray
  Record                          -> VBRecord
  Empty / Null / Missing          -> None
`variant` is True when the value lives in a Variant: VB then promotes on overflow instead of failing.

Number <-> string conversions are implemented once here and used by both sides of the comparison, so
their exact VB formatting rules do not affect the check (only how the two sides use them does).
"""
import math
import struct

INT_RANGE = {'Byte': (0, 255), 'Integer': (-32768, 32767), 'Boolean': (-32768, 32767),
             'Long': (-2147483648, 2147483647)}
NUMERIC = ('Byte', 'Boolean', 'Integer', 'Long', 'Single', 'Double', 'Currency', 'Date')
INTEGRAL = ('Byte', 'Boolean', 'Integer', 'Long')
# rank for arithmetic result types
RANK = {'Byte': 1, 'Boolean': 2, 'Integer': 2, 'Long': 3, 'Single': 4, 'Double': 5, 'Currency': 6, 'Date': 7}
VT = {'Empty': 0, 'Null': 1, 'Integer': 2, 'Long': 3, 'Single': 4, 'Double': 5, 'Currency': 6, 'Date': 7,
      'String': 8, 'Object': 9, 'Error': 10, 'Boolean': 11, 'Variant': 12, 'Byte': 17}
VT_NAME = {v: k for k, v in VT.items()}


class VBError(Exception):
    """A VB runtime error (number as in VB: 5 invalid call, 6 overflow, 9 subscript, 11 division by
    zero, 13 type mismatch, 52 bad file name, 53 file not found, 54 bad file mode, 62 input past end,
    91 object not set, 94 invalid use of Null)."""

    def __init__(self, num, msg=''):
        self.num = num
        self.msg = msg
        super().__init__('VB error %d %s' % (num, msg))


class ObjRef:
    """Identity of an object (shared by both sides: the world hands out ids)."""
    __slots__ = ('oid', 'iface', 'kind')

    def __init__(self, oid, iface, kind='com'):
        self.oid = oid
        self.iface = iface
        self.kind = kind

    def __repr__(self):
        return '<obj %d %s>' % (self.oid, self.iface)

    def __eq__(self, o):
        return isinstance(o, ObjRef) and o.oid == self.oid

    def __hash__(self):
        return self.oid


class V:
    __slots__ = ('t', 'v', 'variant')

    def __init__(self, t, v, variant=False):
        self.t = t
        self.v = v
        self.variant = variant

    def __repr__(self):
        return '%s%s(%r)' % ('Var:' if self.variant else '', self.t, self.v)

    def as_variant(self):
        if self.variant:
            return self
        return V(self.t, self.v, True)

    def plain(self):
        if not self.variant:
            return self
        return V(self.t, self.v, False)


EMPTY = V('Empty', None, True)
NULL = V('Null', None, True)
MISSING = V('Missing', None, True)
NOTHING = V('Object', None)
TRUE = V('Boolean', -1)
FALSE = V('Boolean', 0)


def boolv(b):
    return V('Boolean', -1 if b else 0)


class VBArray:
    """A SAFEARRAY: bounds [(lb, ub), ...] (first dimension first), row-major element list
    (first index varies fastest, like VB/COM)."""
    __slots__ = ('etype', 'bounds', 'data', 'fixed', 'rec')

    def __init__(self, etype, bounds, data=None, fixed=False, rec=None):
        self.etype = etype
        self.bounds = list(bounds)
        self.fixed = fixed
        self.rec = rec            # UdtLayout for record elements
        n = 1
        for lb, ub in self.bounds:
            n *= max(0, ub - lb + 1)
        self.data = data if data is not None else [default_value(etype, rec) for _ in range(n)]

    def offset(self, idx):
        if len(idx) != len(self.bounds):
            raise VBError(9, 'wrong number of dimensions')
        off = 0
        mul = 1
        for i, (lb, ub) in zip(idx, self.bounds):
            if i < lb or i > ub:
                raise VBError(9, 'subscript %d not in %d..%d' % (i, lb, ub))
            off += (i - lb) * mul
            mul *= ub - lb + 1
        return off

    def __repr__(self):
        return '<array %s %s>' % (self.etype, self.bounds)


class UdtLayout:
    """Field layout of a user-defined type: [(name, type, offset, size, array count)]."""

    def __init__(self, name, fields, size):
        self.name = name
        self.fields = fields
        self.size = size
        self.by_name = {f[0].lower(): f for f in fields}


class VBRecord:
    __slots__ = ('layout', 'vals')

    def __init__(self, layout, vals=None):
        self.layout = layout
        self.vals = vals if vals is not None else {
            f[0].lower(): (default_value(f[1]) if not f[4] else
                           V('Array', VBArray(f[1], [(0, f[4] - 1)], fixed=True))) for f in layout.fields}

    def copy(self):
        return VBRecord(self.layout, {k: copy_value(v) for k, v in self.vals.items()})


def default_value(t, rec=None):
    if t in INTEGRAL or t == 'Currency':
        return V(t, 0)
    if t in ('Single', 'Double', 'Date'):
        return V(t, 0.0)
    if t == 'String':
        return V('String', '')
    if t == 'Variant':
        return EMPTY
    if rec is not None:
        return V('Record', VBRecord(rec))
    return V('Object', None)


def copy_value(x):
    if isinstance(x, V):
        if x.t == 'Record':
            return V('Record', x.v.copy(), x.variant)
        if x.t == 'Array':
            return V('Array', copy_array(x.v), x.variant)
        return x
    return x


def copy_array(a):
    if a is None:
        return None
    return VBArray(a.etype, a.bounds, [copy_value(e) for e in a.data], a.fixed, a.rec)


# ------------------------------------------------------------------------------ numbers
def round_half_even(x):
    """VB's rounding for CInt / CLng / implicit conversion (banker's rounding)."""
    if math.isnan(x) or math.isinf(x):
        raise VBError(6)
    f = math.floor(x)
    d = x - f
    if d > 0.5:
        return int(f) + 1
    if d < 0.5:
        return int(f)
    return int(f) if int(f) % 2 == 0 else int(f) + 1


FP_CHECKS = [True]      # the program's floating-point error checks (set by the interpreter)


def f32(x):
    """Round a Python float to single precision (overflow -> VB error 6, or infinity like the FPU when the
    program was compiled without floating-point error checks)."""
    try:
        return struct.unpack('<f', struct.pack('<f', x))[0]
    except OverflowError:
        if not FP_CHECKS[0]:
            return math.copysign(math.inf, x)
        raise VBError(6)


def check_range(t, n):
    lo, hi = INT_RANGE[t]
    if n < lo or n > hi:
        raise VBError(6, '%d does not fit %s' % (n, t))
    return n


# ------------------------------------------------------------------------------ number <-> string
def num_to_str(t, x):
    """CStr of a number (no leading space)."""
    if t == 'Boolean':
        return 'True' if x else 'False'
    if t in ('Byte', 'Integer', 'Long'):
        return str(int(x))
    if t == 'Currency':
        s = '%.4f' % (x / 10000.0)
        s = s.rstrip('0').rstrip('.')
        return s
    if t == 'Date':
        return date_to_str(x)
    digits = 7 if t == 'Single' else 15
    if x == 0:
        return '0'
    if math.isinf(x) or math.isnan(x):
        raise VBError(6)
    s = '%.*G' % (digits, x)
    if 'E' in s:
        mant, exp = s.split('E')
        e = int(exp)
        if -digits < e < 0 and len(mant.replace('-', '').replace('.', '')) - e <= digits + 4:
            s = ('%.*f' % (digits - e - 1, x)).rstrip('0').rstrip('.')
        else:
            s = '%sE%s%02d' % (mant, '-' if e < 0 else '+', abs(e))
    return s


def str_to_num(s, err=True):
    """Parse a numeric string as VB's implicit conversion does (Double); None if not numeric."""
    t = s.strip()
    if not t:
        if err:
            raise VBError(13, 'empty string is not a number')
        return None
    low = t.lower()
    try:
        if low.startswith('&h'):
            return float(int(low[2:].rstrip('&'), 16))
        if low.startswith('&o'):
            return float(int(low[2:].rstrip('&'), 8))
        if low in ('true',):
            return -1.0
        if low in ('false',):
            return 0.0
        return float(t.replace(',', ''))
    except ValueError:
        if err:
            raise VBError(13, '%r is not a number' % s)
        return None


def val_of(s):
    """Val(): the longest numeric prefix (spaces ignored), 0 if none."""
    t = ''.join(ch for ch in s if ch not in ' \t\n')
    low = t.lower()
    if low.startswith('&h'):
        h = ''
        for ch in low[2:]:
            if ch in '0123456789abcdef':
                h += ch
            else:
                break
        if not h:
            return 0.0
        v = int(h, 16)
        if len(h) <= 4 and v >= 0x8000:
            v -= 0x10000
        elif v >= 0x80000000:
            v -= 1 << 32
        return float(v)
    best = 0.0
    i = 0
    num = ''
    if i < len(t) and t[i] in '+-':
        num += t[i]
        i += 1
    digits = False
    while i < len(t) and t[i].isdigit():
        num += t[i]
        i += 1
        digits = True
    if i < len(t) and t[i] == '.':
        num += '.'
        i += 1
        while i < len(t) and t[i].isdigit():
            num += t[i]
            i += 1
            digits = True
    if digits and i < len(t) and t[i] in 'eEdD':
        j = i + 1
        e = ''
        if j < len(t) and t[j] in '+-':
            e += t[j]
            j += 1
        if j < len(t) and t[j].isdigit():
            while j < len(t) and t[j].isdigit():
                e += t[j]
                j += 1
            num += 'e' + e
    if digits:
        try:
            best = float(num)
        except ValueError:
            best = 0.0
    return best


# ------------------------------------------------------------------------------ dates
DATE_EPOCH_DAYS = 693594     # date(1899,12,30).toordinal()


def date_parts(d):
    """OLE date -> (year, month, day, hour, minute, second)."""
    import datetime
    if not (-657434 <= d < 2958466):       # VB dates run from 1 Jan 100 to 31 Dec 9999
        raise VBError(6, 'date out of range')
    days = math.floor(d)
    frac = abs(d - days)
    secs = int(round(frac * 86400))
    if secs >= 86400:
        secs -= 86400
        days += 1
    dt = datetime.date.fromordinal(DATE_EPOCH_DAYS + days)
    return dt.year, dt.month, dt.day, secs // 3600, (secs // 60) % 60, secs % 60


def date_to_str(d):
    y, mo, da, h, mi, s = date_parts(d)
    has_date = math.floor(d) != 0
    has_time = (h, mi, s) != (0, 0, 0)
    out = []
    if has_date or not has_time:
        out.append('%d/%d/%d' % (mo, da, y))
    if has_time:
        ap = 'AM' if h < 12 else 'PM'
        h12 = h % 12 or 12
        out.append('%d:%02d:%02d %s' % (h12, mi, s, ap))
    return ' '.join(out)


# ------------------------------------------------------------------------------ conversions
def to_number(x, want=None):
    """Numeric payload of x (Python int/float) and its numeric type."""
    t = x.t
    if t in NUMERIC:
        return x.v, t
    if t == 'Empty':
        return 0, 'Integer'
    if t == 'String':
        n = str_to_num(x.v)
        return n, 'Double'
    if t == 'Null':
        raise VBError(94)
    if t == 'Missing':
        raise VBError(448, 'missing argument')
    raise VBError(13, 'not a number: %s' % t)


def convert(x, t):
    """Value x converted to declared type t (assignment / ByVal / CInt-style conversion)."""
    if t == 'Variant':
        if x.t == 'Missing':
            return x
        return x.as_variant() if not x.variant else x
    if x.t == t and t not in ('Single',):
        return x if not x.variant else V(t, x.v)
    if t in INTEGRAL:
        if x.t == 'String':
            n = str_to_num(x.v)
        elif x.t == 'Empty':
            n = 0
        elif x.t in NUMERIC:
            n = x.v
        elif x.t == 'Null':
            raise VBError(94)
        else:
            raise VBError(13, '%s to %s' % (x.t, t))
        if isinstance(n, float):
            n = round_half_even(n)
        if t == 'Boolean':
            return V('Boolean', -1 if n != 0 else 0)
        return V(t, check_range(t, int(n)))
    if t in ('Single', 'Double', 'Date'):
        if x.t == 'String':
            if t == 'Date':
                raise VBError(13, 'string to date')
            n = str_to_num(x.v)
        elif x.t == 'Empty':
            n = 0.0
        elif x.t in NUMERIC:
            n = x.v
            if x.t == 'Currency':
                n = n / 10000.0
        elif x.t == 'Null':
            raise VBError(94)
        else:
            raise VBError(13, '%s to %s' % (x.t, t))
        n = float(n)
        if t == 'Single':
            n = f32(n)
        return V(t, n)
    if t == 'Currency':
        n, nt = to_number(x)
        if nt == 'Currency':
            return V('Currency', n)
        return V('Currency', round_half_even(float(n) * 10000))
    if t == 'String':
        return V('String', to_str(x))
    if t in ('Object',) or t not in NUMERIC + ('String', 'Variant', 'Array', 'Record'):
        if x.t == 'Object':
            return V('Object', x.v)
        if x.t == 'Empty':
            raise VBError(13, 'Empty to object')
        raise VBError(13, '%s to object %s' % (x.t, t))
    if t == 'Array':
        if x.t == 'Array':
            return V('Array', x.v)
        raise VBError(13)
    if t == 'Record':
        if x.t == 'Record':
            return V('Record', x.v)
        raise VBError(13)
    raise VBError(13, '%s to %s' % (x.t, t))


def to_str(x):
    t = x.t
    if t == 'String':
        return x.v
    if t == 'Empty':
        return ''
    if t == 'Null':
        raise VBError(94)
    if t in NUMERIC:
        return num_to_str(t, x.v)
    raise VBError(13, '%s to String' % t)


def to_bool(x):
    """Truth value of a condition."""
    if x.t == 'Boolean':
        return x.v != 0
    if x.t in NUMERIC:
        return x.v != 0
    if x.t == 'String':
        low = x.v.strip().lower()
        if low == 'true':
            return True
        if low == 'false':
            return False
        return str_to_num(x.v) != 0
    if x.t == 'Empty':
        return False
    if x.t == 'Null':
        return False
    raise VBError(13, '%s as condition' % x.t)


def to_int(x, t='Long'):
    return convert(x, t).v


def to_float(x):
    return convert(x, 'Double').v


def str_v(x):
    """Str(): like CStr but with a leading space for non-negative numbers."""
    if x.t == 'String':
        n, _ = to_number(x)
        x = V('Double', n)
    s = to_str(x)
    if x.t in NUMERIC and x.t != 'Boolean' and not s.startswith('-'):
        s = ' ' + s
    return s


# ------------------------------------------------------------------------------ operators
def _num_type(x):
    """Numeric type of an operand for arithmetic (strings convert to Double, Empty to Integer)."""
    if x.t in NUMERIC:
        return 'Integer' if x.t == 'Boolean' else x.t
    if x.t == 'String':
        return 'Double'
    if x.t == 'Empty':
        return 'Integer'
    if x.t == 'Null':
        raise VBError(94)
    raise VBError(13, 'arithmetic on %s' % x.t)


def _val(x):
    if x.t in NUMERIC:
        if x.t == 'Currency':
            return x.v / 10000.0
        return x.v
    if x.t == 'String':
        return str_to_num(x.v)
    if x.t == 'Empty':
        return 0
    raise VBError(13)


def wrap_int(t, n):
    """n wrapped into integer type t (what code compiled without overflow checks produces)."""
    if t == 'Byte':
        return n & 0xff
    bits = 16 if t in ('Integer', 'Boolean') else 32
    n &= (1 << bits) - 1
    return n - (1 << bits) if n >= 1 << (bits - 1) else n


def _result(t, n, variant, checks=True):
    """Fit n into type t; a Variant result widens instead of overflowing; without overflow checks a
    typed integer result wraps around."""
    if t in INTEGRAL:
        if isinstance(n, float):
            n = round_half_even(n)
        lo, hi = INT_RANGE[t]
        if lo <= n <= hi:
            return V(t, n, variant)
        if not variant and not checks:
            return V(t, wrap_int(t, n), False)
        if not variant:
            raise VBError(6, '%d overflows %s' % (n, t))
        for t2 in ('Integer', 'Long'):
            lo, hi = INT_RANGE[t2]
            if RANK[t2] > RANK[t] and lo <= n <= hi:
                return V(t2, n, True)
        return V('Double', float(n), True)
    if t == 'Single':
        if variant:
            try:
                return V('Single', f32(n), True)
            except VBError:
                return V('Double', float(n), True)
        return V('Single', float(n), False)      # kept at full precision until stored
    if t == 'Currency':
        return V('Currency', round_half_even(n * 10000), variant)
    if (math.isinf(n) or math.isnan(n)) and FP_CHECKS[0]:
        raise VBError(6)            # (without floating-point checks the FPU just goes on with Inf / NaN)
    return V(t, float(n), variant)


def arith_type(a, b, op):
    ta, tb = _num_type(a), _num_type(b)
    if a.t == 'String' and b.t == 'String' and op == '+':
        return 'String'
    if 'Date' in (ta, tb) and op in ('+', '-'):
        return 'Date' if not (ta == 'Date' and tb == 'Date' and op == '-') else 'Double'
    if 'Currency' in (ta, tb):
        return 'Currency' if 'Double' not in (ta, tb) and 'Single' not in (ta, tb) else 'Double'
    if ta == 'Double' or tb == 'Double':
        return 'Double'
    if ta == 'Single' or tb == 'Single':
        if 'Long' in (ta, tb):
            return 'Double'
        return 'Single'
    if ta == 'Long' or tb == 'Long':
        return 'Long'
    if ta == 'Byte' and tb == 'Byte':
        return 'Byte'
    return 'Integer'


def binop(op, a, b, text_compare=False, checks=True, fp_checks=True):
    """a op b. checks / fp_checks: the program's integer overflow and floating-point error checks
    (they only apply to typed arithmetic; Variant arithmetic is done by the runtime, which checks)."""
    variant = a.variant or b.variant
    if op == '&':
        if a.t == 'Null' and b.t == 'Null':
            return NULL
        sa = '' if a.t == 'Null' else to_str(a)
        sb = '' if b.t == 'Null' else to_str(b)
        return V('String', sa + sb, variant)
    if a.t == 'Null' or b.t == 'Null':
        if op in ('And', 'Or') or op in ('=', '<>', '<', '>', '<=', '>='):
            return NULL
        return NULL
    if op in ('=', '<>', '<', '>', '<=', '>='):
        c = compare(a, b, text_compare)
        r = {'=': c == 0, '<>': c != 0, '<': c < 0, '>': c > 0, '<=': c <= 0, '>=': c >= 0}[op]
        return V('Boolean', -1 if r else 0, variant)
    if op == 'Is':
        if a.t != 'Object' or b.t != 'Object':
            raise VBError(13, 'Is needs objects')
        return boolv(a.v == b.v)
    if op in ('And', 'Or', 'Xor', 'Eqv', 'Imp'):
        return logic(op, a, b)
    if op == '+' and a.t == 'String' and b.t == 'String':
        return V('String', a.v + b.v, variant)
    if op in ('+', '-', '*'):
        t = arith_type(a, b, op)
        x, y = _val(a), _val(b)
        if t in INTEGRAL:
            x, y = int(x), int(y)
        n = x + y if op == '+' else x - y if op == '-' else x * y
        return _result(t, n, variant, checks)
    if op == '/':
        ta, tb = _num_type(a), _num_type(b)
        x, y = float(_val(a)), float(_val(b))
        if y == 0:
            if not fp_checks and not variant:
                return V('Double', math.nan if x == 0 else math.copysign(math.inf, x))
            raise VBError(6 if x == 0 else 11)
        t = 'Single' if ta in ('Byte', 'Integer', 'Single') and tb in ('Byte', 'Integer', 'Single') else 'Double'
        if 'Currency' in (ta, tb):
            t = 'Currency' if 'Double' not in (ta, tb) else 'Double'
        return _result(t, x / y, variant)
    if op in ('\\', 'Mod'):
        ta, tb = _num_type(a), _num_type(b)
        t = 'Long'
        if ta in ('Byte', 'Integer') and tb in ('Byte', 'Integer'):
            t = 'Byte' if ta == tb == 'Byte' else 'Integer'
        x = convert(V(ta, _val(a)), 'Long').v
        y = convert(V(tb, _val(b)), 'Long').v
        if y == 0:
            raise VBError(11)
        q = abs(x) // abs(y)
        if (x < 0) != (y < 0):
            q = -q
        n = q if op == '\\' else x - q * y
        return _result(t, n, variant, checks)
    if op == '^':
        x, y = float(_val(a)), float(_val(b))
        try:
            r = x ** y
        except ZeroDivisionError:
            raise VBError(5)
        if isinstance(r, complex):
            raise VBError(5)
        return _result('Double', r, variant)
    if op == 'Like':
        raise VBError(5, 'Like is not supported')
    raise VBError(5, 'unknown operator %s' % op)


def logic(op, a, b):
    variant = a.variant or b.variant
    if a.t == 'Boolean' and b.t == 'Boolean':
        t = 'Boolean'
    else:
        ta = 'Integer' if a.t in ('Boolean', 'Byte', 'Empty') else a.t
        tb = 'Integer' if b.t in ('Boolean', 'Byte', 'Empty') else b.t
        if ta == 'Integer' and tb == 'Integer':
            t = 'Integer'
            if a.t == 'Byte' and b.t == 'Byte':
                t = 'Byte'
        else:
            t = 'Long'
    x = convert(a, 'Long').v if t != 'Boolean' else a.v
    y = convert(b, 'Long').v if t != 'Boolean' else b.v
    if op == 'And':
        n = x & y
    elif op == 'Or':
        n = x | y
    elif op == 'Xor':
        n = x ^ y
    elif op == 'Eqv':
        n = ~(x ^ y)
    else:
        n = (~x) | y
    if t == 'Boolean':
        return V('Boolean', -1 if n & 1 else 0, variant)
    lo, hi = INT_RANGE[t]
    bits = {'Byte': 8, 'Integer': 16, 'Long': 32}[t]
    n &= (1 << bits) - 1
    if t != 'Byte' and n >= 1 << (bits - 1):
        n -= 1 << bits
    return V(t, n, variant)


def unop(op, a, checks=True):
    if op == 'Not':
        if a.t == 'Null':
            return NULL
        if a.t == 'Boolean':
            return V('Boolean', 0 if a.v else -1, a.variant)
        t = 'Integer' if a.t in ('Integer', 'Byte', 'Empty') else 'Long'
        if a.t == 'Byte':
            return V('Byte', (~a.v) & 0xff, a.variant)
        n = ~convert(a, t).v
        return V(t, n, a.variant)
    if op == 'neg':
        if a.t == 'Null':
            return NULL
        t = _num_type(a)
        if a.t == 'Boolean':
            t = 'Integer'
        if t == 'Byte':
            t = 'Integer'
        n = -_val(a)
        if t in INTEGRAL:
            n = int(n)
        return _result(t, n, a.variant, checks)
    raise VBError(5, 'unknown unary %s' % op)


def str_compare(x, y, text):
    if text:
        x, y = x.lower(), y.lower()
    return (x > y) - (x < y)


def compare(a, b, text_compare=False):
    """-1/0/1 comparison with VB's typed rules."""
    if a.t == 'String' and b.t == 'String':
        return str_compare(a.v, b.v, text_compare)
    if a.t == 'Object' or b.t == 'Object':
        raise VBError(13, 'comparing an object')
    if a.t == 'String' or b.t == 'String':
        if a.variant and b.variant:
            # Variant rules: a numeric Variant is less than a string Variant
            if a.t == 'String' and b.t in NUMERIC:
                return 1
            if b.t == 'String' and a.t in NUMERIC:
                return -1
        if a.t == 'Empty' or b.t == 'Empty':
            return str_compare(to_str(a), to_str(b), text_compare)
        x, y = _val(a), _val(b)
        return (x > y) - (x < y)
    x, y = _val(a), _val(b)
    if a.t == 'Single' or b.t == 'Single':
        # a Single variable compared with a value is compared at single precision only when both are
        # Singles; mixed comparisons happen at the wider precision
        pass
    return (x > y) - (x < y)


# ------------------------------------------------------------------------------ canonical form
def canon(x, depth=0):
    """Hashable canonical form of a value for comparing the two sides."""
    if x is None:
        return None
    if not isinstance(x, V):
        return ('raw', repr(x))
    t = x.t
    if t == 'Single':
        return ('Single', f32(x.v))
    if t in ('Double', 'Date'):
        return (t, float(x.v))
    if t == 'Object':
        return ('Object', x.v.oid if x.v is not None else 0)
    if t == 'Array':
        a = x.v
        if a is None:
            return ('Array', None)
        if depth > 3:
            return ('Array', tuple(a.bounds))
        return ('Array', a.etype, tuple(a.bounds), tuple(canon(e, depth + 1) for e in a.data))
    if t == 'Record':
        r = x.v
        return ('Record', r.layout.name, tuple(sorted((k, canon(v, depth + 1)) for k, v in r.vals.items())))
    return (t, x.v)
