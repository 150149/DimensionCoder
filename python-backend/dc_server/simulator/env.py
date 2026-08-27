# -*- coding: utf-8 -*-
"""进程环境伪造：栈、TEB/PEB、PEB_LDR_DATA、ProcessParameters、参数串——反反调试核心。
x86 布局（32 位程序）：FS:[0x30] → PEB；BeingDebugged=0；NtGlobalFlag=0。"""

import os

# 模拟内存布局（远离 image 0x400000 与动态解密区）
STACK_BASE = 0x70000000          # 栈底（低地址），栈顶 = +0x100000
STACK_SIZE = 0x100000
TEB_ADDR = 0x7FFD0000            # FS 段基址（TEB）
PEB_ADDR = 0x7FFDE000
LDR_ADDR = 0x7FFDF000
PARAMS_ADDR = 0x7FFE0000
ARGS_ADDR = 0x7FFE2000           # 参数字符串区（ASCII + UTF-16）
# wow64 64 位线程环境（GS 段基址 → TEB64 → PEB64）：
TEB64_ADDR = 0x7FFE4000          # x64 TEB（子执行器 GS_BASE）
PEB64_ADDR = 0x7FFE5000          # x64 PEB（TEB64+0x60 指向）
LDR64_ADDR = 0x7FFE6000          # x64 PEB_LDR_DATA
PARAMS64_ADDR = 0x7FFE7000       # x64 RTL_USER_PROCESS_PARAMETERS
ARGS64_ADDR = 0x7FFE8000         # x64 参数串区（UTF-16）
HEAP_BASE = 0x80000000           # 模拟堆起点（apistub 分配）

# x86 PEB 偏移
PEB_BEING_DEBUGGED = 0x02
PEB_LDR = 0x0C
PEB_PROCESS_PARAMETERS = 0x10
PEB_PROCESS_HEAP = 0x1C
PEB_HEAP_PTRS = 0x20
PEB_NT_GLOBAL_FLAG = 0x68

# x64 PEB 偏移
PEB64_BEING_DEBUGGED = 0x02
PEB64_LDR = 0x18
PEB64_PROCESS_PARAMETERS = 0x20
PEB64_PROCESS_HEAP = 0x30
PEB64_NT_GLOBAL_FLAG = 0xBC

# x86 TEB 偏移
TEB_SEH = 0x00
TEB_SELF = 0x18
TEB_PEB = 0x30
TEB_TLS_SLOTS = 0xE10

# x64 TEB 偏移
TEB64_SELF = 0x30
TEB64_PEB = 0x60
TEB64_STACK_BASE = 0x08
TEB64_STACK_LIMIT = 0x10

# RTL_USER_PROCESS_PARAMETERS（x86）
PP_IMAGE_PATH = 0x10             # UNICODE_STRING
PP_CMD_LINE = 0x1C
PP_ENV = 0x70

# RTL_USER_PROCESS_PARAMETERS（x64）
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
    """x64 UNICODE_STRING：Length/MaximumLength(2+2) + Buffer(8)。"""
    raw = _u16s(text)
    return len(raw).to_bytes(2, "little") + (len(raw)).to_bytes(2, "little") + \
        addr.to_bytes(8, "little") + raw[:0]


