import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# paths that computed different floating-point values and share the tail that uses them
# (If a Then x = f(...) Else x = g(...) / obj.SetPan CLng(x) merged by the compiler)
patch('x86sem.py', [(
"""            merged = merge_states([self.out_state[p] for p in preds], preds)
            if merged is None or not addr_regs and not any(""",
"""            fpus = [self.out_state[p].fpu for p in preds]
            fpu_diff = bool(fpus[0]) and all(len(f) == len(fpus[0]) for f in fpus) and \\
                any(not same(a, b) for f in fpus[1:] for a, b in zip(f, fpus[0]))
            merged = merge_states([self.out_state[p] for p in preds], preds)
            if merged is None or not addr_regs and not fpu_diff and not any(""")])
print('patched')
