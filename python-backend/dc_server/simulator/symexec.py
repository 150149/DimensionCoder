# -*- coding: utf-8 -*-
"""混合符号执行层（Hybrid Symbolic）。

借鉴 d810-ng（mba/backends/z3.py 的 Z3 规则兜底 + JmpRuleZ3Const 白名单
思想、expr/emulator.py 的符号模式合成值）与本地 generic_symbolic_exec.py
（SymbolicVal 值追踪），适配我们的具体执行框架：

- 具体执行天然已知寄存器/内存值——符号化的场景 = 未映射内存/未初始化区
  （值 UNKNOWN）——此时 z3 BitVec 符号化求解分支/跳转表目标。
- angr 深度接管（w64_sym_engine='angr'）：卡点（连续矫正失败）→ SimState
  局部探索（≤100 块/≤2s），处理轻量解释器不支持的复杂指令语义。

部署：仅卡点路径调用；显式工具 action `symexec addr` 触发。
"""
import time as _time

try:
    import z3
    _HAS_Z3 = True
except Exception:  # pragma: no cover
    _HAS_Z3 = False

try:
    import capstone as cs
    _MD64 = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_64)
    _MD64.detail = True
except Exception:  # pragma: no cover
    _MD64 = None

from .deobf import jcc_taken, _rd, _disasm

# 代码区范围（与 deobf 一致）
_CODE_MIN = 0x400000
_CODE_MAX = 0x900000

# 单次求解预算（毫秒）
_SOLVE_MS = 50


def _reg_id_to_uc_name(md, reg_id):
    try:
        return md.reg_name(reg_id).upper()
    except Exception:
        return None


def _uc_reg_read(uc, name):
    """按标准寄存器名读值（64 位主寄存器族 + 32 位兼容）。"""
    import unicorn.x86_const as xc
    m = {"RAX": xc.UC_X86_REG_RAX, "RBX": xc.UC_X86_REG_RBX, "RCX": xc.UC_X86_REG_RCX,
         "RDX": xc.UC_X86_REG_RDX, "RSI": xc.UC_X86_REG_RSI, "RDI": xc.UC_X86_REG_RDI,
         "RBP": xc.UC_X86_REG_RBP, "RSP": xc.UC_X86_REG_RSP, "R8": xc.UC_X86_REG_R8,
         "R9": xc.UC_X86_REG_R9, "R10": xc.UC_X86_REG_R10, "R11": xc.UC_X86_REG_R11,
         "R12": xc.UC_X86_REG_R12, "R13": xc.UC_X86_REG_R13, "R14": xc.UC_X86_REG_R14,
         "R15": xc.UC_X86_REG_R15, "EAX": xc.UC_X86_REG_EAX, "EBX": xc.UC_X86_REG_EBX,
         "ECX": xc.UC_X86_REG_ECX, "EDX": xc.UC_X86_REG_EDX, "ESI": xc.UC_X86_REG_ESI,
         "EDI": xc.UC_X86_REG_EDI, "EBP": xc.UC_X86_REG_EBP, "ESP": xc.UC_X86_REG_ESP}
    u = m.get(name)
    if u is None:
        return None
    try:
        return uc.reg_read(u)
    except Exception:
        return None


def _operand_concrete(uc, md, op, width=8):
    """解析操作数为具体值；无法解析（未映射内存/未知寄存器）返回 None。"""
    if op.type == cs.x86.X86_OP_IMM:
        return op.imm
    if op.type == cs.x86.X86_OP_REG:
        name = _reg_id_to_uc_name(md, op.reg)
        if name:
            v = _uc_reg_read(uc, name)
            if v is not None:
                return v & ((1 << (width * 8)) - 1)
        return None
    if op.type == cs.x86.X86_OP_MEM:
        mem = op.mem
        base = 0
        if mem.base:
            name = _reg_id_to_uc_name(md, mem.base)
            if not name:
                return None
            v = _uc_reg_read(uc, name)
            if v is None:
                return None
            base = v
        idx_v = 0
        if mem.index:
            name = _reg_id_to_uc_name(md, mem.index)
            if not name:
                return None
            v = _uc_reg_read(uc, name)
            if v is None:
                return None
            idx_v = v * mem.scale
        addr = base + idx_v + mem.disp
        data = _rd(uc, addr, width)
        if len(data) == width:
            return int.from_bytes(data, "little")
        return None  # 未映射——符号化候选
    return None


