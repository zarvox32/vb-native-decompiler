"""Troopanum 2.0 sound mod: builds a new tr20.bsc with your own sounds in place of the original ones.

    python -I apps/troopanum/utils/tr20_soundmod.py [FOLDER] [--original BOX]

FOLDER is your sound mod: WAV files named like the game's sounds (ship1.wav, enemy hit1.wav, ...),
anywhere in the folder or its subfolders. The script asks for it when it is not given. A sound found
there replaces the original of the same name (upper or lower case does not matter); every other sound
is copied from the original tr20.bsc, so the new box always holds all 269 sounds. The new tr20.bsc is
written into FOLDER. To play the mod, put it in the game's folder in place of the original.

BOX is the original tr20.bsc (default: compiled/Troopanum 2.0/tr20.bsc in this app's folder). It is
only read. Nothing else is written or deleted.

say fatal.wav, register.wav, tone1.wav and tone2.wav are not in tr20.bsc: the game reads them from its
own folder, so to change them, replace those files there.

The box format (MoleBox, as Troopanum 2.0 uses it):

* the files, one after another from the start of the box, each XOR-encoded with one byte (0x95) and
  followed by 0 to 12 bytes of filler;
* the directory: the file names, NUL-terminated and sorted, then one 16-byte entry per file (name
  offset, data offset, size, flags), encrypted with IDEA in ECB mode under a key that is a constant in
  troop.exe;
* a 44-byte trailer: the directory's size (twice), its offset, the size of the names, the number of
  files, the size of the box, the MD5 of the decrypted directory, and a signature.

Only the data offsets and sizes in the directory change. The names, their order, the flags and the
filler are kept, so a mod that replaced nothing would give back the original file byte for byte.
"""
import argparse
import hashlib
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
GAME = 'Troopanum 2.0'
BOX_NAME = 'tr20.bsc'
ORIGINAL_SHA256 = '0ec54be8b8c8bc8d70e7ef92d7276841014824595414a7973cf7baed06c265e3'

KEY = bytes.fromhex('0cfe36a0c11c7c504259772b53deff4e')   # the directory's IDEA key, from troop.exe
SIGNATURE = 0xB13A0C1A                                    # the last 4 bytes of the box
TRAILER = struct.Struct('<6I16sI')
ENTRY = struct.Struct('<4I')

# Sounds the game reads from its own folder, not from the box.
LOOSE_SOUNDS = ('register.wav', 'say fatal.wav', 'tone1.wav', 'tone2.wav')


# IDEA: 64-bit blocks of four big-endian 16-bit words, a 128-bit key, 8 rounds.

def _mul(a, b):
    """Multiplication modulo 65537, where 0 stands for 65536."""
    return ((a or 0x10000) * (b or 0x10000) % 0x10001) & 0xFFFF


def _inv(x):
    """Inverse for _mul (0 and 1 are their own inverses)."""
    return x if x <= 1 else pow(x, 0x10001 - 2, 0x10001)


def _schedule(key):
    """The 52 encryption subkeys: the key's eight words, then the key rotated left 25 bits, and so on."""
    k = int.from_bytes(key, 'big')
    sub = []
    while len(sub) < 52:
        sub += [(k >> (112 - 16 * i)) & 0xFFFF for i in range(8)]
        k = ((k << 25) | (k >> 103)) & ((1 << 128) - 1)
    return sub[:52]


def _invert(ek):
    """The decryption subkeys for the encryption subkeys ek."""
    dk = []
    for r in range(9):
        j = 48 - 6 * r
        a, b = (ek[j + 1], ek[j + 2]) if r in (0, 8) else (ek[j + 2], ek[j + 1])
        dk += [_inv(ek[j]), -a & 0xFFFF, -b & 0xFFFF, _inv(ek[j + 3])]
        if r < 8:
            dk += ek[j - 2:j]
    return dk


def _crypt(data, sub):
    """IDEA in ECB mode: encrypts with encryption subkeys, decrypts with decryption subkeys."""
    out = bytearray()
    for pos in range(0, len(data), 8):
        x1, x2, x3, x4 = struct.unpack_from('>4H', data, pos)
        for k in range(0, 48, 6):
            x1 = _mul(x1, sub[k])
            x2 = (x2 + sub[k + 1]) & 0xFFFF
            x3 = (x3 + sub[k + 2]) & 0xFFFF
            x4 = _mul(x4, sub[k + 3])
            t0 = _mul(x1 ^ x3, sub[k + 4])
            t1 = _mul((t0 + (x2 ^ x4)) & 0xFFFF, sub[k + 5])
            t0 = (t0 + t1) & 0xFFFF
            x1 ^= t1
            x4 ^= t0
            x2, x3 = x3 ^ t1, x2 ^ t0
        out += struct.pack('>4H', _mul(x1, sub[48]), (x3 + sub[49]) & 0xFFFF,
                           (x2 + sub[50]) & 0xFFFF, _mul(x4, sub[51]))
    return bytes(out)


