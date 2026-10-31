"""The instance-data table of a VB form/class: which variables hold strings, objects and fixed-size
arrays (with their bounds).

The object descriptor's "public bytes" pointer leads to the table the VB runtime uses to initialise
and free a new instance. Entries are (instance offset: u16, kind: u16); seen kinds:
    1  String (BSTR)
    3  object reference
    5  fixed-size array: a SAFEARRAY descriptor embedded in the instance at that offset (so the
       array data pointer, the variable the code indexes, is at offset + 0x0C); a template of the
       descriptor (dimensions, element size, element count and lower bound per dimension) follows the
       entry in the table, sometimes after a record-type reference
Kind 0x2005 is a fixed-size array too, same layout (seen in Troopanum 2.0); 0x105 is something else (not an
array).
Kinds 2 / 4 also occur but are not decoded (they are not needed for types or bounds).
"""
import struct


def scan_descriptor(im, a, limit=0x48):
    """First plausible SAFEARRAY template within `limit` bytes after a: (address, dims, cb, bounds)."""
    for d in range(0, limit, 2):
        p = a + d
        if not im.valid(p, 24):
            return None
        cdims, feat, cb, locks, pv = struct.unpack('<HHIII', im.read(p, 16))
        if not (1 <= cdims <= 8) or not (feat & 0x10) or locks or pv or not (0 < cb <= 0x1000):
            continue
        if not im.valid(p + 16, 8 * cdims):
            return None
        bounds = []
        ok = True
        for k in range(cdims):
            n, lb = struct.unpack('<Ii', im.read(p + 16 + 8 * k, 8))
            if not (0 < n < 0x10000 and -0x10000 < lb < 0x10000):
                ok = False
                break
            bounds.append((lb, lb + n - 1))
        if not ok:
            continue
        bounds.reverse()        # the descriptor stores the last dimension first
        return p, cdims, cb, feat, bounds
    return None


def fixed_arrays(im, obj, inst_size=0x10000):
    """{data pointer offset (descriptor offset + 0xC): (element size, [(lb, ub) per dimension])} for the
    fixed-size arrays of object `obj` (a vbmeta.VBObject)."""
    a = obj.public_bytes
    out = {}
    if not a or not im.valid(a, 8):
        return out
    end = a + 0x2000
    p = a + 4
    taken = set()
    while p < end and im.valid(p, 4):
        off, kind = struct.unpack('<HH', im.read(p, 4))
        if kind & 0xdfff == 5 and 0x30 <= off < inst_size:     # (0x2005 also seen: same layout)
            r = scan_descriptor(im, p + 4)
            if r is not None and r[0] not in taken:
                taken.add(r[0])
                dp, cdims, cb, feat, bounds = r
                out[off + 0xC] = (cb, bounds)
                p = dp + 16 + 8 * cdims
                continue
        p += 2
    return out


def field_kinds(im, obj, fixed=None, inst_size=0x10000):
    """{instance offset: 'String' | 'Object'} from kind 1 / kind 3 entries outside array templates."""
    a = obj.public_bytes
    out = {}
    if not a or not im.valid(a, 8):
        return out
    fixed = fixed if fixed is not None else fixed_arrays(im, obj, inst_size)
    p = a + 4
    end = a + 0x2000
    while p < end and im.valid(p, 4):
        off, kind = struct.unpack('<HH', im.read(p, 4))
        if kind & 0xdfff in (1, 3) and 0x30 <= off < inst_size and off % 4 == 0:
            out.setdefault(off, 'String' if kind & 0xdfff == 1 else 'Object')
        p += 2
    return out
