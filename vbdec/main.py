"""VB6 native-code decompiler - command line driver.

usage: python -I main.py <exe> <outdir> [--image dump.bin] [--debug]
                         [--project NAME_OR_FILE --game NAME [--record-omissions]]

Writes <outdir>/<Project>.vbp, one .frm per form, one .bas per module and a DECOMPILER_NOTES.txt.

With --project and --game, the project file's omission rules decide what is left out of the output, and
what was left out is compared with that program's record in the project file: a difference is listed
and the exit status is 3 (--record-omissions stores the new state as the record instead). Without a
project nothing is left out.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from decompiler import Decompiler  # noqa: E402
from emit import ProjectWriter  # noqa: E402
from ir import NAMER  # noqa: E402

KNOWN_DECLARES = {
    'GetTickCount': 'Function GetTickCount Lib "{lib}" () As Long',
    'sndPlaySoundA': 'Function sndPlaySound Lib "{lib}" Alias "sndPlaySoundA" (ByVal lpszSoundName As String, '
                     'ByVal uFlags As Long) As Long',
    'GetSystemDirectoryA': 'Function GetSystemDirectory Lib "{lib}" Alias "GetSystemDirectoryA" '
                           '(ByVal lpBuffer As String, ByVal nSize As Long) As Long',
    'InternetGetConnectedState': 'Function InternetGetConnectedState Lib "{lib}" (ByRef lpdwFlags As Long, '
                                 'ByVal dwReserved As Long) As Long',
    'ShellExecuteA': 'Function ShellExecute Lib "{lib}" Alias "ShellExecuteA" (ByVal hwnd As Long, '
                     'ByVal lpOperation As String, ByVal lpFile As String, ByVal lpParameters As String, '
                     'ByVal lpDirectory As String, ByVal nShowCmd As Long) As Long',
}


NOTES = {}


def declare_line(d):
    tpl = KNOWN_DECLARES.get(d.func)
    if tpl:
        return 'Private Declare ' + tpl.format(lib=d.lib)
    return "' Declare ... %s Lib \"%s\" (parameters not recoverable)" % (d.func, d.lib)


def project_files(dc, lifted, outdir, project=None):
    """Write the project. project: a project.Project whose omission rules apply (None: omit nothing)."""
    pr = dc.proj
    w = ProjectWriter(dc, lifted, project.exclusions() if project else [])
    w.project = project
    os.makedirs(outdir, exist_ok=True)
    # API declares: rename to the alias-free names used in code
    for d in pr.declares:
        if d.func.endswith('A') and d.func in KNOWN_DECLARES and 'Alias' in KNOWN_DECLARES[d.func]:
            d.name = d.func[:-1]
    # procedures per object, in source order
    by_obj = {}
    for va, p in pr.procs.items():
        by_obj.setdefault(p.obj.index if p.obj else -1, []).append(p)
    texts = {}
    # first pass: decide whole-procedure exclusions so callers can drop the calls too
    order = sorted(pr.procs)
    for va in order:
        p = pr.procs[va]
        L = lifted.get(va)
        if L is None:
            continue
        out = safe_proc_text(dc, w, p, L)
        texts[va] = out
        if out.omitted:
            for ex in w.exclusions:
                if ex.label == out.omitted:
                    w.proc_excluded[va] = ex
    # a module whose every procedure touches a `whole_module` omission (a licensing module): omit it whole
    for o in pr.objects:
        if o.kind != 'module':
            continue
        vas = [p.va for p in by_obj.get(o.index, [])]
        for ex in [e for e in w.exclusions if e.whole_module]:
            if not (vas and all(texts.get(va) is not None and (texts[va].excl.get(ex) or
                                                               w.would_drop.get(va, {}).get(ex)) for va in vas)):
                continue
            for va in vas:
                if os.environ.get('VBDEC_NOEXCLUDE'):
                    w.would_exclude[va] = ex.label
                    continue
                w.forced_exclusions[va] = ex
                w.proc_excluded[va] = ex
            break
    # second pass with knowledge of excluded procedures and of the function result types; repeated while
    # procedure signatures still change (a caller emitted before its callee saw the callee's old types)
    for rnd in range(4):
        w.refine_return_types()
        before = (repr(sorted(w.proc_param_types.items())),
                  repr(sorted((va, s.get('rettype')) for va, s in dc.signatures.items())))
        w.excluded_writes = {}
        w.normal_writes = set()
        for va in order:
            p = pr.procs[va]
            L = lifted.get(va)
            if L is None or (va in w.proc_excluded and va not in w.forced_exclusions):
                continue
            try:
                L2 = dc.lift(p)
            except Exception as e:
                dc.warnings.append('lift %s: %s' % (dc.proc_name(p), e))
                continue
            texts[va] = safe_proc_text(dc, w, p, L2)
        w.refine_return_types()
        after = (repr(sorted(w.proc_param_types.items())),
                 repr(sorted((va, s.get('rettype')) for va, s in dc.signatures.items())))
        if after == before:
            break
    # names actually used in the emitted code (declare only those)
    used_text = '\n'.join('\n'.join(t.lines) for t in texts.values() if t is not None)
    import re as _re
    used_names = set(_re.findall(r'\b[mg]_[0-9A-F]+\b', used_text))
    files = []
    globals_used = set()
    for k, c in w.global_hints.items():
        globals_used.add(k)
    for k in w.global_sizes:
        globals_used.add(k)
    modules = [o for o in pr.objects if o.kind == 'module']
    for o in pr.objects:
        procs = by_obj.get(o.index, [])
        if o.kind == 'form':
            procs.sort(key=lambda p: (p.method_index if p.method_index is not None else 10 ** 6, p.va))
        else:
            procs.sort(key=lambda p: p.va)
        lines = []
        if o.kind == 'form':
            fname = o.name + '.frm'
            lines += form_header(dc, o, outdir, w)
        else:
            fname = o.name + ('.cls' if o.kind == 'class' else '.bas')
            lines.append('Attribute VB_Name = "%s"' % o.name)
        if dc.option_compare_text:
            lines.append('Option Compare Text')
        lines.append('')
        impl = [c for c in o.controls if dc.is_impl_iface(c)]
        for c in impl:
            lines.append('Implements %s' % c.name)
        if impl:
            lines.append('')
        if o is (modules[0] if modules else pr.objects[0]):
            for d in pr.declares:
                lines.append(declare_line(d))
            if pr.declares:
                lines.append('')
            gd = [d for d in w.global_decls(o, globals_used) if d.split()[1] in used_names]
            if gd:
                lines.append("' Module-level variables (names were not stored in the executable)")
                lines += gd
                lines.append('')
        if o.kind == 'form':
            fd = [d for d in w.field_decls(o, used_names) if d.split()[1].split('(')[0] in used_names]
            used_types = {d.split(' As ')[1].split()[0] for d in fd if ' As ' in d}
            w.pending_types[o.index] = [x for x in w.pending_types.get(o.index, []) if x[0] in used_types]
            lines += w.type_decls(o)
            if fd:
                lines.append("' Form-level variables (names were not stored in the executable;")
                lines.append("' m_XXX = byte offset of the variable inside the form object)")
                lines += fd
                lines.append('')
        for p in procs:
            t = texts.get(p.va)
            if t is None:
                lines.append("' [decompiler] %s at %X could not be decompiled" % (dc.proc_name(p), p.va))
                lines.append('')
                continue
            lines.append(t.header)
            for name, ty in sorted(t.locals.items()):
                lines.append('    Dim %s As %s' % (name, ty))
            if t.locals:
                lines.append('')
            lines += t.lines
            lines.append(t.footer)
            lines.append('')
        with open(os.path.join(outdir, fname), 'w', encoding='utf-8', newline='\r\n') as f:
            f.write('\n'.join(lines))
        files.append((o, fname))
    write_vbp(dc, files, outdir, project.data.get('vbp_references', []) if project else [])
    write_notes(dc, w, texts, outdir)
    dc.would_exclude = dict(w.would_exclude)      # (VBDEC_NOEXCLUDE) procedures the normal output omits
    dc.omissions = omissions_of(dc, w, texts)
    return files


def omissions_of(dc, w, texts):
    """What the omission rules left out: {'procedures': {name: reason}, 'parts': {name: {reason: number of
    statements}}} - the form a project file records for each program. (With VBDEC_NOEXCLUDE nothing is left
    out, but the same is worked out.)"""
    pr = dc.proj
    procedures, parts = {}, {}
    noexclude = bool(os.environ.get('VBDEC_NOEXCLUDE'))
    for va in sorted(pr.procs):
        name = dc.proc_name(pr.procs[va])
        if noexclude:
            whole, counts = w.would_exclude.get(va), w.would_drop.get(va) or {}
        else:
            t = texts.get(va)
            if t is None:
                continue
            whole, counts = t.omitted, getattr(t, 'excl', None) or {}
        if whole:
            procedures[name] = whole
        elif counts:
            parts[name] = {ex.label: n for ex, n in sorted(counts.items(), key=lambda kv: kv[0].label) if n}
    return {'procedures': procedures, 'parts': {k: v for k, v in parts.items() if v}}


def safe_proc_text(dc, w, p, L):
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


def form_header(dc, o, outdir, w):
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
            reason = (w.project.data.get('omission_reason') if w.project else None) or 'left out by request'
            note = "   ' %s (%s)" % (reason, ', '.join(ex.label for ex in w.exclusions
                                                       if c.name.lower() in ex.controls))
        lines.append('   Begin %s %s%s' % (cls, c.name, note))
        lines.append('   End')
    lines.append('End')
    lines.append('Attribute VB_Name = "%s"' % o.name)
    lines.append('Attribute VB_GlobalNameSpace = False')
    lines.append('Attribute VB_Creatable = False')
    lines.append('Attribute VB_PredeclaredId = True')
    lines.append('Attribute VB_Exposed = False')
    return lines


def control_class(dc, c):
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
    return 'Unknown.Control'


def write_vbp(dc, files, outdir, references=()):
    """The .vbp project file. references: extra `Reference=` lines (type libraries the program uses, from the
    project file)."""
    pr = dc.proj
    lines = ['Type=Exe']
    lines.append('Reference=*\\G{00020430-0000-0000-C000-000000000046}#2.0#0#stdole2.tlb#OLE Automation')
    lines += list(references)
    for o, f in files:
        if o.kind == 'form':
            lines.append('Form=%s' % f)
        elif o.kind == 'module':
            lines.append('Module=%s; %s' % (o.name, f))
        else:
            lines.append('Class=%s; %s' % (o.name, f))
    forms = [o for o, f in files if o.kind == 'form']
    lines.append('Startup="%s"' % (forms[0].name if forms else 'Sub Main'))
    lines.append('ExeName32="%s.exe"' % (pr.exe_name or pr.name))
    lines.append('Name="%s"' % pr.name)
    if pr.description:
        lines.append('Description="%s"' % pr.description)
    lines.append('CompilationType=0')
    with open(os.path.join(outdir, pr.name + '.vbp'), 'w', encoding='utf-8', newline='\r\n') as f:
        f.write('\n'.join(lines) + '\n')


def write_notes(dc, w, texts, outdir):
    pr = dc.proj
    lines = ['Decompiled from %s' % (NOTES.get('label') or os.path.basename(pr.im.path or 'image')),
             'Project: %s   EXE name: %s   Native code: %s' % (pr.name, pr.exe_name, pr.is_native),
             '',
             'This source was reconstructed from compiled (native x86) VB6 code by vb-native-decompiler.',
             'Local/module variable and private procedure names are not stored in VB6 executables, so they',
             'are named after their location: var_XX (stack offset), m_XXX (form object offset),',
             'g_XXXX (global address), Proc_XXXXXX (code address), pN (parameters). Event handlers, control',
             'names, string literals and DirectX/VB member names are original.',
             'Other generated names: rXX = a value the compiler kept in a CPU register (rBX, rSI...);',
             'i, j, k = such register loop counters; idx, idx2 = an array index recovered from a loop that',
             'walked the array with a pointer; var_Scratch / var_1000X = a local the compiler stored in an',
             'unused parameter slot; tmpN = an intermediate result; m_XXX(i).fNN = field at byte offset NN',
             'of a record (Type) array element.',
             '']
    if w.exclusions:
        lines.append('Left out by request (stubbed or replaced by a comment):')
        lines += ['  * %s code' % ex.label for ex in w.exclusions]
        lines += ['  ' + x for x in (w.project.data.get('omission_note', []) if w.project else [])]
        lines.append('')
    co = dc.compile_options()
    lines += ['Compile options visible in the machine code (they change what the program does):',
              '  * integer overflow checks: %s' % ('on' if co['overflow_checks'] else
                                                   'OFF - Integer/Long arithmetic wraps around silently instead '
                                                   'of raising error 6 (Overflow)'),
              '  * array bounds checks: %s' % ('on' if co['bounds_checks'] else
                                               'OFF - an index outside an array reads/writes neighbouring memory '
                                               'instead of raising error 9'),
              '  * floating-point error checks: %s' % ('on' if co['fp_checks'] else
                                                       'OFF - division by zero / overflow give Inf or NaN '
                                                       'instead of errors 11 / 6'),
              '']
    if NOTES.get('extra'):
        lines += [NOTES['extra'], '']
    om = [(dc.proc_name(pr.procs[va]), t.omitted) for va, t in texts.items() if t.omitted]
    if om:
        lines.append('Procedures omitted entirely:')
        for n, why in sorted(om):
            lines.append('  %s  (%s)' % (n, why))
        lines.append('')
    if dc.warnings:
        lines.append('Decompiler warnings:')
        lines += ['  ' + x for x in dc.warnings[:200]]
    with open(os.path.join(outdir, 'DECOMPILER_NOTES.txt'), 'w', encoding='utf-8', newline='\r\n') as f:
        f.write('\n'.join(lines) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('exe')
    ap.add_argument('outdir')
    ap.add_argument('--debug', action='store_true')
    ap.add_argument('--image', help='in-memory image dump (e.g. from molebox_extract.py) to decompile instead')
    ap.add_argument('--base', default='400000', help='load address of --image (hex)')
    ap.add_argument('--label', help='source description for DECOMPILER_NOTES.txt')
    ap.add_argument('--note', help='extra paragraph for DECOMPILER_NOTES.txt')
    ap.add_argument('--project', help='project file (or its app name in the library, apps/): omission rules and records')
    ap.add_argument('--game', help="the program's name in the project file (for its omission record)")
    ap.add_argument('--record-omissions', action='store_true',
                    help='store what was left out as the record in the project file')
    a = ap.parse_args()
    NOTES['label'] = a.label
    NOTES['extra'] = a.note
    import project as projects
    proj = projects.find(a.project, a.game) if (a.project or a.game) else None
    if a.image:
        from pe import Image
        img = Image(path=a.image, mapped=open(a.image, 'rb').read(), base=int(a.base, 16))
        dc = Decompiler(image=img, debug=a.debug)
    else:
        dc = Decompiler(a.exe, debug=a.debug)
    lifted = dc.run_all()
    files = project_files(dc, lifted, a.outdir, proj)
    import structure
    print('wrote', [f for o, f in files], 'procs', len(lifted), 'warnings', len(dc.warnings),
          'unplaced statements', sum(n for _, n in structure.LOST))
    for entry, n in structure.LOST:
        print('  structurer left %d statement(s) out in the procedure containing block %x' % (n, entry))
    if dc.rt.unknown:
        print('runtime functions without handler:', dict(dc.rt.unknown))
    if proj is not None and a.game:
        sys.exit(check_omissions(proj, a.game, dc.omissions, a.record_omissions))


def check_omissions(proj, game, actual, record):
    """Compare what was left out with the program's record in the project file. Returns the exit status:
    0 = as recorded (or newly recorded), 3 = different."""
    import project as projects
    n_proc, n_part = len(actual['procedures']), len(actual['parts'])
    summary = '%d procedure(s) left out whole, %d with parts left out' % (n_proc, n_part)
    if record:
        proj.record_omissions(game, actual)
        print('omissions recorded in %s: %s' % (os.path.basename(proj.path), summary))
        return 0
    recorded = proj.recorded_omissions(game)
    if not recorded:
        print('omissions: %s; %s has no record of them yet (store it with --record-omissions)' % (summary, game))
        return 0
    diff = projects.compare_omissions(recorded, actual)
    if not diff:
        print('omissions: as recorded (%s)' % summary)
        return 0
    print('OMISSIONS DIFFER from the record in %s for %s:' % (os.path.basename(proj.path), game))
    for line in diff:
        print('  ' + line)
    print('If this is intended, run again with --record-omissions to store the new state.')
    return 3


if __name__ == '__main__':
    main()
