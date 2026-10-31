import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# every parameter slot starts out holding its parameter (known before the first store is seen)
patch('x86sem.py', [(
"""        for va, o in self.ctx.arg_slot_written:
            if va == self.proc.va and not (o == 4 and self.is_method):
                init.mem[o] = (4, self.stack_var(o, 4), None)   # the parameter until overwritten""",
"""        for o in range(4, 4 + (self.proc.ret_n or 0), 4):
            if not (o == 4 and self.is_method):
                init.mem[o] = (4, self.stack_var(o, 4), None)   # the parameter until overwritten""")])
print('patched')
