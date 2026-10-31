"""Parser for the VB6 source written by vbdec (.frm / .bas).

It accepts the VB6 subset the decompiler produces and nothing more: anything else (decompiler
markers such as `[cond:jne]`, half-written statements such as `Input #14, `) is a ParseError with
the line number, because the checker has to treat such text as a decompilation defect.

    mod = parse_module(text, filename)
    mod.procs['Proc_418550']  -> Proc(name, kind, params, rettype, locals, body, line)
    mod.fields / mod.globals  -> {name: VarDecl}
    mod.declares              -> {name: Declare}
"""
import re

KEYWORDS = {
    'and', 'or', 'xor', 'eqv', 'imp', 'not', 'mod', 'is', 'like', 'new', 'true', 'false', 'nothing', 'empty',
    'null', 'me', 'if', 'then', 'else', 'elseif', 'end', 'do', 'loop', 'while', 'until', 'for', 'to', 'step',
    'next', 'select', 'case', 'goto', 'exit', 'sub', 'function', 'call', 'set', 'dim', 'as', 'byval', 'byref',
    'private', 'public', 'on', 'error', 'resume', 'open', 'close', 'print', 'write', 'input', 'line', 'append',
    'output', 'binary', 'random', 'access', 'read', 'lock', 'shared', 'len', 'declare', 'lib', 'alias',
    'option', 'compare', 'text', 'implements', 'attribute', 'redim', 'preserve', 'erase', 'with',
}


class ParseError(Exception):
    def __init__(self, msg, line=None, fname=None):
        self.msg = msg
        self.line = line
        self.fname = fname
        super().__init__('%s:%s: %s' % (fname or '?', line if line is not None else '?', msg))


# ----------------------------------------------------------------------------- tokens
TOKEN_RE = re.compile(r'''
    (?P<ws>[ \t]+)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[!#@%&]?)
  | (?P<hex>&[hH][0-9A-Fa-f]+&?)
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*\$?)
  | (?P<op><>|<=|>=|[=<>+\-*/\\^&(),.:;\#])
  | (?P<bad>.)
''', re.X)


class Tok:
    __slots__ = ('kind', 'val', 'low', 'col', 'sp')

    def __init__(self, kind, val, col, sp=False):
        self.kind = kind
        self.val = val
        self.low = val.lower() if kind == 'id' else val
        self.col = col
        self.sp = sp            # whitespace before the token (`Foo (x) * 2` vs `Foo(x)`)

    def __repr__(self):
        return '%s:%r' % (self.kind, self.val)


def strip_comment(line):
    """Remove a trailing ' comment (not inside a string literal)."""
    out = []
    q = False
    for i, ch in enumerate(line):
        if ch == '"':
            q = not q
        elif ch == "'" and not q:
            return line[:i]
        out.append(ch)
    return line


def tokenize(line, lineno, fname):
    toks = []
    pos = 0
    sp = False
    while pos < len(line):
        m = TOKEN_RE.match(line, pos)
        k = m.lastgroup
        v = m.group(k)
        if k == 'bad':
            raise ParseError('unexpected character %r' % v, lineno, fname)
        if k == 'ws':
            pos = m.end()
            sp = True
            continue
        if k == 'id' and v.lower() == 'rem':
            break
        toks.append(Tok(k, v, pos, sp))
        sp = False
        pos = m.end()
    return toks


# ----------------------------------------------------------------------------- AST
class Node:
    __slots__ = ('line',)


class Num(Node):
    __slots__ = ('v', 'suffix', 'is_float')

    def __init__(self, v, suffix, is_float, line):
        self.v, self.suffix, self.is_float, self.line = v, suffix, is_float, line


class StrLit(Node):
    __slots__ = ('s',)

    def __init__(self, s, line):
        self.s, self.line = s, line


class Const(Node):
    """True / False / Nothing / Empty / Null"""
    __slots__ = ('name',)

    def __init__(self, name, line):
        self.name, self.line = name, line


class Name(Node):
    __slots__ = ('name',)

    def __init__(self, name, line):
        self.name, self.line = name, line


class MeRef(Node):
    __slots__ = ()

    def __init__(self, line):
        self.line = line


class Member(Node):
    """obj.name"""
    __slots__ = ('obj', 'name')

    def __init__(self, obj, name, line):
        self.obj, self.name, self.line = obj, name, line


class Apply(Node):
    """target(args): array index, function call or method call with arguments."""
    __slots__ = ('target', 'args')

    def __init__(self, target, args, line):
        self.target, self.args, self.line = target, args, line


class Missing(Node):
    __slots__ = ()

    def __init__(self, line):
        self.line = line


class BinOp(Node):
    __slots__ = ('op', 'a', 'b')

    def __init__(self, op, a, b, line):
        self.op, self.a, self.b, self.line = op, a, b, line


class UnOp(Node):
    __slots__ = ('op', 'a')

    def __init__(self, op, a, line):
        self.op, self.a, self.line = op, a, line


