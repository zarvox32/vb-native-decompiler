"""Troopanum's sound sorter: sorts a game's sounds into folders by what they are used for, with a
_description.txt in each folder.

    python -I apps/troopanum/project/sounds.py [FOLDER]   # re-sort every FOLDER/<game>/sounds
                                                          # (default: the app's source folder)

build_sourcecode.py calls sort_sounds(game, folder) after copying the sounds: project.json names this
file as the project's "sound_sorter". The categories come from reading the
decompiled code: where each file is loaded and when it is played. Licensing, online scores and the
config.dat check are left out of the decompiled code, but the sounds they play are still sorted
(scores online, warning). Files the game never plays go to "unused", which gets no description.

Names are file names without the extension: the same sound can be .wav, .wma or .mid depending on
the version. Classic Troopanum, 1.5, 1.6 and SP 1.6 share one layout (same code, same file names);
Troopanum 2.0 has its own.
"""
import json
import os
import shutil
import sys

UNUSED = 'unused'
DESCRIPTION = '_description.txt'   # the underscore keeps it at the top of the folder

# A layout is a list of folders: (folder name, description header lines, entries).
# An entry is (name, meaning) or ((first, ..., last), meaning); the tuple form is written as one
# "first to last = meaning" line when every file is there.

SHIP_NAMES = ('Ship names from the Classic Troopanum manual: varian = normal ship, zeneeth = rising ship, '
              'tarster = multishot ship (takes more than one hit), '
              'star gazer = moving ship (jumps to a new spot now and then).')

NUMBERS = [
    (tuple(str(n) for n in range(21)), 'zero to twenty'),
    (tuple(str(n) for n in range(30, 100, 10)), 'thirty, forty and so on up to ninety'),
    ('hundred', 'hundred'),
    ('thousand', 'thousand'),
    ('million', 'million'),
]

SCORES_ONLINE = ('scores online', [
    'Posting your score to the online top 10 (the original score server no longer exists).',
], [
    ('check connected', 'make sure you are online, then press Y to send or N to cancel'),
    ('sending score', 'sending your score'),
    ('error sending', 'the score could not be sent'),
    ('you made it', 'your score made the top 10 (your place is spoken after it)'),
    ('say not enough', 'no score to send, or your score is too low for the top 10'),
])

TEST_SPEAKERS = ('test speakers', [
    'Speaker test from the main menu: checks that left and right are not swapped.',
], [
    ('left speaker sound', 'plays in the left speaker'),
    ('right speaker sound', 'plays in the right speaker'),
    ('ask correct speakers', 'asks if you heard left first, then right (Y or N)'),
    ('speaker setting saved', 'you answered N, so left and right will be swapped from now on'),
])

WARNING = ('warning', [
    'Error and warning messages.',
], [
    ('say fatal', 'fatal error at startup (DirectX could not start)'),
    ('say config first', 'run the configuration program first (the port does not need this)'),
    ('register', 'this part needs the registered version (the port treats the game as registered)'),
])

