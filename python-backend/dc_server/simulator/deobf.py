# -*- coding: utf-8 -*-
"""动态去混淆：指令矫正层（Instruction Fixer）。

借鉴 d810-ng 模式匹配引擎（pattern_speedups.py 的指纹索引 + 快速拒绝 +
结构匹配）与不透明谓词规则库（opaque.py 恒等模式字节级翻译），实现
故障驱动的指令矫正：正常路径零开销，仅在异常/乱码卡点调用。

规则：
  R1 junk_short_jump  短跳跳过垃圾区（eb rel，rel∈[2,16]，目标可反汇编）
  R2 pushfd_wrapper   pushfd/popfd 包裹消除（9c ... 9d，中间无副作用）
  R3 const_cond_jump  恒定条件跳转（结合具体寄存器值判定恒真/恒假）
  R4 backscan_jump    垃圾流回退：异常点向前 ≤16 字节扫描 jmp/call
  R5 jumptable        跳转表读取（mov reg,[base+idx*N]; jmp [table+reg*M]）

规则注册表（d810 VerifiableRule 思想）：RULES 元组 + 首字节指纹索引
（OpcodeIndexedStorage：O(1) 分发，先指纹快速拒绝再结构匹配）。
"""
import struct

try:
    import capstone as cs
    _MD32 = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_32)
    _MD64 = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_64)
    # operands 依赖 detail（capstone CsInsn.operands 生命周期陷阱：未开 detail 抛 CS_ERR_DETAIL）
    _MD32.detail = True
    _MD64.detail = True
except Exception:  # pragma: no cover
    _MD32 = _MD64 = None

# 代码区范围（CrackMe image：0x400000-0x840FFF；通用：低 4GB 代码）
_CODE_MIN = 0x400000
_CODE_MAX = 0x900000


def _rd(uc, addr, n):
    """读内存（异常安全）。"""
    try:
        return bytes(uc.mem_read(addr, n))
    except Exception:
        return b""


def _disasm(code, addr, md):
    try:
        return list(md.disasm(code, addr))
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════
# R1: 短跳垃圾跳过（eb rel → 目标处有效代码）
# ═══════════════════════════════════════════════════════════════
def r1_junk_short_jump(uc, addr, md):
    """`eb rel`（rel∈[2,16]）且目标处能反汇编 ≥2 条有效指令 → 返回目标。
    CrackMe 0x7A6928 实测模式：eb 04 d0 cc 37 f8 26 90...（垃圾字节后有效代码）。"""
    b = _rd(uc, addr, 2)
    if len(b) < 2 or b[0] != 0xEB:
        return None
    rel = struct.unpack("b", b[1:2])[0]
    if not (2 <= rel <= 16):
        return None
    tgt = addr + 2 + rel
    insns = _disasm(_rd(uc, tgt, 32), tgt, md)
    if len(insns) >= 2:
        return tgt
    return None


# ═══════════════════════════════════════════════════════════════
# R2: pushfd/popfd 包裹消除（9c ... 9d）
# ═══════════════════════════════════════════════════════════════
def r2_pushfd_wrapper(uc, addr, md):
    """`9c` 起 ≤8 条内 `9d`，中间指令无内存读写/call/jmp → 包裹后地址。"""
    b = _rd(uc, addr, 1)
    if len(b) < 1 or b[0] != 0x9C:
        return None
    insns = _disasm(_rd(uc, addr, 48), addr, md)[:9]
    for i, ins in enumerate(insns):
        if ins.size == 1 and ins.bytes[0] == 0x9D:  # popfd
            for m in insns[1:i]:
                if m.mnemonic in ("call", "jmp", "ret", "int", "syscall", "sysenter"):
                    return None
                if "[" in m.op_str:  # 内存访问
                    return None
            return ins.address + ins.size
    return None


# ═══════════════════════════════════════════════════════════════
# R3: 恒定条件跳转（opaque.py 恒等模式字节级翻译 + 具体寄存器值）
# ═══════════════════════════════════════════════════════════════
# 寄存器宽映射（按 capstone reg id 取 uc 寄存器常量）
_REG_UC = {}


