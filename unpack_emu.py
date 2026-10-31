"""Emulate a packed VB exe with speakeasy until execution reaches the original code range; dump the image.
usage: python -I unpack_emu.py <exe> <out_dump> <text_lo> <text_hi> [extra files to expose next to the exe...]
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'emu'))
import speakeasy
import logging
logging.basicConfig(level=logging.INFO, format='%(message)s')

exe = sys.argv[1]
out = sys.argv[2]
lo = int(sys.argv[3], 16)
hi = int(sys.argv[4], 16)
extras = sys.argv[5:]

root = os.path.dirname(speakeasy.__file__)
cfg = json.load(open(os.path.join(root, 'configs', 'default.json')))
cfg['timeout'] = 3600
cfg['max_api_count'] = 10_000_000
files = [{'mode': 'full_path', 'emu_path': 'c:\\windows\\system32\\' + os.path.basename(exe), 'path': exe}]
for x in extras:
    files.append({'mode': 'full_path', 'emu_path': 'c:\\windows\\system32\\' + os.path.basename(x), 'path': x})
cfg['filesystem']['files'] = files + cfg['filesystem']['files']

se = speakeasy.Speakeasy(config=cfg)
module = se.load_module(exe)
state = {'hit': None, 'count': 0}
WATCH = {}


import collections
ring = collections.deque(maxlen=400)


def on_code(emu, addr, size, ctx):
    state['count'] += 1
    ring.append(addr)
    if addr in WATCH:
        WATCH[addr](emu)
    if lo <= addr < hi and state['hit'] is None:
        state['hit'] = addr
        print('Reached original code at %#x after %d insns' % (addr, state['count']))
        base = module.get_base()
        data = se.mem_read(base, module.get_image_size())
        open(out, 'wb').write(data)
        print('dumped', len(data))
        se.stop()


def _cstr(a):
    b = se.mem_read(a, 128)
    return b.split(bytes(1))[0].decode('latin-1')


def w_resolve(emu):
    edx = se.reg_read('edx'); eax = se.reg_read('eax')
    print('resolve', hex(eax), _cstr(edx))


WATCH[0x432b38] = w_resolve
DUMPED = {}


def w_dirdump(emu):
    if 'dir' not in DUMPED:
        DUMPED['dir'] = 1
        open(out + '.dir', 'wb').write(se.mem_read(0x9000, 0x1310))
        print('dumped directory')


WATCH[0x43b2b5] = w_dirdump


def h_findfirst(emu, api_name, func, params):
    # Report an empty directory: the packer runtime only scans for extra packages here.
    return 0xFFFFFFFF


se.add_api_hook(h_findfirst, 'kernel32', 'FindFirstFileA')
se.add_code_hook(on_code)
try:
    se.run_module(module)
except Exception as e:
    print('run ended:', type(e).__name__, e)
print('hit', state['hit'], 'insns', state['count'])
open(out + '.end', 'wb').write(se.mem_read(module.get_base(), module.get_image_size()))
print('last pcs:', ' '.join('%x' % a for a in list(ring)[-120:]))
try:
    for r in se.emu.get_mem_maps():
        print('map', hex(r.get_base()), hex(r.get_size()), r.get_tag())
except Exception as e:
    print('maps err', e)
rep = se.get_report()
for ep in rep.get('entry_points', []):
    err = ep.get('error')
    if err:
        err = {k: err.get(k) for k in ('type', 'pc', 'address', 'instr', 'regs')}
    print('EP', ep.get('ep_type'), ep.get('start_addr'), 'error:', err)
    for a in ep.get('apis', [])[-40:]:
        print('  ', a.get('pc'), a.get('api_name'), str(a.get('args'))[:150], a.get('ret_val'))