CLASSIC = [
    ('ambiance', [
        'Background messages: during a level, two random messages play, each in the left or right speaker.',
        'They can be turned off in the options menu.',
    ], [
        (tuple('message%d' % n for n in range(1, 11)), 'background messages'),
    ]),
    ('announcement', [
        'Spoken game information. The numbers come from the number folder.',
    ], [
        ('say wave', '"wave", followed by the wave number (shift key, and between levels)'),
        ('say bonus round', '"bonus round", said instead of the wave number on the bonus level'),
        ('say bonus level', 'the bonus level is starting'),
        ('say points', '"points", said after your score'),
        ('say life', '"life", when 1 life is left (enter key)'),
        ('say lives', '"lives"'),
        ('say ship landed', '"ship landed", when 1 ship has landed (enter key)'),
        ('say ships landed', '"ships landed"'),
        ('say game over', 'game over'),
        ('say you win', 'you beat the game'),
        ('say final score', '"final score", followed by your score'),
    ]),
    ('enemy', [
        'Enemy ships, the meteors and the boss.',
        SHIP_NAMES,
    ], [
        ('ship1', 'varian falling'),
        ('ship2', 'zeneeth rising'),
        ('ship3', 'tarster falling'),
        ('ship4', 'star gazer falling'),
        ('ship99', 'meteor falling (bonus level)'),
        ('boss drop', 'boss falling (levels 4, 8 and 12)'),
        ('enemy hit1', 'varian die'),
        ('enemy hit2', 'zeneeth die'),
        ('enemy hit3', 'tarster die'),
        ('enemy hit4', 'star gazer die'),
        ('enemy hit99', 'meteor die'),
        ('boss hit', 'boss knocked off the screen (it comes back until you beat it)'),
        ('not dead', 'enemy or boss hit, but not destroyed yet'),
        ('land', 'an enemy landed on your base'),
        ('gone', 'a zeneeth escaped off the top (you lose points)'),
        ('ship centered', 'beep: an enemy is in your line of fire'),
    ]),
    ('game', [
        'Your gun, moving, and changing levels.',
    ], [
        ('gun1', 'laser shot'),
        ('bounce', 'you hit the edge of the screen'),
        ('hyperbounce', 'you hit the edge with hyperactive sides on and bounced somewhere else'),
        ('next level', 'warp to the next level'),
        ('prior level', 'warp back to the previous level (portal)'),
        ('user dies', 'your base is destroyed, you lose a life'),
    ]),
    ('item', [
        'Special items: shoot them as they fall, then use them.',
    ], [
        ('item drop', 'an item falling'),
        ('item centered', 'beep: an item is in your line of fire'),
        ('item shoot', 'you shot an item and got it'),
        ('item cancel', 'item put away with the spacebar, back to your normal laser'),
        ('no more', 'you have no items to use'),
        ('super laser', 'super laser shot'),
        ('smart bomb', 'smart bomb blast'),
        ('portal', 'portal used, warps you to the next or previous level'),
        ('say super laser', 'voice: super laser (when you get it or pick it)'),
        ('say smart bomb', 'voice: smart bomb'),
        ('say portal', 'voice: portal'),
    ]),
    ('learn', [
        'Learn game sounds menu: each item plays a voice explaining a sound, then the sound itself.',
        'The demo files are only used here; the other sounds it plays are in their own folders.',
    ], [
        ('say ship1 demo', 'explains ship1 demo (varian)'),
        ('ship1 demo', 'varian falling, start to finish'),
        ('say enemy hit1', 'explains enemy hit1 (varian die)'),
        ('say ship2 demo', 'explains ship2 demo (zeneeth)'),
        ('ship2 demo', 'zeneeth rising, start to finish'),
        ('say enemy hit2', 'explains enemy hit2 (zeneeth die)'),
        ('say gone', 'explains gone (zeneeth escaped)'),
        ('say ship3 demo', 'explains ship3 demo (tarster)'),
        ('ship3 demo', 'tarster falling, start to finish'),
        ('say not dead', 'explains not dead'),
        ('say enemy hit3', 'explains enemy hit3 (tarster die)'),
        ('say ship4 demo', 'explains ship4 demo (star gazer)'),
        ('ship4 demo', 'star gazer falling, start to finish'),
        ('say enemy hit4', 'explains enemy hit4 (star gazer die)'),
        ('say land', 'explains land'),
        ('say next level', 'explains next level'),
        ('say prior level', 'explains prior level'),
        ('say bounce', 'explains bounce'),
        ('say ship centered', 'explains ship centered'),
        ('say item drop', 'explains item drop'),
        ('say item shoot', 'explains item shoot'),
        ('say item cancel', 'explains item cancel'),
        ('say no more', 'explains no more'),
        ('say item centered', 'explains item centered'),
        ('say super laser menu', 'explains the super laser'),
        ('say smart bomb menu', 'explains the smart bomb'),
        ('say portal menu', 'explains the portal'),
    ]),
    ('menu', [
        'Spoken menus and options. The "arrows" files are the help spoken when you enter a menu.',
    ], [
        ('game intro', 'title intro when the game starts'),
        ('arrows', 'main menu help'),
        ('start game', 'main menu: start game'),
        ('speed', 'main menu: speed round'),
        ('options', 'main menu: options'),
        ('learn sounds', 'main menu: learn game sounds'),
        ('view top 10', 'main menu: view top 10 scores'),
        ('send score', 'main menu: send score online'),
        ('test speakers', 'main menu: test speakers'),
        ('quit', 'main menu: quit'),
        ('thanks', 'goodbye when you quit'),
        ('difficulty arrows', 'difficulty menu help'),
        ('easy', 'easy'),
        ('normal', 'normal'),
        ('hard', 'insane, the hardest difficulty'),
        ('return to menu', 'return to the main menu'),
        ('options arrows', 'options menu help'),
        ('hyper', 'option: hyperactive sides'),
        ('ambiance', 'option: background messages'),
        ('on', 'on'),
        ('off', 'off'),
        ('sound arrows', 'learn sounds menu help'),
        ('score arrows', 'top 10 menu help'),
    ]),
    ('music', [
        'Music. The intro, intermission, you win and game over tunes are MIDI files.',
    ], [
        ('menu background', 'menu music'),
        ('intro', 'when a game starts'),
        ('background1', 'levels 1 and 2, and the speed round'),
        ('background1a', 'levels 3 and 4'),
        ('background2', 'levels 5 and 6'),
        ('background2a', 'levels 7 and 8'),
        ('background3', 'levels 9 and 10'),
        ('background3a', 'levels 11 and 12'),
        ('background bonus', 'bonus level'),
        ('intermission5', 'before level 5'),
        ('intermission9', 'before level 9'),
        ('you win', 'you beat the game'),
        ('game over', 'game over'),
    ]),
    ('number', [
        'Spoken numbers for the score, wave, lives and landed ships.',
    ], NUMBERS),
    SCORES_ONLINE,
    TEST_SPEAKERS,
    WARNING,
]