class Box:
    """A tr20.bsc read into memory, with its directory decrypted and checked."""

    def __init__(self, path):
        with open(path, 'rb') as f:
            self.data = data = f.read()
        if len(data) < TRAILER.size:
            raise ValueError('too small to be a MoleBox box')
        (self.dir_size, plain_size, self.dir_offset, self.names_size, count, box_size, digest,
         signature) = TRAILER.unpack_from(data, len(data) - TRAILER.size)
        if signature != SIGNATURE or box_size != len(data):
            raise ValueError('not a MoleBox box (no signature at the end)')
        if not (self.dir_size == plain_size == self.names_size + ENTRY.size * count
                and self.dir_size % 8 == 0
                and self.dir_offset + self.dir_size + TRAILER.size == len(data)):
            raise ValueError('a box layout this script does not know')
        directory = _crypt(data[self.dir_offset:self.dir_offset + self.dir_size], _invert(_schedule(KEY)))
        if hashlib.md5(directory).digest() != digest:
            raise ValueError('the directory does not decrypt with the Troopanum 2.0 key')
        self.names_table = directory[:self.names_size]
        self.entries = [list(ENTRY.unpack_from(directory, self.names_size + ENTRY.size * i))
                        for i in range(count)]
        self.names = [self.names_table[e[0]:self.names_table.index(b'\0', e[0])].decode('latin-1')
                      for e in self.entries]
        # the files must be stored in directory order, from offset 0, without overlapping
        self.ends = [e[1] for e in self.entries[1:]] + [self.dir_offset]
        if count == 0 or self.entries[0][1] != 0 or any(o + s > end for (_, o, s, _), end
                                                         in zip(self.entries, self.ends)):
            raise ValueError('a box layout this script does not know')
        self.xor = data[0] ^ ord('R')
        self.table = bytes(b ^ self.xor for b in range(256))     # encodes and decodes
        if any(data[o:o + 4] != b'RIFF'.translate(self.table) for _, o, _, _ in self.entries):
            raise ValueError('a stored file is not an XOR-encoded WAV file')

    def original(self, i):
        """The decoded contents of file i."""
        _, off, size, _ = self.entries[i]
        return self.data[off:off + size].translate(self.table)


def build(box, replacements, out_path):
    """Write the box with the files in replacements ({entry index: path}) swapped in."""
    entries = [list(e) for e in box.entries]
    pos = 0
    with open(out_path, 'wb') as out:
        for i, ((_, off, size, _), end) in enumerate(zip(box.entries, box.ends)):
            if i in replacements:
                with open(replacements[i], 'rb') as f:
                    body = f.read().translate(box.table)
            else:
                body = box.data[off:off + size]
            filler = box.data[off + size:end]
            out.write(body)
            out.write(filler)
            entries[i][1:3] = pos, len(body)
            pos += len(body) + len(filler)
        directory = box.names_table + b''.join(ENTRY.pack(*e) for e in entries)
        total = pos + len(directory) + TRAILER.size
        if total >= 1 << 32:
            raise ValueError('the sounds add up to more than 4 GB')
        out.write(_crypt(directory, _schedule(KEY)))
        out.write(TRAILER.pack(box.dir_size, box.dir_size, pos, box.names_size, len(entries), total,
                               hashlib.md5(directory).digest(), SIGNATURE))


def wav_check(head):
    """(error, warning) for the contents of a mod file: an error if it is not a WAV file, a warning if
    the game might not be able to play it."""
    if len(head) < 12 or head[:4] != b'RIFF' or head[8:12] != b'WAVE':
        return 'not a WAV file (no RIFF/WAVE header)', None
    fmt, has_data, pos = None, False, 12
    while pos + 8 <= len(head):
        cid, size = struct.unpack_from('<4sI', head, pos)
        if cid == b'fmt ' and size >= 16 and pos + 24 <= len(head):
            fmt = struct.unpack_from('<HHIIHH', head, pos + 8)
        elif cid == b'data':
            has_data = True
        pos += 8 + size + (size & 1)
    if fmt is None or not has_data:
        return 'not a complete WAV file (no fmt or data chunk)', None
    tag, channels, rate, _, _, bits = fmt
    if tag != 1 or bits not in (8, 16) or channels not in (1, 2):
        return None, ('format %d, %d channel(s), %d-bit: the game may not be able to play it (it '
                      'expects PCM, 8 or 16-bit, mono or stereo)' % (tag, channels, bits))
    return None, None


