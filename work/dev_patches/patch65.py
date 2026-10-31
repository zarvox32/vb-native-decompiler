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
"""    ap.add_argument('--debug', action='store_true')
    a = ap.parse_args()
    dc = Decompiler(a.exe, debug=a.debug)""",
"""    ap.add_argument('--debug', action='store_true')
    ap.add_argument('--image', help='in-memory image dump (e.g. from molebox_extract.py) to decompile instead')
    ap.add_argument('--base', default='400000', help='load address of --image (hex)')
    a = ap.parse_args()
    if a.image:
        from pe import Image
        img = Image(path=a.image, mapped=open(a.image, 'rb').read(), base=int(a.base, 16))
        dc = Decompiler(image=img, debug=a.debug)
    else:
        dc = Decompiler(a.exe, debug=a.debug)""")])

# pe.Image: mapped dumps keep their path for notes
patch('pe.py', [(
"""        if mapped is not None:
            # an in-memory dump (headers + sections at their RVAs)
            self.pe = pefile.PE(data=bytes(mapped[:0x1000]), fast_load=True)""",
"""        if mapped is not None:
            # an in-memory dump (headers + sections at their RVAs)
            self.pe = pefile.PE(data=bytes(mapped[:0x1000]) + b'\\0' * 0x1000, fast_load=True)""")])
print('patched')
