# -*- coding: utf-8 -*-

import os

STACK_BASE = 0x70000000
STACK_SIZE = 0x100000
TEB_ADDR = 0x7FFD0000
PEB_ADDR = 0x7FFDE000
LDR_ADDR = 0x7FFDF000
PARAMS_ADDR = 0x7FFE0000
ARGS_ADDR = 0x7FFE2000
TEB64_ADDR = 0x7FFE4000
PEB64_ADDR = 0x7FFE5000
LDR64_ADDR = 0x7FFE6000
PARAMS64_ADDR = 0x7FFE7000
ARGS64_ADDR = 0x7FFE8000
HEAP_BASE = 0x80000000

PEB_BEING_DEBUGGED = 0x02
PEB_LDR = 0x0C
PEB_PROCESS_PARAMETERS = 0x10
PEB_PROCESS_HEAP = 0x1C
PEB_HEAP_PTRS = 0x20
PEB_NT_GLOBAL_FLAG = 0x68

PEB64_BEING_DEBUGGED = 0x02
PEB64_LDR = 0x18
PEB64_PROCESS_PARAMETERS = 0x20
PEB64_PROCESS_HEAP = 0x30
PEB64_NT_GLOBAL_FLAG = 0xBC

TEB_SEH = 0x00
TEB_SELF = 0x18
TEB_PEB = 0x30
TEB_TLS_SLOTS = 0xE10

TEB64_SELF = 0x30
TEB64_PEB = 0x60
TEB64_STACK_BASE = 0x08
TEB64_STACK_LIMIT = 0x10

PP_IMAGE_PATH = 0x10
PP_CMD_LINE = 0x1C
PP_ENV = 0x70

PP64_IMAGE_PATH = 0x10
PP64_CMD_LINE = 0x38
PP64_ENV = 0x80

def _w(uc, addr: int, data: bytes) -> None:
    uc.mem_write(addr, data)

