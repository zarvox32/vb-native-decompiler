import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))

# report statements the structurer failed to place (should always be 0)
s = open('main.py', encoding='utf-8').read()
old = "    print('wrote', [f for o, f in files], 'procs', len(lifted), 'warnings', len(dc.warnings))"
assert old in s
s = s.replace(old, """    import structure
    print('wrote', [f for o, f in files], 'procs', len(lifted), 'warnings', len(dc.warnings),
          'unplaced statements', sum(n for _, n in structure.LOST))
    for entry, n in structure.LOST:
        print('  structurer left %d statement(s) out in the procedure containing block %x' % (n, entry))""", 1)
open('main.py', 'w', encoding='utf-8').write(s)
print('patched')
