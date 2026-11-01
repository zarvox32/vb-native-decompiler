"""Tell the format of sound files from their headers: container, codec, sample rate, bits, channels.

    python -I soundinfo.py <folder or file> ...

For a folder, prints how many files there are of each format (subfolders included), with the names
when there are only a few. Knows WAV (RIFF, any codec), WMA and other ASF audio, MIDI (also RIFF MIDI),
MP3, and Ogg Vorbis and Opus. Only the headers are read.

build_sourcecode.py uses it to list the formats of each program's sounds in DECOMPILER_NOTES.txt. It
also gives the formats an app's notes must describe (documentation.md, section 4).
"""
import os
import struct
import sys
import uuid
from collections import Counter

# WAVE format tags (also used inside ASF)
CODECS = {
    0x0001: 'PCM', 0x0002: 'Microsoft ADPCM', 0x0003: 'IEEE float', 0x0006: 'A-law', 0x0007: 'mu-law',
    0x000A: 'WMA Voice', 0x0011: 'IMA ADPCM', 0x0031: 'GSM 6.10', 0x0050: 'MPEG audio', 0x0055: 'MP3',
    0x0160: 'WMA 1', 0x0161: 'WMA 2', 0x0162: 'WMA Pro', 0x0163: 'WMA Lossless',
}
UNCOMPRESSED = (0x0001, 0x0003, 0x0006, 0x0007)   # described by bits per sample, not bit rate
EXTENSIBLE = 0xFFFE

ASF_HEADER = uuid.UUID('75B22630-668E-11CF-A6D9-00AA0062CE6C').bytes_le
ASF_STREAM_PROPERTIES = uuid.UUID('B7DC0791-A9B7-11CF-8EE6-00C00C205365').bytes_le
ASF_AUDIO_MEDIA = uuid.UUID('F8699E40-5B4D-11CF-A8FD-00805F5C442B').bytes_le

MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
MP3_KBPS = {  # (MPEG-1?, layer) -> bit rates by index 1-14
    (True, 1): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
MP3_KBPS[(False, 3)] = MP3_KBPS[(False, 2)]

HEAD = 1 << 16    # bytes read for formats whose header is near the start


def _channels(n):
    return {1: 'mono', 2: 'stereo'}.get(n, '%d channels' % n)


def _waveformat(tag, channels, rate, avg_bytes, bits, extra=b''):
    """'PCM, 22,050 Hz, 16-bit, stereo' from WAVEFORMATEX fields."""
    if tag == EXTENSIBLE and len(extra) >= 8:
        # extra: valid bits (2 bytes), channel mask (4), then the sub-format GUID, which starts with the tag
        tag = struct.unpack_from('<H', extra, 6)[0]
    parts = [CODECS.get(tag, 'codec 0x%04X' % tag), '%s Hz' % format(rate, ',')]
    if tag in UNCOMPRESSED:
        parts.append('%d-bit' % bits)
    parts.append(_channels(channels))
    if tag not in UNCOMPRESSED and avg_bytes:
        parts.append('%d kbit/s' % round(avg_bytes * 8 / 1000))
    return ', '.join(parts)


def _riff(f):
    form = f.read(12)[8:12]
    fmt = None
    while True:
        h = f.read(8)
        if len(h) < 8:
            break
        cid, size = struct.unpack('<4sI', h)
        if form == b'WAVE' and cid == b'fmt ':
            fmt = f.read(size)
            break
        if form == b'RMID' and cid == b'data':
            return 'RIFF ' + _midi(f.read(14))
        f.seek(size + (size & 1), 1)
    if form != b'WAVE':
        return 'RIFF %s, not audio this knows' % form.decode('latin-1')
    if not fmt or len(fmt) < 16:
        return 'WAV without a format chunk'
    tag, channels, rate, avg, _, bits = struct.unpack_from('<HHIIHH', fmt)
    return 'WAV, ' + _waveformat(tag, channels, rate, avg, bits, fmt[18:])


def _asf(b):
    count = struct.unpack_from('<I', b, 24)[0]
    pos = 30
    for _ in range(count):
        guid, size = b[pos:pos + 16], struct.unpack_from('<Q', b, pos + 16)[0]
        if guid == ASF_STREAM_PROPERTIES and b[pos + 24:pos + 40] == ASF_AUDIO_MEDIA:
            tag, channels, rate, avg, _, bits = struct.unpack_from('<HHIIHH', b, pos + 78)
            desc = _waveformat(tag, channels, rate, avg, bits)
            return desc if desc.startswith('WMA') else 'ASF, ' + desc
        if size < 24:
            break
        pos += size
    return 'ASF without an audio stream'


def _midi(b):
    if b[:4] != b'MThd':
        return 'MIDI without a header'
    fmt, tracks, division = struct.unpack_from('>HHH', b, 8)
    timing = ('%d ticks per quarter note' % division if not division & 0x8000
              else 'SMPTE timing')
    return 'MIDI, type %d, %d track%s, %s' % (fmt, tracks, '' if tracks == 1 else 's', timing)


def _mp3(b):
    pos = 0
    if b[:3] == b'ID3':
        s = b[6:10]
        pos = 10 + (s[0] << 21 | s[1] << 14 | s[2] << 7 | s[3])
    while pos + 4 <= len(b):
        if b[pos] == 0xFF and b[pos + 1] & 0xE0 == 0xE0:
            version, layer = (b[pos + 1] >> 3) & 3, 4 - ((b[pos + 1] >> 1) & 3)
            rate_i, kbps_i = (b[pos + 2] >> 2) & 3, b[pos + 2] >> 4
            if version != 1 and layer != 4 and rate_i != 3 and 0 < kbps_i < 15:
                mpeg = {3: '1', 2: '2', 0: '2.5'}[version]
                name = 'MP3 (MPEG-%s layer III)' % mpeg if layer == 3 else \
                    'MPEG-%s audio layer %s' % (mpeg, ('I', 'II')[layer - 1])
                frame = b[pos:pos + 200]
                if b'Xing' in frame or b'VBRI' in frame:
                    kbps = 'variable bit rate'
                else:
                    kbps = '%d kbit/s' % MP3_KBPS[(version == 3, layer)][kbps_i - 1]
                return '%s, %s Hz, %s, %s' % (name, format(MP3_RATES[version][rate_i], ','),
                                              _channels(1 if b[pos + 3] >> 6 == 3 else 2), kbps)
        pos += 1
    return 'MP3 without a frame header'


def _ogg(b):
    packet = b[27 + b[26]:]
    if packet[:7] == b'\x01vorbis':
        channels, rate, _, nominal = struct.unpack_from('<BIii', packet, 11)
        return 'Ogg Vorbis, %s Hz, %s%s' % (format(rate, ','), _channels(channels),
                                            ', %d kbit/s nominal' % round(nominal / 1000) if nominal > 0 else '')
    if packet[:8] == b'OpusHead':
        channels, _, rate = struct.unpack_from('<BHI', packet, 9)
        return 'Ogg Opus, %s (recorded at %s Hz)' % (_channels(channels), format(rate, ','))
    return 'Ogg, a codec this does not know'


def describe(path):
    """One line describing the format of the file at path, from its contents (not its name)."""
    try:
        with open(path, 'rb') as f:
            b = f.read(HEAD)
            if b[:4] == b'RIFF':
                f.seek(0)
                return _riff(f)
        if b[:16] == ASF_HEADER:
            if len(b) < struct.unpack_from('<Q', b, 16)[0]:
                with open(path, 'rb') as f:
                    b = f.read(struct.unpack_from('<Q', b, 16)[0])
            return _asf(b)
        if b[:4] == b'MThd':
            return _midi(b)
        if b[:4] == b'OggS':
            return _ogg(b)
        if b[:3] == b'ID3' or (len(b) > 1 and b[0] == 0xFF and b[1] & 0xE0 == 0xE0):
            return _mp3(b)
        return 'not a sound format this knows'
    except (struct.error, IndexError, KeyError, OSError):
        return 'unreadable header'


def summary(files, root, show=8):
    """Lines counting the files (paths under root) by extension and format, most common first. The
    names are listed for a group of up to `show` files."""
    groups = {}
    for p in files:
        key = (os.path.splitext(p)[1].lower() or '(none)', describe(p))
        groups.setdefault(key, []).append(os.path.relpath(p, root).replace(os.sep, '/'))
    width = max((len(ext) for ext, _ in groups), default=0)
    lines = []
    for (ext, desc), names in sorted(groups.items(), key=lambda g: (-len(g[1]), g[0])):
        line = '%4d  %-*s  %s' % (len(names), width, ext, desc)
        if len(names) <= show:
            line += ': ' + ', '.join(sorted(names))
        lines.append(line)
    return lines


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for arg in sys.argv[1:]:
        if os.path.isdir(arg):
            files = [os.path.join(d, f) for d, _, fs in os.walk(arg) for f in fs]
            print('%s (%d files)' % (arg, len(files)))
            print('\n'.join('  ' + line for line in summary(files, arg)))
        else:
            print('%s: %s' % (arg, describe(arg)))


if __name__ == '__main__':
    main()
