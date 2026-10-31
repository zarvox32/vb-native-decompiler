import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# self-check: every statement of every block handed to the structurer must appear in its output
patch('structure.py', [(
"""def structure(blocks, terms, entry, roots, exit_kw='Sub'):
    s = Structurer(blocks, terms, entry, roots)
    ast = s.run()""",
"""LOST = []          # (entry, number of statements the structurer did not place) - a decompiler bug if any


def placed_statements(ast, out):
    for node in ast:
        if node[0] == 'stmt':
            out.add(id(node[1]))
        elif node[0] == 'if':
            placed_statements(node[2], out)
            placed_statements(node[3], out)
        elif node[0] == 'loop':
            placed_statements(node[2], out)
        elif node[0] == 'select':
            for _, body in node[2]:
                placed_statements(body, out)
    return out


def structure(blocks, terms, entry, roots, exit_kw='Sub'):
    s = Structurer(blocks, terms, entry, roots)
    ast = s.run()
    placed = placed_statements(ast, set())
    reach = s.reach(entry, frozenset()) | {r2 for r in roots for r2 in s.reach(r, frozenset())}
    lost = [st for b in reach for st in blocks.get(b, ()) if id(st) not in placed]
    if lost:
        LOST.append((entry, len(lost)))""")])
print('patched')
