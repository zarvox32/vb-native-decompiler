import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))
from decompiler import Decompiler
from ir import Var, Deref
dc = Decompiler(sys.argv[1], debug=False)
va = int(sys.argv[2], 16)
L = dc.lift(dc.proj.procs[va])
for b, stmts in L.block_stmts.items():
    for s in stmts:
        for e in s.exprs():
            if e is None: continue
            for x in e.walk():
                if isinstance(x, (Deref,)) or (isinstance(x, Var) and x.kind == 'arg'):
                    print(type(x).__name__, getattr(x, 'ty', None), getattr(x, 'size', None))
