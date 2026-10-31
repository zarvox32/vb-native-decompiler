"""Emulated memory for the machine-code side: VB data layouts <-> shared values (vbvalues.V).

  String   BSTR: pointer to UTF-16 text, length in bytes stored 4 bytes before it (0 = empty string)
  Variant  16 bytes: vt (2), reserved (6), payload (8); VT_BYREF (0x4000) payloads point to the value
  Array    SAFEARRAY*: cDims(2) fFeatures(2) cbElements(4) cLocks(4) pvData(4) then per dimension
           cElements(4) lLbound(4), last dimension first; elements first-index-fastest
  Object   pointer to a fake COM object: [vtable][refcount][object id]
  Record   the UDT bytes, fields at their layout offsets
"""
import struct

from vbvalues import (V, VBError, ObjRef, VBArray, VBRecord, EMPTY, NULL, VT, VT_NAME, f32, default_value)
# (inconsistencies of the emulated memory are the checker's problem, not VB run-time errors: they stop the
# run as a machine-problem instead of passing for a VB error the source may raise for another reason)
from mxruntime import RunStopError

SLOT = {'Integer': 2, 'Boolean': 2, 'Long': 4, 'Single': 4, 'Double': 8, 'Date': 8, 'Currency': 8,
        'String': 4, 'Byte': 1, 'Variant': 16, 'Object': 4}

HEAP_BASE = 0x10000000
HEAP_SIZE = 0x04000000