def _reg_uc(reg_id, md):
    """capstone reg id → unicorn reg 常量（按需惰性填充）。"""
    import unicorn.x86_const as xc
    key = reg_id
    if key in _REG_UC:
        return _REG_UC[key]
    name_map = {
        "RAX": xc.UC_X86_REG_RAX, "RBX": xc.UC_X86_REG_RBX, "RCX": xc.UC_X86_REG_RCX,
        "RDX": xc.UC_X86_REG_RDX, "RSI": xc.UC_X86_REG_RSI, "RDI": xc.UC_X86_REG_RDI,
        "RBP": xc.UC_X86_REG_RBP, "RSP": xc.UC_X86_REG_RSP, "R8": xc.UC_X86_REG_R8,
        "R9": xc.UC_X86_REG_R9, "R10": xc.UC_X86_REG_R10, "R11": xc.UC_X86_REG_R11,
        "R12": xc.UC_X86_REG_R12, "R13": xc.UC_X86_REG_R13, "R14": xc.UC_X86_REG_R14,
        "R15": xc.UC_X86_REG_R15,
        "EAX": xc.UC_X86_REG_EAX, "EBX": xc.UC_X86_REG_EBX, "ECX": xc.UC_X86_REG_ECX,
        "EDX": xc.UC_X86_REG_EDX, "ESI": xc.UC_X86_REG_ESI, "EDI": xc.UC_X86_REG_EDI,
        "EBP": xc.UC_X86_REG_EBP, "ESP": xc.UC_X86_REG_ESP,
    }
    # capstone 寄存器名 → 标准名（先试全名，再试窄名）
    try:
        rname = md.reg_name(reg_id).upper()
    except Exception:
        rname = ""
    _REG_UC[key] = name_map.get(rname)
    return _REG_UC[key]


def _reg_val(uc, insn, md):
    """读指令目的寄存器当前值（读主寄存器族，兼容 32/64 位名）。"""
    if insn.operands:
        for op in insn.operands:
            if op.type == cs.x86.X86_OP_REG:
                rid = op.reg
                u = _reg_uc(rid, md)
                if u is not None:
                    try:
                        return uc.reg_read(u)
                    except Exception:
                        pass
                # 窄寄存器（al/cl 等）：读主寄存器再掩码
                try:
                    rname = md.reg_name(rid).upper()
                    main = rname.rstrip("L") + "X" if rname[-1:] in ("L", "H") else rname
                    if rname[-1:] == "H":
                        main = rname[:-1] + "X"
                    m = _REG_UC.get(rid)
                    if m is not None:
                        v = uc.reg_read(m)
                        if rname[-1:] == "L":
                            return v & 0xFF
                        if rname[-1:] == "H":
                            return (v >> 8) & 0xFF
                except Exception:
                    pass
    return None


def _parse_target(insn):
    """解析条件跳转目标（jcc rel8/rel32）。"""
    if insn.operands:
        for op in insn.operands:
            if op.type == cs.x86.X86_OP_IMM:
                return op.imm
    return None


def jcc_taken(mnemonic, zf, cf, sf, of):
    """条件跳转判定（emulator.py _eval_conditional_jump 语义——含符号比较）。
    返回 True=跳转 / False=落空（基于给定标志位）。"""
    m = mnemonic
    if m in ("ja", "jnbe"):
        return (cf == 0) and (zf == 0)
    if m in ("jae", "jnb", "jnc"):
        return cf == 0
    if m in ("jb", "jnae", "jc"):
        return cf == 1
    if m in ("jbe", "jna"):
        return (cf == 1) or (zf == 1)
    if m in ("je", "jz"):
        return zf == 1
    if m in ("jne", "jnz"):
        return zf == 0
    if m in ("jg", "jnle"):
        return (zf == 0) and (sf == of)
    if m in ("jge", "jnl"):
        return sf == of
    if m in ("jl", "jnge"):
        return sf != of
    if m in ("jle", "jng"):
        return (zf == 1) or (sf != of)
    if m == "jo":
        return of == 1
    if m == "jno":
        return of == 0
    if m == "js":
        return sf == 1
    if m == "jns":
        return sf == 0
    if m == "jp":
        return True  # 奇偶位未知——保守取真
    return None  # 无法判定


def r3_const_cond_jump(uc, addr, md):
    """恒定条件跳转（opaque.py JnzRule 思想字节级翻译）：
    向前 ≤8 条内找 `xor r,r` / `sub r,r`（→ r=0，标志位 zf=1 cf=0 sf=0 of=0）
    → 按 jcc 语义判定恒真/恒假 → 恒定目标（真=跳转目标，假=落空地址）。"""
    code = _rd(uc, addr - 32, 80)
    insns = _disasm(code, addr - 32, md)
    if not insns:
        return None
    for i, ins in enumerate(insns):
        if ins.mnemonic in ("ja", "jae", "jb", "jbe", "jc", "je", "jg", "jge",
                            "jl", "jle", "jna", "jnae", "jnb", "jnbe", "jnc",
                            "jne", "jng", "jnge", "jnl", "jnle", "jno", "jns",
                            "jnz", "jo", "js", "jz"):
            tgt = _parse_target(ins)
            if tgt is None:
                return None
            # 向前 ≤8 条找 xor r,r / sub r,r（目的寄存器相同）
            found = False
            for j in range(max(0, i - 8), i):
                m = insns[j]
                if m.mnemonic in ("xor", "sub") and len(m.operands) >= 2:
                    o0, o1 = m.operands[0], m.operands[1]
                    if (o0.type == cs.x86.X86_OP_REG and o1.type == cs.x86.X86_OP_REG
                            and o0.reg == o1.reg):
                        found = True
                        break
            if found:
                taken = jcc_taken(ins.mnemonic, zf=1, cf=0, sf=0, of=0)
                if taken is True:
                    return tgt
                if taken is False:
                    return ins.address + ins.size  # 落空：下一条
            break
    return None