def build_env(uc, image, exe_path: str, args: list, parent_path: str = r"C:\Windows\System32\cmd.exe"):
    """装配 32 位进程环境：栈 + TEB(FS) + PEB + LDR + ProcessParameters + 参数串。
    返回 (stack_top, teb, peb, heap)。"""
    if image.is64:
        raise NotImplementedError("x64 模拟环境尚未实现（本项目目标为 32 位）")

    # 1) 栈
    uc.mem_map(STACK_BASE, STACK_SIZE)
    try:
        # 栈顶页（STACK_BASE+STACK_SIZE）：真实 Windows 栈底页已提交，
        # 程序/Warning 链可能读取栈底之上 4 字节（如 0x70100004）
        uc.mem_map(STACK_BASE + STACK_SIZE, 0x1000)
    except Exception:
        pass
    stack_top = STACK_BASE + STACK_SIZE
    from unicorn import x86_const
    uc.reg_write(x86_const.UC_X86_REG_ESP, stack_top)

    # 2) TEB（FS 段基址）
    # unicorn 2.x 在 32 位模式不支持 FS_BASE（reg_write 被忽略，FS 选择子抛异常）——
    # 实际 FS_BASE=0，因此 fs:[x] 访问地址 = x。策略：把 TEB 结构镜像到 0x0 低地址页，
    # 所有 fs:[offset] 访问（SEH 链/fs:[0x30] PEB）都能命中。
    # 权限 RW 无 X：真实进程 0x0 页不可执行（取指 → #PF → SEH 链），
    # 模拟器通过 FETCH_INVALID hook 走同样的异常驱动控制流。
    from unicorn import UC_PROT_READ, UC_PROT_WRITE
    uc.mem_map(0x0, 0x1000, UC_PROT_READ | UC_PROT_WRITE)   # FS 镜像页（FS_BASE=0 时的 TEB）
    uc.mem_map(TEB_ADDR, 0x1000)
    uc.mem_map(PEB_ADDR, 0x1000)
    _w(uc, 0x0 + TEB_SELF, TEB_ADDR.to_bytes(4, "little"))
    _w(uc, 0x0 + TEB_PEB, PEB_ADDR.to_bytes(4, "little"))
    # FS:[0] SEH 链 = 0（程序自行安装；unicorn 异常不依赖 SEH）
    _w(uc, 0x0 + TEB_SEH, b"\x00" * 4)
    _w(uc, 0x0 + TEB_TLS_SLOTS, b"\x00" * 0x20)
    # 绝对地址 TEB 同样填充（供按 TEB 绝对地址访问的代码）
    _w(uc, TEB_ADDR + TEB_SELF, TEB_ADDR.to_bytes(4, "little"))
    _w(uc, TEB_ADDR + TEB_PEB, PEB_ADDR.to_bytes(4, "little"))
    _w(uc, TEB_ADDR + TEB_SEH, b"\x00" * 4)
    _w(uc, TEB_ADDR + TEB_TLS_SLOTS, b"\x00" * 0x20)

    # 3) PEB
    uc.mem_map(HEAP_BASE, 0x1000)                    # 假堆头页
    _w(uc, PEB_ADDR + PEB_BEING_DEBUGGED, b"\x00")          # BeingDebugged = 0
    _w(uc, PEB_ADDR + PEB_LDR, LDR_ADDR.to_bytes(4, "little"))
    _w(uc, PEB_ADDR + PEB_PROCESS_PARAMETERS, PARAMS_ADDR.to_bytes(4, "little"))
    _w(uc, PEB_ADDR + PEB_PROCESS_HEAP, HEAP_BASE.to_bytes(4, "little"))
    _w(uc, PEB_ADDR + PEB_HEAP_PTRS, HEAP_BASE.to_bytes(4, "little"))
    _w(uc, PEB_ADDR + PEB_NT_GLOBAL_FLAG, b"\x00" * 4)      # NtGlobalFlag = 0
    # 堆区正常标志（HEAP_GROWABLE | HEAP_CLASS_1 = 0x140000? 实际默认 Flags=0x50000062 在调试下；
    # 正常 = 0x140000 附近——置 0x140000 即可）
    _w(uc, HEAP_BASE, (0x140000).to_bytes(4, "little"))     # Flags（假堆头）

    # 4) PEB_LDR_DATA（最小填充：程序可能遍历 Ldr 找模块）
    uc.mem_map(LDR_ADDR, 0x1000)
    _w(uc, LDR_ADDR, b"\x00" * 0x200)

    # 5) ProcessParameters
    uc.mem_map(PARAMS_ADDR, 0x2000)
    _w(uc, PARAMS_ADDR + 0x00, (0x1000).to_bytes(2, "little") + (0x1000).to_bytes(2, "little"))
    _w(uc, PARAMS_ADDR + PP_IMAGE_PATH, _uni_string(ARGS_ADDR + 0x100, exe_path))
    cmd = " ".join([exe_path] + args)
    _w(uc, PARAMS_ADDR + PP_CMD_LINE, _uni_string(ARGS_ADDR + 0x200, cmd))
    # 父进程路径（父进程检测用：ProcessParameters 无父进程字段——父进程在 PEB.ProcessParameters
    # 之上无存储；真实检测走 NtQueryInformationProcess ProcessBasicInformation → InheritedFromUniqueProcessId。
    # 此处仅伪造环境变量区（含父进程路径字符串供扫描类检测）
    env_ascii = (parent_path + "\x00").encode("ascii", "ignore")
    _w(uc, PARAMS_ADDR + PP_ENV, env_ascii + b"\x00" * 0x20)
    # 参数字符串区
    uc.mem_map(ARGS_ADDR, 0x2000)
    _w(uc, ARGS_ADDR + 0x100, _u16s(exe_path))
    _w(uc, ARGS_ADDR + 0x200, _u16s(cmd))

    return stack_top, TEB_ADDR, PEB_ADDR, HEAP_BASE


