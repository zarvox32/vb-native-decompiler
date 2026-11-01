"""Regenerate a project's published source tree from its compiled programs.

    python -I build_sourcecode.py [--project NAME] [--code-only | --sounds-only] [--game NAME]
                                  [--out DIR] [--record-omissions]

The project file (apps/<name>/project/project.json; with only one app, --project can be left out) lists the
programs. For each one this writes the decompiled VB6 project into <source folder>/<program>, copies the
documents the project names, and (unless --code-only) copies the program's sound files into a `sounds`
subfolder and sorts them with the project's sound sorter, if it has one. The formats of the copied sounds
(soundinfo.py) are listed at the end of the program's DECOMPILER_NOTES.txt; rebuilding only the code keeps
that list.

Decompiling happens in a temporary folder first. The published code is replaced only when what the
project's omission rules left out matches that program's record in the project file. If it differs, the
differences are listed, the old code stays, and the run ends with an error; --record-omissions accepts
the new state and stores it as the record. (Example: in the Troopanum project, licensing, online score
posting and the config.dat check are left out.)

Packed programs are decompiled from their unpacked image (the project's "image" entry), for example the
MoleBox-packed Classic Troopanum and Troopanum 2.0, whose images molebox_extract.py dumps.
"""
import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, 'vbdec')]   # python -I leaves the script folder off sys.path
import project as projects  # noqa: E402
import soundinfo  # noqa: E402

SOUND_EXT = ('.wma', '.wav', '.mid', '.mp3', '.ogg')
CODE_FILES = ('.frm', '.bas', '.cls', '.vbp')     # what the decompiler writes (plus notes, pictures)
NOTES = 'DECOMPILER_NOTES.txt'
SOUND_HEADING = 'Sound files in the sounds folder'   # starts the last section of the notes


def read_notes(path):
    """(the notes up to the sound files section, that section) of a notes file; '' for a missing part."""
    if not os.path.exists(path):
        return '', ''
    with open(path, encoding='utf-8') as f:
        text = f.read()
    if text.startswith(SOUND_HEADING):
        return '', text
    at = text.find('\n' + SOUND_HEADING)
    if at < 0:
        return text, ''
    return text[:at + 1], text[at + 1:]


