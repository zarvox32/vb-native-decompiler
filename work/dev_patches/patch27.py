import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# phi assignments on edges (split critical edges with synthetic blocks)
patch('x86sem.py', [(
"""        from simplify import rewrite
        for p, assigns in pending.items():
            t = self.block_term.get(p)
            if t and t[0] in ('cond', 'switch'):
                cond = t[1]
                # the branch condition is evaluated after these assignments: express it with the new
                # values (x+1 -> x) and snapshot anything that still reads an overwritten variable
                for s in assigns:
                    k = ekey(s.src)
                    cond = rewrite(cond, lambda e, k=k, v=s.dst: v if not isinstance(e, Var) and ekey(e) == k else None)
                written = {ekey(s.dst) for s in assigns}
                if any(isinstance(x, Var) and ekey(x) in written for x in cond.walk()):
                    tmp = self.new_temp('Boolean')
                    self.block_stmts[p].append(Assign(tmp, t[1]))
                    cond = tmp
                self.block_term[p] = (t[0], cond) + t[2:]
            self.block_stmts[p].extend(assigns)""",
"""        for p, assigns in pending.items():
            t = self.block_term.get(p)
            if not t or t[0] in ('goto', 'ret', 'stop'):
                self.block_stmts[p].extend(assigns)
                continue
            # p has several successors: the copies belong on the edge(s) into each phi block
            by_target = {}
            for s in assigns:
                tgt = int(s.dst.off.split('@')[1], 16)
                by_target.setdefault(tgt, []).append(s)
            new_t = list(t)
            for tgt, lst in by_target.items():
                eid = self.new_edge_block(lst, tgt)
                if t[0] == 'cond':
                    if new_t[2] == tgt:
                        new_t[2] = eid
                    if new_t[3] == tgt:
                        new_t[3] = eid
                elif t[0] == 'switch':
                    new_t[2] = [eid if x == tgt else x for x in new_t[2]]
            self.block_term[p] = tuple(new_t)

    def new_edge_block(self, stmts, target):
        if not hasattr(self, 'edge_blocks'):
            self.edge_blocks = {}
        eid = 0x70000000 + len(self.edge_blocks)
        self.edge_blocks[eid] = target
        self.block_stmts[eid] = list(stmts)
        self.block_term[eid] = ('goto', target)
        return eid""")])

patch('emit.py', [(
"""        cfg = L.cfg
        reach = cfg.reachable()
        blocks = {}
        terms = {}""",
"""        cfg = L.cfg
        reach = set(cfg.reachable()) | set(getattr(L, 'edge_blocks', {}))
        blocks = {}
        terms = {}"""), (
"""        for k, d in L.vartypes.items():
            for t, n in d.items():
                self.add_hint(k, t, n)""",
"""        for k, d in L.vartypes.items():
            for t, n in d.items():
                self.add_hint(k, t, n)
        self.param_types = {}"""), (
"""        elif k[0] == 'v' and k[1] == 'param':
            self.param_types[k[2]] = t""",
"""        elif k[0] == 'v' and k[1] == 'param' and t in TYPE_SET:
            self.param_types[k[2]] = t""")])

# edge blocks are synthetic: the CFG check for error blocks must not drop them
patch('emit.py', [(
"""        for b in reach:
            if b in cfg.error_blocks or b not in L.block_stmts:
                continue""",
"""        for b in reach:
            if (b in cfg.error_blocks and b < 0x70000000) or b not in L.block_stmts:
                continue""")])

# labels for synthetic blocks
patch('structure.py', [(
"""        self.label_name = label_name or (lambda n: 'L_%X' % n)""",
"""        self.label_name = label_name or (lambda n: 'L_%X' % n if n < 0x70000000 else 'L_E%d' % (n - 0x70000000))""")])

# object-typed retvals are Set
patch('x86sem.py', [(
"""            t = self.call_value(c, self.ctx.vb_type(base_t))
            self.store_ptr(retptr, t, self.tlb.type_size(base_t) if base_t in self.tlb.records else
                           {'Variant': 16, 'Double': 8, 'Currency': 8, 'Date': 8, 'Integer': 2, 'Boolean': 2,
                            'Byte': 1}.get(self.ctx.vb_type(base_t), 4), self.ctx.vb_type(base_t))""",
"""            t = self.call_value(c, self.ctx.vb_type(base_t))
            obj_ret = self.is_iface_type(base_t.rstrip('*'))
            self.store_ptr(retptr, t, self.tlb.type_size(base_t) if base_t in self.tlb.records else
                           {'Variant': 16, 'Double': 8, 'Currency': 8, 'Date': 8, 'Integer': 2, 'Boolean': 2,
                            'Byte': 1}.get(self.ctx.vb_type(base_t), 4), self.ctx.vb_type(base_t),
                           kind='set' if obj_ret else 'let')""")])

# (-(a) And -(b)) <> 0  ->  a And b
patch('simplify.py', [(
"""def is_bool_expr(x):
    if isinstance(x, Bin) and x.op in VBCMP:
        return True""",
"""def strip_bool_negs(x):
    if isinstance(x, Un) and x.op == 'neg' and is_bool_expr(x.a):
        return strip_bool_negs(x.a)
    if isinstance(x, Bin) and x.op in ('And', 'Or', 'Xor'):
        return Bin(x.op, strip_bool_negs(x.a), strip_bool_negs(x.b), ty='Boolean')
    return x


def is_bool_expr(x):
    if isinstance(x, Un) and x.op == 'neg':
        return is_bool_expr(x.a)
    if isinstance(x, Bin) and x.op in VBCMP:
        return True"""), (
"""    if isinstance(b, Const) and b.v == 0 and e.op in ('<>', '=') and is_bool_expr(a):
        return a if e.op == '<>' else negate(a)""",
"""    if isinstance(b, Const) and b.v == 0 and e.op in ('<>', '=') and is_bool_expr(a):
        a = strip_bool_negs(a)
        return a if e.op == '<>' else negate(a)""")])
print('patched')