class NewObj(Node):
    __slots__ = ('cls',)

    def __init__(self, cls, line):
        self.cls, self.line = cls, line


# statements
class Assign(Node):
    __slots__ = ('target', 'expr', 'is_set')

    def __init__(self, target, expr, is_set, line):
        self.target, self.expr, self.is_set, self.line = target, expr, is_set, line


class CallStmt(Node):
    """A procedure/method call used as a statement (`Call X(a)`, `X a, b`, `obj.M a`)."""
    __slots__ = ('target', 'args', 'explicit')

    def __init__(self, target, args, explicit, line):
        self.target, self.args, self.explicit, self.line = target, args, explicit, line


class If(Node):
    __slots__ = ('arms', 'els')     # arms: [(cond, body)], els: body or None

    def __init__(self, arms, els, line):
        self.arms, self.els, self.line = arms, els, line


class DoLoop(Node):
    __slots__ = ('pre', 'post', 'body')   # pre/post: (kind 'while'|'until', cond) or None

    def __init__(self, pre, post, body, line):
        self.pre, self.post, self.body, self.line = pre, post, body, line


class For(Node):
    __slots__ = ('var', 'start', 'end', 'step', 'body')

    def __init__(self, var, start, end, step, body, line):
        self.var, self.start, self.end, self.step, self.body, self.line = var, start, end, step, body, line


class Select(Node):
    __slots__ = ('subject', 'cases', 'els')   # cases: [([expr...], body)]

    def __init__(self, subject, cases, els, line):
        self.subject, self.cases, self.els, self.line = subject, cases, els, line


class Label(Node):
    __slots__ = ('name',)

    def __init__(self, name, line):
        self.name, self.line = name, line


class GoTo(Node):
    __slots__ = ('label',)

    def __init__(self, label, line):
        self.label, self.line = label, line


class Exit(Node):
    __slots__ = ('what',)        # 'sub' | 'function' | 'do' | 'for'

    def __init__(self, what, line):
        self.what, self.line = what, line


class EndStmt(Node):
    __slots__ = ()

    def __init__(self, line):
        self.line = line


class OnError(Node):
    __slots__ = ('mode', 'label')    # mode: 'goto' | 'resume_next' | 'goto0'

    def __init__(self, mode, label, line):
        self.mode, self.label, self.line = mode, label, line


class Resume(Node):
    __slots__ = ('mode', 'label')    # '' (Resume) | 'next' | 'label'

    def __init__(self, mode, label, line):
        self.mode, self.label, self.line = mode, label, line


class FileOpen(Node):
    __slots__ = ('path', 'mode', 'access', 'lock', 'num', 'reclen')

    def __init__(self, path, mode, access, lock, num, reclen, line):
        self.path, self.mode, self.access, self.lock, self.num, self.reclen, self.line = \
            path, mode, access, lock, num, reclen, line


class FileClose(Node):
    __slots__ = ('nums',)

    def __init__(self, nums, line):
        self.nums, self.line = nums, line


class FilePrint(Node):
    """Print #n, items  (items: [(expr, sep)] with sep ';' ',' or None)"""
    __slots__ = ('num', 'items', 'write')

    def __init__(self, num, items, write, line):
        self.num, self.items, self.write, self.line = num, items, write, line


class FileInput(Node):
    __slots__ = ('num', 'targets', 'line_input')

    def __init__(self, num, targets, line_input, line):
        self.num, self.targets, self.line_input, self.line = num, targets, line_input, line


class ReDim(Node):
    __slots__ = ('target', 'bounds', 'preserve')

    def __init__(self, target, bounds, preserve, line):
        self.target, self.bounds, self.preserve, self.line = target, bounds, preserve, line


class Erase(Node):
    __slots__ = ('targets',)

    def __init__(self, targets, line):
        self.targets, self.line = targets, line


class Keyword(Node):
    """Argument-taking statements without a proper call form: DoEvents, Randomize x, Beep, Kill x,
    SaveSetting a, b, c, d, Unload Me, Error n."""
    __slots__ = ('name', 'args')

    def __init__(self, name, args, line):
        self.name, self.args, self.line = name, args, line


# declarations
class TypeRef:
    """As <type>: name, is_array (dynamic `()`), fixed bounds [(lb, ub)] or None"""
    __slots__ = ('name', 'is_array', 'bounds')

    def __init__(self, name, is_array=False, bounds=None):
        self.name, self.is_array, self.bounds = name, is_array, bounds

    def __repr__(self):
        return '%s%s' % (self.name, '()' if self.is_array else '')


class VarDecl:
    __slots__ = ('name', 'type', 'line', 'scope')

    def __init__(self, name, type_, line, scope):
        self.name, self.type, self.line, self.scope = name, type_, line, scope


class Param:
    __slots__ = ('name', 'byval', 'type', 'is_array', 'optional')

    def __init__(self, name, byval, type_, is_array):
        self.name, self.byval, self.type, self.is_array = name, byval, type_, is_array
        self.optional = False