def build_env64(uc, image, exe_path: str, args: list):
    """装配 wow64 64 位线程环境（x64 TEB/PEB/LDR/ProcessParameters）。
    子执行器 GS_BASE=TEB64_ADDR，gs:[0x60] → PEB64；Meng 写 PEB64+2 经
    on_write 同步回主实例，Good（0x81098B movzx ecx,[rcx+2]）读取。
    返回 (teb64, peb64)。"""
    from unicorn import x86_const
    # 1) TEB64（GS 段基址）
    try:
        uc.mem_map(TEB64_ADDR, 0x1000)
    except Exception:
        pass
    _w(uc, TEB64_ADDR + TEB64_SELF, TEB64_ADDR.to_bytes(8, "little"))
    _w(uc, TEB64_ADDR + TEB64_PEB, PEB64_ADDR.to_bytes(8, "little"))
    _w(uc, TEB64_ADDR + TEB64_STACK_BASE, (STACK_BASE + STACK_SIZE).to_bytes(8, "little"))
    _w(uc, TEB64_ADDR + TEB64_STACK_LIMIT, STACK_BASE.to_bytes(8, "little"))
    _w(uc, TEB64_ADDR + 0x00, b"\x00" * 8)      # ExceptionList

    # 2) PEB64
    try:
        uc.mem_map(PEB64_ADDR, 0x1000)
    except Exception:
        pass
    _w(uc, PEB64_ADDR + PEB64_BEING_DEBUGGED, b"\x40")  # cmd.exe 路径选择器（用户笔记）
    _w(uc, PEB64_ADDR + 0x0C, (0x400000).to_bytes(8, "little"))  # ImageBaseAddress
    _w(uc, PEB64_ADDR + PEB64_LDR, LDR64_ADDR.to_bytes(8, "little"))
    _w(uc, PEB64_ADDR + PEB64_PROCESS_PARAMETERS, PARAMS64_ADDR.to_bytes(8, "little"))
    _w(uc, PEB64_ADDR + PEB64_PROCESS_HEAP, HEAP_BASE.to_bytes(8, "little"))
    _w(uc, PEB64_ADDR + PEB64_NT_GLOBAL_FLAG, b"\x00" * 4)

    # 3) x64 PEB_LDR_DATA + 模块链表（ntdll → CrackMe → kernel32 循环链表，真实 wow64 布局）
    try:
        uc.mem_map(LDR64_ADDR, 0x1000)
    except Exception:
        pass
    _w(uc, LDR64_ADDR, b"\x00" * 0x1000)  # 整页清零（覆盖下方节点区）
    _w(uc, LDR64_ADDR + 0x00, (0x58).to_bytes(4, "little"))  # Length（x64 PEB_LDR_DATA）
    _w(uc, LDR64_ADDR + 0x04, (1).to_bytes(4, "little"))     # Initialized
    _nodes = [  # (名称, DllBase, SizeOfImage)
        ("ntdll.dll", 0x7FFB0000, 0x1F0000),
        (os.path.basename(exe_path), 0x400000, image.size_of_image),
        ("kernel32.dll", 0x7FFA0000, 0x120000),
    ]
    for _i, (_nm, _base, _sz) in enumerate(_nodes):
        _node = LDR64_ADDR + 0x200 + _i * 0x100
        # 三条 LIST_ENTRY 均完整循环：尾节点 Flink 回链表头（LDR+0x10/+0x20/+0x28），
        # 首节点 Blink 回链表头——否则程序遍历链表永远回不到头 → 死循环
        _nl = LDR64_ADDR + 0x10 if _i == 2 else LDR64_ADDR + 0x200 + (_i + 1) * 0x100
        _pl = LDR64_ADDR + 0x10 if _i == 0 else LDR64_ADDR + 0x200 + (_i - 1) * 0x100
        _ni = LDR64_ADDR + 0x28 if _i == 2 else LDR64_ADDR + 0x200 + (_i + 1) * 0x100
        _pi = LDR64_ADDR + 0x28 if _i == 0 else LDR64_ADDR + 0x200 + (_i - 1) * 0x100
        # LDR_DATA_TABLE_ENTRY64：+0x00 InLoadOrder / +0x10 InMemoryOrder / +0x20 InInitOrder
        _w(uc, _node + 0x00, _nl.to_bytes(8, "little") + _pl.to_bytes(8, "little"))
        _w(uc, _node + 0x10, (_nl + 0x10).to_bytes(8, "little") + (_pl + 0x10).to_bytes(8, "little"))
        _w(uc, _node + 0x20, _ni.to_bytes(8, "little") + _pi.to_bytes(8, "little"))
        _w(uc, _node + 0x30, _base.to_bytes(8, "little"))     # DllBase
        _w(uc, _node + 0x38, b"\x00" * 8)                    # EntryPoint
        _w(uc, _node + 0x40, _sz.to_bytes(8, "little"))       # SizeOfImage
        # FullDllName（+0x48）与 BaseDllName（+0x58）：UNICODE_STRING64 + 节点内缓冲区
        _full = ("C:\\Windows\\System32\\" + _nm) if _i != 1 else ("C:\\" + _nm)
        _w(uc, _node + 0x48, _uni_string64(_node + 0x80, _full))
        _w(uc, _node + 0x58, _uni_string64(_node + 0xA0, _nm))
        _w(uc, _node + 0x68, (0x20).to_bytes(4, "little"))    # DllCharacteristics
        _w(uc, _node + 0x70, (0x20000).to_bytes(4, "little")) # Flags（Unicode DLL）
    # 链表头：InLoadOrder(+0x10) / InMemoryOrder(+0x20) / InInitOrder(+0x28)
    _h0 = LDR64_ADDR + 0x200
    _h2 = LDR64_ADDR + 0x400
    _w(uc, LDR64_ADDR + 0x10, _h0.to_bytes(8, "little") + _h2.to_bytes(8, "little"))
    _w(uc, LDR64_ADDR + 0x20, (_h0 + 0x10).to_bytes(8, "little") + (_h2 + 0x10).to_bytes(8, "little"))
    _w(uc, LDR64_ADDR + 0x28, (_h0 + 0x20).to_bytes(8, "little") + (_h2 + 0x20).to_bytes(8, "little"))

    # 4) x64 ProcessParameters + 参数串
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
