"""Expression / statement IR for the VB6 native-code decompiler, with VB rendering."""

# --------------------------------------------------------------------------- expressions
PREC = {
    'Imp': 1, 'Eqv': 2, 'Xor': 3, 'Or': 4, 'And': 5, 'Not': 6,
    '=': 7, '<>': 7, '<': 7, '>': 7, '<=': 7, '>=': 7, 'Like': 7, 'Is': 7,
    '&': 8, '+': 9, '-': 9, 'Mod': 10, '\\': 11, '*': 12, '/': 12, 'neg': 13, '^': 14,
}


class Expr:
    ty = None          # VB type name hint
    side = False       # has side effects (calls)

    def children(self):
        return ()

    def walk(self):
        """All nodes of the expression DAG (each shared node once)."""
        seen = set()
        stack = [self]
        while stack:
            e = stack.pop()
            if id(e) in seen:
                continue
            seen.add(id(e))
            yield e
            for c in e.children():
                if c is not None and isinstance(c, Expr):
                    stack.append(c)

    def tree_size(self):
        """Size of the expression as a tree (memoized; shared nodes count every time)."""
        d = self.__dict__
        n = d.get('_n')
        if n is None:
            n = 1
            for c in self.children():
                if c is not None and isinstance(c, Expr):
                    n += c.tree_size()
            d['_n'] = n
        return n

    def has_side_effects(self):
        return any(isinstance(e, Expr) and e.side for e in self.walk())

    def __repr__(self):
        return render(self)


class Const(Expr):
    def __init__(self, v, ty=None):
        self.v = v
        self.ty = ty

    def key(self):
        return ('c', self.v)


class FConst(Expr):
    def __init__(self, v, ty='Double'):
        self.v = v
        self.ty = ty

    def key(self):
        return ('f', self.v)


class Str(Expr):
    ty = 'String'

    def __init__(self, s):
        self.s = s

    def key(self):
        return ('s', self.s)


class Var(Expr):
    """A storage location: kind in local/arg/field/global/ret; off = offset or address."""

    def __init__(self, kind, off, size=4, ty=None, name=None):
        self.kind = kind
        self.off = off
        self.size = size
        self.ty = ty
        self.name = name

    def key(self):
        return ('v', self.kind, self.off)


class Member(Expr):
    """Sub-field of a variable/record: base.field (e.g. a UDT member or variant part)."""

    def __init__(self, base, off, size=4, name=None, ty=None):
        self.base = base
        self.off = off
        self.size = size
        self.name = name
        self.ty = ty

    def children(self):
        return (self.base,)

    def key(self):
        return ('m', ekey(self.base), self.off)


class Deref(Expr):
    """Memory through a pointer value: *(base + off)."""

    def __init__(self, base, off=0, size=4, ty=None):
        self.base = base
        self.off = off
        self.size = size
        self.ty = ty

    def children(self):
        return (self.base,)

    def key(self):
        return ('d', ekey(self.base), self.off, self.size)


class Index(Expr):
    """Array element: arr(i, j...)"""

    def __init__(self, arr, idx, ty=None):
        self.arr = arr
        self.idx = list(idx)
        self.ty = ty

    def children(self):
        return [self.arr] + self.idx

    def key(self):
        return ('ix', ekey(self.arr), tuple(ekey(i) for i in self.idx))


class AddrOf(Expr):
    ty = 'Ptr'

    def __init__(self, e):
        self.e = e

    def children(self):
        return (self.e,)

    def key(self):
        return ('&', ekey(self.e))


class Bin(Expr):
    def __init__(self, op, a, b, ty=None):
        self.op = op
        self.a = a
        self.b = b
        self.ty = ty

    def children(self):
        return (self.a, self.b)

    def key(self):
        return ('b', self.op, ekey(self.a), ekey(self.b))


class Un(Expr):
    def __init__(self, op, a, ty=None):
        self.op = op
        self.a = a
        self.ty = ty

    def children(self):
        return (self.a,)

    def key(self):
        return ('u', self.op, ekey(self.a))


class Call(Expr):
    """Function call.  fn: name string; obj: object expression for methods/properties;
    kind: 'rt' builtin, 'proc' user procedure, 'com' object member, 'api' Declare, 'raw'."""
    side = True

    def __init__(self, fn, args=(), obj=None, kind='rt', ty=None, prop=None, pure=False, paren=True):
        self.fn = fn
        self.args = list(args)
        self.obj = obj
        self.kind = kind
        self.ty = ty
        self.prop = prop         # None | 'get' | 'let' | 'set'
        self.side = not pure
        self.paren = paren
        self.uid = None

    def children(self):
        return ([self.obj] if self.obj is not None else []) + [a for a in self.args if a is not None]

    def key(self):
        return ('call', id(self))