class Proc:
    __slots__ = ('name', 'kind', 'params', 'rettype', 'locals', 'body', 'line', 'scope', 'end_line', 'error')

    def __init__(self, name, kind, params, rettype, scope, line):
        self.name, self.kind, self.params, self.rettype, self.scope, self.line = \
            name, kind, params, rettype, scope, line
        self.locals = {}
        self.body = []
        self.end_line = None
        self.error = None       # ParseError when the body is not valid VB


class Declare:
    __slots__ = ('name', 'kind', 'lib', 'alias', 'params', 'rettype', 'line')

    def __init__(self, name, kind, lib, alias, params, rettype, line):
        self.name, self.kind, self.lib, self.alias, self.params, self.rettype, self.line = \
            name, kind, lib, alias, params, rettype, line


class UdtType:
    __slots__ = ('name', 'fields', 'line')     # fields: [(name, TypeRef)]

    def __init__(self, name, line):
        self.name, self.line = name, line
        self.fields = []


class Module:
    def __init__(self, fname):
        self.fname = fname
        self.name = None
        self.kind = 'module'
        self.option_compare_text = False
        self.fields = {}        # form-level / module-level Dim (Private)
        self.globals = {}       # Public variables
        self.declares = {}
        self.types = {}
        self.procs = {}
        self.proc_order = []
        self.controls = []
        self.implements = []


# ----------------------------------------------------------------------------- expression parser
class ExprParser:
    def __init__(self, toks, lineno, fname):
        self.t = toks
        self.i = 0
        self.line = lineno
        self.fname = fname

    def err(self, msg):
        raise ParseError(msg, self.line, self.fname)

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else None

    def at_end(self):
        return self.i >= len(self.t)

    def next(self):
        tok = self.peek()
        if tok is None:
            self.err('unexpected end of line')
        self.i += 1
        return tok

    def is_op(self, v, k=0):
        tok = self.peek(k)
        return tok is not None and tok.kind == 'op' and tok.val == v

    def is_kw(self, *names, k=0):
        tok = self.peek(k)
        return tok is not None and tok.kind == 'id' and tok.low in names

    def expect_op(self, v):
        tok = self.next()
        if tok.kind != 'op' or tok.val != v:
            self.err('expected %r, found %r' % (v, tok.val))

    def expect_kw(self, name):
        tok = self.next()
        if tok.kind != 'id' or tok.low != name:
            self.err('expected %s, found %r' % (name, tok.val))

    def ident(self):
        tok = self.next()
        if tok.kind != 'id':
            self.err('expected a name, found %r' % tok.val)
        return tok.val

    # precedence climbing, loosest first
    def expr(self):
        return self.imp()

    def _binary(self, sub, ops):
        a = sub()
        while True:
            tok = self.peek()
            if tok is None or tok.kind != 'id' or tok.low not in ops:
                return a
            self.i += 1
            a = BinOp(ops[tok.low], a, sub(), self.line)

    def imp(self):
        return self._binary(self.eqv, {'imp': 'Imp'})

    def eqv(self):
        return self._binary(self.xor, {'eqv': 'Eqv'})

    def xor(self):
        return self._binary(self.or_, {'xor': 'Xor'})

    def or_(self):
        return self._binary(self.and_, {'or': 'Or'})

    def and_(self):
        return self._binary(self.not_, {'and': 'And'})

    def not_(self):
        if self.is_kw('not'):
            self.i += 1
            return UnOp('Not', self.not_(), self.line)
        return self.cmp()

    def cmp(self):
        a = self.concat()
        while True:
            tok = self.peek()
            if tok is None:
                return a
            if tok.kind == 'op' and tok.val in ('=', '<>', '<', '>', '<=', '>='):
                self.i += 1
                a = BinOp(tok.val, a, self.concat(), self.line)
            elif tok.kind == 'id' and tok.low in ('is', 'like'):
                self.i += 1
                a = BinOp('Is' if tok.low == 'is' else 'Like', a, self.concat(), self.line)
            else:
                return a

    def concat(self):
        a = self.add()
        while self.is_op('&'):
            self.i += 1
            a = BinOp('&', a, self.add(), self.line)
        return a

    def add(self):
        a = self.mod()
        while self.is_op('+') or self.is_op('-'):
            op = self.next().val
            a = BinOp(op, a, self.mod(), self.line)
        return a

    def mod(self):
        a = self.idiv()
        while self.is_kw('mod'):
            self.i += 1
            a = BinOp('Mod', a, self.idiv(), self.line)
        return a

    def idiv(self):
        a = self.mul()
        while self.is_op('\\'):
            self.i += 1
            a = BinOp('\\', a, self.mul(), self.line)
        return a

    def mul(self):
        a = self.neg()
        while self.is_op('*') or self.is_op('/'):
            op = self.next().val
            a = BinOp(op, a, self.neg(), self.line)
        return a

    def neg(self):
        if self.is_op('-'):
            self.i += 1
            return UnOp('neg', self.neg(), self.line)
        if self.is_op('+'):
            self.i += 1
            return self.neg()
        return self.pow()

    def pow(self):
        a = self.postfix()
        while self.is_op('^'):
            self.i += 1
            if self.is_op('-'):
                self.i += 1
                b = UnOp('neg', self.postfix(), self.line)
            else:
                b = self.postfix()
            a = BinOp('^', a, b, self.line)
        return a

    def args(self):
        """Argument list after '(' (consumes the ')'); empty arguments become Missing."""
        out = []
        if self.is_op(')'):
            self.i += 1
            return out
        while True:
            if self.is_op(',') or self.is_op(')'):
                out.append(Missing(self.line))
            else:
                out.append(self.expr())
            tok = self.next()
            if tok.kind == 'op' and tok.val == ')':
                return out
            if not (tok.kind == 'op' and tok.val == ','):
                self.err('expected , or ) in argument list, found %r' % tok.val)

    def postfix(self, stmt_target=False):
        """Primary with .member / (args) suffixes. For the target of a call statement a '(' after a
        space starts the first argument instead: `obj.M (a) * 2` passes `(a) * 2`."""
        a = self.primary()
        while True:
            if self.is_op('(') and stmt_target and self.peek().sp:
                return a
            if self.is_op('('):
                self.i += 1
                a = Apply(a, self.args(), self.line)
            elif self.is_op('.'):
                self.i += 1
                a = Member(a, self.ident(), self.line)
            else:
                return a

    def primary(self):
        tok = self.next()
        if tok.kind == 'num':
            return parse_number(tok.val, self.line)
        if tok.kind == 'hex':
            v = tok.val.rstrip('&')
            return Num(int(v[2:], 16), 'hex', False, self.line)
        if tok.kind == 'str':
            return StrLit(tok.val[1:-1].replace('""', '"'), self.line)
        if tok.kind == 'op' and tok.val == '(':
            e = self.expr()
            self.expect_op(')')
            return e
        if tok.kind == 'id':
            if tok.low in ('true', 'false', 'nothing', 'empty', 'null'):
                return Const(tok.low, self.line)
            if tok.low == 'me':
                return MeRef(self.line)
            if tok.low == 'new':
                return NewObj(self.ident(), self.line)
            if tok.low in KEYWORDS and tok.low not in ('len', 'input', 'text', 'line', 'error', 'random',
                                                      'binary', 'read', 'output', 'append', 'lock', 'access',
                                                      'shared', 'compare', 'alias', 'lib', 'step'):
                self.err('keyword %r where a value was expected' % tok.val)
            return Name(tok.val, self.line)
        self.err('unexpected %r' % tok.val)


