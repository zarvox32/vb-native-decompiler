"""Project settings of the apps in the library, apps/<name>/.

An app's folder holds project/ (project.json, the settings, and README.md, the notes), compiled/ (the
compiled programs), source/ (the published source) and work/ (unpacked images, extracted files).
The project file lists the programs to decompile, where their executables and unpacked images are, what
to leave out of the published source (omission rules), a record of what those rules actually omitted
from each program, and what the checker needs to know (the program's folder, its data files, typical
strings). Paths in it are relative to the app's folder. See documentation.md, section "The library".

Without a project, nothing is omitted.
"""
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                    # the repository's top folder
APPS = os.path.join(ROOT, 'apps')               # the library: one folder per app
PROJECT_FILE = os.path.join('project', 'project.json')     # inside an app's folder


class Project:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.folder = os.path.dirname(os.path.dirname(self.path))     # the app's folder: apps/<name>
        with open(self.path, encoding='utf-8') as f:
            self.data = json.load(f)
        self.name = self.data.get('name', os.path.basename(self.folder))
        self.games = self.data.get('games', {})

    def path_of(self, rel):
        """A path from the project file (relative to the app's folder) made absolute."""
        return rel if os.path.isabs(rel) else os.path.join(self.folder, rel.replace('/', os.sep))

    def game(self, name):
        for g, info in self.games.items():
            if g.lower() == name.lower():
                return g, info
        raise KeyError('%s is not a program of project %s (%s)' % (name, self.name, ', '.join(self.games)))

    def exe_path(self, game):
        g, info = self.game(game)
        return os.path.join(self.path_of(self.data.get('compiled_folder', 'compiled')), info['exe'].replace('/', os.sep))

    def source_folder(self):
        return self.path_of(self.data.get('source_folder', 'source'))

    def image_path(self, game):
        g, info = self.game(game)
        return self.path_of(info['image']) if info.get('image') else None

    def exclusions(self):
        from emit import Exclusion
        return [Exclusion(controls=r.get('controls', ()), patterns=r.get('patterns', ()), label=r['label'],
                          strong=r.get('strong', ()), apis=r.get('apis', ()),
                          whole_module=r.get('whole_module', False))
                for r in self.data.get('omission_rules', [])]

    def recorded_omissions(self, game):
        g, info = self.game(game)
        return info.get('omissions')

    def record_omissions(self, game, omissions):
        """Store what was omitted from this program in the project file."""
        with open(self.path, encoding='utf-8') as f:
            data = json.load(f)         # (re-read: another program's record may have been written meanwhile)
        g, _ = self.game(game)
        data['games'][g]['omissions'] = omissions
        with open(self.path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        self.data = data
        self.games = data['games']


def all_projects():
    return [Project(p) for p in sorted(glob.glob(os.path.join(APPS, '*', PROJECT_FILE)))]


def find(project=None, game=None):
    """The project to use: the one given (an app's name, an app or project folder, or a project file),
    else the one that has this program, else the only one there is."""
    if project:
        p = project if os.path.exists(project) else os.path.join(APPS, project)
        if os.path.isfile(p):
            return Project(p)
        for f in (os.path.join(p, PROJECT_FILE), os.path.join(p, 'project.json')):
            if os.path.isfile(f):
                return Project(f)
        raise SystemExit('no app called %r in %s' % (project, APPS))
    projects = all_projects()
    if game:
        for p in projects:
            if any(g.lower() == game.lower() for g in p.games):
                return p
        raise SystemExit('no app in %s has a program called %r' % (APPS, game))
    if len(projects) == 1:
        return projects[0]
    raise SystemExit('give --project (apps in the library: %s)' %
                     ', '.join(os.path.basename(p.folder) for p in projects))


def compare_omissions(recorded, actual):
    """Differences between the recorded and the actual omissions, as readable lines ([] if they agree).
    recorded / actual: {'procedures': {name: label}, 'parts': {name: {label: statements}}}."""
    out = []
    rp, ap = recorded.get('procedures', {}), actual.get('procedures', {})
    for n in sorted(set(rp) | set(ap)):
        if n not in ap:
            out.append('%s is no longer omitted (the record says: whole procedure, %s)' % (n, rp[n]))
        elif n not in rp:
            out.append('%s is now omitted whole (%s); the record does not have it' % (n, ap[n]))
        elif rp[n] != ap[n]:
            out.append('%s is omitted for a different reason: %s (the record says %s)' % (n, ap[n], rp[n]))
    rq, aq = recorded.get('parts', {}), actual.get('parts', {})
    for n in sorted(set(rq) | set(aq)):
        if n in ap or n in rp:
            continue        # already reported as a whole procedure
        r, a = rq.get(n, {}), aq.get(n, {})
        for label in sorted(set(r) | set(a)):
            if r.get(label, 0) != a.get(label, 0):
                out.append('%s: %d statement(s) left out as %s, the record says %d' %
                           (n, a.get(label, 0), label, r.get(label, 0)))
    return out