class Temp(Expr):
    """A value produced by an earlier statement (t = call ...)."""

    def __init__(self, name, ty=None):
        self.name = name
        self.ty = ty

    def key(self):
        return ('t', self.name)


class Me(Expr):
    ty = 'Object'

    def key(self):
        return ('me',)


class Missing(Expr):
    """An omitted optional argument."""

    def key(self):
        return ('missing',)


class Unknown(Expr):
    def __init__(self, text, ty=None):
        self.text = text
        self.ty = ty

    def key(self):
        return ('?', self.text)


class Raw(Expr):
    """Pre-rendered VB text."""

    def __init__(self, text, ty=None, prec=99):
        self.text = text
        self.ty = ty
        self.prec = prec

    def key(self):
        return ('raw', self.text)


def ekey(e):
    if e is None:
        return None
    if isinstance(e, Expr):
        d = e.__dict__
        k = d.get('_k')
        if k is None:
            k = e.key()
            d['_k'] = k
        return k
    return ('py', e)


def same(a, b):
    return ekey(a) == ekey(b)


# --------------------------------------------------------------------------- statements
class Stmt:
    addr = 0

    def exprs(self):
        return ()


class Assign(Stmt):
    def __init__(self, dst, src, addr=0, kind='let'):
        self.dst = dst
        self.src = src
        self.addr = addr
        self.kind = kind          # 'let' | 'set'

    def exprs(self):
        return (self.dst, self.src)


class ExprStmt(Stmt):
    def __init__(self, e, addr=0):
        self.e = e
        self.addr = addr

    def exprs(self):
        return (self.e,)


class Text(Stmt):
    """A complete VB statement as text (e.g. 'On Error Resume Next', 'End')."""

    def __init__(self, text, addr=0, args=()):
        self.text = text
        self.addr = addr
        self.args = list(args)    # expressions referenced (for analyses)

    def exprs(self):
        return tuple(self.args)


class Comment(Stmt):
    def __init__(self, text, addr=0):
        self.text = text
        self.addr = addr


class FmtStmt(Stmt):
    """Statement assembled from a template with expression slots: 'Print #{0}, {1}'."""

    def __init__(self, fmt, args, addr=0):
        self.fmt = fmt
        self.args = list(args)
        self.addr = addr

    def exprs(self):
        return tuple(self.args)


# --------------------------------------------------------------------------- rendering
NAMER = {'fn': None}


def vb_string(s):
    out = []
    buf = ''
    for ch in s:
        o = ord(ch)
        if ch == '"':
            buf += '""'
        elif o < 32 or o == 127:
            if buf:
                out.append('"%s"' % buf)
                buf = ''
            out.append({13: 'vbCr', 10: 'vbLf', 9: 'vbTab', 0: 'vbNullChar'}.get(o, 'Chr$(%d)' % o))
        else:
            buf += ch
    if buf or not out:
        out.append('"%s"' % buf)
    # merge vbCr & vbLf
    s2 = ' & '.join(out)
    return s2.replace('vbCr & vbLf', 'vbCrLf')


def fmt_float(v):
    if v == int(v) and abs(v) < 1e15:
        return '%d' % int(v)
    r = repr(v)
    if 'e' in r:
        m, e = r.split('e')
        return '%sE%s' % (m, e if e.startswith('-') else '+' + e.lstrip('+'))
    return r


def var_name(v):
    f = NAMER['fn']
    if f:
        n = f(v)
        if n:
            return n
    if v.name:
        return v.name
    if v.kind == 'local':
        return 'var_%X' % v.off
    if v.kind == 'arg':
        return 'arg_%X' % v.off
    if v.kind == 'field':
        return 'm_%X' % v.off
    if v.kind == 'global':
        return 'g_%X' % v.off
    if isinstance(v.off, str):
        return '%s_%s' % (v.kind, v.off)
    return '%s_%X' % (v.kind, v.off)