def parse_number(text, line):
    suffix = ''
    if text[-1] in '!#@%&':
        suffix = text[-1]
        text = text[:-1]
    if '.' in text or 'e' in text.lower():
        return Num(float(text), suffix, True, line)
    return Num(int(text), suffix, False, line)


# ----------------------------------------------------------------------------- statements
FILE_MODES = {'input', 'output', 'append', 'binary', 'random'}


class ModuleParser:
    def __init__(self, text, fname):
        self.fname = fname
        raw = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        # join line continuations
        self.lines = []
        buf = None
        start = 0
        for n, l in enumerate(raw, 1):
            s = l.rstrip()
            if buf is None:
                start = n
                buf = s
            else:
                buf += ' ' + s.lstrip()
            if s.endswith(' _'):
                buf = buf[:-2]
                continue
            self.lines.append((start, buf))
            buf = None
        if buf is not None:
            self.lines.append((start, buf))
        self.pos = 0
        self.mod = Module(fname)

    def err(self, msg, line):
        raise ParseError(msg, line, self.fname)

    # -------------------------------------------------------------- driver
    def parse(self):
        mod = self.mod
        while self.pos < len(self.lines):
            lineno, text = self.lines[self.pos]
            self.pos += 1
            code = strip_comment(text).strip()
            if not code:
                continue
            low = code.lower()
            if low.startswith('version '):
                continue
            if low.startswith('begin '):
                mod.kind = 'form'
                self.skip_form_block(code, lineno)
                continue
            if low.startswith('attribute '):
                m = re.match(r'attribute\s+vb_name\s*=\s*"([^"]*)"', code, re.I)
                if m:
                    mod.name = m.group(1)
                continue
            if low == 'option compare text':
                mod.option_compare_text = True
                continue
            if low.startswith('option '):
                continue
            if low.startswith('implements '):
                mod.implements.append(code.split(None, 1)[1].strip())
                continue
            toks = tokenize(code, lineno, self.fname)
            words = [t.low for t in toks if t.kind == 'id']
            first = toks[0].low if toks[0].kind == 'id' else None
            scope = 'private'
            k = 0
            if first in ('private', 'public', 'global', 'friend'):
                scope = 'private' if first == 'private' else 'public'
                k = 1
            kw = toks[k].low if k < len(toks) and toks[k].kind == 'id' else None
            if kw == 'declare':
                d = self.parse_declare(toks[k + 1:], lineno)
                mod.declares[d.name.lower()] = d
                continue
            if kw == 'type':
                t = self.parse_type(toks[k + 1:], lineno)
                mod.types[t.name.lower()] = t
                continue
            if kw in ('sub', 'function'):
                p = self.parse_proc(toks, k, scope, lineno)
                key = p.name.lower()
                if key in mod.procs:
                    self.err('procedure %s defined twice' % p.name, lineno)
                mod.procs[key] = p
                mod.proc_order.append(p)
                continue
            if kw == 'dim' or first in ('private', 'public', 'global'):
                decls = self.parse_dim(toks, k + (1 if kw == 'dim' else 0), lineno,
                                       'public' if scope == 'public' else 'module')
                for d in decls:
                    (mod.globals if scope == 'public' else mod.fields)[d.name.lower()] = d
                continue
            self.err('unexpected module-level statement: %s' % code, lineno)
        return mod

    def skip_form_block(self, first, lineno):
        """Form designer block: record controls, skip to the matching End."""
        depth = 1
        while self.pos < len(self.lines):
            n, text = self.lines[self.pos]
            self.pos += 1
            code = strip_comment(text).strip()
            low = code.lower()
            if low.startswith('begin '):
                parts = code.split()
                if len(parts) >= 3 and depth == 1:
                    self.mod.controls.append((parts[2], parts[1]))
                depth += 1
            elif low == 'end':
                depth -= 1
                if depth == 0:
                    return
        self.err('unterminated Begin block', lineno)

    # -------------------------------------------------------------- declarations
    def parse_typeref(self, p):
        """`As Type` (already past As)."""
        name = p.ident()
        if p.is_op('.'):
            p.i += 1
            name = name + '.' + p.ident()
        return name

    def parse_dim(self, toks, k, lineno, scope):
        p = ExprParser(toks, lineno, self.fname)
        p.i = k
        out = []
        while True:
            name = p.ident()
            is_array = False
            bounds = None
            if p.is_op('('):
                p.i += 1
                if p.is_op(')'):
                    p.i += 1
                    is_array = True
                else:
                    bounds = []
                    while True:
                        lo = p.expr()
                        if p.is_kw('to'):
                            p.i += 1
                            hi = p.expr()
                        else:
                            hi, lo = lo, None
                        bounds.append((lo, hi))
                        tok = p.next()
                        if tok.kind == 'op' and tok.val == ')':
                            break
                        if not (tok.kind == 'op' and tok.val == ','):
                            self.err('bad array bounds', lineno)
                    is_array = True
            tname = 'Variant'
            if p.is_kw('as'):
                p.i += 1
                if p.is_kw('new'):
                    p.i += 1
                tname = self.parse_typeref(p)
            out.append(VarDecl(name, TypeRef(tname, is_array, bounds), lineno, scope))
            if p.at_end():
                return out
            p.expect_op(',')

    def parse_params(self, p):
        params = []
        p.expect_op('(')
        if p.is_op(')'):
            p.i += 1
            return params
        while True:
            byval = False
            optional = False
            if p.is_kw('optional'):
                p.i += 1
                optional = True
            if p.is_kw('byval'):
                p.i += 1
                byval = True
            elif p.is_kw('byref'):
                p.i += 1
            name = p.ident()
            is_array = False
            if p.is_op('('):
                p.i += 1
                p.expect_op(')')
                is_array = True
            tname = 'Variant'
            if p.is_kw('as'):
                p.i += 1
                tname = self.parse_typeref(p)
            prm = Param(name, byval, tname, is_array)
            prm.optional = optional
            params.append(prm)
            tok = p.next()
            if tok.kind == 'op' and tok.val == ')':
                return params
            if not (tok.kind == 'op' and tok.val == ','):
                self.err('bad parameter list', p.line)

    def parse_declare(self, toks, lineno):
        p = ExprParser(toks, lineno, self.fname)
        kind = p.ident().lower()
        name = p.ident()
        p.expect_kw('lib')
        lib = p.next().val.strip('"')
        alias = None
        if p.is_kw('alias'):
            p.i += 1
            alias = p.next().val.strip('"')
        params = self.parse_params(p)
        rettype = None
        if p.is_kw('as'):
            p.i += 1
            rettype = self.parse_typeref(p)
        return Declare(name, kind, lib, alias, params, rettype, lineno)

    def parse_type(self, toks, lineno):
        p = ExprParser(toks, lineno, self.fname)
        t = UdtType(p.ident(), lineno)
        while self.pos < len(self.lines):
            n, text = self.lines[self.pos]
            self.pos += 1
            code = strip_comment(text).strip()
            if not code:
                continue
            if code.lower() == 'end type':
                return t
            ft = tokenize(code, n, self.fname)
            for d in self.parse_dim(ft, 0, n, 'field'):
                t.fields.append((d.name, d.type))
        self.err('unterminated Type', lineno)

    def parse_proc(self, toks, k, scope, lineno):
        p = ExprParser(toks, lineno, self.fname)
        p.i = k
        kind = p.ident().lower()
        name = p.ident()
        params = self.parse_params(p) if p.is_op('(') else []
        rettype = None
        if p.is_kw('as'):
            p.i += 1
            rettype = self.parse_typeref(p)
        if not p.at_end():
            self.err('junk after procedure header', lineno)
        proc = Proc(name, kind, params, rettype, scope, lineno)
        self.cur_proc = proc
        start = self.pos
        try:
            body, end = self.block(('end ' + kind,))
        except ParseError as e:
            # keep going with the next procedure; the checker reports this one as not valid VB
            proc.error = e
            self.pos = start
            while self.pos < len(self.lines):
                ln, text = self.lines[self.pos]
                self.pos += 1
                if text.strip().lower() == 'end ' + kind and not text[:1].isspace():
                    break
            return proc
        proc.body = body
        proc.end_line = end
        return proc

    # -------------------------------------------------------------- blocks
    def block(self, terminators):
        """Statements up to a line starting with one of `terminators` (lower-case prefixes).
        Returns (statements, terminating line's (lineno, code, toks))."""
        out = []
        while self.pos < len(self.lines):
            lineno, text = self.lines[self.pos]
            code = strip_comment(text).strip()
            if not code:
                self.pos += 1
                continue
            low = code.lower()
            for t in terminators:
                if low == t or low.startswith(t + ' ') or low.startswith(t + '\t') or \
                        (t.endswith(':') and low.startswith(t)):
                    self.pos += 1
                    return out, (lineno, code)
            self.pos += 1
            out.extend(self.statement(code, lineno))
        self.err('missing %s' % ' / '.join(terminators), None)

    def statement(self, code, lineno):
        """One source line -> list of statements (blocks consume further lines)."""
        toks = tokenize(code, lineno, self.fname)
        if not toks:
            return []
        t0 = toks[0]
        # label
        if len(toks) == 2 and t0.kind == 'id' and toks[1].kind == 'op' and toks[1].val == ':':
            return [Label(t0.val, lineno)]
        if t0.kind != 'id':
            self.err('statement cannot start with %r' % t0.val, lineno)
        w = t0.low
        if w == 'dim':
            for d in self.parse_dim(toks, 1, lineno, 'local'):
                key = d.name.lower()
                if key in self.cur_proc.locals:
                    self.err('local %s declared twice' % d.name, lineno)
                self.cur_proc.locals[key] = d
            return []
        if w == 'if':
            return [self.parse_if(toks, lineno)]
        if w == 'do':
            return [self.parse_do(toks, lineno)]
        if w == 'for':
            return [self.parse_for(toks, lineno)]
        if w == 'select':
            return [self.parse_select(toks, lineno)]
        if w in ('end', 'loop', 'next', 'else', 'elseif', 'case', 'wend'):
            if w == 'end' and len(toks) == 1:
                return [EndStmt(lineno)]
            self.err('unexpected %s' % code, lineno)
        return [self.simple(toks, lineno)]

    def simple(self, toks, lineno):
        """A statement that fits on one line (also used after `Then` of a one-line If)."""
        p = ExprParser(toks, lineno, self.fname)
        t0 = toks[0]
        w = t0.low if t0.kind == 'id' else None
        n = len(toks)
        if w == 'goto':
            p.i = 1
            lab = p.ident()
            self.done(p)
            return GoTo(lab, lineno)
        if w == 'exit':
            what = toks[1].low if n > 1 else ''
            if what not in ('sub', 'function', 'do', 'for') or n != 2:
                self.err('bad Exit', lineno)
            return Exit(what, lineno)
        if w == 'end' and n == 1:
            return EndStmt(lineno)
        if w == 'on' and n >= 3 and toks[1].low == 'error':
            if n == 4 and toks[2].low == 'resume' and toks[3].low == 'next':
                return OnError('resume_next', None, lineno)
            if n == 4 and toks[2].low == 'goto':
                if toks[3].kind == 'num' and toks[3].val == '0':
                    return OnError('goto0', None, lineno)
                if toks[3].kind == 'id':
                    return OnError('goto', toks[3].val, lineno)
            self.err('bad On Error statement', lineno)
        if w == 'resume':
            if n == 1:
                return Resume('', None, lineno)
            if n == 2 and toks[1].low == 'next':
                return Resume('next', None, lineno)
            if n == 2 and toks[1].kind == 'id':
                return Resume('label', toks[1].val, lineno)
            self.err('bad Resume', lineno)
        if w == 'set':
            p.i = 1
            tgt = p.postfix()
            p.expect_op('=')
            e = p.expr()
            self.done(p)
            return Assign(tgt, e, True, lineno)
        if w == 'call':
            p.i = 1
            tgt = p.postfix()
            self.done(p)
            if isinstance(tgt, Apply):
                return CallStmt(tgt.target, tgt.args, True, lineno)
            return CallStmt(tgt, [], True, lineno)
        if w == 'open':
            return self.parse_open(p, lineno)
        if w == 'close':
            p.i = 1
            nums = []
            while not p.at_end():
                if p.is_op('#'):
                    p.i += 1
                nums.append(p.expr())
                if p.at_end():
                    break
                p.expect_op(',')
            return FileClose(nums, lineno)
        if w in ('print', 'write') and n > 1 and toks[1].kind == 'op' and toks[1].val == '#':
            return self.parse_print(p, w == 'write', lineno)
        if w == 'input' and n > 1 and toks[1].kind == 'op' and toks[1].val == '#':
            p.i = 2
            num = p.expr()
            p.expect_op(',')
            targets = []
            while True:
                if p.at_end() or p.is_op(','):
                    self.err('Input # with an empty target (decompiler could not name the variable)', lineno)
                targets.append(p.postfix())
                if p.at_end():
                    break
                p.expect_op(',')
            return FileInput(num, targets, False, lineno)
        if w == 'line' and n > 2 and toks[1].low == 'input':
            p.i = 2
            p.expect_op('#')
            num = p.expr()
            p.expect_op(',')
            tgt = p.postfix()
            self.done(p)
            return FileInput(num, [tgt], True, lineno)
        if w in ('redim',):
            p.i = 1
            preserve = False
            if p.is_kw('preserve'):
                p.i += 1
                preserve = True
            name = p.postfix()
            if not isinstance(name, Apply):
                self.err('bad ReDim', lineno)
            bounds = []
            for a in name.args:
                if isinstance(a, BinOp) and False:
                    pass
                bounds.append((None, a))
            self.done(p)
            return ReDim(name.target, bounds, preserve, lineno)
        if w == 'erase':
            p.i = 1
            targets = [p.postfix()]
            while not p.at_end():
                p.expect_op(',')
                targets.append(p.postfix())
            return Erase(targets, lineno)
        if w in ('doevents', 'beep') and n == 1:
            return Keyword(w, [], lineno)
        if w in ('randomize', 'kill', 'savesetting', 'unload', 'error', 'deletesetting', 'chdir', 'mkdir',
                 'filecopy', 'setattr', 'sendkeys', 'appactivate', 'load'):
            p.i = 1
            args = []
            while not p.at_end():
                args.append(p.expr())
                if p.at_end():
                    break
                p.expect_op(',')
            return Keyword(w, args, lineno)
        # assignment or call
        tgt = p.postfix(stmt_target=True)
        if p.is_op('='):
            p.i += 1
            e = p.expr()
            self.done(p)
            return Assign(tgt, e, False, lineno)
        if p.at_end():
            if isinstance(tgt, Apply):
                # `obj.Method (x)` / `Proc(x)` written without Call: one parenthesised argument
                return CallStmt(tgt.target, tgt.args, False, lineno)
            return CallStmt(tgt, [], False, lineno)
        # `target arg, arg`
        args = []
        while True:
            if p.is_op(','):
                args.append(Missing(lineno))
            else:
                args.append(p.expr())
            if p.at_end():
                break
            p.expect_op(',')
            if p.at_end():
                args.append(Missing(lineno))
                break
        return CallStmt(tgt, args, False, lineno)

    def done(self, p):
        if not p.at_end():
            self.err('unexpected %r after statement' % p.peek().val, p.line)

    def parse_open(self, p, lineno):
        p.i = 1
        path = p.expr()
        p.expect_kw('for')
        mode = p.ident().lower()
        if mode not in FILE_MODES:
            self.err('bad file mode %s' % mode, lineno)
        access = lock = None
        if p.is_kw('access'):
            p.i += 1
            access = p.ident().lower()
            if p.is_kw('write') and access == 'read':
                p.i += 1
                access = 'read write'
        if p.is_kw('lock', 'shared'):
            lock = p.next().low
            while p.is_kw('read', 'write'):
                lock += ' ' + p.next().low
        p.expect_kw('as')
        if p.is_op('#'):
            p.i += 1
        num = p.expr()
        reclen = None
        if p.is_kw('len'):
            p.i += 1
            p.expect_op('=')
            reclen = p.expr()
        self.done(p)
        return FileOpen(path, mode, access, lock, num, reclen, lineno)

    def parse_print(self, p, is_write, lineno):
        p.i = 2
        num = p.expr()
        items = []
        if p.at_end():
            return FilePrint(num, items, is_write, lineno)
        p.expect_op(',')
        while not p.at_end():
            if p.is_op(';') or p.is_op(','):
                items.append((None, p.next().val))
                continue
            e = p.expr()
            sep = None
            if p.is_op(';') or p.is_op(','):
                sep = p.next().val
            items.append((e, sep))
        return FilePrint(num, items, is_write, lineno)

    def parse_if(self, toks, lineno):
        # find Then
        k = None
        for i, t in enumerate(toks):
            if t.kind == 'id' and t.low == 'then':
                k = i
                break
        if k is None:
            self.err('If without Then', lineno)
        cond = self.cond_expr(toks[1:k], lineno)
        rest = toks[k + 1:]
        if rest:
            # one-line If: `If c Then stmt [Else stmt]`
            j = None
            depth = 0
            for i, t in enumerate(rest):
                if t.kind == 'id' and t.low == 'else':
                    j = i
                    break
            if j is not None:
                return If([(cond, [self.simple(rest[:j], lineno)])], [self.simple(rest[j + 1:], lineno)], lineno)
            return If([(cond, [self.simple(rest, lineno)])], None, lineno)
        arms = []
        els = None
        cur_cond = cond
        while True:
            body, (ln, code) = self.block(('elseif', 'else', 'end if'))
            low = code.lower()
            if cur_cond is not None:
                arms.append((cur_cond, body))
            else:
                els = body
            if low == 'end if':
                return If(arms, els, lineno)
            if low.startswith('elseif'):
                if cur_cond is None:
                    self.err('ElseIf after Else', ln)
                t2 = tokenize(code, ln, self.fname)
                k2 = [i for i, t in enumerate(t2) if t.kind == 'id' and t.low == 'then']
                if not k2 or k2[-1] != len(t2) - 1:
                    self.err('bad ElseIf', ln)
                cur_cond = self.cond_expr(t2[1:k2[-1]], ln)
                continue
            if low == 'else':
                if cur_cond is None:
                    self.err('two Else branches', ln)
                cur_cond = None
                continue
            self.err('bad If block line: %s' % code, ln)

    def cond_expr(self, toks, lineno):
        p = ExprParser(toks, lineno, self.fname)
        e = p.expr()
        self.done(p)
        return e

    def parse_do(self, toks, lineno):
        pre = None
        if len(toks) > 1:
            kind = toks[1].low
            if kind not in ('while', 'until'):
                self.err('bad Do', lineno)
            pre = (kind, self.cond_expr(toks[2:], lineno))
        body, (ln, code) = self.block(('loop',))
        t2 = tokenize(code, ln, self.fname)
        post = None
        if len(t2) > 1:
            kind = t2[1].low
            if kind not in ('while', 'until'):
                self.err('bad Loop', ln)
            post = (kind, self.cond_expr(t2[2:], ln))
        if pre and post:
            self.err('Do with conditions at both ends', lineno)
        return DoLoop(pre, post, body, lineno)

    def parse_for(self, toks, lineno):
        p = ExprParser(toks, lineno, self.fname)
        p.i = 1
        if p.is_kw('each'):
            self.err('For Each is not supported', lineno)
        var = p.postfix()
        p.expect_op('=')
        start = p.expr()
        p.expect_kw('to')
        end = p.expr()
        step = None
        if p.is_kw('step'):
            p.i += 1
            step = p.expr()
        self.done(p)
        body, (ln, code) = self.block(('next',))
        t2 = tokenize(code, ln, self.fname)
        if len(t2) > 2:
            self.err('Next with several variables', ln)
        return For(var, start, end, step, body, lineno)

    def parse_select(self, toks, lineno):
        if len(toks) < 3 or toks[1].low != 'case':
            self.err('bad Select Case', lineno)
        subject = self.cond_expr(toks[2:], lineno)
        cases = []
        els = None
        # skip to first Case
        body, (ln, code) = self.block(('case', 'end select'))
        if body:
            self.err('statements before the first Case', lineno)
        while True:
            low = code.lower()
            if low == 'end select':
                return Select(subject, cases, els, lineno)
            t2 = tokenize(code, ln, self.fname)
            if len(t2) == 2 and t2[1].low == 'else':
                body, (ln2, code2) = self.block(('case', 'end select'))
                if els is not None:
                    self.err('two Case Else', ln)
                els = body
                ln, code = ln2, code2
                continue
            p = ExprParser(t2, ln, self.fname)
            p.i = 1
            vals = []
            while True:
                if p.is_kw('is'):
                    self.err('Case Is is not supported', ln)
                a = p.expr()
                if p.is_kw('to'):
                    p.i += 1
                    vals.append(('range', a, p.expr()))
                else:
                    vals.append(('eq', a))
                if p.at_end():
                    break
                p.expect_op(',')
            body, (ln2, code2) = self.block(('case', 'end select'))
            cases.append((vals, body))
            ln, code = ln2, code2


def parse_module(text, fname='?'):
    return ModuleParser(text, fname).parse()


def parse_file(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        return parse_module(f.read(), path)
