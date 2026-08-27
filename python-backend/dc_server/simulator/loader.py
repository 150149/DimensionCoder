# -*- coding: utf-8 -*-
"""通用 PE 加载器：解析 PE 头（32/64 位）、节区、重定位、导入表、TLS 回调、导出表。"""
import ctypes
from dataclasses import dataclass, field
from typing import Optional, List, Tuple


@dataclass
class ImportEntry:
    dll: str            # DLL 名（如 kernel32.dll）
    name: str           # 函数名（序号导入为空串）
    ordinal: int        # 序号导入时为序号，否则 0
    iat_addr: int       # IAT 槽绝对地址（stub 安装时改写）
    hint: int = 0
    enc_val: int = 0    # 磁盘 IAT 槽原始值（程序可能运行时解密——加密值）


@dataclass
class ExportName:
    name: str
    func_rva: int       # AddressOfFunctions 槽对应的 RVA（未解析地址时 0）


@dataclass
class Image:
    path: str
    is64: bool
    image_base: int
    entry_point: int            # RVA（加载后 VA = image_base + entry_point）
    size_of_image: int
    sections: list              # [(vaddr, vsize, raw_ptr, raw_size, name)]
    imports: list = field(default_factory=list)      # [ImportEntry]
    exports: list = field(default_factory=list)      # [ExportName]
    tls_callbacks: list = field(default_factory=list)  # [VA]
    relocs: list = field(default_factory=list)       # [(vaddr, [word_delta...])]
    subsystem: int = 3
    stack_reserve: int = 0x100000
    heap_reserve: int = 0x100000
    load_config_flags: int = 0
    security_cookie: int = 0      # __security_cookie 变量 VA（LoadConfig）


class _DosHdr(ctypes.Structure):
    _fields_ = [("e_magic", ctypes.c_uint16), ("e_cblp", ctypes.c_uint16),
                ("e_cp", ctypes.c_uint16), ("e_crlc", ctypes.c_uint16),
                ("e_cparhdr", ctypes.c_uint16), ("e_minalloc", ctypes.c_uint16),
                ("e_maxalloc", ctypes.c_uint16), ("e_ss", ctypes.c_uint16),
                ("e_sp", ctypes.c_uint16), ("e_csum", ctypes.c_uint16),
                ("e_ip", ctypes.c_uint16), ("e_cs", ctypes.c_uint16),
                ("e_lfarlc", ctypes.c_uint16), ("e_ovno", ctypes.c_uint16),
                ("e_res", ctypes.c_uint16 * 4), ("e_oemid", ctypes.c_uint16),
                ("e_oeminfo", ctypes.c_uint16), ("e_res2", ctypes.c_uint16 * 10),
                ("e_lfanew", ctypes.c_uint32)]


def _u16(b: bytes, off: int) -> int:
    return ctypes.c_uint16.from_buffer_copy(b[off:off + 2]).value


def _u32(b: bytes, off: int) -> int:
    return ctypes.c_uint32.from_buffer_copy(b[off:off + 4]).value


def _u64(b: bytes, off: int) -> int:
    return ctypes.c_uint64.from_buffer_copy(b[off:off + 8]).value


