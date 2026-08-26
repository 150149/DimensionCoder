# -*- coding: utf-8 -*-
import struct

try:
    import capstone as cs
    _MD32 = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_32)
    _MD64 = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_64)
    _MD32.detail = True
    _MD64.detail = True
except Exception:
    _MD32 = _MD64 = None

_CODE_MIN = 0x400000
_CODE_MAX = 0x900000

def _rd(uc, addr, n):
    try:
        return bytes(uc.mem_read(addr, n))
    except Exception:
        return b""

def _disasm(code, addr, md):
    try:
        return list(md.disasm(code, addr))
    except Exception:
        return []

def r1_junk_short_jump(uc, addr, md):
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

def r2_pushfd_wrapper(uc, addr, md):
    b = _rd(uc, addr, 1)
    if len(b) < 1 or b[0] != 0x9C:
        return None
    insns = _disasm(_rd(uc, addr, 48), addr, md)[:9]
    for i, ins in enumerate(insns):
        if ins.size == 1 and ins.bytes[0] == 0x9D:
            for m in insns[1:i]:
                if m.mnemonic in ("call", "jmp", "ret", "int", "syscall", "sysenter"):
                    return None
                if "[" in m.op_str:
                    return None
            return ins.address + ins.size
    return None

_REG_UC = {}

def _reg_uc(reg_id, md):
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
    try:
        rname = md.reg_name(reg_id).upper()
    except Exception:
        rname = ""
    _REG_UC[key] = name_map.get(rname)
    return _REG_UC[key]

def _reg_val(uc, insn, md):
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
    if insn.operands:
        for op in insn.operands:
            if op.type == cs.x86.X86_OP_IMM:
                return op.imm
    return None

def jcc_taken(mnemonic, zf, cf, sf, of):
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
        return True
    return None

def r3_const_cond_jump(uc, addr, md):
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
                    return ins.address + ins.size
            break
    return None

def r4_backscan_jump(uc, addr, md):
    base = max(addr - 16, _CODE_MIN)
    code = _rd(uc, base, addr - base + 1)
    for off in range(len(code)):
        b = code[off]
        if b == 0xE9 and off + 5 <= len(code):
            rel = int.from_bytes(code[off + 1:off + 5], "little", signed=True)
            tgt = base + off + 5 + rel
            if _CODE_MIN <= tgt < _CODE_MAX:
                return tgt
        elif b == 0xEB and off + 2 <= len(code):
            rel = struct.unpack("b", code[off + 1:off + 2])[0]
            tgt = base + off + 2 + rel
            if _CODE_MIN <= tgt < _CODE_MAX:
                return tgt
        elif b == 0xFF and off + 1 < len(code) and code[off + 1] == 0x25 \
                and off + 6 <= len(code):
            mem = base + off + 6 + int.from_bytes(code[off + 2:off + 6], "little", signed=True)
            tgt = int.from_bytes(_rd(uc, mem, 8), "little")
            if _CODE_MIN <= tgt < _CODE_MAX:
                return tgt
        elif b == 0xE8 and off + 5 <= len(code):
            rel = int.from_bytes(code[off + 1:off + 5], "little", signed=True)
            tgt = base + off + 5 + rel
            if _CODE_MIN <= tgt < _CODE_MAX:
                return tgt
    return None

def r5_jumptable(uc, addr, md):
    code = _rd(uc, addr - 8, 32)
    insns = _disasm(code, addr - 8, md)
    for ins in insns:
        if ins.mnemonic == "jmp" and ins.operands:
            op = ins.operands[0]
            if op.type == cs.x86.X86_OP_MEM:
                mem = op.mem
                base_reg = mem.base
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

def _init_regs(uc, md):
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
    val &= (1 << width) - 1
    regs["ZF"] = 1 if val == 0 else 0
    regs["SF"] = 1 if (val >> (width - 1)) & 1 else 0
    return val

def _set_flags_cmp(regs, a, b, width=64):
    mask = (1 << width) - 1
    regs["ZF"] = 1 if (a & mask) == (b & mask) else 0
    regs["CF"] = 1 if (a & mask) < (b & mask) else 0
    regs["SF"] = 1 if ((a - b) & mask) >> (width - 1) else 0
    sa = a >> (width - 1) if width == 64 else ((a >> 31) & 1)
    sb = b >> (width - 1) if width == 64 else ((b >> 31) & 1)
    regs["OF"] = 1 if sa != sb else 0

def dispatch_guess(uc, addr, md, max_steps=100):
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
            return None, _
        elif m in _CMP_SET:
            tgt = val_of(ops[0]) if ops else None
            taken = jcc_taken(m, regs["ZF"], regs["CF"], regs["SF"], regs["OF"])
            if taken is True and tgt is not None:
                cur = tgt
            elif taken is False:
                cur += ins.size
            else:
                cur += ins.size
        else:
            cur += ins.size
    return None, max_steps

RULES = (
    ("r1_junk_short_jump", (0xEB,), r1_junk_short_jump),
    ("r2_pushfd_wrapper", (0x9C,), r2_pushfd_wrapper),
    ("r3_const_cond_jump", (0x31, 0x33, 0x83, 0x85, 0x3B, 0x29, 0x2B), r3_const_cond_jump),
    ("r4_backscan_jump", (0xE9, 0xEB, 0xFF, 0xE8), r4_backscan_jump),
    ("r5_jumptable", (0xFF,), r5_jumptable),
)

_INDEX = {}
for _i, (_name, _first, _fn) in enumerate(RULES):
    for _b in _first:
        _INDEX.setdefault(_b, []).append(_i)

_SCAN_RULES = (
    ("r3_const_cond_jump", r3_const_cond_jump),
    ("r4_backscan_jump", r4_backscan_jump),
)

def correct_rip(uc, addr, mode=64, budget=5):
    md = _MD64 if mode == 64 else _MD32
    if md is None:
        return None, None
    try:
        b0 = uc.mem_read(addr, 1)[0]
    except Exception:
        return None, None
    cands = _INDEX.get(b0, [])
    if not cands:
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
    for _name, _fn in _SCAN_RULES[:budget]:
        try:
            tgt = _fn(uc, addr, md)
        except Exception:
            continue
        if tgt is not None:
            return tgt, _name
    return None, None

def analyze(uc, addr, size, mode=64):
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
                break
    return out