def _u16s(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"

def _uni_string(addr: int, text: str) -> bytes:
    raw = _u16s(text)
    return len(raw).to_bytes(2, "little") + (len(raw)).to_bytes(2, "little") + \
        addr.to_bytes(4, "little") + raw[:0]

def _uni_string64(addr: int, text: str) -> bytes:
    raw = _u16s(text)
    return len(raw).to_bytes(2, "little") + (len(raw)).to_bytes(2, "little") + \
        addr.to_bytes(8, "little") + raw[:0]

def build_env(uc, image, exe_path: str, args: list, parent_path: str = r"C:\Windows\System32\cmd.exe"):
    if image.is64:
        raise NotImplementedError("x64 模拟环境尚未实现（本项目目标为 32 位）")

    uc.mem_map(STACK_BASE, STACK_SIZE)
    try:
        uc.mem_map(STACK_BASE + STACK_SIZE, 0x1000)
    except Exception:
        pass
    stack_top = STACK_BASE + STACK_SIZE
    from unicorn import x86_const
    uc.reg_write(x86_const.UC_X86_REG_ESP, stack_top)

    from unicorn import UC_PROT_READ, UC_PROT_WRITE
    uc.mem_map(0x0, 0x1000, UC_PROT_READ | UC_PROT_WRITE)
    uc.mem_map(TEB_ADDR, 0x1000)
    uc.mem_map(PEB_ADDR, 0x1000)
    _w(uc, 0x0 + TEB_SELF, TEB_ADDR.to_bytes(4, "little"))
    _w(uc, 0x0 + TEB_PEB, PEB_ADDR.to_bytes(4, "little"))
    _w(uc, 0x0 + TEB_SEH, b"\x00" * 4)
    _w(uc, 0x0 + TEB_TLS_SLOTS, b"\x00" * 0x20)
    _w(uc, TEB_ADDR + TEB_SELF, TEB_ADDR.to_bytes(4, "little"))
    _w(uc, TEB_ADDR + TEB_PEB, PEB_ADDR.to_bytes(4, "little"))
    _w(uc, TEB_ADDR + TEB_SEH, b"\x00" * 4)
    _w(uc, TEB_ADDR + TEB_TLS_SLOTS, b"\x00" * 0x20)

    uc.mem_map(HEAP_BASE, 0x1000)
    _w(uc, PEB_ADDR + PEB_BEING_DEBUGGED, b"\x00")
    _w(uc, PEB_ADDR + PEB_LDR, LDR_ADDR.to_bytes(4, "little"))
    _w(uc, PEB_ADDR + PEB_PROCESS_PARAMETERS, PARAMS_ADDR.to_bytes(4, "little"))
    _w(uc, PEB_ADDR + PEB_PROCESS_HEAP, HEAP_BASE.to_bytes(4, "little"))
    _w(uc, PEB_ADDR + PEB_HEAP_PTRS, HEAP_BASE.to_bytes(4, "little"))
    _w(uc, PEB_ADDR + PEB_NT_GLOBAL_FLAG, b"\x00" * 4)
    _w(uc, HEAP_BASE, (0x140000).to_bytes(4, "little"))

    uc.mem_map(LDR_ADDR, 0x1000)
    _w(uc, LDR_ADDR, b"\x00" * 0x200)

    uc.mem_map(PARAMS_ADDR, 0x2000)
    _w(uc, PARAMS_ADDR + 0x00, (0x1000).to_bytes(2, "little") + (0x1000).to_bytes(2, "little"))
    _w(uc, PARAMS_ADDR + PP_IMAGE_PATH, _uni_string(ARGS_ADDR + 0x100, exe_path))
    cmd = " ".join([exe_path] + args)
    _w(uc, PARAMS_ADDR + PP_CMD_LINE, _uni_string(ARGS_ADDR + 0x200, cmd))
    env_ascii = (parent_path + "\x00").encode("ascii", "ignore")
    _w(uc, PARAMS_ADDR + PP_ENV, env_ascii + b"\x00" * 0x20)
    uc.mem_map(ARGS_ADDR, 0x2000)
    _w(uc, ARGS_ADDR + 0x100, _u16s(exe_path))
    _w(uc, ARGS_ADDR + 0x200, _u16s(cmd))

    return stack_top, TEB_ADDR, PEB_ADDR, HEAP_BASE

def build_env64(uc, image, exe_path: str, args: list):
    from unicorn import x86_const
    try:
        uc.mem_map(TEB64_ADDR, 0x1000)
    except Exception:
        pass
    _w(uc, TEB64_ADDR + TEB64_SELF, TEB64_ADDR.to_bytes(8, "little"))
    _w(uc, TEB64_ADDR + TEB64_PEB, PEB64_ADDR.to_bytes(8, "little"))
    _w(uc, TEB64_ADDR + TEB64_STACK_BASE, (STACK_BASE + STACK_SIZE).to_bytes(8, "little"))
    _w(uc, TEB64_ADDR + TEB64_STACK_LIMIT, STACK_BASE.to_bytes(8, "little"))
    _w(uc, TEB64_ADDR + 0x00, b"\x00" * 8)

    try:
        uc.mem_map(PEB64_ADDR, 0x1000)
    except Exception:
        pass
    _w(uc, PEB64_ADDR + PEB64_BEING_DEBUGGED, b"\x40")
    _w(uc, PEB64_ADDR + 0x0C, (0x400000).to_bytes(8, "little"))
    _w(uc, PEB64_ADDR + PEB64_LDR, LDR64_ADDR.to_bytes(8, "little"))
    _w(uc, PEB64_ADDR + PEB64_PROCESS_PARAMETERS, PARAMS64_ADDR.to_bytes(8, "little"))
    _w(uc, PEB64_ADDR + PEB64_PROCESS_HEAP, HEAP_BASE.to_bytes(8, "little"))
    _w(uc, PEB64_ADDR + PEB64_NT_GLOBAL_FLAG, b"\x00" * 4)

    try:
        uc.mem_map(LDR64_ADDR, 0x1000)
    except Exception:
        pass
    _w(uc, LDR64_ADDR, b"\x00" * 0x1000)
    _w(uc, LDR64_ADDR + 0x00, (0x58).to_bytes(4, "little"))
    _w(uc, LDR64_ADDR + 0x04, (1).to_bytes(4, "little"))
    _nodes = [
        ("ntdll.dll", 0x7FFB0000, 0x1F0000),
        (os.path.basename(exe_path), 0x400000, image.size_of_image),
        ("kernel32.dll", 0x7FFA0000, 0x120000),
    ]
    for _i, (_nm, _base, _sz) in enumerate(_nodes):
        _node = LDR64_ADDR + 0x200 + _i * 0x100
        _nl = LDR64_ADDR + 0x10 if _i == 2 else LDR64_ADDR + 0x200 + (_i + 1) * 0x100
        _pl = LDR64_ADDR + 0x10 if _i == 0 else LDR64_ADDR + 0x200 + (_i - 1) * 0x100
        _ni = LDR64_ADDR + 0x28 if _i == 2 else LDR64_ADDR + 0x200 + (_i + 1) * 0x100
        _pi = LDR64_ADDR + 0x28 if _i == 0 else LDR64_ADDR + 0x200 + (_i - 1) * 0x100
        _w(uc, _node + 0x00, _nl.to_bytes(8, "little") + _pl.to_bytes(8, "little"))
        _w(uc, _node + 0x10, (_nl + 0x10).to_bytes(8, "little") + (_pl + 0x10).to_bytes(8, "little"))
        _w(uc, _node + 0x20, _ni.to_bytes(8, "little") + _pi.to_bytes(8, "little"))
        _w(uc, _node + 0x30, _base.to_bytes(8, "little"))
        _w(uc, _node + 0x38, b"\x00" * 8)
        _w(uc, _node + 0x40, _sz.to_bytes(8, "little"))
        _full = ("C:\\Windows\\System32\\" + _nm) if _i != 1 else ("C:\\" + _nm)
        _w(uc, _node + 0x48, _uni_string64(_node + 0x80, _full))
        _w(uc, _node + 0x58, _uni_string64(_node + 0xA0, _nm))
        _w(uc, _node + 0x68, (0x20).to_bytes(4, "little"))
        _w(uc, _node + 0x70, (0x20000).to_bytes(4, "little"))
    _h0 = LDR64_ADDR + 0x200
    _h2 = LDR64_ADDR + 0x400
    _w(uc, LDR64_ADDR + 0x10, _h0.to_bytes(8, "little") + _h2.to_bytes(8, "little"))
    _w(uc, LDR64_ADDR + 0x20, (_h0 + 0x10).to_bytes(8, "little") + (_h2 + 0x10).to_bytes(8, "little"))
    _w(uc, LDR64_ADDR + 0x28, (_h0 + 0x20).to_bytes(8, "little") + (_h2 + 0x20).to_bytes(8, "little"))

    try:
        uc.mem_map(PARAMS64_ADDR, 0x1000)
    except Exception:
        pass
    try:
        uc.mem_map(ARGS64_ADDR, 0x1000)
    except Exception:
        pass
    _w(uc, PARAMS64_ADDR + PP64_IMAGE_PATH, _uni_string64(ARGS64_ADDR + 0x100, exe_path))
    cmd = " ".join([exe_path] + args)
    _w(uc, PARAMS64_ADDR + PP64_CMD_LINE, _uni_string64(ARGS64_ADDR + 0x200, cmd))
    env_ascii = (r"C:\Windows\System32\cmd.exe" + "\x00").encode("ascii", "ignore")
    _w(uc, PARAMS64_ADDR + PP64_ENV, env_ascii + b"\x00" * 0x20)
    _w(uc, ARGS64_ADDR + 0x100, _u16s(exe_path))
    _w(uc, ARGS64_ADDR + 0x200, _u16s(cmd))

    return TEB64_ADDR, PEB64_ADDR
