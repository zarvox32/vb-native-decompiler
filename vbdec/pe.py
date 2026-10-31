"""PE image access for VB5/6 executables (file on disk, or an in-memory image dump)."""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'lib'))
import pefile  # noqa: E402

MSVBVM60_PATH = r'C:\Windows\SysWOW64\msvbvm60.dll'
_ORDINALS = None


def msvbvm60_ordinals():
    """ordinal -> export name of the installed MSVBVM60.DLL (for ordinal imports)."""
    global _ORDINALS
    if _ORDINALS is None:
        _ORDINALS = {}
        try:
            pe = pefile.PE(MSVBVM60_PATH, fast_load=True)
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']])
            for e in pe.DIRECTORY_ENTRY_EXPORT.symbols:
                if e.name:
                    _ORDINALS[e.ordinal] = e.name.decode()
        except Exception:
            pass
    return _ORDINALS


class Image:
    """Memory-mapped view of a PE image with typed readers."""

    def __init__(self, path=None, mapped=None, base=None):
        if mapped is not None:
            # an in-memory dump (headers + sections at their RVAs)
            self.pe = pefile.PE(data=bytes(mapped[:0x1000]) + b'\0' * 0x1000, fast_load=True)
            self.mem = bytes(mapped)
            self.base = base if base is not None else self.pe.OPTIONAL_HEADER.ImageBase
        else:
            self.pe = pefile.PE(path)
            self.base = self.pe.OPTIONAL_HEADER.ImageBase
            self.mem = bytes(self.pe.get_memory_mapped_image())
        self.path = path
        self.sections = []
        for s in self.pe.sections:
            va = self.base + s.VirtualAddress
            self.sections.append((va, va + max(s.Misc_VirtualSize, s.SizeOfRawData), s.Name.rstrip(b'\0').decode('latin-1'), s.Characteristics))
        if mapped is not None:
            for i, (lo, hi, name, ch) in enumerate(self.sections):
                if b'VB5!' in self.mem[lo - self.base:hi - self.base]:
                    self.sections[i] = (lo, hi, name, ch | 0x20000020)
        self.imports = {}       # IAT slot VA -> function name
        self._load_imports()

    # ---- readers -------------------------------------------------------------
    def valid(self, va, n=1):
        o = va - self.base
        return 0 <= o and o + n <= len(self.mem)

    def read(self, va, n):
        o = va - self.base
        return self.mem[o:o + n]

    def u8(self, va):
        return self.mem[va - self.base]

    def u16(self, va):
        return struct.unpack_from('<H', self.mem, va - self.base)[0]

    def i16(self, va):
        return struct.unpack_from('<h', self.mem, va - self.base)[0]

    def u32(self, va):
        return struct.unpack_from('<I', self.mem, va - self.base)[0]

    def i32(self, va):
        return struct.unpack_from('<i', self.mem, va - self.base)[0]

    def f32(self, va):
        return struct.unpack_from('<f', self.mem, va - self.base)[0]

    def f64(self, va):
        return struct.unpack_from('<d', self.mem, va - self.base)[0]

    def cstr(self, va, maxlen=1024):
        if not self.valid(va):
            return None
        o = va - self.base
        e = self.mem.find(b'\0', o, o + maxlen)
        if e < 0:
            return None
        return self.mem[o:e].decode('latin-1')

    def bstr(self, va):
        """VB string literal: UTF-16 chars at va, byte length in the dword before."""
        if not self.valid(va - 4, 4):
            return None
        n = self.u32(va - 4)
        if n > 0x10000 or n % 2 or not self.valid(va, n):
            return None
        try:
            s = self.mem[va - self.base:va - self.base + n].decode('utf-16le')
        except UnicodeDecodeError:
            return None
        # literal must be NUL terminated
        if self.valid(va + n, 2) and self.u16(va + n) != 0:
            return None
        return s

    def guid(self, va):
        import uuid
        return '{%s}' % str(uuid.UUID(bytes_le=bytes(self.read(va, 16)))).upper()

    def section_of(self, va):
        for lo, hi, name, ch in self.sections:
            if lo <= va < hi:
                return name
        return None

    def is_code(self, va):
        for lo, hi, name, ch in self.sections:
            if lo <= va < hi:
                return bool(ch & 0x20000000)
        return False

    def find(self, pat, start=0):
        i = self.mem.find(pat, start)
        return -1 if i < 0 else i + self.base

    # ---- imports ------------------------------------------------------------
    def _load_imports(self):
        ords = msvbvm60_ordinals()
        found_vb = False
        for e in getattr(self.pe, 'DIRECTORY_ENTRY_IMPORT', []) or []:
            dll = e.dll.decode('latin-1')
            if 'MSVBVM' in dll.upper():
                found_vb = True
            for i in e.imports:
                name = i.name.decode() if i.name else ords.get(i.ordinal, 'ord%d' % i.ordinal)
                self.imports[i.address] = name
        if not found_vb:
            self._scan_vb_import_descriptor()

    def _scan_vb_import_descriptor(self):
        """Packed exes (MoleBox) replace the import directory; the original MSVBVM60
        IMAGE_IMPORT_DESCRIPTOR is usually still present - find and parse it."""
        ords = msvbvm60_ordinals()
        mem = self.mem
        name_rva = None
        for nm in (b'MSVBVM60.DLL\0', b'msvbvm60.dll\0', b'MSVBVM50.DLL\0'):
            i = mem.find(nm)
            while i >= 0:
                name_rva = i
                # descriptor: OFT, TS, FC, Name, FT  -> search for Name == name_rva
                pat = struct.pack('<I', name_rva)
                j = mem.find(pat)
                while j >= 0:
                    d = j - 12
                    if d >= 0 and d % 4 == 0:
                        oft, ts, fc, nm_rva, ft = struct.unpack_from('<5I', mem, d)
                        if nm_rva == name_rva and 0 < ft < len(mem) and 0 < oft < len(mem):
                            self._parse_thunks(oft, ft, ords)
                            return
                    j = mem.find(pat, j + 1)
                i = mem.find(nm, i + 1)

    def _parse_thunks(self, oft, ft, ords):
        k = 0
        while True:
            t = struct.unpack_from('<I', self.mem, oft + 4 * k)[0]
            if t == 0:
                break
            if t & 0x80000000:
                name = ords.get(t & 0xffff, 'ord%d' % (t & 0xffff))
            else:
                name = self.cstr(self.base + t + 2) or '?'
            self.imports[self.base + ft + 4 * k] = name
            k += 1