def write_notes(path, body, section):
    body = body.rstrip('\n')
    with open(path, 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(body + '\n\n' + section if body else section)


def decompile(proj, game, info, dest, record):
    """Decompile into a temporary folder; on success move the code into dest. Returns True if it did."""
    tmp = tempfile.mkdtemp(prefix='decompile_')
    try:
        cmd = [sys.executable, '-I', os.path.join(HERE, 'vbdec', 'main.py'), proj.exe_path(game), tmp,
               '--project', proj.path, '--game', game]
        if proj.image_path(game):
            cmd += ['--image', proj.image_path(game)]
        cmd += ['--label', info.get('label') or os.path.basename(info['exe'])]
        if info.get('note'):
            cmd += ['--note', info['note']]
        if record:
            cmd.append('--record-omissions')
        print('decompiling', game, flush=True)
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print('  %s: the published code was left as it was' % game)
            return False
        # replace the previous code (but not the documents or the sounds)
        _, sound_section = read_notes(os.path.join(dest, NOTES))
        for f in os.listdir(dest):
            p = os.path.join(dest, f)
            if os.path.isfile(p) and (f.lower().endswith(CODE_FILES) or f == NOTES or '_picture' in f):
                os.remove(p)
        for f in os.listdir(tmp):
            shutil.move(os.path.join(tmp, f), os.path.join(dest, f))
        if sound_section:   # the sounds did not change: keep their list in the new notes
            body, _ = read_notes(os.path.join(dest, NOTES))
            write_notes(os.path.join(dest, NOTES), body, sound_section)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sound_sorter(proj):
    path = proj.data.get('sound_sorter')
    if not path:
        return None
    spec = importlib.util.spec_from_file_location('sound_sorter', proj.path_of(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.sort_sounds


def copy_sounds(proj, game, info, dest):
    sdir = os.path.join(dest, 'sounds')
    sources = [proj.path_of(s) for s in info.get('sounds', [])]
    missing = [src for src in sources if not os.path.isdir(src)]
    if missing:
        print('  WARNING: missing extracted sounds in', ', '.join(missing), '- sounds left as they are')
        return
    shutil.rmtree(sdir, ignore_errors=True)   # generated: start clean so old sorting never lingers
    os.makedirs(sdir)
    origin = {}   # lower-case file name -> where it was copied from
    for s, src in zip(info.get('sounds', []), sources):
        files = os.listdir(src)
        # an archive that stores upper-case names (Troopanum 2.0's GUN1.WAV) while the code asks for
        # lower case (gun1.wav): use lower case, so the files also work on case-sensitive systems
        lower = all(f == f.upper() for f in files)
        for f in files:
            shutil.copy2(os.path.join(src, f), os.path.join(sdir, f.lower() if lower else f))
            origin[f.lower()] = "From %s (extracted from the program's archive)" % s
    # loose sound files shipped next to the program (MIDI music etc.)
    skip = {s.lower() for s in proj.data.get('skip_sound_files', [])}
    exe = proj.exe_path(game)
    gdir = os.path.dirname(exe)
    for f in os.listdir(gdir):
        if f.lower().endswith(SOUND_EXT) and f.lower() not in skip:
            dst = os.path.join(sdir, f)
            if not os.path.exists(dst):
                shutil.copy2(os.path.join(gdir, f), dst)
                origin[f.lower()] = 'Loose, next to %s' % os.path.basename(exe)
    print('  sounds:', len(os.listdir(sdir)), 'files')
    sort = sound_sorter(proj)
    if sort is not None:
        sort(game, sdir)
    note_sound_formats(dest, sdir, origin)


def note_sound_formats(dest, sdir, origin):
    """Write (or replace) the list of the sounds' formats at the end of the program's notes. Files the
    sorter wrote (descriptions) are not in origin and are left out."""
    groups = {}
    for d, _, files in os.walk(sdir):
        for f in files:
            if f.lower() in origin:
                groups.setdefault(origin[f.lower()], []).append(os.path.join(d, f))
    lines = [SOUND_HEADING + ' (formats read from the files by build_sourcecode.py; how the program',
             "plays them, and anything that converts them first, is in the app's notes):"]
    for where in sorted(groups):
        n = len(groups[where])
        lines.append('  %s, %d file%s:' % (where, n, '' if n == 1 else 's'))
        lines += ['  ' + line for line in soundinfo.summary(groups[where], sdir)]
    if not groups:
        lines.append('  none')
    body, _ = read_notes(os.path.join(dest, NOTES))
    write_notes(os.path.join(dest, NOTES), body, '\n'.join(lines) + '\n')


def copy_docs(proj, game, dest):
    gdir = os.path.dirname(proj.exe_path(game))
    for f in proj.data.get('documents', []):
        p = os.path.join(gdir, f)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(dest, f))


def main():
    ap = argparse.ArgumentParser()
    only = ap.add_mutually_exclusive_group()
    only.add_argument('--code-only', action='store_true')
    only.add_argument('--sounds-only', action='store_true')
    ap.add_argument('--project', help='app name in the library (apps/), or a project file')
    ap.add_argument('--game', help='only this program')
    ap.add_argument('--out', help="output folder (default: the project's source folder)")
    ap.add_argument('--record-omissions', action='store_true',
                    help='accept what the omission rules left out now and store it as the record')
    a = ap.parse_args()
    proj = projects.find(a.project, a.game)
    out = a.out or proj.source_folder()
    failed = []
    for game, info in proj.games.items():
        if a.game and a.game.lower() != game.lower():
            continue
        dest = os.path.join(out, game)
        os.makedirs(dest, exist_ok=True)
        if not a.sounds_only:
            if decompile(proj, game, info, dest, a.record_omissions):
                copy_docs(proj, game, dest)
            else:
                failed.append(game)
        if not a.code_only:
            print('sounds', game)
            copy_sounds(proj, game, info, dest)
    if failed:
        print('NOT UPDATED (see the messages above):', ', '.join(failed))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
