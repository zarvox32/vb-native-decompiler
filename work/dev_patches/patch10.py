import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('decompiler.py', [(
"""        handlers = {}
        if fi['rtmi']:
            r = self.proj.rtmi(fi['rtmi'])
            if r:
                handlers = r['handlers']""",
"""        handlers = {}
        if fi['rtmi']:
            r = self.proj.rtmi(fi['rtmi'])
            if r:
                end = self.proc_end(p)
                handlers = {k: a for k, a in r['handlers'].items() if p.va < a < end}"""), (
"""    def api_signature(self, d):
        return None""",
"""    API_ARG_BYTES = {'GetTickCount': 0, 'sndPlaySoundA': 8, 'GetSystemDirectoryA': 8,
                     'InternetGetConnectedState': 8, 'ShellExecuteA': 24, 'PathMatchSpecW': 8, 'lstrlenW': 4,
                     'FindNextFileA': 8, 'FindFirstFileA': 8, 'FindClose': 4, 'timeGetTime': 0, 'Sleep': 4,
                     'GetAsyncKeyState': 4, 'GetKeyState': 4, 'mciSendStringA': 16, 'PlaySoundA': 12}

    def api_signature(self, d):
        return self.API_ARG_BYTES.get(d.func)

    def proc_end(self, p):
        if not hasattr(self, '_proc_ends'):
            starts = sorted(self.proj.procs)
            self._proc_ends = {a: b for a, b in zip(starts, starts[1:] + [self.proj.code_end])}
        return self._proc_ends.get(p.va, self.proj.code_end)""")])

patch('cfg.py', [(
"""        while work:
            va = work.pop()
            if va in raw_blocks:
                continue""",
"""        lo, hi = self.proj.code_start, self.proj.code_end
        while work:
            va = work.pop()
            if va in raw_blocks:
                continue
            if not (lo <= va < hi):
                self.warnings.append('branch outside user code to %x' % va)
                continue""")])
print('patched')