def default_original():
    compiled = 'compiled'
    try:
        with open(os.path.join(APP, 'project', 'project.json'), encoding='utf-8') as f:
            compiled = json.load(f).get('compiled_folder', compiled)
    except OSError:
        pass
    return os.path.join(APP, compiled, GAME, BOX_NAME)


def main():
    ap = argparse.ArgumentParser(description='Build a tr20.bsc for Troopanum 2.0 with your own sounds.')
    ap.add_argument('folder', nargs='?', help='the sound mod folder (asked for when not given)')
    ap.add_argument('--original', default=default_original(),
                    help='the original tr20.bsc (default: %(default)s)')
    args = ap.parse_args()

    folder = args.folder if args.folder is not None else input('Sound mod folder: ')
    folder = folder.strip().strip('"')
    if not folder or not os.path.isdir(folder):
        sys.exit('No such folder: %s' % folder)
    folder = os.path.abspath(folder)
    out_path = os.path.join(folder, BOX_NAME)

    if not os.path.isfile(args.original):
        sys.exit('The original tr20.bsc is not at %s.\nPut your copy of Troopanum 2.0 there (see '
                 'apps/troopanum/project/README.md), or give its path with --original.' % args.original)
    if os.path.exists(out_path) and os.path.samefile(out_path, args.original):
        sys.exit('%s is the original tr20.bsc, which this would overwrite. Use another folder for the '
                 'mod.' % out_path)
    try:
        box = Box(args.original)
    except ValueError as e:
        sys.exit('%s: %s' % (args.original, e))
    print('Original: %s (%d sounds)' % (args.original, len(box.names)))
    if hashlib.sha256(box.data).hexdigest() != ORIGINAL_SHA256:
        print('  Note: this is not the tr20.bsc of the original release (its checksum differs). The '
              'sounds you do not replace are copied from it as they are.')

    index = {n.lower(): i for i, n in enumerate(box.names)}
    found, loose, unknown = {}, [], []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for fn in sorted(filenames):
            key = fn.lower()
            path = os.path.join(dirpath, fn)
            if key in index:
                found.setdefault(index[key], []).append(path)
            elif key in LOOSE_SOUNDS:
                loose.append(path)
            elif key.endswith('.wav'):
                unknown.append(path)

    def rel(path):
        return os.path.relpath(path, folder)

    if loose:
        print('Not in tr20.bsc (the game loads these from its own folder; replace them there):')
        print(''.join('  %s\n' % rel(p) for p in loose), end='')
    if unknown:
        print('Not the name of a Troopanum 2.0 sound (left out):')
        print(''.join('  %s\n' % rel(p) for p in unknown), end='')

    errors, replacements, unchanged = [], {}, 0
    for i, paths in sorted(found.items()):
        if len(paths) > 1:
            errors.append('%s is in the mod more than once: %s' % (box.names[i].lower(),
                                                                    ', '.join(rel(p) for p in paths)))
            continue
        with open(paths[0], 'rb') as f:
            body = f.read()
        error, warning = wav_check(body)
        if error:
            errors.append('%s: %s' % (rel(paths[0]), error))
        if warning:
            print('Warning: %s: %s' % (rel(paths[0]), warning))
        if body == box.original(i):
            unchanged += 1
        else:
            replacements[i] = paths[0]
    if errors:
        sys.exit('Nothing was built:\n' + '\n'.join('  ' + e for e in errors))
    if not found:
        sys.exit('No sound in %s has the name of a Troopanum 2.0 sound; nothing to build.' % folder)
    if not replacements:
        sys.exit('Every sound in the mod is the same as the original; nothing to build.')

    print('Replacing %d of %d sounds:' % (len(replacements), len(box.names)))
    for i in sorted(replacements):
        print('  %-24s <- %s' % (box.names[i].lower(), rel(replacements[i])))
    if unchanged:
        print('(%d more sounds in the mod are the same as the original.)' % unchanged)

    if os.path.exists(out_path):
        answer = input('%s already exists. Replace it? [y/N] ' % out_path)
        if answer.strip().lower() not in ('y', 'yes'):
            sys.exit('Nothing was built.')

    # build next to the target and move it into place only once it is complete and checked
    part = out_path + '.part'
    try:
        build(box, replacements, part)
        new = Box(part)
        if new.names != box.names:
            raise ValueError('the names changed')
        os.replace(part, out_path)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise
    print('Wrote %s (%d bytes).' % (out_path, len(new.data)))
    print('Put it in the Troopanum 2.0 folder in place of tr20.bsc.')


if __name__ == '__main__':
    main()
