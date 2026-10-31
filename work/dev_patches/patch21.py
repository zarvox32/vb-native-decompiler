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
"""def make_for_loops(ast):""",
"""def hoist_tails(ast):
    \"\"\"Identical trailing statements of both If branches -> after the If (all nesting levels).\"\"\"
    out = []
    for node in ast:
        k = node[0]
        if k == 'if':
            _, c, th, el = node
            th, el = hoist_tails(th), hoist_tails(el)
            tail = []
            while th and el and th[-1][0] == 'stmt' and el[-1][0] == 'stmt' and \\
                    render_stmt(th[-1][1]) == render_stmt(el[-1][1]):
                tail.insert(0, th.pop())
                el.pop()
            if th or el:
                out.append(('if', c, th, el) if th else ('if', negate(c), el, []))
            out.extend(tail)
            continue
        if k in ('do', 'while', 'dountil', 'dowhile_end'):
            out.append(node[:-1] + (hoist_tails(node[-1]),))
            continue
        if k == 'select':
            out.append(('select', node[1], [(v, hoist_tails(b)) for v, b in node[2]]))
            continue
        out.append(node)
    return out


def make_for_loops(ast):"""), (
"""    ast = tidy(ast)
    ast = make_for_loops(ast)""",
"""    ast = tidy(ast)
    ast = hoist_tails(ast)
    ast = make_for_loops(ast)""")])
print('patched')
