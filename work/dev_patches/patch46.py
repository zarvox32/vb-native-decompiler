import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# control accessors return the control in eax (only `this` is passed)
patch('x86sem.py', [(
"""        if kind == 'control':
            # accessor: control object returned via out pointer
            args, total = self.args_by_sizes([4, 4])
            self.consume(8)
            ctl = Raw(info['name'], 'Object')
            if isinstance(obj, Me):
                val = ctl
            else:
                val = Raw('%s.%s' % (obj, info['name']), 'Object')
            self.store_ptr(args[1], val, 4, 'Object')
            self.set_reg('eax', Const(0))
            return""",
"""        if kind == 'control':
            # accessor: Me.<control> - the control object is returned in eax
            self.consume(4)
            ctl = Raw(info['name'], 'Object')
            if not isinstance(obj, Me):
                ctl = Raw('%s.%s' % (render_obj(obj), info['name']), 'Object')
            ctl.control = info['name']
            self.set_reg('eax', ctl)
            return"""), (
"""class CdqOf(Expr):""",
"""def render_obj(e):
    from ir import render
    return render(e, 99)


class CdqOf(Expr):""")])

# control accessor offsets also come from the form layout when no method matches
patch('decompiler.py', [(
"""        base = self.method_base.get(o.index)
        if base is None:
            return None
        k = (off - base) // 4""",
"""        base = self.method_base.get(o.index)
        ci = (off - FORM_VTSIZE) // 4
        ctls = [c for c in o.controls if c.name != 'Form' and not self.is_impl_iface(c)]
        if base is None or off < base:
            if 0 <= ci < len(ctls) and L.pending_cells() == 4:
                return {'kind': 'control', 'name': ctls[ci].name}
            if base is None:
                return None
        k = (off - base) // 4"""), (
"""    def dispid_name(self, obj, dispid):
        return 'Dispid%s' % (dispid.v if isinstance(dispid, Const) else dispid)""",
"""    def dispid_name(self, obj, dispid):
        d = dispid.v if isinstance(dispid, Const) else None
        cname = getattr(obj, 'control', None) or (obj.text if isinstance(obj, Raw) else None)
        if d is not None and cname:
            for o in self.proj.objects:
                for c in o.controls:
                    if c.name == cname:
                        cls = self.control_typelib_class(c)
                        if cls is not None:
                            for m in cls.members:
                                if m.memid == d:
                                    return m.name
        return 'Dispid%s' % (d if d is not None else dispid)

    def control_typelib_class(self, c):
        \"\"\"Dispatch interface of a control from the referenced OCX type libraries (by ProgID).\"\"\"
        progs = self.ext_progids()
        for prog in progs:
            lib, cls = prog.split('.', 1)
            if cls.lower().startswith(c.name.lower()[:3]) or c.name.lower() in cls.lower():
                cc = self.tlb.by_name.get(cls)
                if cc is not None and cc.kind == 'coclass':
                    for imp in cc.impl:
                        if (imp['flags'] & 1) and not (imp['flags'] & 2):
                            return self.tlb.iface(imp['guid'])
        return None

    def ext_progids(self):
        if not hasattr(self, '_progids'):
            import re as _re
            self._progids = []
            t = self.proj.ext_comp_table
            if t and self.im.valid(t, 0x400):
                blob = self.im.read(t, 0x2000 if self.im.valid(t, 0x2000) else 0x400)
                for m in _re.finditer(rb'([A-Za-z_][A-Za-z0-9_]*\\.[A-Za-z_][A-Za-z0-9_]*)\\x00', blob):
                    s = m.group(1).decode()
                    if s not in self._progids and not s.lower().endswith(('.ocx', '.dll', '.oca')):
                        self._progids.append(s)
        return self._progids""")])

# exclusion threshold
patch('emit.py', [(
"""        if total and dropped:
            ex, n = dropped.most_common(1)[0]
            if n * 2 >= total:
                return ex""",
"""        if total and dropped:
            ex, n = dropped.most_common(1)[0]
            if n * 10 >= total * 3:
                return ex""")])

# Single/Double constants passed to user procedures
patch('emit.py', [(
"""        self.collect_types(p, L, blocks, terms)
        self.boolean_rewrite(blocks, terms)""",
"""        self.collect_types(p, L, blocks, terms)
        self.boolean_rewrite(blocks, terms)
        self.float_args(blocks, terms)"""), (
"""    def boolean_rewrite(self, blocks, terms):""",
"""    def float_args(self, blocks, terms):
        \"\"\"Integer bit patterns passed to Single/Double parameters are float constants.\"\"\"
        import struct as _st

        def fn(x):
            if isinstance(x, Call) and x.kind == 'proc' and getattr(x, 'proc', None) is not None:
                pt = self.proc_param_types.get(x.proc.va)
                if not pt:
                    return None
                start = 4 if (x.proc.obj is not None and x.proc.obj.kind in ('form', 'class')) else 0
                new = list(x.args)
                ch = False
                for i, a in enumerate(new):
                    t = pt.get(start + 4 * i)
                    if t == 'Single' and isinstance(a, Const):
                        new[i] = FConst(_st.unpack('<f', _st.pack('<I', a.v & 0xffffffff))[0], 'Single')
                        ch = True
                if ch:
                    n = Call(x.fn, new, x.obj, x.kind, x.ty, x.prop, not x.side, x.paren)
                    n.proc = x.proc
                    return n
            return None
        for b, stmts in blocks.items():
            for s in stmts:
                stmt_rewrite(s, fn)

    def boolean_rewrite(self, blocks, terms):"""), (
"""        self.proc_excluded = {}    # proc va -> Exclusion (whole procedure omitted)""",
"""        self.proc_excluded = {}    # proc va -> Exclusion (whole procedure omitted)
        self.proc_param_types = {}  # proc va -> {param offset: VB type}"""), (
"""        head, kind, pnames = self.proc_header(p, sig)""",
"""        head, kind, pnames = self.proc_header(p, sig)
        self.proc_param_types[p.va] = dict(self.header_param_types)"""), (
"""            hint = self.param_types.get(off)
            if hint and evm is None:
                ptype = hint
            names[('param', off)] = pname""",
"""            hint = self.param_types.get(off)
            if hint and evm is None:
                ptype = hint
            self.header_param_types[off] = ptype
            names[('param', off)] = pname"""), (
"""        dc = self.dc
        name = dc.proc_name(p)
        is_method = p.obj is not None and p.obj.kind in ('form', 'class')
        params = []""",
"""        dc = self.dc
        name = dc.proc_name(p)
        is_method = p.obj is not None and p.obj.kind in ('form', 'class')
        params = []
        self.header_param_types = {}""")])
print('patched')
