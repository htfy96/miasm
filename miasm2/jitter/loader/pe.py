import struct
from collections import defaultdict

from elfesteem import pe
from elfesteem import cstruct
from elfesteem import *
from miasm2.jitter.csts import *
from utils import canon_libname_libfunc

import logging

log = logging.getLogger('loader_pe')
hnd = logging.StreamHandler()
hnd.setFormatter(logging.Formatter("[%(levelname)s]: %(message)s"))
log.addHandler(hnd)
log.setLevel(logging.CRITICAL)

def get_import_address_pe(e):
    import2addr = defaultdict(set)
    if e.DirImport.impdesc is None:
        return import2addr
    for s in e.DirImport.impdesc:
        # fthunk = e.rva2virt(s.firstthunk)
        # l = "%2d %-25s %s" % (i, repr(s.dlldescname), repr(s))
        libname = s.dlldescname.name.lower()
        for ii, imp in enumerate(s.impbynames):
            if isinstance(imp, pe.ImportByName):
                funcname = imp.name
            else:
                funcname = imp
            # l = "    %2d %-16s" % (ii, repr(funcname))
            import2addr[(libname, funcname)].add(
                e.rva2virt(s.firstthunk + e._wsize * ii / 8))
    return import2addr


def preload_pe(vm, e, runtime_lib, patch_vm_imp=True):
    fa = get_import_address_pe(e)
    dyn_funcs = {}
    # log.debug('imported funcs: %s' % fa)
    for (libname, libfunc), ads in fa.items():
        for ad in ads:
            ad_base_lib = runtime_lib.lib_get_add_base(libname)
            ad_libfunc = runtime_lib.lib_get_add_func(ad_base_lib, libfunc, ad)

            libname_s = canon_libname_libfunc(libname, libfunc)
            dyn_funcs[libname_s] = ad_libfunc
            if patch_vm_imp:
                vm.set_mem(
                    ad, struct.pack(cstruct.size2type[e._wsize], ad_libfunc))
    return dyn_funcs



def is_redirected_export(e, ad):
    # test is ad points to code or dll name
    out = ''
    for i in xrange(0x200):
        c = e.virt(ad + i)
        if c == "\x00":
            break
        out += c
        if not (c.isalnum() or c in "_.-+*$@&#()[]={}"):
            return False
    if not "." in out:
        return False
    i = out.find('.')
    return out[:i], out[i + 1:]


def get_export_name_addr_list(e):
    out = []
    # add func name
    for i, n in enumerate(e.DirExport.f_names):
        addr = e.DirExport.f_address[e.DirExport.f_nameordinals[i].ordinal]
        f_name = n.name.name
        # log.debug('%s %s' % (f_name, hex(e.rva2virt(addr.rva))))
        out.append((f_name, e.rva2virt(addr.rva)))

    # add func ordinal
    for i, o in enumerate(e.DirExport.f_nameordinals):
        addr = e.DirExport.f_address[o.ordinal]
        # log.debug('%s %s %s' % (o.ordinal, e.DirExport.expdesc.base,
        # hex(e.rva2virt(addr.rva))))
        out.append(
            (o.ordinal + e.DirExport.expdesc.base, e.rva2virt(addr.rva)))
    return out



def vm_load_pe(vm, fname, align_s=True, load_hdr=True,
               **kargs):
    e = pe_init.PE(open(fname, 'rb').read(), **kargs)

    aligned = True
    for s in e.SHList:
        if s.addr & 0xFFF:
            aligned = False
            break

    if aligned:
        if load_hdr:
            hdr_len = max(0x200, e.NThdr.sizeofheaders)
            min_len = min(e.SHList[0].addr, 0x1000)#e.NThdr.sizeofheaders)
            pe_hdr = e.content[:hdr_len]
            pe_hdr = pe_hdr + min_len * "\x00"
            pe_hdr = pe_hdr[:min_len]
            vm.add_memory_page(
                e.NThdr.ImageBase, PAGE_READ | PAGE_WRITE, pe_hdr)
        if align_s:
            for i, s in enumerate(e.SHList[:-1]):
                s.size = e.SHList[i + 1].addr - s.addr
                s.rawsize = s.size
                s.data = strpatchwork.StrPatchwork(s.data[:s.size])
                s.offset = s.addr
            s = e.SHList[-1]
            s.size = (s.size + 0xfff) & 0xfffff000
        for s in e.SHList:
            data = str(s.data)
            data += "\x00" * (s.size - len(data))
            # log.debug('SECTION %s %s' % (hex(s.addr),
            # hex(e.rva2virt(s.addr))))
            vm.add_memory_page(
                e.rva2virt(s.addr), PAGE_READ | PAGE_WRITE, data)
            # s.offset = s.addr
        return e

    # not aligned
    log.warning('pe is not aligned, creating big section')
    min_addr = None
    max_addr = None
    data = ""

    if load_hdr:
        data = e.content[:0x400]
        data += (e.SHList[0].addr - len(data)) * "\x00"
        min_addr = 0

    for i, s in enumerate(e.SHList):
        if i < len(e.SHList) - 1:
            s.size = e.SHList[i + 1].addr - s.addr
        s.rawsize = s.size
        s.offset = s.addr

        if min_addr is None or s.addr < min_addr:
            min_addr = s.addr
        if max_addr is None or s.addr + s.size > max_addr:
            max_addr = s.addr + max(s.size, len(s.data))
    min_addr = e.rva2virt(min_addr)
    max_addr = e.rva2virt(max_addr)
    log.debug('%s %s %s' %
              (hex(min_addr), hex(max_addr), hex(max_addr - min_addr)))

    vm.add_memory_page(min_addr,
                          PAGE_READ | PAGE_WRITE,
                          (max_addr - min_addr) * "\x00")
    for s in e.SHList:
        log.debug('%s %s' % (hex(e.rva2virt(s.addr)), len(s.data)))
        vm.set_mem(e.rva2virt(s.addr), str(s.data))
    return e


def vm_load_pe_lib(fname_in, libs, lib_path_base, patch_vm_imp, **kargs):
    fname = os.path.join(lib_path_base, fname_in)
    e = vm_load_pe(fname, **kargs)
    libs.add_export_lib(e, fname_in)
    # preload_pe(e, libs, patch_vm_imp)
    return e


def vm_load_pe_libs(libs_name, libs, lib_path_base="win_dll",
                    patch_vm_imp=True, **kargs):
    lib_imgs = {}
    for fname in libs_name:
        e = vm_load_pe_lib(fname, libs, lib_path_base, patch_vm_imp)
        lib_imgs[fname] = e
    return lib_imgs


def vm_fix_imports_pe_libs(lib_imgs, libs, lib_path_base="win_dll",
                           patch_vm_imp=True, **kargs):
    for e in lib_imgs.values():
        preload_pe(e, libs, patch_vm_imp)