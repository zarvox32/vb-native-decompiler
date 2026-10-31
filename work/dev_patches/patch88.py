import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# packers (MoleBox) leave every original section marked read/write data: in a dump, the section
# holding the VB5! header is the program's code section
patch('pe.py', [(
"""            self.sections.append((va, va + max(s.Misc_VirtualSize, s.SizeOfRawData), s.Name.rstrip(b'\\0').decode('latin-1'), s.Characteristics))
        self.imports = {}""",
"""            self.sections.append((va, va + max(s.Misc_VirtualSize, s.SizeOfRawData), s.Name.rstrip(b'\\0').decode('latin-1'), s.Characteristics))
        if mapped is not None:
            for i, (lo, hi, name, ch) in enumerate(self.sections):
                if b'VB5!' in self.mem[lo - self.base:hi - self.base]:
                    self.sections[i] = (lo, hi, name, ch | 0x20000020)
        self.imports = {}""")])
print('patched')
