"""Idempotent patches applied to the pip-installed speakeasy-emulator / unicorn 1.0.2 in emu/.

Install first:  python -m pip install --target emu speakeasy-emulator
Then run:       python -I patch_speakeasy.py

Patches:
  * unicorn 1.0.2 binding: drop distutils/pkg_resources (removed from modern Python).
  * kernel32.SetFilePointer: treat the distance as a signed LONG (seeking backwards from EOF).
  * Add API handlers the packer runtime (Classic Troopanum / Troopanum 2.0) resolves by name.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.join(HERE, 'emu')


def patch(path, old, new, marker=None):
    s = open(path, encoding='utf-8').read()
    if (marker or new) in s:
        return False
    if old not in s:
        raise SystemExit('anchor not found in %s: %r' % (path, old[:60]))
    s = s.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(s)
    return True


uc = os.path.join(EMU, 'unicorn', 'unicorn.py')
s = open(uc, encoding='utf-8').read()
s2 = s.replace("import distutils.sysconfig\nimport pkg_resources\n", "")
s2 = s2.replace("              pkg_resources.resource_filename(__name__, 'lib'),\n", "")
s2 = s2.replace("              distutils.sysconfig.get_python_lib(),\n", "")
if s2 != s:
    open(uc, 'w', encoding='utf-8').write(s2)
    print('patched unicorn binding')

k32 = os.path.join(EMU, 'speakeasy', 'winenv', 'api', 'usermode', 'kernel32.py')
u32 = os.path.join(EMU, 'speakeasy', 'winenv', 'api', 'usermode', 'user32.py')
g32 = os.path.join(EMU, 'speakeasy', 'winenv', 'api', 'usermode', 'gdi32.py')

patch(k32, """            # TODO add high offset, log access?
            f.seek(lDistanceToMove, dwMoveMethod)
            rv = f.tell()""", """            # TODO add high offset, log access?
            if lDistanceToMove & 0x80000000:
                lDistanceToMove -= 0x100000000
            f.seek(lDistanceToMove, dwMoveMethod)
            rv = f.tell() & 0xFFFFFFFF""")

K32_EXTRA = '''    @apihook('FlushInstructionCache', argc=3)
    def FlushInstructionCache(self, emu, argv, ctx={}):
        return 1

    @apihook('GetFileTime', argc=4)
    def GetFileTime(self, emu, argv, ctx={}):
        # fixed timestamp (2011-02-27) for creation / access / write times
        ft = (129432096000000000).to_bytes(8, 'little')
        for p in argv[1:]:
            if p:
                self.mem_write(p, ft)
        return 1

    @apihook('RtlUnwind', argc=4)
    def RtlUnwind(self, emu, argv, ctx={}):
        return 0

    @apihook('GetTempFileName', argc=4)
    def GetTempFileName(self, emu, argv, ctx={}):
        path, prefix, unique, out = argv
        cw = self.get_char_width(ctx)
        name = 'C:\\\\Windows\\\\Temp\\\\tmp%04X.tmp' % ((unique or 0x1234) & 0xffff)
        if out:
            self.write_mem_string(name, out, cw)
        return (unique or 0x1234) & 0xffff

    @apihook('FormatMessage', argc=7)
    def FormatMessage(self, emu, argv, ctx={}):
        return 0

'''
patch(k32, "    @apihook('GetCurrentProcess', argc=0)\n",
      K32_EXTRA + "    @apihook('GetCurrentProcess', argc=0)\n", marker="apihook('GetFileTime'")

U32_EXTRA = '''    @apihook('ChangeDisplaySettings', argc=2)
    def ChangeDisplaySettings(self, emu, argv, ctx={}):
        return 0

    @apihook('LoadImage', argc=6)
    def LoadImage(self, emu, argv, ctx={}):
        return 0x4440

'''
G32_EXTRA = '''    @apihook('AddFontResource', argc=1)
    def AddFontResource(self, emu, argv, ctx={}):
        return 1

    @apihook('RemoveFontResource', argc=1)
    def RemoveFontResource(self, emu, argv, ctx={}):
        return 1

'''


def add_methods(path, extra, marker):
    s = open(path, encoding='utf-8').read()
    if marker in s:
        return
    i = s.index('    @apihook(')  # insert before first handler in the class
    s = s[:i] + extra + s[i:]
    open(path, 'w', encoding='utf-8').write(s)
    print('patched', os.path.basename(path))


add_methods(u32, U32_EXTRA, "apihook('ChangeDisplaySettings'")
add_methods(g32, G32_EXTRA, "apihook('AddFontResource'")
# File objects: load lazily on seek/tell too, and compute the size without re-reading the data
fm = os.path.join(EMU, 'speakeasy', 'windows', 'fileman.py')
patch(fm, """    def seek(self, offset, whence):
        if whence not in [io.SEEK_CUR, io.SEEK_SET, io.SEEK_END]:
            return
        if self.data:""", """    def seek(self, offset, whence):
        if whence not in [io.SEEK_CUR, io.SEEK_SET, io.SEEK_END]:
            return
        if not self.data and self.config:
            self.data = self.handle_file_data()
        if self.data:""")
patch(fm, """    def tell(self):
        if self.data:""", """    def tell(self):
        if not self.data and self.config:
            self.data = self.handle_file_data()
        if self.data:""")
patch(fm, """        off = self.data.tell()
        self.data.seek(0, io.SEEK_SET)
        size = len(self.data.read())
        self.data.seek(off, io.SEEK_SET)
        return size""", """        return self.data.getbuffer().nbytes""")
print('fileman patched')
print('speakeasy patches applied')
