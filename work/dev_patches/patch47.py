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
"""              patterns=(r'^https?://', r'\\.asp(\\?|$)', r'^&?User(Name|Email|Country|Score|NameVarify)=',
                        r'^&?(GameID|TransID|GameDifficulty|GameLevel)=', r'update_\\w*scores'),""",
"""              patterns=(r'^https?://', r'\\.asp(\\?|$)', r'^&?User(Name|Email|Country|Score|NameVarify)=',
                        r'^&?(GameID|TransID|GameDifficulty|GameLevel)=', r'update_\\w*scores',
                        r'sending score', r'error ?sending', r'check connected', r'say not enough',
                        r'you made it', r'^321troop456$', r'^1998troop2', r'^\\\\config\\.dat$'),"""), (
"""        for k, d in L.vartypes.items():
            for t, n in d.items():
                self.add_hint(k, t, n)
        self.param_types = {}""",
"""        self.param_types = {}
        for k, d in L.vartypes.items():
            for t, n in d.items():
                self.add_hint(k, t, n)""")])
print('patched')