def load_pe(path: str, preferred_base: Optional[int] = None) -> Image:
    """解析 PE 文件为 Image 元数据。preferred_base 指定时应用重定位到该基址。"""
    with open(path, "rb") as f:
        data = f.read()
    dos = _DosHdr.from_buffer_copy(data[:0x40])
    if dos.e_magic != 0x5A4D:
        raise ValueError(f"非 PE 文件（无 MZ 头）: {path}")
    pe_off = dos.e_lfanew
    if pe_off + 4 > len(data) or _u32(data, pe_off) != 0x00004550:
        raise ValueError(f"PE 签名缺失: {path}")
    coff = pe_off + 4
    machine = _u16(data, coff)
    n_sections = _u16(data, coff + 2)
    opt_size = _u16(data, coff + 16)
    opt = coff + 20
    if opt + opt_size > len(data):
        raise ValueError("可选头越界")
    opt_magic = _u16(data, opt)
    if opt_magic not in (0x10B, 0x20B):
        raise ValueError(f"未知可选头 magic: 0x{opt_magic:X}")
    is64 = opt_magic == 0x20B

    # 可选头字段（按 32/64 布局偏移）
    if is64:
        image_base = _u64(data, opt + 24)
        entry_rva = _u32(data, opt + 16)
        size_image = _u32(data, opt + 56)
        subsystem = _u16(data, opt + 68)
        stack_reserve = _u64(data, opt + 72)
        heap_reserve = _u64(data, opt + 96)
        dd_off = opt + 112
        n_opt = 15
    else:
        image_base = _u32(data, opt + 28)
        entry_rva = _u32(data, opt + 16)
        size_image = _u32(data, opt + 56)
        subsystem = _u16(data, opt + 68)
        stack_reserve = _u32(data, opt + 72)
        heap_reserve = _u32(data, opt + 96)
        dd_off = opt + 96
        n_opt = 16

    def dd(i: int):
        off = dd_off + i * 8
        return _u32(data, off), _u32(data, off + 4)

    base = preferred_base if preferred_base is not None else image_base
    if base != image_base:
        # 重定位后基址变化：entry 等 RVA 不变，仅基址变
        pass

    # 节区
    sec_off = opt + opt_size
    sections = []
    for i in range(n_sections):
        off = sec_off + i * 40
        if off + 40 > len(data):
            break
        name = data[off:off + 8].rstrip(b"\x00").decode("ascii", "ignore")
        vsize = _u32(data, off + 8)
        vaddr = _u32(data, off + 12)
        raw_size = _u32(data, off + 16)
        raw_ptr = _u32(data, off + 20)
        sections.append((vaddr, vsize, raw_ptr, raw_size, name))

    def rva2off(rva: int):
        for va, vs, ro, rs, _nm in sections:
            if ro and va <= rva < va + vs:
                return ro + (rva - va)
        return None

    # 导入表（dd[1] = import）
    imports = []
    imp_rva, imp_size = dd(1)
    if imp_rva and imp_size:
        off = rva2off(imp_rva)
        if off is not None:
            while off + 20 <= len(data):
                oft = _u32(data, off)           # OriginalFirstThunk
                dll_rva = _u32(data, off + 12)  # Name
                iat_rva = _u32(data, off + 16)  # FirstThunk
                if not oft and not iat_rva and not dll_rva:
                    break
                if not dll_rva:
                    break
                dll_off = rva2off(dll_rva)
                dll_name = ""
                if dll_off is not None:
                    end = data.find(b"\x00", dll_off)
                    dll_name = data[dll_off:end].decode("ascii", "ignore") if end != -1 else ""
                thunk_rva = oft if oft else iat_rva
                thunk_off = rva2off(thunk_rva)
                iat_off = rva2off(iat_rva)
                if thunk_off is not None and iat_off is not None:
                    step = 8 if is64 else 4
                    for j in range(0, 0x4000, step):
                        if thunk_off + j + step > len(data):
                            break
                        if is64:
                            val = _u64(data, thunk_off + j)
                        else:
                            val = _u32(data, thunk_off + j)
                        if val == 0:
                            break
                        if val & 0x8000000000000000 if is64 else val & 0x80000000:
                            # 序号导入
                            imports.append(ImportEntry(dll_name, "", val & 0xFFFF,
                                                       base + iat_rva + j))
                        else:
                            hint_off = rva2off(val & 0x7FFFFFFF)
                            fn = ""
                            hint = 0
                            if hint_off is not None and hint_off + 2 <= len(data):
                                hint = _u16(data, hint_off)
                                end = data.find(b"\x00", hint_off + 2)
                                fn = data[hint_off + 2:end].decode("ascii", "ignore") if end != -1 else ""
                            # 磁盘 IAT 槽原始值（运行时解密型保护的加密值）
                            enc = _u32(data, iat_off + j) if not is64 else _u64(data, iat_off + j)
                            imports.append(ImportEntry(dll_name, fn, 0,
                                                       base + iat_rva + j, hint, enc))
                off += 20

    # 重定位表（dd[5] = base reloc）
    relocs = []
    rel_rva, rel_size = dd(5)
    if rel_rva and rel_size and base != image_base:
        off = rva2off(rel_rva)
        if off is not None:
            while off + 8 <= len(data) and off < len(data):
                page = _u32(data, off)
                size = _u32(data, off + 4)
                if size < 8:
                    break
                count = (size - 8) // 2
                items = []
                for k in range(count):
                    if off + 8 + k * 2 + 2 > len(data):
                        break
                    v = _u16(data, off + 8 + k * 2)
                    typ = v >> 12
                    delta = v & 0xFFF
                    if typ == 3:          # HIGHLOW
                        items.append(delta)
                    elif typ == 10:       # DIR64
                        items.append(delta)
                if items:
                    relocs.append((page, items))
                off += size

    # TLS 回调（dd[9] = tls）
    tls_callbacks = []
    tls_rva, tls_size = dd(9)
    if tls_rva:
        off = rva2off(tls_rva)
        if off is not None:
            if is64:
                addr_of_callbacks = _u64(data, off + 24)
            else:
                addr_of_callbacks = _u32(data, off + 12)
            if addr_of_callbacks:
                cb_rva = addr_of_callbacks - image_base
                cb_off = rva2off(cb_rva)
                step = 8 if is64 else 4
                if cb_off is not None:
                    k = 0
                    while cb_off + k * step + step <= len(data):
                        if is64:
                            v = _u64(data, cb_off + k * step)
                        else:
                            v = _u32(data, cb_off + k * step)
                        if v == 0:
                            break
                        tls_callbacks.append(v)
                        k += 1

    # 导出表（dd[0]）
    exports = []
    exp_rva, exp_size = dd(0)
    if exp_rva and exp_size:
        off = rva2off(exp_rva)
        if off is not None and off + 40 <= len(data):
            num_funcs = _u32(data, off + 20)
            num_names = _u32(data, off + 24)
            funcs_rva = _u32(data, off + 28)
            names_rva = _u32(data, off + 32)
            ords_rva = _u32(data, off + 36)
            funcs_off = rva2off(funcs_rva)
            names_off = rva2off(names_rva)
            ords_off = rva2off(ords_rva)
            if funcs_off is not None and names_off is not None and ords_off is not None:
                for k in range(num_names):
                    if k * 4 + 4 > len(data) or names_off + k * 4 + 4 > len(data):
                        break
                    nrva = _u32(data, names_off + k * 4)
                    no = rva2off(nrva)
                    if no is None:
                        continue
                    end = data.find(b"\x00", no)
                    nm = data[no:end].decode("ascii", "ignore") if end != -1 else ""
                    ord_idx = _u16(data, ords_off + k * 2)
                    frva = _u32(data, funcs_off + ord_idx * 4) if funcs_off + ord_idx * 4 + 4 <= len(data) else 0
                    exports.append(ExportName(nm, frva))

    # Load Config（dd[10]）安全 cookie 与标志
    load_cfg_flags = 0
    security_cookie = 0
    lc_rva, lc_size = dd(10)
    if lc_rva and lc_size >= 64:
        off = rva2off(lc_rva)
        if off is not None and off + 72 <= len(data):
            if is64:
                load_cfg_flags = _u32(data, off + 48)
                security_cookie = _u64(data, off + 88)
            else:
                load_cfg_flags = _u32(data, off + 52)
                security_cookie = _u32(data, off + 60)

    return Image(path=path, is64=is64, image_base=base, entry_point=entry_rva,
                 size_of_image=size_image, sections=sections, imports=imports,
                 exports=exports, tls_callbacks=tls_callbacks, relocs=relocs,
                 subsystem=subsystem, stack_reserve=stack_reserve,
                 heap_reserve=heap_reserve, load_config_flags=load_cfg_flags,
                 security_cookie=security_cookie)