class Mem:
    def __init__(self, uc, prog):
        self.uc = uc
        self.prog = prog                  # vbinterp.Program (types, layouts, interfaces)
        self.heap = HEAP_BASE
        self.obj_by_ptr = {}              # object block -> ObjRef
        self.ptr_by_oid = {}              # oid -> object block
        self.vtables = {}                 # interface name -> vtable address
        self.make_vtable = None           # (iface name) -> address, set by the machine

    def reset(self):
        self.heap = HEAP_BASE
        self.obj_by_ptr = {}
        self.ptr_by_oid = {}

    # ------------------------------------------------------------------ raw access
    def read(self, a, n):
        return bytes(self.uc.mem_read(a, n))

    def write(self, a, b):
        self.uc.mem_write(a, bytes(b))

    def u8(self, a):
        return self.read(a, 1)[0]

    def u16(self, a):
        return struct.unpack('<H', self.read(a, 2))[0]

    def i16(self, a):
        return struct.unpack('<h', self.read(a, 2))[0]

    def u32(self, a):
        return struct.unpack('<I', self.read(a, 4))[0]

    def i32(self, a):
        return struct.unpack('<i', self.read(a, 4))[0]

    def f32(self, a):
        return struct.unpack('<f', self.read(a, 4))[0]

    def f64(self, a):
        return struct.unpack('<d', self.read(a, 8))[0]

    def w8(self, a, v):
        self.write(a, struct.pack('<B', v & 0xff))

    def w16(self, a, v):
        self.write(a, struct.pack('<H', v & 0xffff))

    def w32(self, a, v):
        self.write(a, struct.pack('<I', v & 0xffffffff))

    def wf32(self, a, v):
        self.write(a, struct.pack('<f', v))

    def wf64(self, a, v):
        self.write(a, struct.pack('<d', v))

    def alloc(self, n, fill=0):
        a = (self.heap + 15) & ~15
        if a + n + 16 >= HEAP_BASE + HEAP_SIZE:
            raise MemoryError('emulated heap exhausted')
        self.heap = a + n + 16
        if fill is not None:
            self.write(a, bytes([fill]) * n)
        return a

    # ------------------------------------------------------------------ strings
    def bstr(self, s):
        if s is None:
            return 0
        data = s.encode('utf-16-le', 'surrogatepass')
        a = self.alloc(len(data) + 6)
        self.w32(a, len(data))
        self.write(a + 4, data + b'\0\0')
        return a + 4

    def read_bstr(self, p):
        if p == 0:
            return ''
        n = self.u32(p - 4)
        if n > 0x100000:
            raise RunStopError('bad BSTR at %x' % p)
        return self.read(p, n).decode('utf-16-le', 'replace')

    def ansi(self, s):
        data = s.encode('latin-1', 'replace')
        a = self.alloc(len(data) + 1)
        self.write(a, data + b'\0')
        return a

    def read_ansi(self, p):
        out = b''
        while True:
            b = self.read(p, 64)
            k = b.find(b'\0')
            if k >= 0:
                out += b[:k]
                return out.decode('latin-1')
            out += b
            p += 64
            if len(out) > 0x10000:
                return out.decode('latin-1')

    # ------------------------------------------------------------------ objects
    def obj_ptr(self, o):
        if o is None:
            return 0
        p = self.ptr_by_oid.get(o.oid)
        if p is None:
            p = self.alloc(16)
            self.w32(p, self.make_vtable(o))
            self.w32(p + 4, 1)
            self.w32(p + 8, o.oid)
            self.ptr_by_oid[o.oid] = p
            self.obj_by_ptr[p] = o
        return p

    def read_obj(self, p):
        if p == 0:
            return None
        o = self.obj_by_ptr.get(p)
        if o is None:
            raise RunStopError('pointer %x is not an object the checker created' % p)
        return o

    # ------------------------------------------------------------------ typed slots
    def slot_size(self, t, layout=None):
        if layout is not None:
            return layout.size
        return SLOT.get(t, 4)

    def write_value(self, a, t, v, layout=None, is_array=False):
        """Store V v (already of type t) into a slot of type t at address a."""
        if is_array:
            self.w32(a, self.write_array(v.v) if v.v is not None else 0)
            return
        if layout is not None:
            self.write_record(a, v.v)
            return
        if t in ('Integer', 'Boolean'):
            self.w16(a, v.v)
        elif t == 'Byte':
            self.w8(a, v.v)
        elif t == 'Long':
            self.w32(a, v.v)
        elif t == 'Single':
            self.wf32(a, v.v)
        elif t in ('Double', 'Date'):
            self.wf64(a, v.v)
        elif t == 'Currency':
            self.write(a, struct.pack('<q', v.v))
        elif t == 'String':
            self.w32(a, self.bstr(v.v))
        elif t == 'Variant':
            self.write_variant(a, v)
        else:
            if v.t != 'Object':
                raise RunStopError('storing %s into an object slot' % v.t)
            self.w32(a, self.obj_ptr(v.v))

    def read_value(self, a, t, layout=None, is_array=False):
        if is_array:
            p = self.u32(a)
            return V('Array', self.read_array(p, t, layout) if p else None)
        if layout is not None:
            return V('Record', self.read_record(a, layout))
        if t in ('Integer',):
            return V('Integer', self.i16(a))
        if t == 'Boolean':
            return V('Boolean', self.i16(a))
        if t == 'Byte':
            return V('Byte', self.u8(a))
        if t == 'Long':
            return V('Long', self.i32(a))
        if t == 'Single':
            return V('Single', self.f32(a))
        if t in ('Double', 'Date'):
            return V(t, self.f64(a))
        if t == 'Currency':
            return V('Currency', struct.unpack('<q', self.read(a, 8))[0])
        if t == 'String':
            return V('String', self.read_bstr(self.u32(a)))
        if t == 'Variant':
            return self.read_variant(a)
        return V('Object', self.read_obj(self.u32(a)))

    # ------------------------------------------------------------------ variants
    def write_variant(self, a, v):
        self.write(a, b'\0' * 16)
        t = v.t
        if t in ('Empty', 'Missing'):
            if t == 'Missing':
                self.w16(a, 10)
                self.w32(a + 8, 0x80020004)
            return
        if t == 'Null':
            self.w16(a, 1)
            return
        if t == 'Array':
            arr = v.v
            vt = VT.get(arr.etype, 12) if arr is not None else 12
            self.w16(a, 0x2000 | vt)
            self.w32(a + 8, self.write_array(arr) if arr is not None else 0)
            return
        if t == 'Object':
            self.w16(a, 9)
            self.w32(a + 8, self.obj_ptr(v.v))
            return
        if t == 'Record':
            raise RunStopError('record in a Variant')
        self.w16(a, VT[t])
        self.write_value(a + 8, t, v)

    def read_variant(self, a, depth=0):
        vt = self.u16(a) & 0x7fff     # 0x8000 (VT_RESERVED): VB marks string literals it does not own
        if vt & 0x4000:
            base = vt & 0xbfff
            p = self.u32(a + 8)
            if base == 12:
                return self.read_variant(p, depth + 1)
            if base & 0x2000:
                return V('Array', self.read_array(self.u32(p), VT_NAME.get(base & 0xfff, 'Variant')), True)
            return self.read_value(p, VT_NAME.get(base, 'Long')).as_variant()
        if vt & 0x2000:
            p = self.u32(a + 8)
            return V('Array', self.read_array(p, VT_NAME.get(vt & 0xfff, 'Variant')) if p else None, True)
        if vt == 0:
            return EMPTY
        if vt == 1:
            return NULL
        if vt == 10:
            return V('Missing', None, True)
        if vt in (9, 13):
            return V('Object', self.read_obj(self.u32(a + 8)), True)
        name = VT_NAME.get(vt)
        if name is None or name == 'Variant':
            raise RunStopError('variant type %d' % vt)
        return self.read_value(a + 8, name).as_variant()

    # ------------------------------------------------------------------ arrays
    def write_array(self, arr, at=None):
        """Build a SAFEARRAY for arr; `at`: address of a descriptor embedded in an object (fixed-size
        array of a form: the variable the code indexes is the data pointer at at + 0x0C)."""
        if arr is None:
            return 0
        nd = len(arr.bounds)
        size = self.slot_size(arr.etype, arr.rec)
        psa = at if at is not None else self.alloc(16 + 8 * nd)
        self.w16(psa, nd)
        feats = 0x80 if arr.fixed else 0
        if arr.etype == 'String':
            feats |= 0x100
        elif arr.etype == 'Variant':
            feats |= 0x800
        elif arr.rec is not None:
            feats |= 0x20
        self.w16(psa + 2, feats)
        self.w32(psa + 4, size)
        self.w32(psa + 8, 0)
        data = self.alloc(max(1, size * len(arr.data)))
        self.w32(psa + 12, data)
        for k, (lb, ub) in enumerate(reversed(arr.bounds)):
            self.w32(psa + 16 + 8 * k, ub - lb + 1)
            self.w32(psa + 20 + 8 * k, lb)
        for i, e in enumerate(arr.data):
            self.write_value(data + i * size, arr.etype, e, arr.rec)
        return psa

    def read_array(self, psa, etype, layout=None):
        if psa == 0:
            return None
        nd = self.u16(psa)
        size = self.u32(psa + 4)
        data = self.u32(psa + 12)
        bounds = []
        for k in range(nd):
            n = self.u32(psa + 16 + 8 * k)
            lb = self.i32(psa + 20 + 8 * k)
            bounds.append((lb, lb + n - 1))
        bounds.reverse()
        if layout is None and etype is not None:
            layout = self.prog.layout(etype)
        want = self.slot_size(etype, layout)
        if size != want:
            raise RunStopError('array element size %d, declared type %s has %d' % (size, etype, want))
        arr = VBArray(etype, bounds, data=[], fixed=bool(self.u16(psa + 2) & 0x80), rec=layout)
        n = 1
        for lb, ub in bounds:
            n *= ub - lb + 1
        arr.data = [self.read_value(data + i * size, etype, layout) for i in range(n)]
        return arr

    # ------------------------------------------------------------------ records
    def write_record(self, a, rec):
        lay = rec.layout
        self.write(a, b'\0' * lay.size)
        for name, t, off, size, count in lay.fields:
            v = rec.vals[name.lower()]
            if count:
                arr = v.v
                for i in range(count):
                    self.write_value(a + off + i * size, t, arr.data[i], self.prog.layout(t))
            else:
                self.write_value(a + off, t, v, self.prog.layout(t))

    def read_record(self, a, lay):
        rec = VBRecord(lay, {})
        for name, t, off, size, count in lay.fields:
            sub = self.prog.layout(t)
            if count:
                arr = VBArray(t, [(0, count - 1)], [self.read_value(a + off + i * size, t, sub)
                                                    for i in range(count)], fixed=True, rec=sub)
                rec.vals[name.lower()] = V('Array', arr)
            else:
                rec.vals[name.lower()] = self.read_value(a + off, t, sub)
        return rec