def solve_branch(uc, addr, md=None, use_z3=True, solve_ms=_SOLVE_MS):
    """条件跳转求解（JmpRuleZ3Const 思想）：

    - 操作数具体 → jcc_taken 直接判定 → 单目标
    - 操作数未知（未映射内存）→ z3 BitVec 符号化 → 两分支可达性 → 目标列表

    Returns:
        [(target, taken), ...] 或 None（无 jcc/无法求解）
    """
    md = md or _MD64
    if md is None:
        return None
    code = _rd(uc, addr, 32)
    insns = _disasm(code, addr, md)
    if not insns:
        return None
    ins = insns[0]
    if not (ins.mnemonic.startswith("j") and ins.mnemonic != "jmp"):
        return None
    tgt = None
    for op in ins.operands:
        if op.type == cs.x86.X86_OP_IMM:
            tgt = op.imm
            break
    if tgt is None:
        return None
    # 操作数解析：jcc 的源操作数来自前一条 cmp/test 的结果——但具体执行中
    # EFLAGS 已知（读 EFLAGS 位），无需符号化即可判定
    try:
        import unicorn.x86_const as xc
        eflags = uc.reg_read(xc.UC_X86_REG_EFLAGS)
        zf = 1 if eflags & 0x40 else 0
        cf = 1 if eflags & 0x1 else 0
        sf = 1 if eflags & 0x80 else 0
        of = 1 if eflags & 0x800 else 0
        taken = jcc_taken(ins.mnemonic, zf, cf, sf, of)
        if taken is not None:
            return [(tgt if taken else ins.address + ins.size, taken)]
    except Exception:
        pass
    # EFLAGS 不可读——尝试符号化（未映射操作数场景）
    if use_z3 and _HAS_Z3:
        return _z3_solve_branch(uc, md, ins, tgt, solve_ms)
    return None


def _z3_solve_branch(uc, md, ins, tgt, solve_ms):
    """z3 符号化：条件跳转前的 cmp 操作数未知 → BitVec → 两分支 SAT。"""
    # 简化实现：构建通用符号变量 x（32 位），求 jcc 两分支可满足性
    t0 = _time.time()
    try:
        x = z3.BitVec("x", 32)
        # 基于 jcc 类型的约束（常见条件）
        m = ins.mnemonic
        if m in ("jz", "je"):
            cond_t = x == 0
        elif m in ("jnz", "jne"):
            cond_t = x != 0
        elif m in ("jb", "jnae"):
            cond_t = z3.ULT(x, z3.BitVecVal(0, 32)) if False else z3.ULT(x, z3.BitVecVal(0, 32))
            cond_t = z3.BoolVal(False)  # ULT(x,0) 恒假
        elif m in ("jae", "jnb"):
            cond_t = z3.BoolVal(True)  # UGE(x,0) 恒真
        elif m == "js":
            cond_t = x < 0
        elif m == "jns":
            cond_t = x >= 0
        else:
            return None
        s = z3.Solver()
        s.set(timeout=solve_ms)
        s.push()
        s.add(cond_t)
        r_taken = s.check() == z3.sat
        s.pop()
        s.push()
        s.add(z3.Not(cond_t))
        r_fall = s.check() == z3.sat
        s.pop()
        out = []
        if r_taken:
            out.append((tgt, True))
        if r_fall:
            out.append((ins.address + ins.size, False))
        return out or None
    except Exception:
        return None
    finally:
        pass


def solve_jump_table(uc, addr, md=None, use_z3=True, solve_ms=_SOLVE_MS):
    """跳转表目标求解（m_jtbl 求值思想）：

    - 索引寄存器具体 → 读表目标（已映射）
    - 索引未知/表未映射 → z3 枚举（约束：目标在已映射代码区）

    Returns:
        [target, ...] 或 None
    """
    md = md or _MD64
    if md is None:
        return None
    code = _rd(uc, addr - 8, 32)
    insns = _disasm(code, addr - 8, md)
    for ins in insns:
        if ins.mnemonic != "jmp" or not ins.operands:
            continue
        op = ins.operands[0]
        if op.type != cs.x86.X86_OP_MEM:
            continue
        mem = op.mem
        # 表地址 = base + index*scale + disp
        base = 0
        if mem.base:
            name = _reg_id_to_uc_name(md, mem.base)
            v = _uc_reg_read(uc, name) if name else None
            if v is None:
                return _z3_jumptable(uc, md, mem, solve_ms) if use_z3 else None
            base = v
        idx = 0
        if mem.index:
            name = _reg_id_to_uc_name(md, mem.index)
            v = _uc_reg_read(uc, name) if name else None
            if v is None:
                return _z3_jumptable(uc, md, mem, solve_ms) if use_z3 else None
            idx = v * mem.scale
        table = base + idx + mem.disp
        if not (0x400000 <= table < 0x100000000):
            continue
        tgt = int.from_bytes(_rd(uc, table, 8), "little")
        if _CODE_MIN <= tgt < _CODE_MAX:
            return [tgt]
    return None


