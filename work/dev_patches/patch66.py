import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vbdec'))


def patch(path, pairs):
    s = open(path, encoding='utf-8').read()
    for old, new in pairs:
        if old not in s:
            raise SystemExit('anchor not found in %s:\n%s' % (path, old[:200]))
        s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)


patch('main.py', [(
"""def write_notes(dc, w, texts, outdir):
    pr = dc.proj
    lines = ['Decompiled from %s' % os.path.basename(pr.im.path or 'image'),""",
"""def write_notes(dc, w, texts, outdir):
    pr = dc.proj
    lines = ['Decompiled from %s' % (NOTES.get('label') or os.path.basename(pr.im.path or 'image')),"""), (
"""             'Left out by request (stubbed or replaced by a comment):',
             '  * licensing / registration code',
             '  * online score posting',
             '']""",
"""             'Left out by request (stubbed or replaced by a comment):'] + \\
            ['  * %s code' % ex.label for ex in w.exclusions] + \\
            ['  Variables that only omitted code assigns are marked in their declaration. For example the',
             '  "registered" flag set by the licensing code: treat the game as registered (full version).',
             '']
    if NOTES.get('extra'):
        lines += [NOTES['extra'], '']"""), (
"""def declare_line(d):""",
"""NOTES = {}


def declare_line(d):"""), (
"""    ap.add_argument('--base', default='400000', help='load address of --image (hex)')
    a = ap.parse_args()""",
"""    ap.add_argument('--base', default='400000', help='load address of --image (hex)')
    ap.add_argument('--label', help='source description for DECOMPILER_NOTES.txt')
    ap.add_argument('--note', help='extra paragraph for DECOMPILER_NOTES.txt')
    a = ap.parse_args()
    NOTES['label'] = a.label
    NOTES['extra'] = a.note""")])

p = '../build_sourcecode.py'
s = open(p, encoding='utf-8').read()
old = """    'Classic Troopanum': {'image': os.path.join(WORK, 'classic_box', 'image.bin'),
                          'sounds': [os.path.join(WORK, 'classic_box', 'files')]},"""
new = """    'Classic Troopanum': {'image': os.path.join(WORK, 'classic_box', 'image.bin'),
                          'sounds': [os.path.join(WORK, 'classic_box', 'files')],
                          'label': 'troop.exe (MoleBox-packed; decompiled from its unpacked in-memory image)',
                          'note': "Classic Troopanum's game code is byte-identical to Troopanum 1.6's troop.exe; "
                                  "the 2011 re-release only repackaged it with MoleBox and moved the sounds "
                                  "into cltr02.bsc."},
    'Troopanum 2.0': {'sounds': [os.path.join(WORK, 'tr20_box', 'files')],
                      'note': 'The sounds were packed in tr20.bsc (MoleBox); the code loads them by file name.'},"""
assert old in s
s = s.replace(old, new)
s = s.replace("""    'Troopanum 2.0': {'sounds': [os.path.join(WORK, 'tr20_box', 'files')]},
}""", "}")
s = s.replace("""    if info.get('image'):
        cmd += ['--image', info['image']]""", """    if info.get('image'):
        cmd += ['--image', info['image']]
    cmd += ['--label', info.get('label') or 'troop.exe']
    if info.get('note'):
        cmd += ['--note', info['note']]""")
open(p, 'w', encoding='utf-8').write(s)
print('patched')
