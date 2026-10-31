"""Quick VB5/6 header inspector: prints project info, objects, and p-code/native status."""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib'))
import pefile


class Img:
    def __init__(self, path):
        self.pe = pefile.PE(path)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.mem = self.pe.get_memory_mapped_image()

    def u32(self, va):
        return struct.unpack_from('<I', self.mem, va - self.base)[0]

    def u16(self, va):
        return struct.unpack_from('<H', self.mem, va - self.base)[0]

    def cstr(self, va):
        o = va - self.base
        e = self.mem.index(b'\0', o)
        return self.mem[o:e].decode('latin-1')

    def find(self, sig):
        return self.mem.find(sig) + self.base


def main(path):
    im = Img(path)
    hdr = im.find(b'VB5!')
    print('VBHeader at', hex(hdr))
    proj = im.u32(hdr + 0x30)
    print('ProjectName', im.cstr(hdr + im.u32(hdr + 0x64)), 'Exe', im.cstr(hdr + im.u32(hdr + 0x5C)))
    print('FormCount', im.u16(hdr + 0x44), 'ExternalCount', im.u16(hdr + 0x46))
    native = im.u32(proj + 0x20)
    print('lpNativeCode', hex(native), '=> NATIVE' if native else '=> P-CODE')
    otab = im.u32(proj + 0x04)
    total = im.u16(otab + 0x2A)
    arr = im.u32(otab + 0x30)
    for i in range(total):
        d = arr + i * 0x30
        name = im.cstr(im.u32(d + 0x18))
        ftype = im.u32(d + 0x28)
        oi = im.u32(d + 0x00)
        mcount = im.u32(d + 0x1C)
        print(f'  obj {i}: {name:24s} type={ftype:#x} methods={mcount} objinfo={oi:#x}')


if __name__ == '__main__':
    for p in sys.argv[1:]:
        print('=====', p)
        try:
            main(p)
        except Exception as e:
            print('ERR', e)
