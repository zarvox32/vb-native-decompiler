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
"""    files = []
    globals_used = set()""",
"""    # names actually used in the emitted code (declare only those)
    used_text = '\\n'.join('\\n'.join(t.lines) for t in texts.values() if t is not None)
    import re as _re
    used_names = set(_re.findall(r'\\b[mg]_[0-9A-F]+\\b', used_text))
    files = []
    globals_used = set()"""), (
"""            gd = w.global_decls(o, globals_used)
            if gd:""",
"""            gd = [d for d in w.global_decls(o, globals_used) if d.split()[1] in used_names]
            if gd:"""), (
"""            fd = w.field_decls(o)
            if fd:""",
"""            fd = [d for d in w.field_decls(o) if d.split()[1].split('(')[0] in used_names]
            if fd:""")])
print('patched')