# ═══════════════════════════════════════════════════════════════
# R4: 垃圾流向后扫描（异常点向前 ≤16 字节找 jmp/call 解析目标）
# ═══════════════════════════════════════════════════════════════
def r4_backscan_jump(uc, addr, md):
    """异常点（addr）向前 ≤16 字节扫描 E9/EB/FF 25/E8 → 解析目标。
    目标须在代码区（_CODE_MIN.._CODE_MAX）。"""
    base = max(addr - 16, _CODE_MIN)
    code = _rd(uc, base, addr - base + 1)
    for off in range(len(code)):
        b = code[off]
        if b == 0xE9 and off + 5 <= len(code):  # jmp rel32
            rel = int.from_bytes(code[off + 1:off + 5], "little", signed=True)
            tgt = base + off + 5 + rel
            if _CODE_MIN <= tgt < _CODE_MAX:
                return tgt
        elif b == 0xEB and off + 2 <= len(code):  # jmp rel8
            rel = struct.unpack("b", code[off + 1:off + 2])[0]
            tgt = base + off + 2 + rel
            if _CODE_MIN <= tgt < _CODE_MAX:
                return tgt
        elif b == 0xFF and off + 1 < len(code) and code[off + 1] == 0x25 \
                and off + 6 <= len(code):  # jmp [rip+disp32]
            mem = base + off + 6 + int.from_bytes(code[off + 2:off + 6], "little", signed=True)
            tgt = int.from_bytes(_rd(uc, mem, 8), "little")
            if _CODE_MIN <= tgt < _CODE_MAX:
                return tgt
        elif b == 0xE8 and off + 5 <= len(code):  # call rel32
            rel = int.from_bytes(code[off + 1:off + 5], "little", signed=True)
            tgt = base + off + 5 + rel
            if _CODE_MIN <= tgt < _CODE_MAX:
                return tgt
    return None


# ═══════════════════════════════════════════════════════════════
# R5: 跳转表读取（m_jtbl 求值思想）
# ═══════════════════════════════════════════════════════════════
def r5_jumptable(uc, addr, md):
    """`jmp [table + reg*N]` / `jmp qword ptr [reg]` → 读表目标列表。
    结合当前寄存器值（具体执行天然已知索引）。"""
    code = _rd(uc, addr - 8, 32)
    insns = _disasm(code, addr - 8, md)
    for ins in insns:
        if ins.mnemonic == "jmp" and ins.operands:
            op = ins.operands[0]
            if op.type == cs.x86.X86_OP_MEM:
                mem = op.mem
                base_reg = mem.base
                # 计算表地址：reg 值 + disp
                try:
                    regv = 0
                    if base_reg:
                        u = _reg_uc(base_reg, md)
                        if u is not None:
                            regv = uc.reg_read(u)
                    idx = regv + mem.disp
                    if 0x400000 <= idx < 0xF0000000:
                        tgt = int.from_bytes(_rd(uc, idx, 8), "little")
                        if _CODE_MIN <= tgt < _CODE_MAX:
                            return tgt
                except Exception:
                    pass
    return None


# ═══════════════════════════════════════════════════════════════
# 状态机推演（simulate_dispatcher 思想 + emulator.py 条件跳转语义）：
# 从 addr 起轻量解释（mov/cmp/jcc/jmp/ret/add/sub/xor/and/or），
# 找乱码区/分发器的“出口”（jmp 到有效代码区或 ret）。
# ═══════════════════════════════════════════════════════════════
def _init_regs(uc, md):
    """初始化寄存器快照（64 位主寄存器族）。"""
    regs = {}
    for name in ("RAX", "RBX", "RCX", "RDX", "RSI", "RDI", "RBP", "RSP",
                 "R8", "R9", "R10", "R11", "R12", "R13", "R14", "R15"):
        u = _reg_uc_by_name(name)
        if u is not None:
            try:
                regs[name] = uc.reg_read(u)
            except Exception:
                pass
    regs["ZF"] = regs["CF"] = regs["SF"] = regs["OF"] = 0
    return regs


