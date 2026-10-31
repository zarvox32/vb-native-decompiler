"""MSVBVM60 runtime function semantics -> VB statements/expressions.

Conventions (verified against call sites in the Troopanum executables):
  * operator helpers take (dst, rhs, lhs): the operand pushed first is the left-hand side
    (__vbaVarAdd/Sub/Cat..., __vbaStrCat, __vbaStrCmp, __vbaVarTst*)
  * __vbaStrCopy/StrMove/VarMove/VarCopy/VarDup: ecx = destination, edx = source
  * __vbaFree*: ecx (single) or cdecl lists; __vbaI2I4/__vbaI2Abs take ecx
  * __vbaFp*: operate on st(0)
"""
import struct

from ir import (Const, FConst, Str, Var, Member, Deref, AddrOf, Bin, Un, Call, Temp, Me, Missing, Unknown, Raw,
                Assign, ExprStmt, Text, Comment, FmtStmt, ekey)
from lift import SAddr, StrCmpResult, BoolVal, SetCC, DataPtr, CodePtr, ImportFn, s16, s32

VAROPS = {'__vbaVarAdd': '+', '__vbaVarSub': '-', '__vbaVarMul': '*', '__vbaVarDiv': '/', '__vbaVarIdiv': '\\',
          '__vbaVarMod': 'Mod', '__vbaVarPow': '^', '__vbaVarAnd': 'And', '__vbaVarOr': 'Or', '__vbaVarXor': 'Xor',
          '__vbaVarEqv': 'Eqv', '__vbaVarImp': 'Imp', '__vbaVarCat': '&'}
VARTST = {'Eq': '=', 'Ne': '<>', 'Lt': '<', 'Le': '<=', 'Gt': '>', 'Ge': '>='}
FILE_MODES = {1: 'Input', 2: 'Output', 4: 'Random', 8: 'Append', 0x20: 'Binary'}
CI = {'_CIsin': 'Sin', '_CIcos': 'Cos', '_CItan': 'Tan', '_CIatan': 'Atn', '_CIexp': 'Exp', '_CIlog': 'Log',
      '_CIsqrt': 'Sqr'}
NOOP_STD = {'__vbaHresultCheckObj': 16, '__vbaHresultCheck': 4, '__vbaRecDestruct': 8, '__vbaRecDestructAnsi': 8,
            '__vbaAryUnlock': 4, '__vbaAryLock': 8, '__vbaFreeObjList': 0, '__vbaFreeStrList': 0,
            '__vbaFreeVarList': 0}
NOOP_REG = {'__vbaFreeStr', '__vbaFreeObj', '__vbaFreeVar', '__vbaSetSystemError', '__vbaExitProc',
            '__vbaGenerateBoundsError', '__vbaErrorOverflow', '__vbaFPException', '__vbaFpR4', '__vbaFpR8',
            '__vbaFpCmpCy', '__vbaExceptHandler'}


