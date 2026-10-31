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
"""        # 3) assign owning objects to free procedures by address ranges
        self._assign_owners()""",
"""        # 2b) unreferenced procedures: 16-byte aligned starts after NOP padding with a VB prologue
        PROLOGUES = (b'\\x55\\x8B\\xEC', b'\\x83\\xEC', b'\\x81\\xEC', b'\\x51', b'\\x53', b'\\x56', b'\\x57',
                     b'\\x8B\\x44\\x24', b'\\x8B\\x4C\\x24', b'\\x8B\\x54\\x24', b'\\x33\\xC0', b'\\x6A')
        a = (lo + 15) & ~15
        while a < hi:
            if a not in self.procs and im.valid(a - 1, 8) and im.u8(a - 1) == 0x90 and \\
                    any(im.read(a, len(pp)) == pp for pp in PROLOGUES):
                first = im.u32(a)
                if not (lo <= first < hi) and self._looks_like_proc(a):
                    self.procs[a] = Proc(a, None)
            a += 16
        # 3) assign owning objects to free procedures by address ranges
        self._assign_owners()"""), (
"""    def _assign_owners(self):""",
"""    def _looks_like_proc(self, va):
        \"\"\"Decode forward: plausible straight code ending in a ret within a short distance.\"\"\"
        n = 0
        for i in _md.disasm(self.im.read(va, 0x4000), va):
            n += 1
            if i.mnemonic in ('ret', 'retn'):
                return n >= 3
            if i.mnemonic in ('int3', 'hlt', 'in', 'out', 'insb', 'insd', 'outsb', 'outsd', 'arpl', 'bound',
                              'popal', 'pushal', 'das', 'aas', 'daa', 'aaa', 'into', 'iretd', 'retf'):
                return False
            if n > 4000:
                return False
        return False

    def _assign_owners(self):"""), (
"""        if len(mods) == 1:
            for va in free:
                self.procs[va].obj = mods[0]
            return
        guess = self._module_by_data_refs(free, mods)
        last = mods[0] if mods else None
        for va in free:
            o = guess.get(va) or last
            if o is None:
                # attach to the nearest preceding owned object
                o = max(((v, ob) for v, ob in owned if v < va), default=(0, self.objects[0]))[1]
            self.procs[va].obj = o
            last = o""",
"""        if len(mods) == 1:
            for va in free:
                self.procs[va].obj = mods[0]
            return
        if not mods:
            for va in free:
                self.procs[va].obj = max(((v, ob) for v, ob in owned if v < va), default=(0, self.objects[0]))[1]
            return
        # module code is emitted in object order: split the (address-sorted) free procedures into
        # contiguous groups, choosing split points that agree best with the data each one touches
        guess = self._module_by_data_refs(free, mods)
        n, m = len(free), len(mods)
        INF = -10 ** 9
        best = [[INF] * (n + 1) for _ in range(m + 1)]
        back = [[0] * (n + 1) for _ in range(m + 1)]
        best[0][0] = 0
        for j in range(1, m + 1):
            for i in range(n + 1):
                for k in range(i + 1):
                    if best[j - 1][k] == INF:
                        continue
                    score = best[j - 1][k] + sum(1 for va in free[k:i] if guess.get(va) is mods[j - 1]) + \\
                        (0.5 if i > k else 0)
                    if score > best[j][i]:
                        best[j][i] = score
                        back[j][i] = k
        i = n
        for j in range(m, 0, -1):
            k = back[j][i]
            for va in free[k:i]:
                self.procs[va].obj = mods[j - 1]
            i = k""")])
print('patched')