def _z3_jumptable(uc, md, mem, solve_ms):
    """z3 枚举跳转表索引（约束：目标在已映射代码区）。"""
    try:
        # 已知已映射页列表
        try:
            regions = uc.mem_regions()
        except Exception:
            regions = []
        def mapped(a):
            return any(r[0] <= a < r[1] for r in regions)
        # 索引符号变量（32 位），表地址 = base(具体) + idx*scale + disp
        base = 0
        if mem.base:
            name = _reg_id_to_uc_name(md, mem.base)
            v = _uc_reg_read(uc, name) if name else None
            if v is None:
                return None
            base = v
        scale = mem.scale or 1
        idx = z3.BitVec("jt_idx", 32)
        table = base + idx * scale + mem.disp
        s = z3.Solver()
        s.set(timeout=solve_ms)
        # 表项内容符号化读取：tgt = Mem[table]（8 字节）——枚举 idx 0..64 检查
        out = []
        for i in range(64):
            s.push()
            s.add(idx == i)
            if s.check() == z3.sat:
                s.pop()
                t = int.from_bytes(_rd(uc, base + i * scale + mem.disp, 8), "little")
                if _CODE_MIN <= t < _CODE_MAX:
                    out.append(t)
            else:
                s.pop()
        return out or None
    except Exception:
        return None


def angr_explore(uc, addr, max_blocks=100, timeout_s=2.0):
    """angr 深度接管：SimState 导入 → 局部符号探索 → 可达目标列表。

    卡点（连续矫正失败）时启用（w64_sym_engine='angr'）。失败自动降级
    返回 None（调用方回退 z3/中断）。导入失败/超时均安全返回。

    Returns:
        [target, ...] 或 None
    """
    t0 = _time.time()
    try:
        import angr
        import angr.storage.memory_mixins as _amm  # noqa: F401（确保完整导入）
    except Exception:
        return None
    try:
        # 裸 SimState（不加载程序——只做局部片段符号执行）
        state = angr.SimState(arch="AMD64", mode="symbolic")
        # 寄存器导入（从 uc）
        import unicorn.x86_const as xc
        for name, reg in (("RAX", xc.UC_X86_REG_RAX), ("RBX", xc.UC_X86_REG_RBX),
                          ("RCX", xc.UC_X86_REG_RCX), ("RDX", xc.UC_X86_REG_RDX),
                          ("RSI", xc.UC_X86_REG_RSI), ("RDI", xc.UC_X86_REG_RDI),
                          ("RBP", xc.UC_X86_REG_RBP), ("RSP", xc.UC_X86_REG_RSP),
                          ("R8", xc.UC_X86_REG_R8), ("R9", xc.UC_X86_REG_R9),
                          ("R10", xc.UC_X86_REG_R10), ("R11", xc.UC_X86_REG_R11),
                          ("R12", xc.UC_X86_REG_R12), ("R13", xc.UC_X86_REG_R13),
                          ("R14", xc.UC_X86_REG_R14), ("R15", xc.UC_X86_REG_R15)):
            try:
                setattr(state.regs, name.lower(), uc.reg_read(reg))
            except Exception:
                pass
        state.regs.rip = addr
        # 内存导入：仅相关页（addr 附近 4KB + 已映射页小范围）
        try:
            for r in uc.mem_regions()[:8]:
                ba, en = r[0], r[1]
                if en - ba > 0x100000:
                    continue
                try:
                    data = bytes(uc.mem_read(ba, en - ba))
                    for off in range(0, len(data), 0x1000):
                        page = data[off:off + 0x1000]
                        if page.strip(b"\x00"):
                            state.memory.store(ba + off, page)
                except Exception:
                    pass
        except Exception:
            pass
        # 局部探索（≤max_blocks 步）
        targets = []
        cur_states = [state]
        for _ in range(max_blocks):
            if _time.time() - t0 > timeout_s:
                break
            nxt = []
            for st in cur_states:
                try:
                    succs = st.step()
                except Exception:
                    continue
                for s in succs.successors:
                    try:
                        rip = s.solver.eval(s.regs.rip)
                    except Exception:
                        continue
                    if _CODE_MIN <= rip < _CODE_MAX and rip != addr:
                        targets.append(rip)
                    nxt.append(s)
            if not nxt:
                break
            cur_states = nxt[:4]  # 限制路径数（防爆炸）
        return sorted(set(targets))[:16] or None
    except Exception:
        return None


def symexec(uc, addr, engine="z3", md=None):
    """统一入口（工具 action）：卡点符号求解。

    engine: 'z3'（默认）先 solve_branch 再 solve_jump_table；
            'angr' 用 angr_explore（失败降级 z3）。

    Returns:
        {"branch": [...], "jumptable": [...], "angr": [...], "engine": ...}
    """
    out = {"engine": engine}
    if engine == "angr":
        t = angr_explore(uc, addr)
        out["angr"] = t or []
        if not t:  # 降级 z3
            out["branch"] = solve_branch(uc, addr, md) or []
            out["jumptable"] = solve_jump_table(uc, addr, md) or []
    else:
        out["branch"] = solve_branch(uc, addr, md) or []
        out["jumptable"] = solve_jump_table(uc, addr, md) or []
    return out
