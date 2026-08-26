# -*- coding: utf-8 -*-
import time as _time

try:
    import z3
    _HAS_Z3 = True
except Exception:
    _HAS_Z3 = False

try:
    import capstone as cs
    _MD64 = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_64)
    _MD64.detail = True
except Exception:
    _MD64 = None

from .deobf import jcc_taken, _rd, _disasm

_CODE_MIN = 0x400000
_CODE_MAX = 0x900000

_SOLVE_MS = 50

def _reg_id_to_uc_name(md, reg_id):
    try:
        return md.reg_name(reg_id).upper()
    except Exception:
        return None

def _uc_reg_read(uc, name):
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
        return None
    return None

def solve_branch(uc, addr, md=None, use_z3=True, solve_ms=_SOLVE_MS):
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
    if use_z3 and _HAS_Z3:
        return _z3_solve_branch(uc, md, ins, tgt, solve_ms)
    return None

def _z3_solve_branch(uc, md, ins, tgt, solve_ms):
    t0 = _time.time()
    try:
        x = z3.BitVec("x", 32)
        m = ins.mnemonic
        if m in ("jz", "je"):
            cond_t = x == 0
        elif m in ("jnz", "jne"):
            cond_t = x != 0
        elif m in ("jb", "jnae"):
            cond_t = z3.ULT(x, z3.BitVecVal(0, 32)) if False else z3.ULT(x, z3.BitVecVal(0, 32))
            cond_t = z3.BoolVal(False)
        elif m in ("jae", "jnb"):
            cond_t = z3.BoolVal(True)
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
    try:
        try:
            regions = uc.mem_regions()
        except Exception:
            regions = []
        def mapped(a):
            return any(r[0] <= a < r[1] for r in regions)
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
    t0 = _time.time()
    try:
        import angr
        import angr.storage.memory_mixins as _amm  # noqa: F401
    except Exception:
        return None
    try:
        state = angr.SimState(arch="AMD64", mode="symbolic")
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
            cur_states = nxt[:4]
        return sorted(set(targets))[:16] or None
    except Exception:
        return None

def symexec(uc, addr, engine="z3", md=None):
    out = {"engine": engine}
    if engine == "angr":
        t = angr_explore(uc, addr)
        out["angr"] = t or []
        if not t:
            out["branch"] = solve_branch(uc, addr, md) or []
            out["jumptable"] = solve_jump_table(uc, addr, md) or []
    else:
        out["branch"] = solve_branch(uc, addr, md) or []
        out["jumptable"] = solve_jump_table(uc, addr, md) or []
    return out
