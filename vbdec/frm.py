"""Partial decoder for VB6 form designer data (the binary form stored in the executable).

Only what is useful for a port is recovered: the form name, its Caption and any embedded
pictures (JPEG/BMP/GIF/ICO), which are written out as files."""
import struct


def parse_form(gui):
    info = {'name': None, 'caption': None, 'images': []}
    if not gui or gui[:4] != b'\xff\xcc\x31\x00':
        return info
    # name: u16 length + chars + NUL, shortly after the GUID block
    i = gui.find(b'\x00', 0x5C)
    k = 0x5C
    while k < 0x80:
        n = struct.unpack_from('<H', gui, k)[0]
        if 0 < n < 40 and k + 2 + n < len(gui) and gui[k + 2 + n] == 0 and \
                all(32 < c < 127 for c in gui[k + 2:k + 2 + n]):
            info['name'] = gui[k + 2:k + 2 + n].decode('latin-1')
            k = k + 3 + n
            break
        k += 1
    # Caption: property 0x01 followed by u16 length + text + NUL (first property of a Form)
    for j in range(k, min(k + 16, len(gui) - 4)):
        if gui[j] == 0x01:
            n = struct.unpack_from('<H', gui, j + 1)[0]
            if 0 <= n < 256 and j + 3 + n < len(gui) and gui[j + 3 + n] == 0:
                txt = gui[j + 3:j + 3 + n]
                if all(32 <= c < 127 for c in txt):
                    info['caption'] = txt.decode('latin-1')
                    break
    # embedded pictures
    pos = 0
    while True:
        j = gui.find(b'\xff\xd8\xff', pos)
        if j < 0:
            break
        size = None
        if j >= 4:
            n = struct.unpack_from('<I', gui, j - 4)[0]
            if 0 < n <= len(gui) - j:
                size = n
        if size is None:
            e = gui.find(b'\xff\xd9', j)
            size = (e + 2 - j) if e > 0 else len(gui) - j
        info['images'].append(('jpg', gui[j:j + size]))
        pos = j + size
    for sig, ext in ((b'BM', 'bmp'), (b'GIF8', 'gif')):
        pos = 0
        while True:
            j = gui.find(sig, pos)
            if j < 0:
                break
            if ext == 'bmp':
                n = struct.unpack_from('<I', gui, j + 2)[0] if j + 6 <= len(gui) else 0
                if 54 < n <= len(gui) - j and gui[j + 14:j + 18] in (b'\x28\x00\x00\x00', b'\x0c\x00\x00\x00'):
                    info['images'].append(('bmp', gui[j:j + n]))
                    pos = j + n
                    continue
            elif ext == 'gif' and j >= 4:
                n = struct.unpack_from('<I', gui, j - 4)[0]
                if 0 < n <= len(gui) - j:
                    info['images'].append(('gif', gui[j:j + n]))
                    pos = j + n
                    continue
            pos = j + 1
    return info