def apply_relocs(image: Image, data: bytes, preferred_base: int) -> bytes:
    """preferred_base 与文件默认基址不同时，对映射字节应用重定位（HIGHLOW/DIR64）。
    返回修正后的字节（仅含 raw 数据覆盖的节区内容，与原文件同尺寸）。"""
    if preferred_base == image.image_base or not image.relocs:
        return data
    delta = preferred_base - image.image_base
    out = bytearray(data)
    for page_rva, items in image.relocs:
        for d in items:
            # 需要重定位的位置在文件中的偏移 = 节区定位
            off = None
            for va, vs, ro, rs, _nm in image.sections:
                if ro and va <= page_rva + d < va + vs:
                    off = ro + (page_rva + d - va)
                    break
            if off is None or off + 4 > len(out):
                continue
            if image.is64:
                if off + 8 > len(out):
                    continue
                v = _u64(out, off)
                _write_u64(out, off, (v + delta) & 0xFFFFFFFFFFFFFFFF)
            else:
                v = _u32(out, off)
                _write_u32(out, off, (v + delta) & 0xFFFFFFFF)
    return bytes(out)


def _write_u32(b: bytearray, off: int, v: int) -> None:
    b[off:off + 4] = ctypes.c_uint32(v).value.to_bytes(4, "little")


def _write_u64(b: bytearray, off: int, v: int) -> None:
    b[off:off + 8] = ctypes.c_uint64(v).value.to_bytes(8, "little")
