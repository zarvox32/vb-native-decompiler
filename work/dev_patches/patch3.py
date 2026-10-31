import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# records of locals belong to the procedure being lifted
patch('decompiler.py', [(
"""    def note_record(self, L, var, rec):
        self.records[ekey(var)] = rec""",
"""    def note_record(self, L, var, rec):
        if isinstance(var, Var) and var.kind == 'local':
            if not hasattr(L, 'records'):
                L.records = {}
            L.records[ekey(var)] = rec
        else:
            self.records[ekey(var)] = rec""")])

patch('emit.py', [(
"""        self.cur_obj = p.obj
        self.param_types = {}
        blocks, terms = self.prepare(p, L)""",
"""        self.cur_obj = p.obj
        self.param_types = {}
        self.cur_records = dict(self.dc.records)
        self.cur_records.update(getattr(L, 'records', {}))
        blocks, terms = self.prepare(p, L)"""), (
"""            if isinstance(x, Member) and x.name is None and ekey(x.base) in self.dc.records:
                rec = self.dc.records[ekey(x.base)]""",
"""            if isinstance(x, Member) and x.name is None and ekey(x.base) in self.cur_records:
                rec = self.cur_records[ekey(x.base)]"""), (
"""        for k, rec in self.dc.records.items():
            if k[0] != 'v' or k[1] != x.kind:""",
"""        for k, rec in self.cur_records.items():
            if k[0] != 'v' or k[1] != x.kind:"""), (
"""        for k, rec in self.dc.records.items():
            self.add_hint(k, rec, 100)""",
"""        for k, rec in self.cur_records.items():
            self.add_hint(k, rec, 100)""")])

# testproc needs the same attribute
patch('testproc.py', [(
"""    w = ProjectWriter(dc, {})""",
"""    w = ProjectWriter(dc, {})
    w.cur_records = {}""")])
print('patched')
