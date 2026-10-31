"""Type library database built from typelibs.json (see tlbdump.py).

tlbdump runs in 64-bit Python, so vtable offsets in the JSON are for 8-byte pointers;
they are halved here to get the 32-bit offsets used by the VB executables."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JSON = os.path.join(HERE, '..', 'typelibs.json')

PARAM_IN, PARAM_OUT, PARAM_RETVAL, PARAM_OPT = 1, 2, 8, 0x10

SIZES32 = {'Integer': 2, 'Long': 4, 'Single': 4, 'Double': 8, 'Currency': 8, 'Date': 8, 'String': 4,
           'Object': 4, 'Boolean': 2, 'Variant': 16, 'IUnknown': 4, 'Byte': 1, 'SCODE': 4, 'HRESULT': 4,
           'LPSTR': 4, 'LPWSTR': 4, 'Void': 0}


class Member:
    def __init__(self, d, iface):
        self.name = d['name']
        self.invkind = d['invkind']
        self.vt = d['oVft'] // 2
        self.params = d['params']
        self.ret = d['ret']
        self.iface = iface
        self.memid = d['memid']

    @property
    def visible_params(self):
        return [p for p in self.params if not (p['flags'] & PARAM_RETVAL)]

    @property
    def retval(self):
        for p in self.params:
            if p['flags'] & PARAM_RETVAL:
                return p
        return None

    def __repr__(self):
        return '<%s.%s %s @%x>' % (self.iface.name, self.name, self.invkind, self.vt)


class Interface:
    def __init__(self, d, lib):
        self.name = d['name']
        self.guid = d['guid'].upper()
        self.kind = d['kind']
        self.lib = lib
        self.vtsize = d.get('cbSizeVft', 0) // 2
        self.members = [Member(f, self) for f in d.get('funcs', [])]
        self.by_vt = {}
        for m in self.members:
            self.by_vt.setdefault(m.vt, []).append(m)
        self.impl = d.get('impl', [])
        self.vars = d.get('vars', [])
        self.alias = d.get('alias')

    def member(self, vt, prefer=None):
        ms = self.by_vt.get(vt)
        if not ms:
            return None
        if prefer:
            for m in ms:
                if m.invkind == prefer:
                    return m
        return ms[0]

    def event_by_index(self, i):
        """Event sink slot i -> event (vtable slot 3+i of the source interface)."""
        return self.member((3 + i) * 4)


class TypeLibDB:
    def __init__(self, path=DEFAULT_JSON):
        libs = json.load(open(path))
        self.by_guid = {}
        self.by_name = {}
        self.coclass = {}
        self.records = {}
        self.enums = {}
        self.const_by_value = {}
        for lib in libs:
            for t in lib['types']:
                if 'error' in t:
                    continue
                it = Interface(t, lib['name'])
                self.by_guid.setdefault(it.guid, it)
                self.by_name.setdefault(t['name'], it)
                if t['kind'] == 'coclass':
                    self.coclass[it.guid] = it
                elif t['kind'] == 'record':
                    self.records[t['name']] = it
                elif t['kind'] == 'enum':
                    self.enums[t['name']] = it
        self._layouts = {}

    def iface(self, guid):
        if not guid:
            return None
        return self.by_guid.get(guid.upper())

    def default_iface(self, coclass_guid, source=False):
        cc = self.coclass.get((coclass_guid or '').upper())
        if not cc:
            return None
        for imp in cc.impl:
            is_src = bool(imp['flags'] & 2)
            is_def = bool(imp['flags'] & 1)
            if is_def and is_src == source:
                return self.iface(imp['guid'])
        for imp in cc.impl:
            if bool(imp['flags'] & 2) == source:
                return self.iface(imp['guid'])
        return None

    def coclass_for_iface(self, guid, source=None):
        g = (guid or '').upper()
        for cc in self.coclass.values():
            for imp in cc.impl:
                if imp['guid'].upper() == g and (source is None or bool(imp['flags'] & 2) == source):
                    return cc
        return None

    def type_size(self, t):
        if t.startswith('CArray:'):
            _, elem, n, lb = t.split(':')
            return self.type_size(elem) * int(n)
        if t.endswith('*'):
            return 4
        if t in SIZES32:
            return SIZES32[t]
        if t in self.records:
            return self.layout(t)[1]
        it = self.by_name.get(t)
        if it is not None:
            if it.kind == 'enum':
                return 4
            if it.kind == 'alias' and it.alias:
                return self.type_size(it.alias)
            if it.kind in ('interface', 'dispatch', 'coclass'):
                return 4
        return 4

    def type_align(self, t):
        if t.startswith('CArray:'):
            return self.type_align(t.split(':')[1])
        if t in self.records:
            fields, size, align = self.layout(t)
            return align
        return min(max(self.type_size(t), 1), 8)

    def layout(self, rec):
        """32-bit layout of a typelib record: ([(name, off, type)], size, align)."""
        if rec in self._layouts:
            return self._layouts[rec]
        r = self.records[rec]
        off = 0
        fields = []
        maxa = 1
        for v in r.vars:
            t = v['type']
            sz = self.type_size(t)
            al = min(self.type_align(t), 8) if sz else 1
            maxa = max(maxa, al)
            off = (off + al - 1) // al * al
            fields.append((v['name'], off, t))
            off += sz
        size = (off + maxa - 1) // maxa * maxa
        self._layouts[rec] = (fields, size, maxa)
        return self._layouts[rec]

    def class_name_for(self, iface_name):
        """Name VB code uses for an object type: the coclass whose default interface this is."""
        it = self.by_name.get(iface_name)
        if it is None:
            return iface_name.lstrip('_')
        for cc in self.coclass.values():
            for imp in cc.impl:
                if imp['guid'].upper() == it.guid and (imp['flags'] & 1) and not (imp['flags'] & 2):
                    return cc.name
        return iface_name.lstrip('_')

    def enum_name(self, enum, value):
        e = self.enums.get(enum)
        if not e:
            return None
        for v in e.vars:
            if v['value'] == value:
                return v['name']
        return None