_REG_NAME_UC = {}


def _reg_uc_by_name(name):
    """"按标准寄存器名取 unicorn 常量（惰性缓存）。"""
    import unicorn.x86_const as xc
    if not _REG_NAME_UC:
        for n, v in (("RAX", xc.UC_X86_REG_RAX), ("RBX", xc.UC_X86_REG_RBX),
                     ("RCX", xc.UC_X86_REG_RCX), ("RDX", xc.UC_X86_REG_RDX),
                     ("RSI", xc.UC_X86_REG_RSI), ("RDI", xc.UC_X86_REG_RDI),
                     ("RBP", xc.UC_X86_REG_RBP), ("RSP", xc.UC_X86_REG_RSP),
                     ("R8", xc.UC_X86_REG_R8), ("R9", xc.UC_X86_REG_R9),
                     ("R10", xc.UC_X86_REG_R10), ("R11", xc.UC_X86_REG_R11),
                     ("R12", xc.UC_X86_REG_R12), ("R13", xc.UC_X86_REG_R13),
                     ("R14", xc.UC_X86_REG_R14), ("R15", xc.UC_X86_REG_R15)):
            _REG_NAME_UC[n] = v
    return _REG_NAME_UC.get(name)


_CMP_SET = ("je", "jz", "jne", "jnz", "ja", "jae", "jb", "jbe",
            "jg", "jge", "jl", "jle", "jc", "jnc", "jna", "jnae",
            "jnb", "jnbe", "jng", "jnge", "jnl", "jnle")


def _set_flags_arith(regs, val, width=64):
    """算术结果标志（简化：zf/sf/of——cf 由 cmp 单独维护）。"""
    val &= (1 << width) - 1
    regs["ZF"] = 1 if val == 0 else 0
    regs["SF"] = 1 if (val >> (width - 1)) & 1 else 0
    return val


def _set_flags_cmp(regs, a, b, width=64):
    """比较标志（emulator.py m_setae/m_setb 等语义——含无符号 CF 与符号比较）。"""
    mask = (1 << width) - 1
    regs["ZF"] = 1 if (a & mask) == (b & mask) else 0
    regs["CF"] = 1 if (a & mask) < (b & mask) else 0
    regs["SF"] = 1 if ((a - b) & mask) >> (width - 1) else 0
    sa = a >> (width - 1) if width == 64 else ((a >> 31) & 1)
    sb = b >> (width - 1) if width == 64 else ((b >> 31) & 1)
    regs["OF"] = 1 if sa != sb else 0


def dispatch_guess(uc, addr, md, max_steps=100):
    """状态机/乱码区出口推演：轻量解释（mov/cmp/jcc/jmp/ret/算术），
    从 addr 起最多 max_steps 步，返回 (出口地址 or None, 步数)。
    出口 = jmp 到代码区目标 / ret（返回 None + 'ret' 标记由调用方处理）。"""
    regs = _init_regs(uc, md)
    cur = addr
    for _ in range(max_steps):
        code = _rd(uc, cur, 64)
        if not code:
            return None, _
        insns = _disasm(code, cur, md)
        if not insns:
            return None, _
        ins = insns[0]
        m = ins.mnemonic
        ops = ins.op_str.split(", ")
        # 立即数/寄存器值解析
        def val_of(s):
            s = s.strip()
            try:
                if s.lower().startswith("0x"):
                    return int(s, 16)
                return int(s)
            except ValueError:
                pass
            name = s.upper()
            if name in regs:
                return regs[name]
            return None
        if m == "mov" and len(ops) >= 2:
            dst, src = ops[0].strip(), ops[1].strip()
            if dst.upper() in regs:
                v = val_of(src)
                if v is not None:
                    regs[dst.upper()] = v
            cur += ins.size
        elif m == "cmp" and len(ops) >= 2:
            a, b = val_of(ops[0]), val_of(ops[1])
            if a is not None and b is not None:
                _set_flags_cmp(regs, a, b)
            cur += ins.size
        elif m in ("add", "sub", "xor", "and", "or") and len(ops) >= 2:
            dst, src = ops[0].strip(), ops[1].strip()
            if dst.upper() in regs:
                a, b = regs.get(dst.upper()), val_of(src)
                if b is not None:
                    if m == "add":
                        regs[dst.upper()] = _set_flags_arith(regs, a + b)
                    elif m == "sub":
                        regs[dst.upper()] = _set_flags_arith(regs, a - b)
                    elif m == "xor":
                        regs[dst.upper()] = _set_flags_arith(regs, a ^ b)
                    elif m == "and":
                        regs[dst.upper()] = _set_flags_arith(regs, a & b)
                    elif m == "or":
                        regs[dst.upper()] = _set_flags_arith(regs, a | b)
            cur += ins.size
        elif m == "jmp":
            tgt = val_of(ops[0]) if ops else None
            if tgt is not None and _CODE_MIN <= tgt < _CODE_MAX:
                return tgt, _
            cur += ins.size
        elif m == "ret":
            return None, _  # 出口 ret：调用方决定（桥返回点 0x401110）
        elif m in _CMP_SET:
            tgt = val_of(ops[0]) if ops else None
            taken = jcc_taken(m, regs["ZF"], regs["CF"], regs["SF"], regs["OF"])
            if taken is True and tgt is not None:
                cur = tgt
            elif taken is False:
                cur += ins.size
            else:
                cur += ins.size  # 无法判定——保守顺序执行
        else:
            cur += ins.size  # 其他指令跳过（内存/栈操作简化）
    return None, max_steps

