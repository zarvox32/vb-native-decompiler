import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# a Variant converted on return (CInt(v) stored through the result pointer) gives the converted type
patch('decompiler.py', [(
"""    def _size_type(self, size, e):
        ty = getattr(e, 'ty', None)""",
"""    def _size_type(self, size, e):
        ty = getattr(e, 'conv', None) or getattr(e, 'ty', None)""")])

# parameters indexed as arrays are declared `p1() As <element type>`
patch('emit.py', [(
"""    def note_array(self, base, stride, size, kind='pv'):
        k = ekey(base)""",
"""    def note_array(self, base, stride, size, kind='pv'):
        if isinstance(base, Var) and base.kind == 'param':
            self.cur_param_arrays[base.off] = stride
        k = ekey(base)"""), (
"""        self.reg_names = {}
        self.index_vars = 0""",
"""        self.reg_names = {}
        self.index_vars = 0
        self.cur_param_arrays = {}"""), (
"""            hint = self.param_types.get(off)
            if hint and evm is None:
                ptype = hint
            self.header_param_types[off] = ptype
            names[('param', off)] = pname
            params.append('%s%s As %s' % ('' if byref else 'ByVal ', pname, ptype))""",
"""            hint = self.param_types.get(off)
            if hint and evm is None:
                ptype = hint
            arr = ''
            if off in getattr(self, 'cur_param_arrays', {}) and evm is None:
                # arrays are always passed ByRef
                arr, byref = '()', True
                ptype = {2: 'Integer', 4: 'Long', 8: 'Double', 16: 'Variant', 1: 'Byte'}.get(
                    self.cur_param_arrays[off], 'Variant')
            self.header_param_types[off] = ptype
            names[('param', off)] = pname
            params.append('%s%s%s As %s' % ('' if byref else 'ByVal ', pname, arr, ptype))""")])
print('patched')
