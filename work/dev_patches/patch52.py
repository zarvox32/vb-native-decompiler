import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# 1. DirectInput key names for DIKEYBOARDSTATE.key(n)
patch('emit.py', [(
"""                if t.startswith('CArray:'):
                    _, elem, n, lb = t.split(':')
                    es = self.tlb.type_size(elem)
                    k, r = divmod(rel, es)
                    if k < int(n) and r == 0:
                        return '%s(%d)' % (name, k + int(lb))""",
"""                if t.startswith('CArray:'):
                    _, elem, n, lb = t.split(':')
                    es = self.tlb.type_size(elem)
                    k, r = divmod(rel, es)
                    if k < int(n) and r == 0:
                        if rec == 'DIKEYBOARDSTATE':
                            nm = self.tlb.enum_name('CONST_DIKEYFLAGS', k + int(lb))
                            if nm:
                                return '%s(%s)' % (name, nm)
                        return '%s(%d)' % (name, k + int(lb))""")])

# 2. De Morgan when both sides are boolean conditions
patch('lift.py', [(
"""    if isinstance(c, Bin) and c.op in ('And', 'Or') and c.ty == 'Cond':
        return Bin('Or' if c.op == 'And' else 'And', negate(c.a), negate(c.b), ty='Cond')
    return Un('Not', c, ty='Boolean')""",
"""    if isinstance(c, Bin) and c.op in ('And', 'Or') and (c.ty == 'Cond' or (is_condition(c.a) and is_condition(c.b))):
        return Bin('Or' if c.op == 'And' else 'And', negate(c.a), negate(c.b), ty=c.ty or 'Boolean')
    return Un('Not', c, ty='Boolean')


def is_condition(c):
    \"\"\"A pure truth value (comparison / Not / And-Or of those): safe for De Morgan.\"\"\"
    if isinstance(c, Bin) and c.op in VBCMP:
        return True
    if isinstance(c, Un) and c.op == 'Not' and is_condition(c.a):
        return True
    if isinstance(c, Bin) and c.op in ('And', 'Or'):
        return is_condition(c.a) and is_condition(c.b)
    return False"""), (
"""def negate(c):
    \"\"\"Logical negation of a condition expression.\"\"\"""",
"""VBCMP = {'=', '<>', '<', '<=', '>', '>='}


def negate(c):
    \"\"\"Logical negation of a condition expression.\"\"\"""")])

# 3. register loop counters get conventional names
patch('emit.py', [(
"""        out.lines = lines
        out.locals = self.local_decls(p, L, blocks, terms, fname)
        return out""",
"""        out.lines = lines
        out.locals = self.local_decls(p, L, blocks, terms, fname)
        self.rename_loop_registers(out)
        return out

    def rename_loop_registers(self, out):
        \"\"\"For-loop counters that only lived in a CPU register (rBX...) -> i, j, k...\"\"\"
        text = '\\n'.join(out.lines)
        regs = []
        for m in re.finditer(r'^\\s*For (r[A-Z]{2}\\d*) = ', text, re.M):
            if m.group(1) not in regs:
                regs.append(m.group(1))
        if not regs:
            return
        taken = set(re.findall(r'\\b[A-Za-z_]\\w*\\b', text)) | set(out.locals)
        pool = [n for n in ('i', 'j', 'k', 'n', 'm', 'q') if n not in taken]
        mapping = {}
        for r in regs:
            if not pool:
                break
            mapping[r] = pool.pop(0)
        if not mapping:
            return
        pat = re.compile(r'\\b(%s)\\b' % '|'.join(map(re.escape, mapping)))
        out.lines = [pat.sub(lambda m: mapping[m.group(1)], l) for l in out.lines]
        out.locals = {mapping.get(k, k): v for k, v in out.locals.items()}""")])
print('patched')
