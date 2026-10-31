import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))
from decompiler import Decompiler
from ir import render_stmt
dc = Decompiler(sys.argv[1], debug=False)
va = int(sys.argv[2], 16)
p = dc.proj.procs[va]
L = dc.lift(p)
for b, stmts in L.block_stmts.items():
    for s in stmts:
        for e in s.exprs():
            if e is None:
                continue
            pass
        print(render_stmt(s))
print(dc.analyze_signature(p, L))
