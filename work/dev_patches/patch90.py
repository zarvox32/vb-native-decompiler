import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


# a parameter handed on unchanged (its pointer) to a ByRef parameter of another procedure is ByRef too
patch('decompiler.py', [(
"""            except Exception as e:
                self.warnings.append('pass2 %s: %s' % (self.proc_name(p), e))
                if self.debug:
                    import traceback
                    traceback.print_exc()
        return results""",
"""            except Exception as e:
                self.warnings.append('pass2 %s: %s' % (self.proc_name(p), e))
                if self.debug:
                    import traceback
                    traceback.print_exc()
        self.propagate_byref(results)
        return results

    def propagate_byref(self, lifted):
        edges = []
        for va, L in lifted.items():
            for stmts in L.block_stmts.values():
                for s in stmts:
                    for e in s.exprs():
                        if e is None:
                            continue
                        for x in e.walk():
                            t = getattr(x, 'proc', None) if isinstance(x, Call) and x.kind == 'proc' else None
                            if t is None:
                                continue
                            first = 1 if t.obj is not None and t.obj.kind in ('form', 'class') else 0
                            for i, a in enumerate(x.args):
                                if isinstance(a, Var) and a.kind == 'arg':
                                    edges.append((va, a.off, t.va, 4 * (i + first)))
        changed = True
        while changed:
            changed = False
            for cva, coff, tva, toff in edges:
                ts = self.signatures.get(tva) or {}
                cs = self.signatures.get(cva)
                if cs is None or not ts.get('byref', {}).get(toff):
                    continue
                if not cs.setdefault('byref', {}).get(coff):
                    cs['byref'][coff] = True
                    sz = ts.get('sizes', {}).get(toff)
                    if sz:
                        cs.setdefault('sizes', {})[coff] = sz
                    changed = True""")])
print('patched')