class Runtime:
    def __init__(self, ctx):
        self.ctx = ctx
        self.unknown = {}

    def call(self, L, name):
        if name in NOOP_REG:
            if name in ('__vbaFreeStr', '__vbaFreeObj', '__vbaFreeVar'):
                L.set_reg('eax', Const(0))
            return
        if name in NOOP_STD:
            L.consume(NOOP_STD[name])
            return
        if name in VAROPS:
            return self.var_binop(L, VAROPS[name])
        if name.startswith('__vbaVarTst') or name.startswith('__vbaVarTextTst'):
            return self.var_tst(L, name)
        if name.startswith('__vbaVarCmp') or name.startswith('__vbaVarTextCmp'):
            return self.var_cmp(L, name)
        if name in CI:
            L.fset(0, Call(CI[name], [L.fget(0)], pure=True, ty='Double'))
            return
        if name.startswith('_adj_f'):
            return self.adj_fdiv(L, name)
        f = getattr(self, 'h_' + name, None)
        if f is None:
            self.unknown[name] = self.unknown.get(name, 0) + 1
            L.warn('no handler for runtime function %s' % name)
            n = L.pending_cells()
            args, _ = L.args_by_sizes([4] * (n // 4))
            L.consume(n)
            t = L.call_value(Call(name, args, kind='raw'))
            L.set_reg('eax', t)
            L.invalidate_locals()
            return
        return f(L)

    # ------------------------------------------------------------------ helpers
    def std_args(self, L, n):
        args, _ = L.args_by_sizes([4] * n)
        L.consume(4 * n)
        return args

    def pure(self, L, fn, args, ty, reg='eax'):
        L.set_reg(reg, Call(fn, args, pure=True, ty=ty))

    def adj_fdiv(self, L, name):
        # FDIV-bug workaround helpers (normally pruned): st0 = st0 / mem operand
        if name.startswith('_adj_fdiv_m64') or name.startswith('_adj_fdivr_m64'):
            v = L.cell8(0)
            L.consume(8)
        elif name.endswith('_r'):
            v = L.fget(1)
        else:
            v = L.cell(0)
            L.consume(4)
        a = L.fget(0)
        L.fset(0, Bin('/', v, a) if 'fdivr' in name else Bin('/', a, v))

    # ------------------------------------------------------------------ strings
    def h___vbaStrCopy(self, L):
        dst, src = L.reg('ecx'), L.reg('edx')
        L.note_type(src, 'String')        # copying from a variable: that variable is a String
        L.store_ptr(dst, self.bstr(src), 4, 'String')
        L.set_reg('eax', src)

    def h___vbaStrMove(self, L):
        dst, src = L.reg('ecx'), L.reg('edx')
        L.store_ptr(dst, self.bstr(src), 4, 'String')
        L.set_reg('eax', src)

    @staticmethod
    def bstr(e):
        """A string operand: a null BSTR is the empty string (the compiler passes 0 for "")."""
        if isinstance(e, Const) and e.v == 0:
            return Str('')
        return e

    def h___vbaStrCat(self, L):
        rhs, lhs = self.std_args(L, 2)
        L.set_reg('eax', Bin('&', self.bstr(lhs), self.bstr(rhs), ty='String'))

    def h___vbaStrCmp(self, L):
        rhs, lhs = self.std_args(L, 2)
        L.set_reg('eax', StrCmpResult(self.bstr(lhs), self.bstr(rhs)))

    def h___vbaStrTextCmp(self, L):
        rhs, lhs = self.std_args(L, 2)
        self.ctx.option_compare_text = True
        L.set_reg('eax', StrCmpResult(self.bstr(lhs), self.bstr(rhs), text=True))

    def h___vbaStrComp(self, L):
        cmp_, rhs, lhs = self.std_args(L, 3)
        L.set_reg('eax', Call('StrComp', [self.bstr(lhs), self.bstr(rhs), cmp_], pure=True, ty='Integer'))

    def h___vbaLenBstr(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'Len', [s], 'Long')

    def h___vbaLenBstrB(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'LenB', [s], 'Long')

    def h___vbaLenVar(self, L):
        dst, p = self.std_args(L, 2)
        L.store_ptr(dst, Call('Len', [L.variant_at(p)], pure=True, ty='Long'), 16, 'Variant')
        L.set_reg('eax', dst)

    def conv_str(self, L, size=4):
        if size == 8:
            v = L.cell8(0)
            L.consume(8)
        else:
            v, = self.std_args(L, 1)
        L.set_reg('eax', Call('CStr', [v], pure=True, ty='String'))

    def h___vbaStrI2(self, L):
        self.conv_str(L)

    def h___vbaStrI4(self, L):
        self.conv_str(L)

    def h___vbaStrUI1(self, L):
        self.conv_str(L)

    def h___vbaStrBool(self, L):
        self.conv_str(L)

    def h___vbaStrR8(self, L):
        self.conv_str(L, 8)

    def h___vbaStrR4(self, L):
        self.conv_str(L)

    def h___vbaStrCy(self, L):
        self.conv_str(L, 8)

    def h___vbaStrDate(self, L):
        self.conv_str(L, 8)

    def h___vbaStrVarMove(self, L):
        p, = self.std_args(L, 1)
        v = L.variant_at(p)
        L.set_reg('eax', v)

    def h___vbaStrVarCopy(self, L):
        p, = self.std_args(L, 1)
        L.set_reg('eax', L.variant_at(p))

    def h___vbaStrVarVal(self, L):
        # __vbaStrVarVal(BSTR *tmp, VARIANT *v) -> BSTR
        tmp, p = self.std_args(L, 2)
        v = L.variant_at(p)
        if isinstance(tmp, SAddr) and L.loc_kind(tmp.o) in ('local', 'cell'):
            L.mem_store(tmp.o, 4, v, None)
        L.set_reg('eax', v)

    def h___vbaStrToAnsi(self, L):
        dst, s = self.std_args(L, 2)
        if isinstance(dst, SAddr):
            L.mem_store(dst.o, 4, s, None)
        L.set_reg('eax', s)

    def h___vbaStrToUnicode(self, L):
        dst, ansi = self.std_args(L, 2)
        L.set_reg('eax', Const(0))

    def h___vbaStrFixstr(self, L):
        n, s = self.std_args(L, 2)
        L.set_reg('eax', s)

    def h___vbaR8Str(self, L):
        s, = self.std_args(L, 1)
        L.fpush(Call('CDbl', [s], pure=True, ty='Double'))

    def h___vbaR4Str(self, L):
        s, = self.std_args(L, 1)
        L.fpush(Call('CSng', [s], pure=True, ty='Single'))

    def h___vbaI2Str(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'CInt', [s], 'Integer')

    def h___vbaI4Str(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'CLng', [s], 'Long')

    def h___vbaBoolStr(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'CBool', [s], 'Boolean')

    def h___vbaInStr(self, L):
        start, s1, s2, cmp_ = self.std_args(L, 4)
        args = [start, s1, s2]
        if isinstance(cmp_, Const) and cmp_.v == 1:
            args.append(Raw('vbTextCompare'))
        self.pure(L, 'InStr', args, 'Long')

    def h___vbaInStrVar(self, L):
        # (VARIANT* ret, VARIANT* start, VARIANT* s1, VARIANT* s2, int compare)
        dst, start, s1, s2, cmp_ = self.std_args(L, 5)
        args = [L.variant_at(start), L.variant_at(s1), L.variant_at(s2)]
        if isinstance(args[0], Missing):
            args = args[1:]
        if isinstance(cmp_, Const) and cmp_.v == 1:
            args.append(Raw('vbTextCompare'))
        L.store_ptr(dst, Call('InStr', args, pure=True, ty='Variant'), 16, 'Variant')
        L.set_reg('eax', dst)

    def h___vbaMidStmtBstr(self, L):
        # Mid$(s, start, len) = value
        val, pstr, length, start = self.std_args(L, 4)
        L.emit(FmtStmt('Mid$({0}, {1}, {2}) = {3}', [L.ptr_target(pstr), start, length, val]))

    # ------------------------------------------------------------------ variants
    def h___vbaVarMove(self, L):
        dst, src = L.reg('ecx'), L.reg('edx')
        v = L.variant_at(src)
        L.store_ptr(dst, v, 16, 'Variant')
        L.set_reg('eax', dst)

    h___vbaVarCopy = h___vbaVarMove

    def h___vbaVarDup(self, L):
        dst, src = L.reg('ecx'), L.reg('edx')
        v = L.variant_at(src)
        if isinstance(dst, SAddr) and L.loc_kind(dst.o) in ('local', 'cell'):
            L.mem_store(dst.o, 16, v, None)
        else:
            L.store_ptr(dst, v, 16, 'Variant')
        L.set_reg('eax', dst)

    def h___vbaVarZero(self, L):
        dst = L.reg('ecx')
        L.store_ptr(dst, Raw('Empty'), 16, 'Variant')

    def var_binop(self, L, op):
        dst, rhs, lhs = self.std_args(L, 3)
        a, b = L.variant_at(lhs), L.variant_at(rhs)
        v = Bin(op, a, b, ty='Variant')
        self.var_result(L, dst, v)

    def var_result(self, L, dst, v):
        if isinstance(dst, SAddr) and L.loc_kind(dst.o) in ('local', 'cell'):
            # temporary result variant
            L.mem_store(dst.o, 16, v, None)
        else:
            L.store_ptr(dst, v, 16, 'Variant')
        L.set_reg('eax', dst)

    def h___vbaVarNot(self, L):
        dst, a = self.std_args(L, 2)
        self.var_result(L, dst, Un('Not', L.variant_at(a), ty='Variant'))

    def h___vbaVarNeg(self, L):
        dst, a = self.std_args(L, 2)
        self.var_result(L, dst, Un('neg', L.variant_at(a), ty='Variant'))

    def h___vbaVarInt(self, L):
        dst, a = self.std_args(L, 2)
        self.var_result(L, dst, Call('Int', [L.variant_at(a)], pure=True, ty='Variant'))

    def h___vbaVarFix(self, L):
        dst, a = self.std_args(L, 2)
        self.var_result(L, dst, Call('Fix', [L.variant_at(a)], pure=True, ty='Variant'))

    def h___vbaVarAbs(self, L):
        dst, a = self.std_args(L, 2)
        self.var_result(L, dst, Call('Abs', [L.variant_at(a)], pure=True, ty='Variant'))

    def var_tst(self, L, name):
        rel = VARTST[name[-2:]]
        rhs, lhs = self.std_args(L, 2)
        if 'Text' in name:
            self.ctx.option_compare_text = True
        L.set_reg('eax', BoolVal(Bin(rel, L.variant_at(lhs), L.variant_at(rhs), ty='Boolean')))

    def var_cmp(self, L, name):
        rel = VARTST[name[-2:]]
        dst, rhs, lhs = self.std_args(L, 3)
        if 'Text' in name:
            self.ctx.option_compare_text = True
        self.var_result(L, dst, Bin(rel, L.variant_at(lhs), L.variant_at(rhs), ty='Boolean'))

    def conv_var(self, L, ty, fn=None):
        p, = self.std_args(L, 1)
        v = L.variant_at(p)
        if fn:
            v = Call(fn, [v], pure=True, ty=ty)
        else:
            if getattr(v, 'ty', None) in (None, 'Variant'):
                v.__dict__.setdefault('conv', ty)
        return v

    def h___vbaI4Var(self, L):
        L.set_reg('eax', self.conv_var(L, 'Long'))

    def h___vbaI2Var(self, L):
        L.set_reg('eax', self.conv_var(L, 'Integer'))

    def h___vbaUI1Var(self, L):
        L.set_reg('eax', self.conv_var(L, 'Byte'))

    def h___vbaBoolVar(self, L):
        v = self.conv_var(L, 'Boolean')
        L.set_reg('eax', BoolVal(v) if not isinstance(v, BoolVal) else v)

    h___vbaBoolVarNull = h___vbaBoolVar

    def h___vbaR8Var(self, L):
        L.fpush(self.conv_var(L, 'Double'))

    def h___vbaR4Var(self, L):
        L.fpush(self.conv_var(L, 'Single'))

    def h___vbaDateVar(self, L):
        v = self.conv_var(L, 'Date')
        v.__dict__['date_hint'] = True
        L.fpush(v)

    def h___vbaObjVar(self, L):
        L.set_reg('eax', self.conv_var(L, 'Object'))

    def h___vbaVarVargNofree(self, L):
        L.set_reg('eax', L.reg('edx'))

    # ------------------------------------------------------------------ numbers
    def h___vbaFpI2(self, L):
        v = L.fpop()
        L.set_reg('eax', self.fallible(L, self.round_conv(v, 'CInt', 'Integer')))

    def h___vbaFpI4(self, L):
        v = L.fpop()
        L.set_reg('eax', self.fallible(L, self.round_conv(v, 'CLng', 'Long')))

    def h___vbaFpUI1(self, L):
        v = L.fpop()
        L.set_reg('eax', self.fallible(L, self.round_conv(v, 'CByte', 'Byte')))

    @staticmethod
    def fallible(L, e):
        """A conversion that can fail (overflow) happens here, not where a register carries it later:
        keep it as a statement at this point (a temporary; it is inlined again where that changes
        nothing)."""
        if isinstance(e, Call) and e.fn in ('CInt', 'CLng', 'CByte'):
            return L.call_value(e, e.ty)
        return e

    def round_conv(self, v, fn, ty):
        if isinstance(v, FConst) and v.v == int(v.v):
            return Const(int(v.v), ty)
        if getattr(v, 'ty', None) == ty and not isinstance(v, Bin):
            return v
        # Int()/Fix() keep their argument's floating-point type in VB: the conversion stays visible, so that
        # arithmetic on the result is Integer/Long arithmetic as in the machine code (emit drops it again
        # where the value is simply stored into a variable of that type)
        return Call(fn, [v], pure=True, ty=ty)

    def h___vbaFPInt(self, L):
        L.fset(0, Call('Int', [L.fget(0)], pure=True, ty='Double'))

    def h___vbaFPFix(self, L):
        L.fset(0, Call('Fix', [L.fget(0)], pure=True, ty='Double'))

    # Int/Fix straight to an integer: the result is an Integer/Long (CInt(Int(x))), not a floating-point value
    def h___vbaR8IntI2(self, L):
        L.set_reg('eax', self.fallible(L, Call('CInt', [Call('Int', [L.fpop()], pure=True, ty='Double')],
                                              pure=True, ty='Integer')))

    def h___vbaR8IntI4(self, L):
        L.set_reg('eax', self.fallible(L, Call('CLng', [Call('Int', [L.fpop()], pure=True, ty='Double')],
                                              pure=True, ty='Long')))

    def h___vbaR8FixI2(self, L):
        L.set_reg('eax', self.fallible(L, Call('CInt', [Call('Fix', [L.fpop()], pure=True, ty='Double')],
                                              pure=True, ty='Integer')))

    def h___vbaR8FixI4(self, L):
        L.set_reg('eax', self.fallible(L, Call('CLng', [Call('Fix', [L.fpop()], pure=True, ty='Double')],
                                              pure=True, ty='Long')))

    # Long -> Integer and Integer/Long -> Byte: the runtime checks the range (error 6 Overflow)
    def h___vbaI2I4(self, L):
        v = L.reg('ecx')
        L.set_reg('eax', v if isinstance(v, Const) and -32768 <= v.v <= 32767 else
                  self.fallible(L, Call('CInt', [v], pure=True, ty='Integer')))

    def h___vbaUI1I2(self, L):
        v = L.reg('ecx')
        L.set_reg('eax', v if isinstance(v, Const) and 0 <= v.v <= 255 else
                  self.fallible(L, Call('CByte', [v], pure=True, ty='Byte')))

    h___vbaUI1I4 = h___vbaUI1I2

    def h___vbaCopyBytes(self, L):
        # __vbaCopyBytes(len, dst, src): fixed-size record (UDT) assignment
        n, dst, src = self.std_args(L, 3)
        size = n.v if isinstance(n, Const) else 4
        val = L.deref_ptr(src, size)
        if isinstance(val, Var):
            val = Var(val.kind, val.off, size, val.ty, val.name)
        tgt = L.ptr_target(dst, size)
        if isinstance(tgt, Var):
            tgt = Var(tgt.kind, tgt.off, size, tgt.ty, tgt.name)
            L.invalidate_var(tgt, size)
        # a record copied into a record-typed place is a record of that type (and the other way round)
        for a, b in ((tgt, val), (val, tgt)):
            rt = self.ctx.record_type_at(L, a, size)
            if rt and isinstance(b, Var) and self.ctx.record_type_at(L, b, size) is None:
                self.ctx.note_record(L, b, rt)
        L.emit(Assign(tgt, val))
        L.set_reg('eax', dst)

    def h___vbaI2Sgn(self, L):
        L.set_reg('eax', Call('Sgn', [L.reg('ecx')], pure=True, ty='Integer'))

    def h___vbaI2Abs(self, L):
        L.set_reg('eax', Call('Abs', [L.reg('ecx')], pure=True, ty='Integer'))

    def h___vbaI4Abs(self, L):
        L.set_reg('eax', Call('Abs', [L.reg('ecx')], pure=True, ty='Long'))

    def h___vbaR8Abs(self, L):
        L.fset(0, Call('Abs', [L.fget(0)], pure=True, ty='Double'))

    def h___vbaR8Sgn(self, L):
        L.set_reg('eax', Call('Sgn', [L.fpop()], pure=True, ty='Integer'))

    def h___vbaI4Sgn(self, L):
        L.set_reg('eax', Call('Sgn', [L.reg('ecx')], pure=True, ty='Integer'))

    def h___vbaPowerR8(self, L):
        b = L.cell8(0)
        a = L.cell8(8)
        L.consume(16)
        L.fpush(Bin('^', a, b, ty='Double'))

    def h__allmul(self, L):
        alo, ahi, blo, bhi = self.std_args(L, 4)
        L.set_reg('eax', Bin('*', alo, blo))
        L.set_reg('edx', Unknown('allmul_hi'))

    # ------------------------------------------------------------------ objects
    def h___vbaObjSet(self, L):
        dst, src = self.std_args(L, 2)
        L.store_ptr(dst, src, 4, 'Object', kind='set')
        L.set_reg('eax', src)

    h___vbaObjSetAddref = h___vbaObjSet

    def h___vbaNew(self, L):
        desc, = self.std_args(L, 1)
        cls = self.ctx.class_name(desc)
        L.set_reg('eax', Raw('New %s' % cls, 'Object'))
        L.cur_new_class = cls

    def h___vbaNew2(self, L):
        desc, pvar = self.std_args(L, 2)
        self.ctx.note_global_object(pvar, desc)

    def h___vbaCastObj(self, L):
        obj, iid = self.std_args(L, 2)
        L.set_reg('eax', obj)

    def h___vbaCastObjVar(self, L):
        p, iid = self.std_args(L, 2)
        L.set_reg('eax', L.variant_at(p))

    def h___vbaObjIs(self, L):
        b, a = self.std_args(L, 2)
        L.set_reg('eax', BoolVal(Bin('Is', a, b, ty='Boolean')))

    # ------------------------------------------------------------------ control flow / errors
    def h___vbaOnError(self, L):
        n, = self.std_args(L, 1)
        if isinstance(n, Const):
            v = n.v
            if v == -1:
                L.emit(Text('On Error Resume Next'))
            elif v == 0:
                L.emit(Text('On Error GoTo 0'))
            else:
                lab = self.ctx.handler_label(L, v)
                L.emit(Text('On Error GoTo %s' % lab))
        else:
            L.emit(Text('On Error GoTo [%s]' % n))

    def h___vbaEnd(self, L):
        L.emit(Text('End'))

    def h___vbaStopExe(self, L):
        L.emit(Text('Stop'))

    def h___vbaChkstk(self, L):
        n = L.reg('eax')
        if isinstance(n, Const):
            L.st.sp -= n.v
            if L.local_low is None:
                pass

    def h___vbaResume(self, L):
        n, = self.std_args(L, 1)
        if isinstance(n, Const) and n.v == 0:
            L.emit(Text('Resume'))
        elif isinstance(n, Const) and n.v == -1:
            L.emit(Text('Resume Next'))
        else:
            L.emit(Text('Resume %s' % n))

    def h___vbaErrorOverflow(self, L):
        pass

    def h___vbaError(self, L):
        n, = self.std_args(L, 1)
        L.emit(FmtStmt('Error {0}', [n]))

    # ------------------------------------------------------------------ arrays
    def h___vbaUbound(self, L):
        dim, psa = self.std_args(L, 2)
        args = [self.ctx.array_ref(L, psa)]
        if not (isinstance(dim, Const) and dim.v == 1):
            args.append(dim)
        self.pure(L, 'UBound', args, 'Long')

    def h___vbaLbound(self, L):
        dim, psa = self.std_args(L, 2)
        args = [self.ctx.array_ref(L, psa)]
        if not (isinstance(dim, Const) and dim.v == 1):
            args.append(dim)
        self.pure(L, 'LBound', args, 'Long')

    def redim(self, L, preserve):
        # cdecl: (features, cbElem, ppsa, vt, nDims, then per dim: ubound, lbound)
        feats = L.cell(0)
        cb = L.cell(4)
        ppsa = L.cell(8)
        vt = L.cell(12)
        nd = L.cell(16)
        dims = []
        if isinstance(nd, Const):
            for k in range(nd.v):
                ub = L.cell(20 + 8 * k)
                lb = L.cell(24 + 8 * k)
                dims.append((lb, ub))
        arr = L.ptr_target(ppsa, 4)
        if isinstance(arr, Var):
            L.invalidate_var(arr, 4)
            o = L.var_loc(arr)
            if o is not None:
                for k in list(L.st.mem):
                    if k == o:
                        del L.st.mem[k]
        self.ctx.note_array(L, arr, vt, cb, len(dims))
        txt = ', '.join('%s To %s' % (lb, ub) if not (isinstance(lb, Const) and lb.v == 0) else '%s' % ub
                        for lb, ub in reversed(dims))
        exprs = [arr]
        for lb, ub in reversed(dims):
            exprs += [lb, ub]
        fmt = 'ReDim %s{0}(' % ('Preserve ' if preserve else '')
        parts = []
        i = 1
        for lb, ub in reversed(dims):
            if isinstance(lb, Const) and lb.v == 0:
                parts.append('{%d}' % (i + 1))
            else:
                parts.append('{%d} To {%d}' % (i, i + 1))
            i += 2
        L.emit(FmtStmt(fmt + ', '.join(parts) + ')', exprs))

    def h___vbaRedim(self, L):
        self.redim(L, False)

    def h___vbaRedimPreserve(self, L):
        self.redim(L, True)

    def h___vbaErase(self, L):
        flags, ppsa = self.std_args(L, 2)
        t = L.ptr_target(ppsa, 4)
        if isinstance(t, Var):
            o = L.var_loc(t)
            if o is not None:
                L.st.mem.pop(o, None)
            L.emit(FmtStmt('Erase {0}', [t]))
            return
        L.emit(FmtStmt('Erase {0}', [self.ctx.array_ref(L, ppsa)]))

    def h___vbaAryDestruct(self, L):
        flags, ppsa = self.std_args(L, 2)
        t = L.ptr_target(ppsa, 4)
        if isinstance(t, Var):
            L.invalidate_var(t, 4)
        if isinstance(t, Var) and t.kind in ('field', 'global'):
            L.emit(FmtStmt('Erase {0}', [t]))

    def h___vbaAryCopy(self, L):
        dst, src = self.std_args(L, 2)
        L.store_ptr(dst, L.deref_ptr(src, 4), 4, 'Array')

    def h___vbaAryMove(self, L):
        dst, src = self.std_args(L, 2)
        L.store_ptr(dst, L.deref_ptr(src, 4), 4, 'Array')

    def h___vbaAryVar(self, L):
        vt, p = self.std_args(L, 2)
        L.set_reg('eax', L.variant_at(p))

    def h___vbaAryConstruct2(self, L):
        # __vbaAryConstruct2(descriptor, template, vt): a fixed-size local array; the descriptor sits in the
        # frame (its data pointer, the variable the code indexes, 12 bytes further) and the template
        # gives the dimensions, element size and bounds
        pdesc, tmpl, vt = self.std_args(L, 3)
        va = tmpl.va if isinstance(tmpl, DataPtr) else tmpl.v if isinstance(tmpl, Const) else None
        if isinstance(pdesc, SAddr) and va is not None:
            import instdata
            r = instdata.scan_descriptor(L.im, va & 0xffffffff, 2)
            if r is not None:
                _, cdims, cb, feat, bounds = r
                data = L.stack_var(pdesc.o + 0xC, 4)
                if isinstance(data, Var):
                    self.ctx.local_fixed_arrays[(L.proc.va,) + tuple(data.key())] = (
                        cb, bounds, vt.v if isinstance(vt, Const) else None)

    # ------------------------------------------------------------------ late binding
    def late_args(self, L, start, argc):
        """Late-bound call args: argc VARIANTs by value (16 bytes each), last argument first."""
        out = []
        for k in range(argc):
            out.append(L.variant_cells(start + 16 * k))
        out.reverse()
        return out

    def h___vbaLateIdCall(self, L):
        obj, dispid, argc = L.cell(0), L.cell(4), L.cell(8)
        n = argc.v if isinstance(argc, Const) else 0
        args = self.late_args(L, 12, n)
        nm = self.ctx.dispid_name(obj, dispid)
        L.call_stmt(Call(nm, args, obj=obj, kind='com'))

    def h___vbaLateIdCallLd(self, L):
        dst, obj, dispid, argc = L.cell(0), L.cell(4), L.cell(8), L.cell(12)
        n = argc.v if isinstance(argc, Const) else 0
        args = self.late_args(L, 16, n)
        nm = self.ctx.dispid_name(obj, dispid)
        t = L.call_value(Call(nm, args, obj=obj, kind='com', ty='Variant'))
        self.var_result(L, dst, t)

    def h___vbaLateIdSt(self, L):
        obj, dispid = L.cell(0), L.cell(4)
        val = L.variant_cells(8)
        nm = self.ctx.dispid_name(obj, dispid)
        L.emit(Assign(Call(nm, [], obj=obj, kind='com', prop='let'), val))

    def h___vbaLateIdStAd(self, L):
        self.h___vbaLateIdSt(L)

    def h___vbaLateMemCallLd(self, L):
        dst, obj, name, argc = L.cell(0), L.cell(4), L.cell(8), L.cell(12)
        n = argc.v if isinstance(argc, Const) else 0
        args = self.late_args(L, 16, n)
        nm = name.s if isinstance(name, Str) else str(name)
        t = L.call_value(Call(nm, args, obj=obj, kind='com', ty='Variant'))
        self.var_result(L, dst, t)

    def h___vbaLateMemCall(self, L):
        obj, name, argc = L.cell(0), L.cell(4), L.cell(8)
        n = argc.v if isinstance(argc, Const) else 0
        args = self.late_args(L, 12, n)
        nm = name.s if isinstance(name, Str) else str(name)
        L.call_stmt(Call(nm, args, obj=obj, kind='com'))

    def h___vbaVarLateMemCallLd(self, L):
        dst, pobj, name, argc = L.cell(0), L.cell(4), L.cell(8), L.cell(12)
        n = argc.v if isinstance(argc, Const) else 0
        args = self.late_args(L, 16, n)
        nm = name.s if isinstance(name, Str) else str(name)
        t = L.call_value(Call(nm, args, obj=L.variant_at(pobj), kind='com', ty='Variant'))
        self.var_result(L, dst, t)

    def h___vbaVarLateMemSt(self, L):
        # (VARIANT* obj, name, VARIANT value by value)
        pobj, name = L.cell(0), L.cell(4)
        val = L.variant_cells(8)
        nm = name.s if isinstance(name, Str) else str(name)
        L.emit(Assign(Call(nm, [], obj=L.variant_at(pobj), kind='com', prop='let'), val))
        L.consume(24)

    def h___vbaLateMemSt(self, L):
        obj, name = L.cell(0), L.cell(4)
        val = L.variant_cells(8)
        nm = name.s if isinstance(name, Str) else str(name)
        L.emit(Assign(Call(nm, [], obj=obj, kind='com', prop='let'), val))

    # ------------------------------------------------------------------ file I/O
    def h___vbaFileOpen(self, L):
        mode, reclen, fnum, fname = self.std_args(L, 4)
        m = mode.v if isinstance(mode, Const) else None
        txt = 'Open {0} For %s' % (FILE_MODES.get(m & 0xff, 'Mode%s' % m) if m is not None else '[mode]')
        if m is not None:
            acc = (m >> 8) & 0xf
            if acc:
                txt += ' Access %s' % {1: 'Read', 2: 'Write', 3: 'Read Write'}.get(acc, str(acc))
            lock = (m >> 12) & 0xf
            if lock:
                txt += ' %s' % {1: 'Lock Read Write', 2: 'Lock Write', 3: 'Lock Read', 4: 'Shared'}.get(lock, '')
        txt += ' As #{1}'
        args = [fname, fnum]
        if isinstance(reclen, Const) and reclen.v not in (-1, 0):
            txt += ' Len = {2}'
            args.append(reclen)
        L.emit(FmtStmt(txt, args))

    def h___vbaFileClose(self, L):
        n, = self.std_args(L, 1)
        L.emit(FmtStmt('Close #{0}', [n]))

    def h___vbaFileCloseAll(self, L):
        L.emit(Text('Close'))

    def h___vbaFreeFile(self, L):
        self.pure(L, 'FreeFile', [], 'Integer')
        L.st.regs['eax'].paren = False

    def io_desc(self, L, desc):
        """Format descriptor of Print/Write/Input: [word count][code per item]."""
        if isinstance(desc, Str) and getattr(desc, 'va', None):
            desc = DataPtr(desc.va)      # descriptor bytes that happen to look like a BSTR
        if isinstance(desc, DataPtr):
            im = L.im
            n = im.u16(desc.va)
            if 0 < n < 64:
                return [im.u8(desc.va + 2 + k) for k in range(n)]
        return None

    def io_items(self, L, codes, start, byref):
        items = []
        off = start
        for c in codes or []:
            vt = c & 0x3f
            if vt == 0:
                continue                # empty item (e.g. `Print #1,` writes a blank line)
            if byref:
                p = L.cell(off)
                size = {2: 2, 3: 4, 4: 4, 5: 8, 6: 8, 7: 8, 8: 4, 11: 2, 12: 16, 17: 1}.get(vt, 4)
                ty = {2: 'Integer', 3: 'Long', 4: 'Single', 5: 'Double', 7: 'Date', 8: 'String', 11: 'Boolean',
                      12: 'Variant', 17: 'Byte'}.get(vt)
                tgt = L.ptr_target(p, size, ty)
                if ty:
                    L.note_type(tgt, ty, 100)       # the item's type code is the variable's declared type
                items.append(tgt)
                off += 4
            else:
                if vt in (5, 6, 7):
                    items.append(L.cell8(off))
                    off += 8
                elif vt == 12:
                    items.append(L.variant_at(L.cell(off)))
                    off += 4
                else:
                    v = L.cell(off)
                    ty = {2: 'Integer', 3: 'Long', 4: 'Single', 8: 'String', 11: 'Boolean', 17: 'Byte'}.get(vt)
                    if ty and isinstance(v, Var):
                        L.note_type(v, ty, 100)     # a variable passed as is: its declared type
                    items.append(v)
                    off += 4
        return items

    def h___vbaInputFile(self, L):
        desc, fnum = L.cell(0), L.cell(4)
        codes = self.io_desc(L, desc)
        items = self.io_items(L, codes, 8, True)
        L.emit(FmtStmt('Input #{0}, ' + ', '.join('{%d}' % (k + 1) for k in range(len(items))), [fnum] + items))
        L.invalidate_locals()

    def h___vbaWriteFile(self, L):
        desc, fnum = L.cell(0), L.cell(4)
        codes = self.io_desc(L, desc)
        items = self.io_items(L, codes, 8, False)
        L.emit(FmtStmt('Write #{0}, ' + ', '.join('{%d}' % (k + 1) for k in range(len(items))), [fnum] + items))

    def h___vbaPrintFile(self, L):
        desc, fnum = L.cell(0), L.cell(4)
        codes = self.io_desc(L, desc)
        items = self.io_items(L, codes, 8, False)
        if not items:
            L.emit(FmtStmt('Print #{0},', [fnum]))
            return
        # item codes: 0x80 = the statement ends (new line), 0x40 = comma (next print zone), neither = semicolon
        seps = [None if c & 0x80 else (',' if c & 0x40 else ';') for c in (codes or []) if c & 0x3f]
        seps += [None] * (len(items) - len(seps))
        text = 'Print #{0}, '
        for k in range(len(items)):
            text += '{%d}' % (k + 1)
            if seps[k] is not None:
                text += seps[k] + (' ' if k + 1 < len(items) else '')
        L.emit(FmtStmt(text, [fnum] + items))

    def h___vbaLineInputStr(self, L):
        pstr, fnum = self.std_args(L, 2)
        L.emit(FmtStmt('Line Input #{0}, {1}', [fnum, L.ptr_target(pstr, 4, 'String')]))
        L.invalidate_locals()

    def h___vbaLineInputVar(self, L):
        pvar, fnum = self.std_args(L, 2)
        L.emit(FmtStmt('Line Input #{0}, {1}', [fnum, L.ptr_target(pvar, 16, 'Variant')]))
        L.invalidate_locals()

    def h_rtcEndOfFile(self, L):
        n, = self.std_args(L, 1)
        L.set_reg('eax', Call('EOF', [n], pure=False, ty='Boolean'))

    def h_rtcFileLength(self, L):
        n, = self.std_args(L, 1)
        L.set_reg('eax', Call('LOF', [n], pure=False, ty='Long'))

    # ------------------------------------------------------------------ rtc* library functions
    def var_fn(self, L, name, nargs, conv=None, ty='Variant'):
        """rtcXxxVar(VARIANT* ret, args...) family."""
        args = self.std_args(L, nargs + 1)
        dst = args[0]
        vals = [L.variant_at(a) if conv and conv[i] == 'v' else a for i, a in enumerate(args[1:])]
        self.var_result(L, dst, Call(name, vals, pure=True, ty=ty))

    def h_rtcAnsiValueBstr(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'Asc', [s], 'Integer')

    def h_rtcTrimBstr(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'Trim$', [s], 'String')

    def h_rtcLeftTrimBstr(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'LTrim$', [s], 'String')

    def h_rtcRightTrimBstr(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'RTrim$', [s], 'String')

    def h_rtcTrimVar(self, L):
        self.var_fn(L, 'Trim', 1, 'v')

    def h_rtcLeftTrimVar(self, L):
        self.var_fn(L, 'LTrim', 1, 'v')

    def h_rtcRightTrimVar(self, L):
        self.var_fn(L, 'RTrim', 1, 'v')

    def h_rtcSpaceVar(self, L):
        self.var_fn(L, 'Space', 1, 'i')

    def h_rtcSpaceBstr(self, L):
        n, = self.std_args(L, 1)
        self.pure(L, 'Space$', [n], 'String')

    def h_rtcKillFiles(self, L):
        p, = self.std_args(L, 1)
        L.emit(FmtStmt('Kill {0}', [L.variant_at(p)]))

    def h_rtcChangeDir(self, L):
        s, = self.std_args(L, 1)
        L.emit(FmtStmt('ChDir {0}', [s]))

    def h_rtcMakeDir(self, L):
        s, = self.std_args(L, 1)
        L.emit(FmtStmt('MkDir {0}', [s]))

    def h_rtcBeep(self, L):
        L.emit(Text('Beep'))

    def h_rtcGetTimer(self, L):
        L.fpush(Raw('Timer', 'Single'))

    def h_rtcGetPresentDate(self, L):
        dst, = self.std_args(L, 1)
        self.var_result(L, dst, Raw('Now', 'Variant'))

    def h_rtcGetDateVar(self, L):
        dst, = self.std_args(L, 1)
        self.var_result(L, dst, Raw('Date', 'Variant'))

    def h_rtcGetTimeVar(self, L):
        dst, = self.std_args(L, 1)
        self.var_result(L, dst, Raw('Time', 'Variant'))

    def h_rtcR8ValFromBstr(self, L):
        s, = self.std_args(L, 1)
        L.fpush(Call('Val', [s], pure=True, ty='Double'))

    def h_rtcRandomNext(self, L):
        p, = self.std_args(L, 1)
        v = L.variant_at(p)
        args = [] if isinstance(v, Missing) else [v]
        c = Call('Rnd', args, ty='Single', paren=bool(args))
        L.fpush(L.call_value(c, 'Single'))

    def h_rtcRandomize(self, L):
        p, = self.std_args(L, 1)
        v = L.variant_at(p)
        if isinstance(v, Missing):
            L.emit(Text('Randomize'))
        else:
            L.emit(FmtStmt('Randomize {0}', [v]))

    def h_rtcMsgBox(self, L):
        prompt, buttons, title, helpf, ctxid = self.std_args(L, 5)
        args = [L.variant_at(prompt), buttons, L.variant_at(title), L.variant_at(helpf), L.variant_at(ctxid)]
        if isinstance(buttons, Const):
            args[1] = Raw(self.ctx.msgbox_flags(buttons.v)) if buttons.v else Missing()
        t = L.call_value(Call('MsgBox', args, ty='Integer'), 'Integer')
        L.set_reg('eax', t)

    def h_rtcInputBox(self, L):
        args = self.std_args(L, 7)
        vals = [L.variant_at(a) for a in args[:3]] + args[3:5] + [L.variant_at(a) for a in args[5:]]
        L.set_reg('eax', L.call_value(Call('InputBox', vals, ty='String'), 'String'))

    def h_rtcDoEvents(self, L):
        L.emit(Text('DoEvents'))
        L.set_reg('eax', Const(0))

    def h_rtcArray(self, L):
        dst, ppsa = self.std_args(L, 2)
        t = L.ptr_target(ppsa, 4)
        c = Call('Array', [Unknown('items')], pure=True, ty='Variant')
        if isinstance(t, Var):
            c.items_of = t
        self.var_result(L, dst, c)

    def h_rtcVarBstrFromAnsi(self, L):
        self.var_fn(L, 'Chr', 1, 'i')

    def h_rtcBstrFromAnsi(self, L):
        n, = self.std_args(L, 1)
        self.pure(L, 'Chr$', [n], 'String')

    def h_rtcVarStrFromVar(self, L):
        self.var_fn(L, 'Str', 1, 'v')

    def h_rtcStrFromVar(self, L):
        p, = self.std_args(L, 1)
        self.pure(L, 'Str$', [L.variant_at(p)], 'String')

    def h_rtcLeftCharBstr(self, L):
        s, n = self.std_args(L, 2)
        self.pure(L, 'Left$', [s, n], 'String')

    def h_rtcRightCharBstr(self, L):
        s, n = self.std_args(L, 2)
        self.pure(L, 'Right$', [s, n], 'String')

    def h_rtcLeftCharVar(self, L):
        self.var_fn(L, 'Left', 2, 'vi')

    def h_rtcRightCharVar(self, L):
        self.var_fn(L, 'Right', 2, 'vi')

    def h_rtcMidCharVar(self, L):
        dst, p, start, plen = self.std_args(L, 4)
        args = [L.variant_at(p), start, L.variant_at(plen)]
        if isinstance(args[2], Missing):
            args = args[:2]
        self.var_result(L, dst, Call('Mid', args, pure=True, ty='Variant'))

    def h_rtcMidCharBstr(self, L):
        s, start, plen = self.std_args(L, 3)
        args = [s, start, L.variant_at(plen)]
        if isinstance(args[2], Missing):
            args = args[:2]
        self.pure(L, 'Mid$', args, 'String')

    def h_rtcLowerCaseBstr(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'LCase$', [s], 'String')

    def h_rtcUpperCaseBstr(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'UCase$', [s], 'String')

    def h_rtcLowerCaseVar(self, L):
        self.var_fn(L, 'LCase', 1, 'v')

    def h_rtcUpperCaseVar(self, L):
        self.var_fn(L, 'UCase', 1, 'v')

    def h_rtcDir(self, L):
        p, attrs = self.std_args(L, 2)
        args = [L.variant_at(p), attrs]
        if isinstance(attrs, Const) and attrs.v == 0:
            args = args[:1]
        if isinstance(args[0], Missing):
            args = []
        t = L.call_value(Call('Dir', args, ty='String', paren=bool(args)), 'String')
        L.set_reg('eax', t)

    def h_rtcDatePart(self, L):
        dst, interval, pdate, fdow, fwoy = self.std_args(L, 5)
        args = [interval, L.variant_at(pdate)]
        self.var_result(L, dst, Call('DatePart', args, pure=True, ty='Variant'))

    def h_rtcDateAdd(self, L):
        dst = L.cell(0)
        interval = L.cell(4)
        num = L.cell8(8)
        pdate = L.cell(16)
        L.consume(20)
        self.var_result(L, dst, Call('DateAdd', [interval, num, L.variant_at(pdate)], pure=True, ty='Variant'))

    def h_rtcDateDiff(self, L):
        dst, interval, p1, p2, fdow, fwoy = self.std_args(L, 6)
        self.var_result(L, dst, Call('DateDiff', [interval, L.variant_at(p1), L.variant_at(p2)], pure=True,
                                     ty='Variant'))

    def h_rtcErrObj(self, L):
        L.set_reg('eax', Raw('Err', 'Object'))

    def h_rtcGetSetting(self, L):
        # rtcGetSetting(BSTR app, BSTR section, BSTR key, VARIANT default /*by value*/)
        app, sec, key = L.cell(0), L.cell(4), L.cell(8)
        dflt = L.variant_cells(12)
        L.consume(28)
        args = [app, sec, key, dflt]
        if isinstance(args[3], Missing):
            args = args[:3]
        L.set_reg('eax', L.call_value(Call('GetSetting', args, ty='String'), 'String'))

    def h_rtcSaveSetting(self, L):
        app, sec, key, val = self.std_args(L, 4)
        L.emit(FmtStmt('SaveSetting {0}, {1}, {2}, {3}', [app, sec, key, val]))

    def h_rtcDeleteSetting(self, L):
        app, psec, pkey = self.std_args(L, 3)
        L.emit(FmtStmt('DeleteSetting {0}, {1}, {2}', [app, L.variant_at(psec), L.variant_at(pkey)]))

    def h_rtcSplit(self, L):
        # rtcSplit(VARIANT* ret, BSTR expression, VARIANT* delimiter, long limit, long compare)
        dst, expr, pdelim, count, cmp_ = self.std_args(L, 5)
        args = [expr, L.variant_at(pdelim)]
        if isinstance(args[1], Missing):
            args = args[:1]
        self.var_result(L, dst, Call('Split', args, pure=True, ty='Variant'))

    def h_rtcReplace(self, L):
        # (VARIANT* ret?) - observed: rtcReplace(expr, find, repl, start, count, compare) -> BSTR
        n = L.pending_cells()
        args, _ = L.args_by_sizes([4] * (n // 4))
        L.consume(n)
        vals = args[:3]
        L.set_reg('eax', Call('Replace', vals, pure=True, ty='String'))

    def h_rtcStrConvVar2(self, L):
        dst, p, conv, lcid = self.std_args(L, 4)
        self.var_result(L, dst, Call('StrConv', [L.variant_at(p), conv], pure=True, ty='Variant'))

    def h_rtcShell(self, L):
        p, style = self.std_args(L, 2)
        L.fpush(L.call_value(Call('Shell', [L.variant_at(p), style], ty='Double'), 'Double'))

    def h_rtcCommandVar(self, L):
        dst, = self.std_args(L, 1)
        self.var_result(L, dst, Raw('Command', 'Variant'))

    def h_rtcCurrentDir(self, L):
        dst, drive = self.std_args(L, 2)
        self.var_result(L, dst, Raw('CurDir', 'Variant'))

    def h_rtcSendKeys(self, L):
        p, wait = self.std_args(L, 2)
        L.emit(FmtStmt('SendKeys {0}', [L.variant_at(p)]))

    def h_rtcAppActivate(self, L):
        p, wait = self.std_args(L, 2)
        L.emit(FmtStmt('AppActivate {0}', [L.variant_at(p)]))

    def h_rtcHexVarFromVar(self, L):
        self.var_fn(L, 'Hex', 1, 'v')

    def h_rtcGetFileAttr(self, L):
        s, = self.std_args(L, 1)
        self.pure(L, 'GetAttr', [s], 'Integer')

    def h_rtcSetFileAttr(self, L):
        s, a = self.std_args(L, 2)
        L.emit(FmtStmt('SetAttr {0}, {1}', [s, a]))

    def h_rtcFileCopy(self, L):
        a, b = self.std_args(L, 2)
        L.emit(FmtStmt('FileCopy {0}, {1}', [a, b]))

    def h_rtcIsNumeric(self, L):
        p, = self.std_args(L, 1)
        self.pure(L, 'IsNumeric', [L.variant_at(p)], 'Boolean')

    def h_rtcIsDate(self, L):
        p, = self.std_args(L, 1)
        self.pure(L, 'IsDate', [L.variant_at(p)], 'Boolean')

    def h_rtcIsArray(self, L):
        p, = self.std_args(L, 1)
        self.pure(L, 'IsArray', [L.variant_at(p)], 'Boolean')

    def h_rtcIsEmpty(self, L):
        p, = self.std_args(L, 1)
        self.pure(L, 'IsEmpty', [L.variant_at(p)], 'Boolean')

    def h_rtcIsNull(self, L):
        p, = self.std_args(L, 1)
        self.pure(L, 'IsNull', [L.variant_at(p)], 'Boolean')

    def h_rtcCreateObject2(self, L):
        dst, pname, pserver = self.std_args(L, 3)
        self.var_result(L, dst, Call('CreateObject', [L.variant_at(pname)], ty='Object'))

    def h_rtcStringBstr(self, L):
        n, p = self.std_args(L, 2)
        self.pure(L, 'String$', [n, L.variant_at(p)], 'String')

    def h_rtcStringVar(self, L):
        dst, n, p = self.std_args(L, 3)
        self.var_result(L, dst, Call('String', [n, L.variant_at(p)], pure=True, ty='Variant'))

    def h_rtcInStrRev(self, L):
        n = L.pending_cells()
        args, _ = L.args_by_sizes([4] * (n // 4))
        L.consume(n)
        L.set_reg('eax', Call('InStrRev', args[:3], pure=True, ty='Long'))

    def h_rtcSgnVar(self, L):
        self.var_fn(L, 'Sgn', 1, 'v')

    def h_rtcAbsVar(self, L):
        self.var_fn(L, 'Abs', 1, 'v')

    def h_rtcFixVar(self, L):
        self.var_fn(L, 'Fix', 1, 'v')

    def h_rtcIntVar(self, L):
        self.var_fn(L, 'Int', 1, 'v')

    def h_rtcVarFromFormatVar(self, L):
        dst, p, pfmt, fdow, fwoy = self.std_args(L, 5)
        args = [L.variant_at(p), L.variant_at(pfmt)]
        if isinstance(args[1], Missing):
            args = args[:1]
        self.var_result(L, dst, Call('Format', args, pure=True, ty='Variant'))

    def h_rtcBstrFromFormatVar(self, L):
        p, pfmt, fdow, fwoy = self.std_args(L, 4)
        args = [L.variant_at(p), L.variant_at(pfmt)]
        if isinstance(args[1], Missing):
            args = args[:1]
        self.pure(L, 'Format$', args, 'String')
