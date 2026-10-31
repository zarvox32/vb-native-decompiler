"""Debug helper: decompile selected procedures of an exe and print them.
usage: python -I testproc.py <exe> <hexaddr|all> [--raw] [--time]"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from decompiler import Decompiler  # noqa: E402
from emit import ProjectWriter  # noqa: E402
from ir import render_stmt, render, NAMER  # noqa: E402


def main():
    exe = sys.argv[1]
    which = sys.argv[2]
    raw = '--raw' in sys.argv
    if exe.endswith('.bin'):        # in-memory dump from molebox_extract.py
        from pe import Image
        dc = Decompiler(image=Image(path=exe, mapped=open(exe, 'rb').read(), base=0x400000), debug=True)
    else:
        dc = Decompiler(exe, debug=True)
    t0 = time.time()
    procs = sorted(dc.proj.procs) if which == 'all' else [int(x, 16) for x in which.split(',')]
    # pass 1 for signatures of everything (needed for calls), timed
    times = []
    for va in sorted(dc.proj.procs):
        p = dc.proj.procs[va]
        t = time.time()
        L = dc.lift(p)
        dc.signatures[va] = dc.analyze_signature(p, L)
        times.append((time.time() - t, va, len(L.cfg.blocks)))
    dc.solve_method_bases()
    dc.pass_no = 2
    if '--time' in sys.argv:
        times.sort(reverse=True)
        print('pass1 total %.1fs; slowest:' % sum(t for t, _, _ in times))
        for t, va, nb in times[:8]:
            print('  %x %.2fs blocks=%d' % (va, t, nb))
    w = ProjectWriter(dc, {})
    w.cur_records = {}
    for va in procs:
        p = dc.proj.procs[va]
        t = time.time()
        L = dc.lift(p)
        if raw:
            NAMER['fn'] = None
            for b in sorted(L.block_stmts):
                print('-- block %x term=%s' % (b, L.block_term[b][:2] if L.block_term[b][0] != 'cond' else
                                               ('cond', render(L.block_term[b][1]), hex(L.block_term[b][2]),
                                                hex(L.block_term[b][3]))))
                for s in L.block_stmts[b]:
                    print('    %x  %s' % (s.addr, render_stmt(s)))
        out = w.proc_text(p, L)
        print(out.header)
        for n, ty in sorted(out.locals.items()):
            print('    Dim %s As %s' % (n, ty))
        print('\n'.join(out.lines))
        print(out.footer)
        print("' (%.2fs)" % (time.time() - t))
        if L.warnings:
            print("' warnings:", L.warnings[:20])
    print('method bases', {k: hex(v) for k, v in dc.method_base.items()})


main()
