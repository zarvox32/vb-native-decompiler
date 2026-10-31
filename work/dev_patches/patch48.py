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
"""                        r'sending score', r'error ?sending', r'check connected', r'say not enough',
                        r'you made it', r'^321troop456$', r'^1998troop2', r'^\\\\config\\.dat$'),
              label='online score posting'),
]""",
"""                        r'sending score', r'error ?sending', r'check connected', r'say not enough',
                        r'you made it', r'^321troop456$', r'^1998troop2'),
              label='online score posting', strong=(r'^https?://', r'\\.asp(\\?|$)')),
    Exclusion(controls=(),
              patterns=(r'^\\\\config\\.dat$', r'say config first'),
              label='configuration (config.dat)'),
]"""), (
"""    def __init__(self, controls=(), patterns=(), label=''):
        self.controls = {c.lower() for c in controls}
        self.patterns = [re.compile(p, re.I) for p in patterns]
        self.label = label""",
"""    def __init__(self, controls=(), patterns=(), label='', strong=()):
        self.controls = {c.lower() for c in controls}
        self.patterns = [re.compile(p, re.I) for p in patterns]
        self.strong = [re.compile(p, re.I) for p in strong]
        self.label = label"""), (
"""        total = 0
        dropped = Counter()
        for b, stmts in blocks.items():
            out = []
            for s in stmts:
                total += 1
                ex = self.excluded_by(s)""",
"""        total = 0
        dropped = Counter()
        for b, stmts in blocks.items():
            for s in stmts:
                for e in iter_exprs_in(s):
                    for x in e.walk():
                        for ex in self.exclusions:
                            # a procedure that builds a score URL / uses the excluded control is dedicated to it
                            if isinstance(x, Str) and any(p.search(x.s) for p in ex.strong):
                                return ex
                            if ex.strong and (getattr(x, 'control', None) or '').lower() in ex.controls:
                                return ex
        for b, stmts in blocks.items():
            out = []
            for s in stmts:
                total += 1
                ex = self.excluded_by(s)""")])
print('patched')
