import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))
from decompiler import Decompiler

class LogSet(set):
    def add(self, x):
        import inspect
        f = inspect.currentframe().f_back
        lf = f.f_locals.get('self')
        print('me slot write in %x at %x %s' % (x, lf.cur.addr, lf.cur.text))
        super().add(x)

dc = Decompiler(sys.argv[1], debug=False)
dc.me_slot_written = LogSet()
dc.run_all() if not len(sys.argv) > 2 else None
