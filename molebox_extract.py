"""Unpack a MoleBox-wrapped program by emulating its runtime (speakeasy/unicorn) - nothing is executed natively.

What it does:
  1. Runs the MoleBox loader in the emulator with the box data file(s) exposed next to the exe.
  2. When the loader asks for MSVBVM60.DLL (its virtual file system is ready and the original
     sections are already decompressed), it
       * dumps the in-memory image (original .text/.data/.rsrc) to <outdir>/image.bin,
       * writes the box directory (file names) to <outdir>/names.txt, and
       * if --extract is given, runs a generated x86 routine that calls the runtime's own
         CreateFileA/GetFileSize/ReadFile/CloseHandle hooks for every file in the box,
         saving each decoded file into <outdir>/files/.  This is slow (hours for a large box);
         for a box that just stores the files XOR-encoded, bsc_split.py does the same in seconds
         from names.txt.

usage: python -I molebox_extract.py <exe> <outdir> --box <file.bsc> [--extract | --list]
       [--hooks 0xCreateFileA,0xGetFileSize,0xReadFile,0xCloseHandle] [--names names.txt]

The hook addresses are read from the runtime's API hook table automatically (it pairs a
replacement-function pointer with a pointer to the API name string, e.g. "CreateFileA").
The file names come from --names (one per line) or from the decrypted box directory,
captured automatically when the runtime parses it.
"""
import sys, os, json, struct, argparse, logging

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'emu'))
import speakeasy  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('exe')
ap.add_argument('outdir')
ap.add_argument('--box', action='append', default=[])
ap.add_argument('--extract', action='store_true')
ap.add_argument('--list', action='store_true', help='only write the box file names to <outdir>/names.txt')
ap.add_argument('--names')
ap.add_argument('--hooks')
ap.add_argument('--verbose', action='store_true')
args = ap.parse_args()

logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format='%(message)s')
os.makedirs(args.outdir, exist_ok=True)
EMUDIR = 'c:\\windows\\system32\\'

root = os.path.dirname(speakeasy.__file__)
cfg = json.load(open(os.path.join(root, 'configs', 'default.json')))
cfg['timeout'] = 0
cfg['max_api_count'] = 50_000_000
files = [{'mode': 'full_path', 'emu_path': EMUDIR + os.path.basename(args.exe), 'path': args.exe}]
for x in args.box:
    files.append({'mode': 'full_path', 'emu_path': EMUDIR + os.path.basename(x), 'path': x})
cfg['filesystem']['files'] = files + cfg['filesystem']['files']

se = speakeasy.Speakeasy(config=cfg)
module = se.load_module(args.exe)
BASE = module.get_base()
SIZE = module.get_image_size()
state = {'driver': False, 'done': False, 'extracted': 0, 'failed': []}


def rd(addr, n):
    return bytes(se.mem_read(addr, n))


def u32(addr):
    return struct.unpack('<I', rd(addr, 4))[0]


def find_hooks():
    """Locate the runtime's (replacement, name) table and return name -> replacement address."""
    img = rd(BASE, SIZE)
    want = [b'CreateFileA\0', b'ReadFile\0', b'GetFileSize\0', b'CloseHandle\0']
    out = {}
    for w in want:
        pos = img.find(w)
        while pos >= 0:
            sva = BASE + pos
            ref = img.find(struct.pack('<I', sva))
            while ref >= 0:
                fn = struct.unpack_from('<I', img, ref - 4)[0]
                if BASE < fn < BASE + SIZE and ref % 4 == 0:
                    out[w[:-1].decode()] = fn
                    break
                ref = img.find(struct.pack('<I', sva), ref + 1)
            if w[:-1].decode() in out:
                break
            pos = img.find(w, pos + 1)
    return out


