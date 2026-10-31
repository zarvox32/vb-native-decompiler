"""Split a MoleBox data box that stores its files as XOR-encoded WAVs, one after the other (Troopanum 2.0's
tr20.bsc), into separate files - the fast alternative to `molebox_extract.py --extract`.

usage: python -I bsc_split.py <box.bsc> <names.txt> <outdir>

names.txt is the box directory written by `molebox_extract.py --list` (the names are encrypted in the
box itself). The files are stored in directory order, each one a RIFF chunk followed by 0-12 bytes of
padding; the XOR key is found from the first chunk's "RIFF" signature.
"""
import os
import struct
import sys


def main():
    box, names_file, outdir = sys.argv[1:4]
    data = open(box, 'rb').read()
    key = data[0] ^ ord('R')
    if any(data[i] ^ key != b'RIFF'[i] for i in range(4)):
        sys.exit('%s does not start with an XOR-encoded RIFF chunk' % box)
    dec = bytes(b ^ key for b in data)
    names = [l.rstrip('\r\n') for l in open(names_file, encoding='latin-1') if l.strip()]
    os.makedirs(outdir, exist_ok=True)
    pos = 0
    for n in names:
        if dec[pos:pos + 4] != b'RIFF':
            sys.exit('expected a RIFF chunk for %s at offset %d' % (n, pos))
        size = struct.unpack_from('<I', dec, pos + 4)[0] + 8
        open(os.path.join(outdir, n), 'wb').write(dec[pos:pos + size])
        pos += size
        nxt = dec.find(b'RIFF', pos, pos + 64)
        if nxt >= 0:
            pos = nxt                     # skip the padding after the chunk
    print('XOR key 0x%02X: %d files, %d of %d bytes used' % (key, len(names), pos, len(dec)))


if __name__ == '__main__':
    main()