RULES = (
    ("r1_junk_short_jump", (0xEB,), r1_junk_short_jump),
    ("r2_pushfd_wrapper", (0x9C,), r2_pushfd_wrapper),
    ("r3_const_cond_jump", (0x31, 0x33, 0x83, 0x85, 0x3B, 0x29, 0x2B), r3_const_cond_jump),
    ("r4_backscan_jump", (0xE9, 0xEB, 0xFF, 0xE8), r4_backscan_jump),
    ("r5_jumptable", (0xFF,), r5_jumptable),
)

# 首字节 → 规则索引（预计算，注册时一次完成）
_INDEX = {}
for _i, (_name, _first, _fn) in enumerate(RULES):
    for _b in _first:
        _INDEX.setdefault(_b, []).append(_i)

# 范围扫描兜底规则（R3 恒定条件跳转 / R4 向后扫描）：本质是向前扫描，
# 不受 addr 首字节限制——首字节索引未命中时补跑（d810 MopTracker 预算思想）
_SCAN_RULES = (
    ("r3_const_cond_jump", r3_const_cond_jump),
    ("r4_backscan_jump", r4_backscan_jump),
)


def correct_rip(uc, addr, mode=64, budget=5):
    """故障驱动矫正：按首字节指纹分发规则，命中返回 (目标, 规则名)。

    Args:
        uc: unicorn 实例（主实例或子执行器）
        addr: 异常指令地址（RIP-1 处）
        mode: 32/64（capstone 模式）
        budget: 最多尝试规则数（性能预算，超限返回 None）

    Returns:
        (target, rule_name) 命中；否则 (None, None)
    """
    md = _MD64 if mode == 64 else _MD32
    if md is None:
        return None, None
    try:
        b0 = uc.mem_read(addr, 1)[0]
    except Exception:
        return None, None
    cands = _INDEX.get(b0, [])
    if not cands:
        # 范围扫描兜底：R3/R4（向前扫描规则——异常点非规则首字节）
        for _name, _fn in _SCAN_RULES[:budget]:
            try:
                tgt = _fn(uc, addr, md)
            except Exception:
                continue
            if tgt is not None:
                return tgt, _name
        return None, None
    for idx in cands[:budget]:
        _name, _first, _fn = RULES[idx]
        try:
            tgt = _fn(uc, addr, md)
        except Exception:
            continue
        if tgt is not None:
            return tgt, _name
    # 首字节规则未命中——补范围扫描（R3/R4）
    for _name, _fn in _SCAN_RULES[:budget]:
        try:
            tgt = _fn(uc, addr, md)
        except Exception:
            continue
        if tgt is not None:
            return tgt, _name
    return None, None


def analyze(uc, addr, size, mode=64):
    """工具 action（deobf）：对地址范围跑规则管道 → 命中列表。

    Returns:
        [(addr, rule_name, target), ...]
    """
    out = []
    md = _MD64 if mode == 64 else _MD32
    if md is None:
        return out
    for off in range(0, size, 1):
        a = addr + off
        try:
            b0 = uc.mem_read(a, 1)[0]
        except Exception:
            continue
        for idx in _INDEX.get(b0, []):
            _name, _first, _fn = RULES[idx]
            try:
                tgt = _fn(uc, a, md)
            except Exception:
                continue
            if tgt is not None:
                out.append((a, _name, tgt))
                break  # 每地址最多 1 条
    return out