def asm_driver(code_va, names, name_va, hooks, buf_va, buf_size, var_va):
    """Generate x86 code: for each name -> open/size/read/close via the runtime hooks; mark after each."""
    c = bytearray()
    marks = []

    def push_imm(v):
        c.extend(b'\x68' + struct.pack('<I', v & 0xffffffff))

    def call_abs(v):
        c.extend(b'\xb8' + struct.pack('<I', v) + b'\xff\xd0')  # mov eax, v ; call eax

    def u32(v):
        return struct.pack('<I', v & 0xffffffff)

    def jz_placeholder():
        c.extend(b'\x0f\x84' + b'\0\0\0\0')
        return len(c)                     # rel32 is relative to this offset

    def patch_rel(at_end, target):
        c[at_end - 4:at_end] = struct.pack('<i', target - at_end)

    for i, _ in enumerate(names):
        # h = CreateFileA(name, GENERIC_READ, FILE_SHARE_READ, 0, OPEN_EXISTING, 0x80, 0)
        push_imm(0); push_imm(0x80); push_imm(3); push_imm(0); push_imm(1); push_imm(0x80000000)
        push_imm(name_va[i])
        call_abs(hooks['CreateFileA'])
        c.extend(b'\xa3' + u32(var_va))                         # mov [var], eax  (handle)
        push_imm(0); c.extend(b'\x50')                           # push 0 ; push eax
        call_abs(hooks['GetFileSize'])
        c.extend(b'\xa3' + u32(var_va + 4))                     # mov [var+4], eax (size)
        c.extend(b'\xc7\x05' + u32(var_va + 16) + u32(0))       # total = 0
        c.extend(b'\xc7\x05' + u32(var_va + 12) + u32(1))       # ok = 1
        # some runtimes return at most one block per ReadFile: loop until everything is read
        loop = len(c)
        c.extend(b'\xa1' + u32(var_va + 4))                     # mov eax, [size]
        c.extend(b'\x2b\x05' + u32(var_va + 16))                # sub eax, [total]
        j1 = jz_placeholder()
        push_imm(0); push_imm(var_va + 8)                        # lpOverlapped, &nread
        c.extend(b'\x50')                                        # push eax (remaining)
        c.extend(b'\xb9' + u32(buf_va))                         # mov ecx, buf
        c.extend(b'\x03\x0d' + u32(var_va + 16))                # add ecx, [total]
        c.extend(b'\x51')                                        # push ecx
        c.extend(b'\xff\x35' + u32(var_va))                     # push [handle]
        call_abs(hooks['ReadFile'])
        c.extend(b'\xa3' + u32(var_va + 12))                    # mov [ok], eax
        c.extend(b'\x85\xc0')                                    # test eax, eax
        j2 = jz_placeholder()
        c.extend(b'\xa1' + u32(var_va + 8))                     # mov eax, [nread]
        c.extend(b'\x85\xc0')                                    # test eax, eax
        j3 = jz_placeholder()
        c.extend(b'\x01\x05' + u32(var_va + 16))                # add [total], eax
        c.extend(b'\xe9' + b'\0\0\0\0')                          # jmp loop
        patch_rel(len(c), loop)
        done = len(c)
        for j in (j1, j2, j3):
            patch_rel(j, done)
        c.extend(b'\xff\x35' + u32(var_va))                     # push [handle]
        call_abs(hooks['CloseHandle'])
        marks.append(code_va + len(c))
        c.extend(b'\x90')                                        # nop  <- marker i
    end = code_va + len(c)
    c.extend(b'\x90\xeb\xfe')                                    # nop ; jmp $  (stopped by hook)
    return bytes(c), marks, end


def on_dir_parse(emu, api_name, func, params):
    # remember (call site, original-case string) before the runtime upper-cases it
    s = rd(params[0], params[1]) if params[0] and params[1] else b''
    ret = u32(se.reg_read('esp'))
    state.setdefault('upper_calls', []).append((ret, s.decode('latin-1')))
    return func(params)


def box_names():
    """File names from the box directory parse: the CharUpperBuffA call site used most often."""
    from collections import Counter
    calls = state.get('upper_calls', [])
    if not calls:
        return []
    site = Counter(r for r, _ in calls).most_common(1)[0][0]
    return list(dict.fromkeys(n for r, n in calls if r == site))


