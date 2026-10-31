import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('main.py', [(
"""        if o.kind == 'form':
            fname = o.name + '.frm'
            lines += form_header(dc, o)""",
"""        if o.kind == 'form':
            fname = o.name + '.frm'
            lines += form_header(dc, o, outdir, w)"""), (
"""def form_header(dc, o):
    lines = ['VERSION 5.00']
    lines.append('Begin VB.Form %s' % o.name)
    for c in o.controls:
        if c.name == 'Form' or dc.is_impl_iface(c):
            continue
        cls = control_class(dc, c)
        lines.append('   Begin %s %s' % (cls, c.name))
        lines.append('   End')
    lines.append('End')""",
"""def form_header(dc, o, outdir, w):
    from frm import parse_form
    info = parse_form(o.gui)
    lines = ['VERSION 5.00']
    lines.append('Begin VB.Form %s' % o.name)
    if info['caption'] is not None:
        lines.append('   Caption         =   "%s"' % info['caption'].replace('"', '""'))
    for k, (ext, data) in enumerate(info['images']):
        fn = '%s_picture%s.%s' % (o.name, '' if k == 0 else k + 1, ext)
        with open(os.path.join(outdir, fn), 'wb') as f:
            f.write(data)
        lines.append("   ' Picture: %s (extracted from the form; %d bytes)" % (fn, len(data)))
    lines.append("   ' Other designer properties were not decoded.")
    excluded = set()
    for ex in w.exclusions:
        excluded |= ex.controls
    for c in o.controls:
        if c.name == 'Form' or dc.is_impl_iface(c):
            continue
        cls = control_class(dc, c)
        note = ''
        if c.name.lower() in excluded:
            note = "   ' not needed for the port (%s)" % ', '.join(ex.label for ex in w.exclusions
                                                             if c.name.lower() in ex.controls)
        lines.append('   Begin %s %s%s' % (cls, c.name, note))
        lines.append('   End')
    lines.append('End')"""), (
"""def control_class(dc, c):
    cc = dc.tlb.coclass_for_iface(c.guid, source=True)
    if cc is not None:
        return '%s.%s' % (cc.lib if cc.lib != 'VB' else 'VB', cc.name)
    return 'Unknown.Control  \\' event interface %s' % c.guid""",
"""def control_class(dc, c):
    cc = dc.tlb.coclass_for_iface(c.guid, source=True)
    if cc is not None:
        return '%s.%s' % (cc.lib if cc.lib != 'VB' else 'VB', cc.name)
    for prog in dc.ext_progids():
        lib, cls = prog.split('.', 1)
        if c.name.lower() in cls.lower() or cls.lower().startswith(c.name.lower()):
            return prog
    for prog in dc.ext_progids():
        lib, cls = prog.split('.', 1)
        if ''.join(ch for ch in cls if ch.isupper()).lower() == c.name.lower():
            return prog
    return 'Unknown.Control'""")])
print('patched')
