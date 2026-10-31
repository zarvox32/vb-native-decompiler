"""Dump COM type libraries to JSON: interfaces (vtable offsets, members, params), coclasses, enums."""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib'))
import comtypes
import comtypes.typeinfo as ti
import comtypes.automation as au

VT = {0: 'Empty', 1: 'Null', 2: 'Integer', 3: 'Long', 4: 'Single', 5: 'Double', 6: 'Currency', 7: 'Date',
      8: 'String', 9: 'Object', 10: 'SCODE', 11: 'Boolean', 12: 'Variant', 13: 'IUnknown', 14: 'Decimal',
      16: 'Byte', 17: 'Byte', 18: 'Integer', 19: 'Long', 20: 'Currency', 21: 'Currency', 22: 'Long', 23: 'Long',
      24: 'Void', 25: 'HRESULT', 26: 'Ptr', 27: 'SafeArray', 28: 'CArray', 29: 'UserDefined', 30: 'LPSTR', 31: 'LPWSTR'}

INVKIND = {1: 'method', 2: 'get', 4: 'let', 8: 'set'}
TKIND = {0: 'enum', 1: 'record', 2: 'module', 3: 'interface', 4: 'dispatch', 5: 'coclass', 6: 'alias', 7: 'union'}


def tdesc_str(tinfo, td):
    vt = td.vt
    if vt == 26:  # VT_PTR
        return tdesc_str(tinfo, td._.lptdesc[0]) + '*'
    if vt == 27:
        return tdesc_str(tinfo, td._.lptdesc[0]) + '()'
    if vt == 28:  # VT_CARRAY: fixed-size array inside a record
        try:
            ad = td._.lpadesc[0]
            elem = tdesc_str(tinfo, ad.tdescElem)
            n = 1
            lb = 0
            bounds = ad.rgbounds
            for i in range(ad.cDims):
                n *= bounds[i].cElements
                if i == 0:
                    lb = bounds[i].lLbound
            return 'CArray:%s:%d:%d' % (elem, n, lb)
        except Exception:
            return 'CArray'
    if vt == 29:
        try:
            ref = tinfo.GetRefTypeInfo(td._.hreftype)
            return ref.GetDocumentation(-1)[0]
        except Exception:
            return 'UDT?'
    return VT.get(vt, 'vt%d' % vt)


def dump_typeinfo(tinfo):
    ta = tinfo.GetTypeAttr()
    name, doc, _, _ = tinfo.GetDocumentation(-1)
    kind = TKIND.get(ta.typekind, str(ta.typekind))
    out = {'name': name, 'kind': kind, 'guid': str(ta.guid), 'flags': ta.wTypeFlags, 'cbSizeVft': ta.cbSizeVft}
    if ta.typekind in (3, 4):  # interface/dispatch: if dual, get the vtable half
        if ta.typekind == 4 and (ta.wTypeFlags & 0x40):  # TYPEFLAG_FDUAL
            try:
                href = tinfo.GetRefTypeOfImplType(-1)
                tinfo = tinfo.GetRefTypeInfo(href)
                ta = tinfo.GetTypeAttr()
                out['dual'] = True
                out['cbSizeVft'] = ta.cbSizeVft
            except Exception:
                pass
    funcs = []
    for i in range(ta.cFuncs):
        fd = tinfo.GetFuncDesc(i)
        names = tinfo.GetNames(fd.memid, fd.cParams + 1)
        params = []
        for p in range(fd.cParams):
            ed = fd.lprgelemdescParam[p]
            flags = ed._.paramdesc.wParamFlags
            params.append({'name': names[p + 1] if p + 1 < len(names) else 'p%d' % p,
                           'type': tdesc_str(tinfo, ed.tdesc), 'flags': flags})
        funcs.append({'name': names[0], 'memid': fd.memid, 'invkind': INVKIND.get(fd.invkind, fd.invkind),
                      'oVft': fd.oVft, 'params': params, 'ret': tdesc_str(tinfo, fd.elemdescFunc.tdesc),
                      'funckind': fd.funckind, 'callconv': fd.callconv, 'optional': fd.cParamsOpt})
    out['funcs'] = funcs
    vars_ = []
    for i in range(ta.cVars):
        vd = tinfo.GetVarDesc(i)
        vname = tinfo.GetNames(vd.memid, 1)[0]
        val = None
        if vd.varkind == 2:  # VAR_CONST
            try:
                val = vd._.lpvarValue[0].value
            except Exception:
                val = None
        vars_.append({'name': vname, 'memid': vd.memid, 'value': val if isinstance(val, (int, float, str)) else None,
                      'type': tdesc_str(tinfo, vd.elemdescVar.tdesc), 'oInst': getattr(vd._, 'oInst', None) if vd.varkind == 0 else None})
    out['vars'] = vars_
    impl = []
    tattr = tinfo.GetTypeAttr()
    for i in range(tattr.cImplTypes):
        try:
            href = tinfo.GetRefTypeOfImplType(i)
            r = tinfo.GetRefTypeInfo(href)
            rname = r.GetDocumentation(-1)[0]
            iflags = tinfo.GetImplTypeFlags(i)
            impl.append({'name': rname, 'guid': str(r.GetTypeAttr().guid), 'flags': iflags})
        except Exception:
            pass
    out['impl'] = impl
    if ta.typekind == 6:
        out['alias'] = tdesc_str(tinfo, ta.tdescAlias)
    return out


def dump_lib(path):
    tl = ti.LoadTypeLibEx(path, 2)
    la = tl.GetLibAttr()
    lib = {'path': path, 'name': tl.GetDocumentation(-1)[0], 'guid': str(la.guid),
           'major': la.wMajorVerNum, 'minor': la.wMinorVerNum, 'types': []}
    for i in range(tl.GetTypeInfoCount()):
        try:
            lib['types'].append(dump_typeinfo(tl.GetTypeInfo(i)))
        except Exception as e:
            lib['types'].append({'name': tl.GetDocumentation(i)[0], 'error': str(e)})
    return lib


if __name__ == '__main__':
    # usage: python tlbdump.py <out.json> [--project NAME_OR_FILE] [more type library paths...]
    # The VB6 runtime, VB6.OLB and OLE Automation are always dumped, plus the "type_libraries" of every
    # app in the library (typelibs.json is shared by all of them), or only of the --project given.
    args = sys.argv[2:]
    extra = []
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vbdec'))
    import project as projects
    if '--project' in args:
        k = args.index('--project')
        chosen = [projects.find(args[k + 1])]
        del args[k:k + 2]
    else:
        chosen = projects.all_projects()
    for proj in chosen:
        extra += [t for t in proj.data.get('type_libraries', []) if t not in extra]
    extra += args
    olb = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'untrusted_vb6olb', 'VB6.OLB')
    if not os.path.isfile(olb):
        raise SystemExit('%s is missing. It is not part of the repository; documentation.md, section 11, '
                         'says where to get it.' % olb)
    libs = [r'C:\Windows\SysWOW64\msvbvm60.dll', r'C:\Windows\SysWOW64\msvbvm60.dll\3', olb] + \
        extra + [r'C:\Windows\SysWOW64\stdole2.tlb']
    allx = [dump_lib(p) for p in libs]
    json.dump(allx, open(sys.argv[1], 'w'), indent=0)
    for l in allx:
        print(l['name'], len(l['types']), sum(1 for t in l['types'] if 'error' in t))
