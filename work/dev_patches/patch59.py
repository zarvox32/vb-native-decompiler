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
"""            if votes:
                k = max(votes, key=votes.get)
                res[va] = self.objects[k]
        return res""",
"""            if votes:
                k = max(votes, key=votes.get)
                res[va] = self.objects[k]
        # procedures without data votes follow their callers / callees (call-graph affinity)
        calls = {va: set() for va in free}
        fs = set(free)
        for va in free:
            idx = allp.index(va)
            end = allp[idx + 1] if idx + 1 < len(allp) else self.code_end
            for ins in _md.disasm(im.read(va, min(end - va, 0x20000)), va):
                if ins.mnemonic == 'call' and ins.op_str.startswith('0x'):
                    t = int(ins.op_str, 16)
                    if t in fs and t != va:
                        calls[va].add(t)
                        calls[t].add(va)
        for _ in range(len(free)):
            changed = False
            for va in free:
                if va in res:
                    continue
                c = {}
                for o in calls[va]:
                    if o in res:
                        c[res[o].index] = c.get(res[o].index, 0) + 1
                if c:
                    res[va] = self.objects[max(c, key=c.get)]
                    changed = True
            if not changed:
                break
        return res""")])
print('patched')