TR20 = [
    ('ambiance', [
        'Background messages: during a level, two random messages play, each in the left or right speaker.',
        'They can be turned off in the program settings. Gorbian levels have none.',
    ], [
        (tuple('message%d' % n for n in range(1, 11)), 'background messages'),
    ]),
    ('announcement', [
        'Spoken game information. The numbers come from the number folder.',
    ], [
        ('say wave', '"wave", followed by the wave number (shift key, and between levels)'),
        ('say bonus round', '"bonus round", said instead of the wave number on a bonus round'),
        ('say points', '"points", said after your score'),
        ('say life', '"life", when 1 life is left (enter key)'),
        ('say lives', '"lives"'),
        ('say ship landed', '"ship landed", when 1 ship has landed (enter key)'),
        ('say ships landed', '"ships landed"'),
        ('extra life', 'you got an extra life'),
        ('reverse', 'insane only: the left and right arrows are swapped, or back to normal'),
        ('say game over', 'game over'),
        ('say you win', 'you beat the game'),
        ('say final score', '"final score", followed by your score'),
    ]),
    ('bonus round', [
        'The bonus rounds: the landing ship round after level 4, the ship barrage round after level 8,',
        'and a hidden meteorite round (type D A N in the main menu).',
    ], [
        ('say lander level', 'the landing ship round is starting'),
        ('lander ship', 'your landing ship; it gets lower in pitch as you come down'),
        ('thrust', 'your thruster (arrow keys)'),
        ('lander wind', 'the wind pushing you; where and how loud it is tells its direction and strength'),
        ('lander wall', 'you bounced off a wall'),
        ('lander crash', 'you crashed'),
        ('lander win', 'you landed safely'),
        ('barrage1', 'ship barrage intro; the number of sets you need is spoken after it'),
        ('barrage2', 'end of the intro on easy: the prize is a special item'),
        ('barrage3', 'end of the intro on normal and insane: the prize is an extra life'),
        ('set complete', 'you finished a set of ships; the next set number is spoken after it'),
        ('say meteorite level', 'the meteorite round is starting'),
        ('ship0', 'meteor falling'),
        ('meteor hit', 'meteor die'),
    ]),
    ('enemy', [
        'Enemy ships on normal levels, and the bomber.',
    ], [
        ('ship1', 'cruiser falling'),
        ('ship2', 'lunar lifter rising'),
        ('ship3', 'enforcer falling (takes more than one hit)'),
        ('ship4', 'hover craft falling (jumps to a new spot now and then)'),
        ('ship5', 'liquidator falling (sways left and right)'),
        ('enemy hit1', 'cruiser die'),
        ('enemy hit2', 'lunar lifter die'),
        ('enemy hit3', 'enforcer die'),
        ('enemy hit4', 'hover craft die'),
        ('enemy hit5', 'liquidator die'),
        ('not dead', 'enemy hit, but not destroyed yet'),
        ('land', 'an enemy landed on your base'),
        ('gone', 'a lunar lifter escaped off the top'),
        ('ship2 wave', 'the escaped lunar lifter called in a wave of ships'),
        ('ship2 steal', 'the escaped lunar lifter stole one of your items'),
        ('bomber drop', 'bomber falling'),
        ('bomber not dead', 'bomber hit; the pitch goes up with each hit (also used for hits on Lord Vector)'),
        ('bomber dead', 'bomber die'),
        ('ship centered', 'beep: an enemy is in your line of fire'),
    ]),
    ('game', [
        'Your gun, moving, and changing levels.',
    ], [
        ('gun1', 'laser shot'),
        ('bounce', 'you hit the edge of the screen'),
        ('hyperbounce', 'you hit the edge with hyperactive sides on and bounced somewhere else'),
        ('warn center', 'center beep: you passed the middle of the screen (the L key turns it on or off)'),
        ('locate', 'shows where you are on the board (insert key)'),
        ('life alarm', 'one more landing will destroy your base'),
        ('user dies', 'your base is destroyed, you lose a life'),
        ('next level', 'warp to the next level'),
        ('prior level', 'warp back to the previous level (portal)'),
        ('confirm exit', 'escape pressed during a game: enter quits, any other key keeps playing'),
    ]),
    ('gorb', [
        'Gorbian ships. On gorbian levels you move a target: left and right to line up, then up and down to aim.',
    ], [
        ('warn gorb', 'a gorbian level is starting'),
        ('gorb1', 'gorbian cruiser falling'),
        ('gorb2', 'gorbian pod rising'),
        ('gorb3', 'gorbian infiltrator falling'),
        ('gorb4', 'gorbian destroyer falling'),
        ('gorb5', 'gorbian fleet ship falling'),
        ('gorb hit', 'gorbian ship die'),
        ('target0', 'slow target beep: not lined up with anything'),
        ('target1', 'faster target beep: lined up left and right, now aim up or down'),
        ('target2', 'rapid target beep: on target, fire'),
        ('target wall', 'your target hit the top or bottom edge'),
    ]),
    ('item', [
        'Special items: shoot them as they fall, then use them (page up and page down, or the number keys).',
    ], [
        ('item drop', 'an item falling'),
        ('item centered', 'beep: an item is in your line of fire'),
        ('item shoot', 'you shot an item and got it'),
        ('item cancel', 'item put away with the spacebar, back to your normal laser'),
        ('no more', 'you do not have that item, or no items at all'),
        ('super laser', 'super laser shot'),
        ('smart bomb', 'smart bomb blast'),
        ('portal', 'portal used, warps you to the next or previous level'),
        ('nuke', 'ground nuke used, clears all landed ships'),
        ('rifle', 'rifle shot'),
        ('shield', 'moon rock shield turned on'),
        ('shield active', 'moon rock shield running'),
        ('reinforcer', 'base reinforced: more ships can land before you lose a life'),
        ('say super laser', 'voice: super laser (when you get it, pick it, or list your items with I)'),
        ('say smart bomb', 'voice: smart bomb'),
        ('say portal', 'voice: portal'),
        ('say nuke', 'voice: ground nuke'),
        ('say rifle', 'voice: rifle'),
        ('say shield', 'voice: moon rock shield'),
    ]),
    ('learn', [
        'Learn game sounds menu: each item plays a voice explaining a sound, then the sound itself.',
        'The demo files are only used here; the other sounds it plays are in their own folders.',
    ], [
        ('say ship1 demo', 'explains ship1 demo (cruiser)'),
        ('ship1 demo', 'cruiser falling, start to finish'),
        ('say enemy hit1', 'explains enemy hit1 (cruiser die)'),
        ('say ship2 demo', 'explains ship2 demo (lunar lifter)'),
        ('ship2 demo', 'lunar lifter rising, start to finish'),
        ('say enemy hit2', 'explains enemy hit2 (lunar lifter die)'),
        ('say gone', 'explains gone (lunar lifter escaped)'),
        ('say ship3 demo', 'explains ship3 demo (enforcer)'),
        ('ship3 demo', 'enforcer falling, start to finish'),
        ('say not dead', 'explains not dead'),
        ('say enemy hit3', 'explains enemy hit3 (enforcer die)'),
        ('say ship4 demo', 'explains ship4 demo (hover craft)'),
        ('ship4 demo', 'hover craft falling, start to finish'),
        ('say enemy hit4', 'explains enemy hit4 (hover craft die)'),
        ('say ship5 demo', 'explains ship5 demo (liquidator)'),
        ('ship5 demo', 'liquidator falling, start to finish'),
        ('say enemy hit5', 'explains enemy hit5 (liquidator die)'),
        ('say bomber demo', 'explains bomber demo'),
        ('bomber demo', 'bomber falling and getting hit'),
        ('say bomber dead', 'explains bomber dead'),
        ('say gorb1 demo', 'explains gorb1 demo (gorbian cruiser)'),
        ('gorb1 demo', 'gorbian cruiser'),
        ('say gorb2 demo', 'explains gorb2 demo (gorbian pod)'),
        ('gorb2 demo', 'gorbian pod'),
        ('say gorb3 demo', 'explains gorb3 demo (gorbian infiltrator)'),
        ('gorb3 demo', 'gorbian infiltrator'),
        ('say gorb4 demo', 'explains gorb4 demo (gorbian destroyer)'),
        ('gorb4 demo', 'gorbian destroyer'),
        ('say gorb5 demo', 'explains gorb5 demo (gorbian fleet ship)'),
        ('gorb5 demo', 'gorbian fleet ship'),
        ('say land', 'explains land'),
        ('say ship centered', 'explains ship centered'),
        ('ship centered demo', 'ship centered beep'),
        ('say gorb ship centered', 'explains aiming at a gorbian ship'),
        ('gorb centered demo', 'gorbian target beeps'),
        ('say next level', 'explains next level'),
        ('say prior level', 'explains prior level'),
        ('say bounce', 'explains bounce'),
        ('say item drop', 'explains item drop'),
        ('say item shoot', 'explains item shoot'),
        ('say item cancel', 'explains item cancel'),
        ('say no more', 'explains no more'),
        ('say item centered', 'explains item centered'),
        ('item centered demo', 'item centered beep'),
        ('say super laser menu', 'explains the super laser'),
        ('say smart bomb menu', 'explains the smart bomb'),
        ('say portal menu', 'explains the portal'),
        ('say nuke menu', 'explains the ground nuke'),
        ('say rifle menu', 'explains the rifle'),
        ('say shield menu', 'explains the moon rock shield'),
        ('say rock', 'explains rock (moon rock)'),
        ('say smash rock', 'explains smash rock'),
    ]),
    ('lord vector', [
        'Level 16: Lord Vector, his three energy shields and his bombs.',
    ], [
        ('armor1', 'left energy shield'),
        ('armor2', 'center energy shield'),
        ('armor3', 'right energy shield'),
        ('shield destroy', 'an energy shield destroyed'),
        ('vector3', 'Lord Vector speaks as he arrives, after all shields are down'),
        ('ship9', "Lord Vector's ship"),
        ('ship8', "Lord Vector's bomb falling; if it lands on you, your base is destroyed"),
        ('vector1', 'Lord Vector speaks after you hit him 20 times'),
        ('vector2', 'Lord Vector speaks when 5 more hits will finish him'),
        ('mamma destroy', "Lord Vector's ship destroyed, you win"),
    ]),
    ('menu', [
        'Spoken menus and settings. The "arrows" files are the help spoken when you enter a menu.',
    ], [
        ('game intro', 'title intro when the game starts'),
        ('arrows', 'main menu help'),
        ('start game', 'main menu: start game'),
        ('speed', 'main menu: speed round'),
        ('options', 'main menu: program settings'),
        ('view top 10', 'main menu: view top 10 scores'),
        ('send score', 'main menu: post score online'),
        ('learn sounds', 'main menu: learn game sounds'),
        ('test speakers', 'main menu: test speaker orientation'),
        ('quit', 'main menu: quit'),
        ('thanks', 'goodbye when you quit'),
        ('difficulty arrows', 'difficulty menu help (also the top 10 menu)'),
        ('easy', 'easy'),
        ('normal', 'normal'),
        ('hard', 'insane, the hardest difficulty'),
        ('return to menu', 'return to the main menu'),
        ('mission arrows', 'mission menu help'),
        ('mission1', 'mission 1: walk in space'),
        ('mission2', 'mission 2: resurrection'),
        ('mission3', 'mission 3: gorbian revenge'),
        ('mission4', 'mission 4: bomber madness'),
        ('mission5', 'mission 5: half and half'),
        ('mission6', 'mission 6: baby black hole'),
        ('random mission', 'random mission'),
        ('speed arrows', 'speed round menu help'),
        ('regular round', 'speed round with normal ships (also in the top 10 menu)'),
        ('gorb round', 'speed round with gorbian ships (also in the top 10 menu)'),
        ('options arrows', 'program settings help'),
        ('hyper', 'setting: hyperactive sides'),
        ('ambiance', 'setting: background messages'),
        ('background ambiance', 'setting: background music'),
        ('automatic items', 'setting: number keys use items right away'),
        ('on', 'on'),
        ('off', 'off'),
        ('sound arrows', 'learn sounds menu help'),
        ('score arrows', 'top 10 list help'),
        ('say normal board', 'hidden: control+B in the main menu, normal board width'),
        ('say wider board', 'hidden: wider board'),
        ('say widest board', 'hidden: widest board'),
    ]),
    ('moon rock', [
        'Moon rocks: your laser cannot hit them. Use the side lasers (alt plus left or right arrow).',
    ], [
        ('rock', 'a moon rock on the screen'),
        ('shoot rock', 'side laser shot'),
        ('kill rock', 'moon rock destroyed'),
        ('smash rock', 'you ran into a moon rock; your laser is off for a few seconds'),
        ('no gun', 'you fired while your laser was off'),
    ]),
    ('music', [
        'Music. In the resurrection mission the level music plays in reverse order.',
    ], [
        ('menu background', 'menu music'),
        ('intro', 'when a game starts'),
        ('background1', 'levels 1 and 2, and the speed round'),
        ('background1a', 'levels 3 and 4'),
        ('background2', 'levels 5 and 6'),
        ('background2a', 'levels 7 and 8'),
        ('background3', 'levels 9 and 10'),
        ('background3a', 'levels 11 and 12'),
        ('background4', 'levels 13 to 15, and the landing ship and ship barrage rounds'),
        ('background4a', 'level 16, Lord Vector'),
        ('background gorb', 'gorbian levels'),
        ('background meteor', 'hidden meteorite round'),
        ('background ship2', 'a wave of ships called in by a lunar lifter'),
        ('intermission5', 'before level 5'),
        ('intermission9', 'before level 9'),
        ('intermission16', 'before level 16'),
        ('pause background', 'game paused (P key)'),
    ]),
    ('number', [
        'Spoken numbers for the score, wave, lives, landed ships and item counts.',
    ], NUMBERS),
    SCORES_ONLINE,
    TEST_SPEAKERS,
    WARNING,
]


