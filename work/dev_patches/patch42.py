import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# VB globals live in the data section that holds ProjectInfo.lpNativeCode (FDIV flag);
# packed executables mark every section writable, so section flags cannot be trusted.
patch('lift.py', [(
"""    def section_writable(self, va):
        for lo, hi, name, ch in self.im.sections:
            if lo <= va < hi:
                return bool(ch & 0x80000000)
        return False""",
"""    def section_writable(self, va):
        return self.ctx.is_data_va(va)""")])
patch('decompiler.py', [(
"""    def api_signature(self, d):""",
"""    def is_data_va(self, va):
        \"\"\"Is va inside VB's data section (module/global variables)?\"\"\"
        if not hasattr(self, '_data_sec'):
            self._data_sec = None
            anchor = self.proj.native_code
            for lo, hi, name, ch in self.im.sections:
                if anchor and lo <= anchor < hi and not (lo <= self.proj.code_start < hi):
                    self._data_sec = (lo, hi)
            if self._data_sec is None:
                for lo, hi, name, ch in self.im.sections:
                    if (ch & 0x80000000) and not (ch & 0x20000000) and name.lower() in ('.data', ''):
                        self._data_sec = (lo, hi)
                        break
        if self._data_sec is None:
            return False
        lo, hi = self._data_sec
        return lo <= va < hi

    def api_signature(self, d):""")])

# dynamic array with a constant index
patch('simplify.py', [(
"""        lb = [(t, c) for t, c in rest if isinstance(t, Deref) and t.off == 0x14 and same(t.base, psa)]
        rest = [(t, c) for t, c in rest if not (isinstance(t, Deref) and t.off == 0x14 and same(t.base, psa))]
        if len(rest) != 1 or len(lb) > 1:
            return None""",
"""        lb = [(t, c) for t, c in rest if isinstance(t, Deref) and t.off == 0x14 and same(t.base, psa)]
        rest = [(t, c) for t, c in rest if not (isinstance(t, Deref) and t.off == 0x14 and same(t.base, psa))]
        if not rest and len(lb) == 1 and lb[0][1] < 0:
            stride = -lb[0][1]
            k, field = divmod(const, stride)
            return psa, Const(k), stride, field, 'psa'
        if len(rest) != 1 or len(lb) > 1:
            return None""")])
print('patched')
