"""Python models of MSVBVM60 runtime functions (machine-code side). Only the functions met so far are
modelled (those the Troopanum games import); a program that calls another one stops with machine-problem.

Argument conventions follow vbdec/runtime.py (verified against real call sites):
operator helpers take (dst, rhs, lhs) with the left operand pushed first; __vbaStrCopy/StrMove/VarMove/
VarCopy/VarDup take ecx = destination, edx = source; __vbaFree* take ecx or a cdecl list; FPU
functions read st(0) (the stub stores it to SCR_IN first) and return through the fld trampoline.
Values go through vbvalues, the same semantics the interpreter uses.
"""
import math
import struct

from vbvalues import (V, VBError, ObjRef, VBArray, EMPTY, NULL, MISSING, convert, to_str, to_int, to_float,
                      to_bool, str_v, binop, unop, val_of, str_to_num, f32, round_half_even, check_range,
                      date_parts, canon, VT_NAME, VT, INTEGRAL)
from world import StopRun, print_text, write_text, input_value

FILE_MODES = {1: 'input', 2: 'output', 4: 'random', 8: 'append', 0x20: 'binary'}


class RunStopError(Exception):
    pass


class Runtime:
    # functions that take their argument in st(0): the stub pops it into SCR_IN
    FPU_IN = {'__vbaFpI2', '__vbaFpI4', '__vbaFPInt', '__vbaR8IntI2', '__vbaR8IntI4', '__vbaFPFix',
              '__vbaR8FixI2', '__vbaR8FixI4', '_CIsin', '_CIcos', '_CItan', '_CIatan', '_CIexp', '_CIlog',
              '_CIsqrt', '__vbaFpUI1'}

    def __init__(self, m):
        self.m = m
        self.mem = m.mem

    @property
    def world(self):
        return self.m.world

    def call(self, name):
        f = getattr(self, 'h_' + name, None)
        if f is None:
            if name.startswith('_adj_f'):
                raise RunStopError('FDIV adjustment %s called (the FDIV flag should be clear)' % name)
            raise RunStopError('runtime function %s is not modelled' % name)
        f()

    # ------------------------------------------------------------------ helpers
    def a(self, i):
        return self.m.arg(i)

    def ia(self, i):
        v = self.m.arg(i)
        return v - (1 << 32) if v & 0x80000000 else v

    def h_DllFunctionCall(self):
        # a Declare's call stub asks the runtime for the function's address (descriptor pointer), then jumps
        # to it with the caller's arguments still in place: answer with the declare's own stub
        ent = self.m.declare_stubs.get(self.a(0))
        if ent is None:
            raise RunStopError('DllFunctionCall for an unknown declare descriptor %x' % self.a(0))
        self.ret(ent[1], 4)

    def ret(self, eax=0, pop=0, edx=None):
        self.m.ret(eax, pop, edx)

    def var(self, p):
        return self.mem.read_variant(p)

    def set_var(self, p, v):
        self.mem.write_variant(p, v)

    def bstr(self, p):
        return self.mem.read_bstr(p)

    def new_bstr(self, s):
        return self.mem.bstr(s)

    def var_result(self, dst, v, pop):
        self.set_var(dst, v.as_variant() if not v.variant else v)
        self.ret(dst, pop)

    # ------------------------------------------------------------------ strings
    def h___vbaStrCopy(self):
        dst, src = self.m.reg('ecx'), self.m.reg('edx')
        n = self.new_bstr(self.bstr(src)) if src else 0
        self.mem.w32(dst, n)
        self.ret(n)

    def h___vbaStrMove(self):
        dst, src = self.m.reg('ecx'), self.m.reg('edx')
        self.mem.w32(dst, src)
        self.ret(src)

    def h___vbaStrCat(self):
        rhs, lhs = self.a(0), self.a(1)
        self.ret(self.new_bstr(self.bstr(lhs) + self.bstr(rhs)), 8)

    def _cmp(self, text):
        rhs, lhs = self.bstr(self.a(0)), self.bstr(self.a(1))
        if text:
            lhs, rhs = lhs.lower(), rhs.lower()
        return (lhs > rhs) - (lhs < rhs)

    def h___vbaStrCmp(self):
        self.ret(self._cmp(False) & 0xffffffff, 8)

    def h___vbaStrTextCmp(self):
        self.ret(self._cmp(True) & 0xffffffff, 8)

    def h___vbaLenBstr(self):
        self.ret(len(self.bstr(self.a(0))), 4)

    def h___vbaStrI2(self):
        v = self.a(0) & 0xffff
        v = v - 0x10000 if v & 0x8000 else v
        self.ret(self.new_bstr(to_str(V('Integer', v))), 4)

    def h___vbaStrI4(self):
        self.ret(self.new_bstr(to_str(V('Long', self.ia(0)))), 4)

    def h___vbaStrBool(self):
        v = self.a(0) & 0xffff
        self.ret(self.new_bstr(to_str(V('Boolean', -1 if v else 0))), 4)

    def h___vbaStrR8(self):
        x = self.mem.f64(self.m.arg_at(0))
        self.ret(self.new_bstr(to_str(V('Double', x))), 8)

    def h___vbaStrR4(self):
        x = self.mem.f32(self.m.arg_at(0))
        self.ret(self.new_bstr(to_str(V('Single', x))), 4)

    def h___vbaStrVarMove(self):
        p = self.a(0)
        v = self.var(p)
        s = to_str(v)
        self.set_var(p, EMPTY)
        self.ret(self.new_bstr(s), 4)

    def h___vbaStrVarVal(self):
        ptmp, p = self.a(0), self.a(1)
        n = self.new_bstr(to_str(self.var(p)))
        self.mem.w32(ptmp, n)
        self.ret(n, 8)

    def h___vbaStrToAnsi(self):
        pdst, s = self.a(0), self.a(1)
        a = self.mem.ansi(self.bstr(s)) if s else 0
        self.mem.w32(pdst, a)
        self.ret(a, 8)

    def h___vbaStrToUnicode(self):
        pdst, ansi = self.a(0), self.a(1)
        n = self.new_bstr(self.mem.read_ansi(ansi)) if ansi else 0
        self.mem.w32(pdst, n)
        self.ret(n, 8)

    def h___vbaR8Str(self):
        x = convert(V('String', self.bstr(self.a(0))), 'Double').v
        self.m.ret_fpu(x, 4)

    def h___vbaI4Str(self):
        self.ret(convert(V('String', self.bstr(self.a(0))), 'Long').v & 0xffffffff, 4)

    def h___vbaI2Str(self):
        self.ret(convert(V('String', self.bstr(self.a(0))), 'Integer').v & 0xffff, 4)

    def h___vbaInStr(self):
        start, s1, s2, cmp_ = self.ia(0), self.bstr(self.a(1)), self.bstr(self.a(2)), self.ia(3)
        if start < 1:
            raise VBError(5)
        if cmp_ == 1:
            s1, s2 = s1.lower(), s2.lower()
        if start > len(s1):
            r = 0
        elif s2 == '':
            r = start
        else:
            r = s1.find(s2, start - 1) + 1
        self.ret(r, 16)

    def h_rtcAnsiValueBstr(self):
        s = self.bstr(self.a(0))
        if not s:
            raise VBError(5)
        self.ret(s[0].encode('latin-1', 'replace')[0], 4)

    def h_rtcR8ValFromBstr(self):
        self.m.ret_fpu(val_of(self.bstr(self.a(0))), 4)

    def h_rtcReplace(self):
        s, f, r = self.bstr(self.a(0)), self.bstr(self.a(1)), self.bstr(self.a(2))
        start, count, cmp_ = self.ia(3), self.ia(4), self.ia(5)
        if start != 1 or count != -1:
            raise RunStopError('Replace with start/count not modelled')
        if f == '':
            out = s
        elif cmp_ == 1:
            out = ''
            i = 0
            ls, lf = s.lower(), f.lower()
            while True:
                j = ls.find(lf, i)
                if j < 0:
                    out += s[i:]
                    break
                out += s[i:j] + r
                i = j + len(f)
        else:
            out = s.replace(f, r)
        self.ret(self.new_bstr(out), 24)

    # ------------------------------------------------------------------ variant string functions
    def h_rtcTrimVar(self):
        dst, p = self.a(0), self.a(1)
        v = self.var(p)
        self.var_result(dst, NULL if v.t == 'Null' else V('String', to_str(v).strip(' '), True), 8)

    def h_rtcSpaceVar(self):
        dst, n = self.a(0), self.ia(1)
        if n < 0:
            raise VBError(5)
        self.var_result(dst, V('String', ' ' * n, True), 8)

    def h_rtcVarBstrFromAnsi(self):
        dst, n = self.a(0), self.ia(1)
        if n < 0 or n > 255:
            raise VBError(5)
        self.var_result(dst, V('String', bytes([n]).decode('latin-1'), True), 8)

    def h_rtcVarStrFromVar(self):
        dst, p = self.a(0), self.a(1)
        v = self.var(p)
        self.var_result(dst, V('String', str_v(v), True), 8)

    def h_rtcLeftCharVar(self):
        dst, p, n = self.a(0), self.a(1), self.ia(2)
        if n < 0:
            raise VBError(5)
        v = self.var(p)
        self.var_result(dst, NULL if v.t == 'Null' else V('String', to_str(v)[:n], True), 12)

    def h_rtcRightCharVar(self):
        dst, p, n = self.a(0), self.a(1), self.ia(2)
        if n < 0:
            raise VBError(5)
        v = self.var(p)
        if v.t == 'Null':
            return self.var_result(dst, NULL, 12)
        s = to_str(v)
        self.var_result(dst, V('String', s[len(s) - n:] if n else '', True), 12)

    def h_rtcMidCharVar(self):
        dst, p, start, plen = self.a(0), self.a(1), self.ia(2), self.a(3)
        if start < 1:
            raise VBError(5)
        v = self.var(p)
        lv = self.var(plen)
        if v.t == 'Null':
            return self.var_result(dst, NULL, 16)
        s = to_str(v)
        if lv.t in ('Missing',):
            r = s[start - 1:]
        else:
            n = to_int(lv)
            if n < 0:
                raise VBError(5)
            r = s[start - 1:start - 1 + n]
        self.var_result(dst, V('String', r, True), 16)

    def h_rtcIsNumeric(self):
        v = self.var(self.a(0))
        if v.t in ('Integer', 'Long', 'Single', 'Double', 'Currency', 'Byte', 'Boolean', 'Date'):
            r = True
        elif v.t == 'String':
            r = str_to_num(v.v, err=False) is not None
        else:
            r = False
        self.ret(0xffff if r else 0, 4)

    def h_rtcSplit(self):
        dst, s, pdelim, count, cmp_ = self.a(0), self.bstr(self.a(1)), self.a(2), self.ia(3), self.ia(4)
        dv = self.var(pdelim)
        d = ' ' if dv.t == 'Missing' else to_str(dv)
        if count != -1:
            raise RunStopError('Split with a count not modelled')
        if s == '':
            parts = []
        elif d == '':
            parts = [s]
        elif cmp_ == 1:
            from vbinterp import _split_text
            parts = _split_text(s, d)
        else:
            parts = s.split(d)
        arr = VBArray('String', [(0, len(parts) - 1)], [V('String', x) for x in parts])
        self.var_result(dst, V('Array', arr, True), 20)

    def h_rtcVarFromFormatVar(self):
        dst, p, pfmt = self.a(0), self.a(1), self.a(2)
        from vbinterp import format_value
        fv = self.var(pfmt)
        fmt = '' if fv.t == 'Missing' else to_str(fv)
        self.var_result(dst, V('String', format_value(self.var(p), fmt), True), 20)

    # ------------------------------------------------------------------ variants
    def h___vbaVarMove(self):
        dst, src = self.m.reg('ecx'), self.m.reg('edx')
        v = self.var(src)
        self.set_var(dst, v)
        self.set_var(src, EMPTY)
        self.ret(dst)

    def h___vbaVarCopy(self):
        dst, src = self.m.reg('ecx'), self.m.reg('edx')
        self.set_var(dst, self.var(src))
        self.ret(dst)

    def h___vbaVarDup(self):
        dst, src = self.m.reg('ecx'), self.m.reg('edx')
        self.set_var(dst, self.var(src))
        self.ret(dst)

    def h___vbaVarVargNofree(self):
        p = self.m.reg('edx')
        vt = self.mem.u16(p)
        if vt == (0x4000 | 12):
            p = self.mem.u32(p + 8)
        self.ret(p)

    def _vbin(self, op):
        dst, rhs, lhs = self.a(0), self.a(1), self.a(2)
        r = binop(op, self.var(lhs), self.var(rhs), self.m.prog.option_compare_text)
        self.var_result(dst, r, 12)

    def h___vbaVarAdd(self):
        self._vbin('+')

    def h___vbaVarSub(self):
        self._vbin('-')

    def h___vbaVarMul(self):
        self._vbin('*')

    def h___vbaVarDiv(self):
        self._vbin('/')

    def h___vbaVarAnd(self):
        self._vbin('And')

    def h___vbaVarOr(self):
        self._vbin('Or')

    def h___vbaVarCat(self):
        self._vbin('&')

    def h___vbaVarNot(self):
        dst, p = self.a(0), self.a(1)
        self.var_result(dst, unop('Not', self.var(p)), 8)

    def h___vbaVarInt(self):
        dst, p = self.a(0), self.a(1)
        v = self.var(p)
        if v.t == 'Null':
            return self.var_result(dst, NULL, 8)
        if v.t in INTEGRAL:
            r = V('Integer' if v.t == 'Boolean' else v.t, v.v, True)
        elif v.t == 'String':
            r = V('Double', float(math.floor(str_to_num(v.v))), True)
        elif v.t == 'Empty':
            r = V('Integer', 0, True)
        else:
            r = V(v.t, float(math.floor(v.v)), True)
        self.var_result(dst, r, 8)

    def _vtst(self, op, text):
        rhs, lhs = self.var(self.a(0)), self.var(self.a(1))
        r = binop(op, lhs, rhs, text)
        b = r.t == 'Boolean' and r.v != 0
        self.ret(0xffff if b else 0, 8)

    def h___vbaVarTextTstEq(self):
        self._vtst('=', True)

    def h___vbaVarTextTstNe(self):
        self._vtst('<>', True)

    def h___vbaVarTextTstLt(self):
        self._vtst('<', True)

    def h___vbaVarTextTstLe(self):
        self._vtst('<=', True)

    def h___vbaVarTextTstGt(self):
        self._vtst('>', True)

    def h___vbaVarTextTstGe(self):
        self._vtst('>=', True)

    def h___vbaVarTstEq(self):
        self._vtst('=', False)

    def h___vbaVarTstNe(self):
        self._vtst('<>', False)

    def h___vbaVarTstLt(self):
        self._vtst('<', False)

    def h___vbaVarTstGt(self):
        self._vtst('>', False)

    def h___vbaVarTextCmpNe(self):
        dst, rhs, lhs = self.a(0), self.a(1), self.a(2)
        self.var_result(dst, binop('<>', self.var(lhs), self.var(rhs), True), 12)

    def h___vbaVarTextCmpEq(self):
        dst, rhs, lhs = self.a(0), self.a(1), self.a(2)
        self.var_result(dst, binop('=', self.var(lhs), self.var(rhs), True), 12)

    def _conv(self, t):
        return convert(self.var(self.a(0)), t)

    def h___vbaI4Var(self):
        self.ret(self._conv('Long').v & 0xffffffff, 4)

    def h___vbaI2Var(self):
        self.ret(self._conv('Integer').v & 0xffff, 4)

    def h___vbaUI1Var(self):
        self.ret(self._conv('Byte').v, 4)

    def h___vbaBoolVar(self):
        self.ret(0xffff if self._conv('Boolean').v else 0, 4)

    def h___vbaBoolVarNull(self):
        v = self.var(self.a(0))
        if v.t == 'Null':
            return self.ret(0, 4)
        self.ret(0xffff if convert(v, 'Boolean').v else 0, 4)

    def h___vbaDateVar(self):
        self.m.ret_fpu(self._conv('Date').v, 4)

    def h___vbaR8Var(self):
        self.m.ret_fpu(self._conv('Double').v, 4)

    def h___vbaR4Var(self):
        self.m.ret_fpu(self._conv('Single').v, 4, single=True)

    def h___vbaStrVarCopy(self):
        self.ret(self.new_bstr(to_str(self.var(self.a(0)))), 4)

    # ------------------------------------------------------------------ numbers
    def _int_from_fpu(self, t, rounding):
        x = self.m.fpu_arg()
        if math.isnan(x) or math.isinf(x):
            raise VBError(6)
        n = rounding(x)
        return check_range(t, int(n))

    def h___vbaFpI2(self):
        self.ret(self._int_from_fpu('Integer', round_half_even) & 0xffff)

    def h___vbaFpI4(self):
        self.ret(self._int_from_fpu('Long', round_half_even) & 0xffffffff)

    def h___vbaFpUI1(self):
        self.ret(self._int_from_fpu('Byte', round_half_even))

    def h___vbaR8IntI2(self):
        self.ret(self._int_from_fpu('Integer', math.floor) & 0xffff)

    def h___vbaR8IntI4(self):
        self.ret(self._int_from_fpu('Long', math.floor) & 0xffffffff)

    def h___vbaR8FixI2(self):
        self.ret(self._int_from_fpu('Integer', math.trunc) & 0xffff)

    def h___vbaR8FixI4(self):
        self.ret(self._int_from_fpu('Long', math.trunc) & 0xffffffff)

    def h___vbaFPInt(self):
        x = self.m.fpu_arg()
        # (Inf / NaN - possible without floating-point checks - stay as they are, like the FPU's frndint)
        self.m.ret_fpu(float(math.floor(x)) if math.isfinite(x) else x)

    def h___vbaFPFix(self):
        x = self.m.fpu_arg()
        self.m.ret_fpu(float(math.trunc(x)) if math.isfinite(x) else x)

    def _ci(self, f):
        try:
            self.m.ret_fpu(f(self.m.fpu_arg()))
        except (ValueError, OverflowError):
            raise VBError(5)

    def h__CIsin(self):
        self._ci(math.sin)

    def h__CIcos(self):
        self._ci(math.cos)

    def h__CItan(self):
        self._ci(math.tan)

    def h__CIatan(self):
        self._ci(math.atan)

    def h__CIexp(self):
        self._ci(math.exp)

    def h__CIlog(self):
        self._ci(math.log)

    def h__CIsqrt(self):
        self._ci(math.sqrt)

    def h___vbaFpR4(self):
        self.ret(self.m.reg('eax'))

    def h___vbaFpR8(self):
        self.ret(self.m.reg('eax'))

    def h___vbaFPException(self):
        from unicorn.x86_const import UC_X86_REG_FPSW
        sw = self.m.uc.reg_read(UC_X86_REG_FPSW)
        raise VBError(11 if sw & 4 else 6, 'floating point exception (status %x)' % sw)

    def h___vbaErrorOverflow(self):
        raise VBError(6, 'overflow (compiled check)')

    def h___vbaGenerateBoundsError(self):
        raise VBError(9, 'subscript out of range (compiled check)')

    def h___vbaI2I4(self):
        v = self.m.reg('ecx')
        v = v - (1 << 32) if v & 0x80000000 else v
        check_range('Integer', v)
        self.ret(v & 0xffff)

    def h___vbaI2Abs(self):
        v = self.m.reg('ecx') & 0xffff
        v = v - 0x10000 if v & 0x8000 else v
        self.ret(check_range('Integer', abs(v)) & 0xffff)

    def h___vbaI4Abs(self):
        v = self.m.reg('ecx')
        v = v - (1 << 32) if v & 0x80000000 else v
        self.ret(check_range('Long', abs(v)) & 0xffffffff)

    def h___vbaI2Sgn(self):
        v = self.m.reg('ecx') & 0xffff
        v = v - 0x10000 if v & 0x8000 else v
        self.ret(((v > 0) - (v < 0)) & 0xffff)

    def h___vbaI4Sgn(self):
        v = self.m.reg('ecx')
        v = v - (1 << 32) if v & 0x80000000 else v
        self.ret(((v > 0) - (v < 0)) & 0xffffffff)

    def h__allmul(self):
        a = self.a(0) | (self.a(1) << 32)
        b = self.a(2) | (self.a(3) << 32)
        r = (a * b) & 0xffffffffffffffff
        self.ret(r & 0xffffffff, 16, edx=r >> 32)

    # ------------------------------------------------------------------ objects
    def h___vbaObjSet(self):
        pdst, src = self.a(0), self.a(1)
        self.mem.w32(pdst, src)
        self.ret(src, 8)

    h___vbaObjSetAddref = h___vbaObjSet

    def h___vbaCastObj(self):
        self.ret(self.a(0), 8)

    def h___vbaObjIs(self):
        b, a = self.a(0), self.a(1)
        self.ret(0xffff if a == b else 0, 8)

    def _class(self, desc):
        from lift import DataPtr
        return self.m.dc.class_name(DataPtr(desc))

    def h___vbaNew(self):
        v = self.world.new_object(self._class(self.a(0)))
        self.ret(self.mem.obj_ptr(v.v), 4)

    def h___vbaNew2(self):
        desc, pvar = self.a(0), self.a(1)
        cls = self._class(desc)
        v = self.world.new_object(cls)
        self.mem.w32(pvar, self.mem.obj_ptr(v.v))
        self.ret(0, 8)

    def h___vbaHresultCheckObj(self):
        hr = self.ia(0)
        if hr < 0:
            raise VBError(hr & 0xffff, 'HRESULT %x' % (hr & 0xffffffff))
        self.ret(hr & 0xffffffff, 16)

    def h___vbaHresultCheck(self):
        hr = self.ia(0)
        if hr < 0:
            raise VBError(hr & 0xffff, 'HRESULT %x' % (hr & 0xffffffff))
        self.ret(hr & 0xffffffff, 4)

    def h_rtcErrObj(self):
        o = ObjRef(-1, 'ErrObject', 'err')
        self.ret(self.mem.obj_ptr(o))

    def err_member(self, off):
        it = self.m.prog.tlb.by_name.get('_ErrObject')
        mm = it.member(off) if it else None
        if mm is None:
            raise RunStopError('Err member at %x' % off)
        n = mm.name.lower()
        esp = self.m.reg('esp')
        if n == 'number' and mm.invkind == 'get':
            self.mem.w32(self.mem.u32(esp + 8), self.m.err_number)
            return self.ret(0, 8)
        if n == 'description' and mm.invkind == 'get':
            self.mem.w32(self.mem.u32(esp + 8), self.new_bstr('error %d' % self.m.err_number))
            return self.ret(0, 8)
        if n == 'clear':
            self.m.err_number = 0
            return self.ret(0, 4)
        raise RunStopError('Err.%s not modelled' % mm.name)

    # ------------------------------------------------------------------ freeing
    def h___vbaFreeStr(self):
        p = self.m.reg('ecx')
        self.mem.w32(p, 0)
        self.ret(0)

    def h___vbaFreeObj(self):
        p = self.m.reg('ecx')
        self.mem.w32(p, 0)
        self.ret(0)

    def h___vbaFreeVar(self):
        p = self.m.reg('ecx')
        self.set_var(p, EMPTY)
        self.ret(0)

    def _free_list(self, fn):
        n = self.a(0)
        for k in range(n):
            fn(self.a(1 + k))
        self.ret(0, 0)

    def h___vbaFreeStrList(self):
        self._free_list(lambda p: self.mem.w32(p, 0))

    def h___vbaFreeObjList(self):
        self._free_list(lambda p: self.mem.w32(p, 0))

    def h___vbaFreeVarList(self):
        self._free_list(lambda p: self.set_var(p, EMPTY))

    def h___vbaRecDestruct(self):
        self.ret(0, 8)

    def h___vbaExitProc(self):
        self.ret(0)

    def h___vbaSetSystemError(self):
        self.ret(self.m.reg('eax'))

    def h___vbaChkstk(self):
        n = self.m.reg('eax')
        esp = self.m.reg('esp')
        ra = self.mem.u32(esp)
        # the stack space comes zero-filled (MSVBVM60 does `rep stosd` with 0): VB's locals start at 0
        self.mem.write(esp + 4 - n, b'\0' * n)
        self.m.set_reg('esp', esp + 4 - n)
        self.m.set_reg('eip', ra)

    # ------------------------------------------------------------------ errors
    def h___vbaOnError(self):
        n = self.ia(0)
        self.m.on_error = None if n == 0 else n
        self.m.in_handler = False
        self.m.frame_ebp = self.m.reg('ebp')
        if n == -1 and not self.m.statement_starts:
            raise RunStopError('On Error Resume Next without statement numbers in the code (not modelled)')
        self.ret(0, 4)

    def h___vbaEnd(self):
        self.world.simple('end')

    # ------------------------------------------------------------------ arrays
    def _psa(self, p):
        return self.mem.u32(p)

    def h___vbaUbound(self):
        dim, psa = self.ia(0), self.a(1)
        if psa == 0:
            raise VBError(9)
        nd = self.mem.u16(psa)
        if dim < 1 or dim > nd:
            raise VBError(9)
        k = nd - dim
        n = self.mem.u32(psa + 16 + 8 * k)
        lb = self.mem.i32(psa + 20 + 8 * k)
        self.ret((lb + n - 1) & 0xffffffff, 8)

    def h___vbaLbound(self):
        dim, psa = self.ia(0), self.a(1)
        if psa == 0:
            raise VBError(9)
        nd = self.mem.u16(psa)
        k = nd - dim
        self.ret(self.mem.i32(psa + 20 + 8 * k) & 0xffffffff, 8)

    def h___vbaRedim(self):
        self._redim(False)

    def h___vbaRedimPreserve(self):
        self._redim(True)

    def _redim(self, preserve):
        feats, cb, ppsa, vt, nd = self.a(0), self.a(1), self.a(2), self.a(3), self.a(4)
        bounds = []
        for k in range(nd):
            ub = self.ia(5 + 2 * k)
            lb = self.ia(6 + 2 * k)
            bounds.append((lb, ub))
        bounds.reverse()
        old = self.mem.u32(ppsa)
        # the array features say what the elements are (vt is 0 for variants, strings and objects)
        if feats & 0x800:
            etype = 'Variant'
        elif feats & 0x100:
            etype = 'String'
        elif feats & 0x600:
            etype = 'Object'
        elif vt:
            etype = VT_NAME.get(vt)
        else:
            etype = {1: 'Byte', 2: 'Integer', 4: 'Long', 8: 'Double'}.get(cb)
        if etype is None or etype in ('Empty', 'Null', 'Error'):
            raise RunStopError('ReDim of element type %d' % vt)
        arr = VBArray(etype, bounds)
        if preserve and old:
            raise RunStopError('ReDim Preserve not modelled')
        self.mem.w32(ppsa, self.mem.write_array(arr))
        self.ret(0, 0)

    def h___vbaErase(self):
        flags, ppsa = self.a(0), self.a(1)
        psa = self.mem.u32(ppsa)
        if psa:
            if self.mem.u16(psa + 2) & 0x80:
                size = self.mem.u32(psa + 4)
                n = 1
                for k in range(self.mem.u16(psa)):
                    n *= self.mem.u32(psa + 16 + 8 * k)
                self.mem.write(self.mem.u32(psa + 12), b'\0' * (size * n))
            else:
                self.mem.w32(ppsa, 0)
        self.ret(0, 8)

    def h___vbaAryDestruct(self):
        flags, ppsa = self.a(0), self.a(1)
        psa = self.mem.u32(ppsa)
        if psa and not (self.mem.u16(psa + 2) & 0x80):
            self.mem.w32(ppsa, 0)
        self.ret(0, 8)

    def h___vbaAryCopy(self):
        pdst, psrc = self.a(0), self.a(1)
        src = self.mem.u32(psrc)
        if src == 0:
            self.mem.w32(pdst, 0)
        else:
            nd = self.mem.u16(src)
            size = self.mem.u32(src + 4)
            n = 1
            for k in range(nd):
                n *= self.mem.u32(src + 16 + 8 * k)
            new = self.mem.alloc(16 + 8 * nd)
            self.mem.write(new, self.mem.read(src, 16 + 8 * nd))
            data = self.mem.alloc(max(1, size * n))
            self.mem.write(data, self.mem.read(self.mem.u32(src + 12), size * n))
            self.mem.w32(new + 12, data)
            self.mem.w16(new + 2, self.mem.u16(new + 2) & ~0x80)
            self.mem.w32(pdst, new)
        self.ret(pdst, 8)

    def h___vbaAryVar(self, ):
        vt, p = self.a(0), self.a(1)
        v = self.var(p)
        if v.t != 'Array':
            raise VBError(13, 'not an array')
        vvt = self.mem.u16(p)
        if vvt & 0x4000:
            self.ret(self.mem.u32(self.mem.u32(p + 8)), 8)
        else:
            self.ret(self.mem.u32(p + 8), 8)

    def h___vbaCopyBytes(self):
        n, dst, src = self.a(0), self.a(1), self.a(2)
        self.mem.write(dst, self.mem.read(src, n))
        self.ret(dst, 12)

    def h_rtcArray(self):
        dst, ppsa = self.a(0), self.a(1)
        psa = self.mem.u32(ppsa)
        arr = self.mem.read_array(psa, 'Variant') if psa else VBArray('Variant', [(0, -1)])
        self.var_result(dst, V('Array', arr, True), 8)

    def h___vbaAryConstruct2(self):
        # (descriptor, template, vt): a fixed-size local array: copy the template descriptor, give it
        # zeroed data (strings / objects / variants all start as zero bytes)
        pdesc, tmpl = self.a(0), self.a(1)
        nd = self.mem.u16(tmpl)
        size = 16 + 8 * nd
        self.mem.write(pdesc, self.mem.read(tmpl, size))
        cb = self.mem.u32(tmpl + 4)
        n = 1
        for k in range(nd):
            n *= self.mem.u32(tmpl + 16 + 8 * k)
        self.mem.w32(pdesc + 12, self.mem.alloc(max(1, cb * n)))
        self.ret(pdesc, 12)

    # ------------------------------------------------------------------ the outside world
    def h_rtcDoEvents(self):
        self.world.doevents()
        self.ret(0, 0)

    def h_rtcBeep(self):
        self.world.simple('beep')
        self.ret(0, 0)

    def h_rtcRandomize(self):
        v = self.var(self.a(0))
        self.world.randomize(None if v.t == 'Missing' else v)
        self.ret(0, 4)

    def h_rtcRandomNext(self):
        v = self.var(self.a(0))
        r = self.world.rnd(None if v.t == 'Missing' else v)
        self.m.ret_fpu(r.v, 4, single=True)

    def h_rtcGetTimer(self):
        r = self.world.timer()
        self.m.ret_fpu(r.v, 0, single=True)

    def h_rtcGetPresentDate(self):
        dst = self.a(0)
        self.var_result(dst, self.world.now('now'), 4)

    def h_rtcGetDateVar(self):
        dst = self.a(0)
        self.var_result(dst, self.world.now('date'), 4)

    def h_rtcGetTimeVar(self):
        dst = self.a(0)
        self.var_result(dst, self.world.now('time'), 4)

    def h_rtcDatePart(self):
        dst, iv, pdate = self.a(0), self.bstr(self.a(1)).lower(), self.a(2)
        d = convert(self.var(pdate), 'Date').v
        y, mo, da, h, mi, s = date_parts(d)
        r = {'yyyy': y, 'm': mo, 'd': da, 'h': h, 'n': mi, 's': s}.get(iv)
        if r is None:
            raise RunStopError('DatePart %r' % iv)
        self.var_result(dst, V('Integer', r, True), 20)

    def h_rtcMsgBox(self):
        prompt, buttons, title = self.var(self.a(0)), self.ia(1), self.var(self.a(2))
        r = self.world.msgbox(prompt, V('Long', buttons), title)
        self.ret(r.v, 20)

    def h_rtcGetSetting(self):
        app, sec, key = self.bstr(self.a(0)), self.bstr(self.a(1)), self.bstr(self.a(2))
        dv = self.mem.read_variant(self.m.arg_at(12))
        default = '' if dv.t in ('Missing', 'Empty') else to_str(dv)
        self.ret(self.new_bstr(self.world.getsetting(app, sec, key, default)), 28)

    def h_rtcSaveSetting(self):
        app, sec, key, val = (self.bstr(self.a(k)) for k in range(4))
        self.world.savesetting(app, sec, key, val)
        self.ret(0, 16)

    # files
    def h___vbaFileOpen(self):
        mode, reclen, fnum, fname = self.a(0), self.ia(1), self.ia(2), self.bstr(self.a(3))
        m = FILE_MODES.get(mode & 0xff)
        if m is None:
            raise RunStopError('file mode %x' % mode)
        self.world.file_open(fname, m, fnum)
        self.ret(0, 16)

    def h___vbaFileClose(self):
        self.world.file_close(self.ia(0))
        self.ret(0, 4)

    def _io_codes(self, desc):
        n = self.mem.u16(desc)
        return [self.mem.u8(desc + 2 + k) for k in range(n)]

    def _io_items(self, codes, start_arg):
        """Print/Write item values (by value, cdecl after desc and file number)."""
        items = []
        off = 4 * start_arg
        for c in codes:
            vt = c & 0x3f
            if vt == 0:
                items.append(None)
                continue
            pos = self.m.arg_at(off)
            if vt in (5, 6, 7):
                items.append(self.mem.read_value(pos, VT_NAME[vt]))
                off += 8
            elif vt == 12:
                items.append(self.mem.read_variant(self.mem.u32(pos)))
                off += 4
            elif vt == 4:
                items.append(V('Single', self.mem.f32(pos)))
                off += 4
            else:
                name = VT_NAME.get(vt)
                if name is None:
                    raise RunStopError('Print item type %d' % vt)
                items.append(self.mem.read_value(pos, name))
                off += 4
        return items

    def h___vbaPrintFile(self):
        desc, fnum = self.a(0), self.ia(1)
        codes = self._io_codes(desc)
        vals = self._io_items(codes, 2)
        items = []
        for c, v in zip(codes, vals):
            # 0x80: the statement ends here (new line: a file written with Print, such as Troopanum's stats.txt,
            # has one line per Print statement);
            # 0x40: comma (next print zone); neither: semicolon
            sep = None if c & 0x80 else (',' if c & 0x40 else ';')
            items.append((v, sep))
        self.world.file_write_text(fnum, print_text(items), 'print')
        self.ret(0, 0)

    def h___vbaWriteFile(self):
        desc, fnum = self.a(0), self.ia(1)
        codes = self._io_codes(desc)
        vals = [v for v in self._io_items(codes, 2) if v is not None]
        self.world.file_write_text(fnum, write_text(vals), 'write')
        self.ret(0, 0)

    def h___vbaInputFile(self):
        desc, fnum = self.a(0), self.ia(1)
        codes = self._io_codes(desc)
        k = 2
        for c in codes:
            vt = c & 0x3f
            if vt == 0:
                continue
            ptr = self.a(k)
            k += 1
            t = VT_NAME.get(vt)
            text = self.world.file_input_field(fnum)
            v = input_value(text, t if t else 'Variant')
            if t == 'Variant':
                self.mem.write_variant(ptr, v)
            else:
                self.mem.write_value(ptr, t, v)
        self.ret(0, 0)

    def h___vbaLineInputStr(self):
        pstr, fnum = self.a(0), self.ia(1)
        s = self.world.file_line_input(fnum)
        self.mem.w32(pstr, self.new_bstr(s))
        self.ret(0, 8)

    def h_rtcEndOfFile(self):
        self.ret(0xffff if self.world.file_eof(self.ia(0)) else 0, 4)

    def h_rtcFileLength(self):
        self.ret(self.world.file_lof(self.ia(0)), 4)

    def h_rtcKillFiles(self):
        self.world.kill(to_str(self.var(self.a(0))))
        self.ret(0, 4)

    def h_rtcDir(self):
        v = self.var(self.a(0))
        attrs = self.ia(1)
        p = None if v.t == 'Missing' else to_str(v)
        self.ret(self.new_bstr(self.world.dir(p, attrs)), 8)

    # late binding (controls)
    def _late(self, obj_ptr, dispid, argc, args_start, kind):
        o = self.mem.read_obj(obj_ptr)
        it = self.m.prog.iface_of(o.iface)
        name = None
        if it is not None:
            for mm in it.members:
                if mm.memid == dispid:
                    name = mm.name
                    break
        if name is None:
            raise RunStopError('late-bound dispid %d on %s' % (dispid, o.iface))
        args = []
        for k in range(argc):
            args.append(self.mem.read_variant(self.m.arg_at(4 * args_start + 16 * k)))
        args.reverse()
        return o, name, args

    def h___vbaLateIdCallLd(self):
        dst, obj, dispid, argc = self.a(0), self.a(1), self.ia(2), self.ia(3)
        o, name, args = self._late(obj, dispid, argc, 4, 'method')
        ret, outs = self.world.com_call(o, name, 'method', args, {}, 'Variant')
        self.set_var(dst, ret)
        self.ret(dst, 0)

    def h___vbaLateIdCall(self):
        obj, dispid, argc = self.a(0), self.ia(1), self.ia(2)
        o, name, args = self._late(obj, dispid, argc, 3, 'method')
        self.world.com_call(o, name, 'method', args, {}, None)
        self.ret(0, 0)

    def h___vbaLateIdSt(self):
        obj, dispid = self.a(0), self.ia(1)
        o = self.mem.read_obj(obj)
        it = self.m.prog.iface_of(o.iface)
        name = None
        for mm in (it.members if it else []):
            if mm.memid == dispid:
                name = mm.name
        if name is None:
            raise RunStopError('late-bound property %d' % dispid)
        v = self.mem.read_variant(self.m.arg_at(8))
        self.world.com_call(o, name, 'let', [v], {}, None)
        self.ret(0, 0)

    # event sinks / startup (never part of a procedure body)
    def h_EVENT_SINK_AddRef(self):
        self.ret(1, 4)

    def h_EVENT_SINK_Release(self):
        self.ret(1, 4)

    def h_EVENT_SINK2_AddRef(self):
        self.ret(1, 4)

    def h_EVENT_SINK2_Release(self):
        self.ret(1, 4)
