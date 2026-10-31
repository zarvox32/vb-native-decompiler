import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('emit.py', [(
"""        for b, stmts in blocks.items():
            out = []
            for s in stmts:
                total += 1
                ex = self.excluded_by(s)
                if ex is not None:
                    dropped[ex] += 1""",
"""        self.last_dropped = dropped
        for b, stmts in blocks.items():
            out = []
            for s in stmts:
                total += 1
                ex = self.excluded_by(s)
                if ex is not None:
                    self.note_excluded_writes(s, ex)
                    dropped[ex] += 1"""), (
"""    def apply_exclusions(self, p, blocks, terms):""",
"""    def note_excluded_writes(self, s, ex):
        if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind in ('field', 'global'):
            self.excluded_writes.setdefault(ekey(s.dst), set()).add(ex.label)

    def apply_exclusions(self, p, blocks, terms):"""), (
"""        self.proc_param_types = {}  # proc va -> {param offset: VB type}""",
"""        self.proc_param_types = {}  # proc va -> {param offset: VB type}
        self.excluded_writes = {}   # var key -> labels of omitted code that assigns it
        self.normal_writes = set()  # var keys assigned by code that is kept
        self.last_dropped = Counter()"""), (
"""        ex = self.apply_exclusions(p, blocks, terms)
        self.cur_names = dict(pnames)
        if ex is not None:
            out.omitted = ex.label""",
"""        if p.va in self.forced_exclusions:
            ex = self.forced_exclusions[p.va]
            for b, stmts in blocks.items():
                for s in stmts:
                    self.note_excluded_writes(s, ex)
        else:
            ex = self.apply_exclusions(p, blocks, terms)
            for b, stmts in blocks.items():
                for s in stmts:
                    if isinstance(s, Assign) and isinstance(s.dst, Var) and s.dst.kind in ('field', 'global'):
                        self.normal_writes.add(ekey(s.dst))
        out.excl = Counter(self.last_dropped) if ex is None else Counter({ex: 1})
        self.cur_names = dict(pnames)
        if ex is not None:
            if p.va not in self.forced_exclusions:
                for b, stmts in blocks.items():
                    for s in stmts:
                        self.note_excluded_writes(s, ex)
            out.omitted = ex.label"""), (
"""        self.proc_excluded = {}    # proc va -> Exclusion (whole procedure omitted)""",
"""        self.proc_excluded = {}    # proc va -> Exclusion (whole procedure omitted)
        self.forced_exclusions = {}  # proc va -> Exclusion (e.g. a whole licensing module)"""), (
"""            if self.dc.global_objects.get(va):
                continue
            t = choose_type(self.global_hints.get(va, Counter()), self.global_sizes.get(va, 4))
            out.append('Public g_%X As %s' % (va & 0xffff, t))
        return out""",
"""            if self.dc.global_objects.get(va):
                continue
            t = choose_type(self.global_hints.get(va, Counter()), self.global_sizes.get(va, 4))
            out.append('Public g_%X As %s%s' % (va & 0xffff, t, self.omitted_writer_note(('v', 'global', va))))
        return out

    def omitted_writer_note(self, k):
        labels = self.excluded_writes.get(k)
        if labels and k not in self.normal_writes:
            return "   ' only assigned by omitted %s code" % ' / '.join(sorted(labels))
        return ''"""), (
"""            t = choose_type(self.field_hints.get(k, Counter()), self.field_sizes.get(k, 4))
            out.append('Dim m_%X As %s' % (off, t))
        return out""",
"""            t = choose_type(self.field_hints.get(k, Counter()), self.field_sizes.get(k, 4))
            out.append('Dim m_%X As %s%s' % (off, t, self.omitted_writer_note(('v', 'field', off))))
        return out""")])

# main: whole licensing module
patch('main.py', [(
"""    # second pass with knowledge of excluded procedures
    for va in order:""",
"""    # a module whose every procedure touches licensing is the licensing module: omit it whole
    for o in pr.objects:
        if o.kind != 'module':
            continue
        vas = [p.va for p in by_obj.get(o.index, [])]
        lic = [ex for ex in w.exclusions if ex.label.startswith('licensing')]
        if vas and lic and all(texts.get(va) is not None and texts[va].excl.get(lic[0]) for va in vas):
            for va in vas:
                w.forced_exclusions[va] = lic[0]
                w.proc_excluded[va] = lic[0]
    # second pass with knowledge of excluded procedures
    w.excluded_writes = {}
    w.normal_writes = set()
    for va in order:"""), (
"""        if L is None or va in w.proc_excluded:
            continue""",
"""        if L is None or (va in w.proc_excluded and va not in w.forced_exclusions):
            continue""")])
print('patched')
