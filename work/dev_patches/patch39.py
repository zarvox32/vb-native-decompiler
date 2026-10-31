import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('tlb.py', [(
"""    def type_size(self, t):
        if t.endswith('*'):
            return 4""",
"""    def type_size(self, t):
        if t.startswith('CArray:'):
            _, elem, n, lb = t.split(':')
            return self.type_size(elem) * int(n)
        if t.endswith('*'):
            return 4"""), (
"""    def type_align(self, t):
        if t in self.records:""",
"""    def type_align(self, t):
        if t.startswith('CArray:'):
            return self.type_align(t.split(':')[1])
        if t in self.records:"""), (
"""    def enum_name(self, enum, value):""",
"""    def class_name_for(self, iface_name):
        \"\"\"Name VB code uses for an object type: the coclass whose default interface this is.\"\"\"
        it = self.by_name.get(iface_name)
        if it is None:
            return iface_name.lstrip('_')
        for cc in self.coclass.values():
            for imp in cc.impl:
                if imp['guid'].upper() == it.guid and (imp['flags'] & 1) and not (imp['flags'] & 2):
                    return cc.name
        return iface_name.lstrip('_')

    def enum_name(self, enum, value):""")])

patch('emit.py', [(
"""    def field_path(self, rec, off):
        fields, size, align = self.tlb.layout(rec)
        for name, foff, t in reversed(fields):
            if foff <= off:
                rel = off - foff
                if rel == 0:
                    return name
                if t in self.tlb.records:
                    sub = self.field_path(t, rel)
                    return '%s.%s' % (name, sub) if sub else None
                return None
        return None""",
"""    def field_path(self, rec, off):
        fields, size, align = self.tlb.layout(rec)
        for name, foff, t in reversed(fields):
            if foff <= off:
                rel = off - foff
                if t.startswith('CArray:'):
                    _, elem, n, lb = t.split(':')
                    es = self.tlb.type_size(elem)
                    k, r = divmod(rel, es)
                    if k < int(n) and r == 0:
                        return '%s(%d)' % (name, k + int(lb))
                    if k < int(n) and elem in self.tlb.records:
                        sub = self.field_path(elem, r)
                        return '%s(%d).%s' % (name, k + int(lb), sub) if sub else None
                    return None
                if rel == 0:
                    return name
                if t in self.tlb.records:
                    sub = self.field_path(t, rel)
                    return '%s.%s' % (name, sub) if sub else None
                return None
        return None"""), (
"""        for k in keys:
            off = k[1]
            if (o.index, off) in self.control_by_storage:
                continue""",
"""        for k in keys:
            off = k[1]
            if (o.index, off) in self.control_by_storage:
                continue
            if off < 0x30:
                continue        # object header (vtable, reference count...), not a variable
            if self.inside_record_field(o, off):
                continue"""), (
"""    def global_decls(self, o, globals_used):""",
"""    def inside_record_field(self, o, off):
        \"\"\"Is this form field part of a record-typed field declared elsewhere?\"\"\"
        for k, rec in self.dc.records.items():
            if k[0] == 'v' and k[1] == 'field':
                base = k[2]
                size = self.tlb.layout(rec)[1]
                if base < off < base + size:
                    return True
        return False

    def global_decls(self, o, globals_used):"""), (
"""                    m = getattr(x, 'member', None)
                    if m is not None:
                        self.add_hint(ekey(x.obj), m.iface.name.lstrip('_'), 3)""",
"""                    m = getattr(x, 'member', None)
                    if m is not None:
                        self.add_hint(ekey(x.obj), self.tlb.class_name_for(m.iface.name), 3)""")])

patch('decompiler.py', [(
"""    def note_obj_type(self, obj, name):
        if name.startswith('_'):
            name = name[1:]
        self.obj_types[ekey(obj)][name] += 1""",
"""    def note_obj_type(self, obj, name):
        name = self.tlb.class_name_for(name)
        self.obj_types[ekey(obj)][name] += 1""")])

# record the VB type of variables wrapped into variant temps (vt tells the type, e.g. VT_DATE)
patch('lift.py', [(
"""        vt = vt_e
        byref = bool(vt & 0x4000)
        base = vt & 0xfff""",
"""        vt = vt_e
        byref = bool(vt & 0x4000)
        base = vt & 0xfff
        if val8 is not None and not byref and base in VT_NAMES:
            pv = self.var_parts(val8[1])
            if pv is not None:
                self.note_type(self.var_at(pv[0], pv[1], 4), VT_NAMES[base])""")])
print('patched')