def start_driver():
    hooks = {}
    if args.hooks:
        a = [int(x, 16) for x in args.hooks.split(',')]
        hooks = dict(zip(['CreateFileA', 'GetFileSize', 'ReadFile', 'CloseHandle'], a))
    else:
        hooks = find_hooks()
    print('runtime hooks:', {k: hex(v) for k, v in hooks.items()})
    if args.names:
        names = [l.strip() for l in open(args.names, encoding='latin-1') if l.strip()]
    else:
        names = box_names()
    state['names'] = names
    print('files to extract:', len(names))
    buf_size = 64 * 1024 * 1024
    buf_va = se.mem_alloc(buf_size, tag='extract.buf')
    var_va = se.mem_alloc(0x100, tag='extract.vars')
    blob = bytearray()
    name_va = []
    strs_va = se.mem_alloc(0x10000, tag='extract.names')
    for n in names:
        name_va.append(strs_va + len(blob))
        blob.extend((EMUDIR + n).encode('latin-1') + b'\0')
    se.mem_write(strs_va, bytes(blob))
    code_va = se.mem_alloc(0x100000, tag='extract.code')
    code, marks, end = asm_driver(code_va, names, name_va, hooks, buf_va, buf_size, var_va)
    se.mem_write(code_va, code)
    fdir = os.path.join(args.outdir, 'files')
    os.makedirs(fdir, exist_ok=True)

    def make_mark(i):
        def cb(emu, addr, size, ctx):
            h, sz, nread, ok, total = struct.unpack('<5I', rd(var_va, 20))
            n = names[i]
            if h in (0, 0xffffffff) or total == 0 or total != sz:
                state['failed'].append(n)
                print('  FAILED', n, hex(h), 'size', sz, 'read', total, 'ok', ok)
                if h in (0, 0xffffffff) or total == 0:
                    return
            data = rd(buf_va, total)
            open(os.path.join(fdir, n), 'wb').write(data)
            state['extracted'] += 1
        return cb

    for i, m in enumerate(marks):
        se.add_code_hook(make_mark(i), begin=m, end=m)

    def on_end(emu, addr, size, ctx):
        state['done'] = True
        se.stop()
    se.add_code_hook(on_end, begin=end, end=end)
    return code_va


def on_loadlib(emu, api_name, func, params):
    name = ''
    try:
        name = se.read_mem_string(params[0], 1) if params[0] else ''
    except Exception:
        pass
    if 'MSVBVM60' in name.upper() and not state['driver']:
        state['driver'] = True
        img = rd(BASE, SIZE)
        open(os.path.join(args.outdir, 'image.bin'), 'wb').write(img)
        print('dumped in-memory image (%d bytes) at LoadLibrary(%s)' % (len(img), name))
        names = box_names()
        with open(os.path.join(args.outdir, 'names.txt'), 'w', encoding='latin-1') as f:
            f.write(''.join(n + '\n' for n in names))
        print('box directory: %d names -> names.txt' % len(names))
        if args.list or not args.extract:
            state['done'] = True
            se.stop()
            return 0
        code_va = start_driver()
        # complete the stdcall ourselves and continue in the driver instead of the caller
        argc = 3 if api_name.endswith('ExA') else 1
        se.reg_write('esp', se.reg_read('esp') + 4 + 4 * argc)
        se.reg_write('eip', code_va)
        return 0
    return func(params)


def h_findfirst(emu, api_name, func, params):
    return 0xFFFFFFFF  # empty directory listing (the runtime only scans for extra boxes)


se.add_api_hook(on_loadlib, 'kernel32', 'LoadLibraryExA')
se.add_api_hook(on_loadlib, 'kernel32', 'LoadLibraryA')
se.add_api_hook(h_findfirst, 'kernel32', 'FindFirstFileA')
se.add_api_hook(on_dir_parse, 'user32', 'CharUpperBuffA')

try:
    se.run_module(module)
except Exception as e:
    print('run ended:', type(e).__name__, e)

print('driver started:', state['driver'], 'finished:', state['done'],
      'extracted:', state['extracted'], 'failed:', len(state['failed']))
if not state['done']:
    rep = se.get_report()
    for ep in rep.get('entry_points', []):
        err = ep.get('error')
        if err:
            print('error:', {k: err.get(k) for k in ('type', 'pc', 'address', 'instr')})
        for a in ep.get('apis', [])[-15:]:
            print('  ', a.get('pc'), a.get('api_name'), str(a.get('args'))[:120], a.get('ret_val'))