def _with(layout, folder, header=None, add=(), drop=()):
    """Copy of layout with one folder changed: extra header lines, extra entries, entries removed."""
    out = []
    for name, head, entries in layout:
        if name == folder:
            head = head + list(header or [])
            entries = [e for e in entries if e[0] not in drop] + list(add)
        out.append((name, head, entries))
    return out


# Troopanum SP 1.6 is the Spanish version: Spanish voice, English file names, and Spanish numbers.
# Its code says hundreds as 100 to 900 plus "and" (y); the English "hundred" is never used.
SP16 = _with(CLASSIC, 'number', header=[
    'This is the Spanish version: the voice is Spanish, the file names are English.',
], add=[
    (tuple(str(n) for n in range(100, 1000, 100)), 'one hundred to nine hundred'),
    ('and', '"y" (and), as in treinta y cinco'),
], drop=['hundred'])

LAYOUTS = {
    'Classic Troopanum': CLASSIC,
    'Troopanum 1.5': CLASSIC,
    'Troopanum 1.6': CLASSIC,
    'Troopanum SP 1.6': SP16,
    'Troopanum 2.0': TR20,
}


def _names(entry):
    return entry[0] if isinstance(entry[0], tuple) else (entry[0],)


def _stem(filename):
    return os.path.splitext(filename)[0].lower()


