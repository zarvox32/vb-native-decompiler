import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('emit.py', [(
"""              label='online score posting', strong=(r'^https?://', r'\\.asp(\\?|$)')),""",
"""              label='online score posting', strong=(r'^https?://', r'\\.asp(\\?|$)'),
              apis=('InternetGetConnectedState',)),"""), (
"""    def __init__(self, controls=(), patterns=(), label='', strong=()):
        self.controls = {c.lower() for c in controls}""",
"""    def __init__(self, controls=(), patterns=(), label='', strong=(), apis=()):
        self.apis = set(apis)
        self.controls = {c.lower() for c in controls}"""), (
"""                    if isinstance(x, Call) and x.kind == 'proc' and getattr(x, 'proc', None) is not None:
                        if self.proc_excluded.get(x.proc.va) is ex:
                            return ex""",
"""                    if isinstance(x, Call) and x.kind == 'proc' and getattr(x, 'proc', None) is not None:
                        if self.proc_excluded.get(x.proc.va) is ex:
                            return ex
                    if isinstance(x, Call) and x.kind == 'api' and getattr(x, 'decl', None) is not None and \\
                            x.decl.func in ex.apis:
                        return ex""")])
print('patched')
