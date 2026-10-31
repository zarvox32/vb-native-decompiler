import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('vbmeta.py', [(
"""            if a not in self.procs and im.valid(a - 1, 8) and im.u8(a - 1) == 0x90 and \\
                    any(im.read(a, len(pp)) == pp for pp in PROLOGUES):""",
"""            if a not in self.procs and im.valid(a - 1, 8) and im.u8(a - 1) == 0x90 and \\
                    self._after_padding(a) and any(im.read(a, len(pp)) == pp for pp in PROLOGUES):"""), (
"""    def _looks_like_proc(self, va):""",
"""    def _after_padding(self, a):
        \"\"\"The NOP run before `a` must follow a ret / jmp (end of the previous procedure).\"\"\"
        im = self.im
        s = a - 1
        while im.valid(s - 1, 1) and im.u8(s - 1) == 0x90 and a - s < 16:
            s -= 1
        if im.u8(s - 1) == 0xC3:
            return True
        if im.valid(s - 3, 3) and im.u8(s - 3) == 0xC2:
            return True
        if im.valid(s - 5, 5) and im.u8(s - 5) == 0xE9:
            return True
        if im.valid(s - 2, 2) and im.u8(s - 2) == 0xEB:
            return True
        return False

    def _looks_like_proc(self, va):"""), (
"""        mods = [o for o in self.objects if o.kind == 'module']
        spans = {}""",
"""        mods = [o for o in self.objects if o.kind == 'module']
        # a free procedure inside a form's code range belongs to that form
        for o in self.objects:
            if o.kind not in ('form', 'class'):
                continue
            vas = [va for va, ob in owned if ob is o]
            if not vas:
                continue
            lo_, hi_ = min(vas), max(vas)
            for va in procs:
                if self.procs[va].obj is None and lo_ < va < hi_:
                    self.procs[va].obj = o
        owned = [(va, self.procs[va].obj) for va in procs if self.procs[va].obj is not None]
        spans = {}"""), (
"""        ranges = []
        for o in mods:
            for a in (o.module_public, o.module_static):
                if a:
                    ranges.append((a, o))
        ranges.sort(key=lambda x: x[0])""",
"""        ranges = []
        for o in mods:
            for a in (o.module_public, o.module_static):
                if a:
                    ranges.append((a, o))
        ranges.sort(key=lambda x: x[0])
        bounds = {}
        for i, (a, o) in enumerate(ranges):
            bounds[a] = ranges[i + 1][0] if i + 1 < len(ranges) else a + 0x1000"""), (
"""                for a, o in ranges:
                    if a <= v < a + 0x1000:
                        if best is None or a > best[0]:
                            best = (a, o)""",
"""                for a, o in ranges:
                    if a <= v < bounds[a]:
                        best = (a, o)""")])
print('patched')