def _description(header, entries, present):
    lines = list(header) + ['']
    for entry in entries:
        names = [n for n in _names(entry) if n in present]
        if not names:
            continue
        if isinstance(entry[0], tuple) and len(names) == len(entry[0]) > 1:
            lines.append('%s to %s = %s' % (names[0], names[-1], entry[1]))
        else:
            lines.extend('%s = %s' % (n, entry[1]) for n in names)
    return '\r\n'.join(lines) + '\r\n'


def sort_sounds(game, sdir):
    """Move the files in sdir into category folders and write their _description.txt files."""
    layout = LAYOUTS[game]
    folder_of = {}
    for folder, _, entries in layout:
        for entry in entries:
            for n in _names(entry):
                folder_of[n] = folder
    files = [f for f in os.listdir(sdir) if os.path.isfile(os.path.join(sdir, f))]
    present = {}
    for f in files:
        folder = folder_of.get(_stem(f), UNUSED)
        os.makedirs(os.path.join(sdir, folder), exist_ok=True)
        shutil.move(os.path.join(sdir, f), os.path.join(sdir, folder, f))
        present.setdefault(folder, set()).add(_stem(f))
    for folder, header, entries in layout:
        if folder in present:
            with open(os.path.join(sdir, folder, DESCRIPTION), 'w', newline='') as fh:
                fh.write(_description(header, entries, present[folder]))
    for folder in sorted(present):
        print('  %-14s %d files' % (folder, len(present[folder])))


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    if len(sys.argv) > 1:
        root = sys.argv[1]
    else:
        with open(os.path.join(here, 'project.json'), encoding='utf-8') as f:
            root = os.path.join(here, '..', json.load(f).get('source_folder', 'source'))
    for game in LAYOUTS:
        sdir = os.path.join(root, game, 'sounds')
        if not os.path.isdir(sdir):
            continue
        # flatten first, so sorting again after a change starts from the plain file list
        for dirpath, _, filenames in list(os.walk(sdir))[1:]:
            for f in filenames:
                if f != DESCRIPTION:
                    shutil.move(os.path.join(dirpath, f), os.path.join(sdir, f))
        for d in [d for d in os.listdir(sdir) if os.path.isdir(os.path.join(sdir, d))]:
            shutil.rmtree(os.path.join(sdir, d))
        print(game)
        sort_sounds(game, sdir)


if __name__ == '__main__':
    sys.exit(main())
