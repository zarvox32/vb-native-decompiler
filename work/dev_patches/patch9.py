import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('structure.py', [(
"""            if self.is_exit_target(x, loops) or x in stops or (x in self.done and x not in (loops[-1].header if loops else ())):""",
"""            if self.is_exit_target(x, loops) or x in stops or \\
                    (x in self.done and not (loops and x == loops[-1].header)):""")])

patch('main.py', [(
"""        out = w.proc_text(p, L)
        texts[va] = out""",
"""        out = safe_proc_text(dc, w, p, L)
        texts[va] = out"""), (
"""        L2 = dc.lift(p)
        texts[va] = w.proc_text(p, L2)""",
"""        try:
            L2 = dc.lift(p)
        except Exception as e:
            dc.warnings.append('lift %s: %s' % (dc.proc_name(p), e))
            continue
        texts[va] = safe_proc_text(dc, w, p, L2)"""), (
"""def form_header(dc, o):""",
"""def safe_proc_text(dc, w, p, L):
    try:
        return w.proc_text(p, L)
    except Exception as e:
        import traceback
        from emit import ProcOut
        dc.warnings.append('%s: %s' % (dc.proc_name(p), e))
        if dc.debug:
            traceback.print_exc()
        out = ProcOut()
        out.header = 'Private Sub %s()' % dc.proc_name(p)
        out.footer = 'End Sub'
        out.lines = ["    ' [decompiler] failed to decompile this procedure: %s" % e]
        return out


def form_header(dc, o):""")])
print('patched')