def render(e, parent_prec=0):
    if e is None:
        return ''
    if isinstance(e, Const):
        v = e.v
        if e.ty == 'Boolean':
            return 'True' if v else 'False'
        if isinstance(v, int) and e.ty in ('Long', None) and (v > 0xFFFF or v < -0xFFFF) and v > 0x7fffffff:
            v -= 1 << 32
        if isinstance(v, int) and v < 0:
            s = str(v)
            return '(%s)' % s if parent_prec >= PREC['neg'] else s
        if isinstance(v, int) and e.ty == 'Hex':
            return '&H%X' % v
        return str(v)
    if isinstance(e, FConst):
        s = fmt_float(e.v)
        if e.v < 0 and parent_prec >= PREC['neg']:
            return '(%s)' % s
        return s
    if isinstance(e, Str):
        s = vb_string(e.s)
        if ' & ' in s and parent_prec > PREC['&']:
            return '(%s)' % s
        return s
    if isinstance(e, Var):
        return var_name(e)
    if isinstance(e, Member):
        if e.name:
            return '%s.%s' % (render(e.base, 99), e.name)
        return '%s.field_%X' % (render(e.base, 99), e.off)
    if isinstance(e, Deref):
        b = render(e.base, 99)
        if e.off:
            return 'Mem%d(%s + %d)' % (e.size * 8, b, e.off)
        return 'Mem%d(%s)' % (e.size * 8, b)
    if isinstance(e, Index):
        return '%s(%s)' % (render(e.arr, 99), ', '.join(render(i) for i in e.idx))
    if isinstance(e, AddrOf):
        return 'VarPtr(%s)' % render(e.e)
    if isinstance(e, Temp):
        return e.name
    if isinstance(e, Me):
        return 'Me'
    if isinstance(e, Missing):
        return ''
    if isinstance(e, Unknown):
        return '[%s]' % e.text
    if isinstance(e, Raw):
        if e.prec < parent_prec:
            return '(%s)' % e.text
        return e.text
    if isinstance(e, Un):
        if e.op == 'neg':
            s = '-' + render(e.a, PREC['neg'])
            p = PREC['neg']
        elif e.op == 'Not':
            s = 'Not ' + render(e.a, PREC['Not'])
            p = PREC['Not']
        else:
            s = '%s(%s)' % (e.op, render(e.a))
            p = 99
        return '(%s)' % s if p < parent_prec else s
    if isinstance(e, Bin) and e.op == 'Is':
        a, b = e.a, e.b
        if isinstance(a, Const) and a.v == 0:
            a, b = b, a
        right = 'Nothing' if isinstance(b, Const) and b.v == 0 else render(b, 8)
        s = '%s Is %s' % (render(a, 8), right)
        return '(%s)' % s if PREC['Is'] < parent_prec else s
    if isinstance(e, Bin):
        p = PREC.get(e.op, 7)
        left = render(e.a, p)
        # right operand: same-precedence non-associative ops need parens
        rp = p + 1 if e.op in ('-', '/', '\\', 'Mod', '^', '&') or p == 7 else p
        right = render(e.b, rp)
        s = '%s %s %s' % (left, e.op, right)
        return '(%s)' % s if p < parent_prec else s
    if isinstance(e, Call):
        return render_call(e, parent_prec)
    return '<%s>' % type(e).__name__


def render_args(args):
    out = [render(a) if not isinstance(a, Missing) else '' for a in args]
    while out and out[-1] == '':
        out.pop()
    return ', '.join(out)


def render_call(e, parent_prec=0):
    a = render_args(e.args)
    if e.obj is not None:
        o = render(e.obj, 99)
        head = '%s.%s' % (o, e.fn) if e.fn else o
    else:
        head = e.fn
    if e.prop in ('get', 'let', 'set') and not e.args:
        return head
    if not e.args and not e.paren:
        return head
    return '%s(%s)' % (head, a)


def render_stmt(s):
    """Single-line rendering of a non-control statement."""
    if isinstance(s, Assign):
        d = render(s.dst)
        if isinstance(s.dst, Call) and s.dst.prop in ('let', 'set') and s.dst.args:
            d = '%s(%s)' % (render(Call(s.dst.fn, [], obj=s.dst.obj, prop='get')), render_args(s.dst.args))
        src = render(s.src)
        if s.kind == 'set':
            if isinstance(s.src, Const) and s.src.v == 0:
                src = 'Nothing'
            return 'Set %s = %s' % (d, src)
        return '%s = %s' % (d, src)
    if isinstance(s, ExprStmt):
        e = s.e
        if isinstance(e, Call):
            a = render_args(e.args)
            if e.obj is not None:
                head = '%s.%s' % (render(e.obj, 99), e.fn) if e.fn else render(e.obj, 99)
            else:
                head = e.fn
            if e.kind == 'proc' or e.kind == 'api':
                return ('Call %s(%s)' % (head, a)) if a else head
            return ('%s %s' % (head, a)) if a else head
        return "' expr: " + render(e)
    if isinstance(s, Text):
        return s.text
    if isinstance(s, FmtStmt):
        return s.fmt.format(*[render(x) for x in s.args])
    if isinstance(s, Comment):
        return "' " + s.text
    return "' <%s>" % type(s).__name__
