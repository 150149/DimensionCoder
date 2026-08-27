# -*- coding: utf-8 -*-
"""dcflow_sim 工具实现：子命令分发（orchestrator._invoke_tool 调用）。
会话按 task_id 存于调用方（_sim_sessions 字典），跨工具轮保持。"""
from typing import Optional

from unicorn import x86_const

from .engine import SimSession


def _arg(args: dict, key: str, default=None):
    v = args.get(key, default)
    if isinstance(v, str):
        v = v.strip()
    return v


def _int_addr(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        return int(s, 16) if s.lower().startswith(("0x", "&h")) else int(s)
    except ValueError:
        return None


def run_sim_tool(task_id: str, sessions: dict, args: dict) -> str:
    """dcflow_sim 子命令入口。sessions: {task_id: SimSession}。"""
    action = _arg(args, "action", "")
    sess = sessions.get(task_id)
    try:
        if action == "load":
            exe = _arg(args, "exe", "")
            if not exe:
                return "[Error] err: load 需要 exe 路径"
            if sess is not None:
                sess.cleanup()
            sess = SimSession(task_id)
            sessions[task_id] = sess
            # 2026-08-19：inputs 数组优先（显式按 scanf 调用顺序消费，空串保留
            # 占位——程序有 N 次 scanf 就传 N 个输入）；name/serial 旧参数兼容
            # （空值被过滤——只传 serial 时首个 scanf 会消费掉它，后续 scanf EOF）。
            raw_inputs = _arg(args, "inputs")
            if isinstance(raw_inputs, list):
                inputs = [str(s) for s in raw_inputs]
            else:
                inputs = [s for s in (_arg(args, "name"), _arg(args, "serial")) if s]
            try:
                return sess.load(exe, [], inputs)
            except Exception as e:  # noqa: BLE001
                return f"[Error] err: 加载失败 {e}"

        if sess is None:
            return "[Error] err: 会话未创建（先 load）"

        if action == "run":
            return _fmt(sess.run(until_addr=_int_addr(_arg(args, "until_addr")),
                                 steps_limit=int(_arg(args, "steps_limit") or 0),
                                 timeout_ms=int(_arg(args, "timeout_seconds") or 0) * 1000,
                                 trace_on=bool(_arg(args, "trace"))))
        if action in ("ff", "fast"):
            # 安全区快跑（2026-08-19）：快照兜底 + 卸载全域 CODE hook 纯 C 执行。
            # 只适用于纯计算循环区（如 main 前 hex 解析 0x800 乘加循环）；
            # 遇 int3/异常/特殊地址自动回滚（状态无损），返回 fast_rollback。
            # 2026-08-19：命令名统一用完整词 fast（AI 可读），旧名 ff 兼容。
            addr = _int_addr(_arg(args, "until_addr")) or _int_addr(_arg(args, "addr"))
            if not addr:
                return "[Error] err: fast 需要 until_addr（快进目标地址）"
            return _fmt(sess.fast_forward(
                addr,
                count=int(_arg(args, "count") or 0),
                timeout_ms=int(_arg(args, "timeout_seconds") or 30) * 1000))
        if action == "step":
            # 单步诊断（2026-08-19）：hook 全保留（int3/异常/API 语义完整）
            # 执行 N 条指令，返回停点 EIP——AI 配合 regs/mem 逐段观察。
            n = max(1, int(_arg(args, "count") or _arg(args, "size") or 1))
            eip0 = sess.uc.reg_read(x86_const.UC_X86_REG_EIP)
            try:
                sess.uc.emu_start(eip0, 0, count=n)
                eip1 = sess.uc.reg_read(x86_const.UC_X86_REG_EIP)
                return f"step {n}: 0x{eip0:X} -> 0x{eip1:X}（可 regs/mem 查看）"
            except Exception as e:  # noqa: BLE001
                # P13（2026-08-20）：真实异常不再伪装成正常输出（DB 实证误标）
                return f"[Error] step {n}: 0x{eip0:X} 异常 {e}"
        if action == "regs":
            return "\n".join(f"  {k} = {v}" for k, v in sess.regs().items())
        if action == "mem":
            addr = _int_addr(_arg(args, "addr"))
            size = int(_arg(args, "size") or 64)
            if addr is None:
                return "[Error] err: mem 需要 addr"
            return sess.dump(addr, size, _arg(args, "out_file", ""))
        if action == "dump":
            addr = _int_addr(_arg(args, "addr"))
            size = int(_arg(args, "size") or 0x1000)
            out = _arg(args, "out_file", "")
            if addr is None or not out:
                return "[Error] err: dump 需要 addr + out_file"
            return sess.dump(addr, size, out)
        if action == "write":
            addr = _int_addr(_arg(args, "addr"))
            data = _arg(args, "data", "")
            if addr is None or not data:
                return "[Error] err: write 需要 addr + data(hex)"
            return sess.patch(addr, data)
        if action == "patch":
            return sess.patch(_int_addr(_arg(args, "addr")) or 0, _arg(args, "data", ""))
        if action == "hook":
            sess._trace_on = True
            return "trace 已开启（下次 run 记录执行流）"
        if action == "snapshot":
            return sess.snapshot()
        if action == "restore":
            return sess.restore()
        if action == "replay":
            inputs = [s for s in (_arg(args, "name"), _arg(args, "serial")) if s]
            if not inputs:
                return "[Error] err: replay 需要 name/serial"
            return _fmt(sess.replay(inputs))
        if action == "trace":
            return sess.trace(int(_arg(args, "size") or 200))
        if action == "dyncode":
            return sess.dyncode()
        if action == "antidbg":
            return sess.antidbg_report()
        if action == "output":
            return sess.output_text()
        if action == "cleanup":
            sess.cleanup()
            sessions.pop(task_id, None)
            return "会话已清理"
        if action == "status":
            return _fmt(sess.run(trace_on=False)) if False else \
                "\n".join([
                    f"loaded: {sess.exe_path}",
                    f"eip: {sess.regs().get('eip')}",
                    f"inputs: {sess.inputs}",
                    f"output: {sess.output_text()[:200]!r}",
                    f"api_calls: {len(sess.api_calls)}",
                    f"antidbg:\n{sess.antidbg_report()}",
                ])
        if action == "deobf":
            # 去混淆管道：对地址范围跑 R1-R5 规则 → 命中列表（d810 规则引擎思想）
            addr = _int_addr(_arg(args, "addr"))
            size = int(_arg(args, "size") or 0x200)
            if not addr:
                return "[Error] err: deobf 需要 addr"
            try:
                from .deobf import analyze as _deobf_analyze
                hits = _deobf_analyze(sess.uc, addr, size, mode=32)
            except Exception as e:
                return "[Error] err: %r" % e
            if not hits:
                return "deobf 0x%X..0x%X: 无规则命中" % (addr, addr + size)
            return "\n".join("0x%X: %s -> 0x%X" % (a, r, t) for a, r, t in hits)
        if action == "fixcfg":
            # 控制流矫正：分发器/乱码区出口推演（simulate_dispatcher 思想）
            addr = _int_addr(_arg(args, "addr"))
            if not addr:
                return "[Error] err: fixcfg 需要 addr"
            try:
                import capstone as cs
                from .deobf import dispatch_guess
                md = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_32)
                md.detail = True
                tgt, steps = dispatch_guess(sess.uc, addr, md, 100)
            except Exception as e:
                return "[Error] err: %r" % e
            if tgt is None:
                return "fixcfg 0x%X: %d 步未找到出口（可能 ret/乱码）" % (addr, steps)
            return "fixcfg 0x%X: 出口 0x%X（%d 步）" % (addr, tgt, steps)
        if action == "symexec":
            # 混合符号执行：卡点求解（z3 默认；angr 需 w64_sym_engine='angr'）
            addr = _int_addr(_arg(args, "addr"))
            if not addr:
                return "[Error] err: symexec 需要 addr"
            engine = _arg(args, "size") or "z3"  # 复用 size 参数传引擎名（宽松兼容）
            if engine not in ("z3", "angr"):
                engine = "z3"
            try:
                import capstone as cs
                from .symexec import symexec as _sym
                md = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_32)
                md.detail = True
                out = _sym(sess.uc, addr, engine=engine, md=md)
            except Exception as e:
                return "[Error] err: %r" % e
            lines = ["symexec 0x%X (engine=%s):" % (addr, out.get("engine", engine))]
            for k in ("branch", "jumptable", "angr"):
                v = out.get(k) or []
                if v:
                    lines.append("  %s: %s" % (k, ", ".join(
                        "0x%X%s" % (t[0], "(taken)" if len(t) > 1 and t[1] else "") if isinstance(t, tuple) else "0x%X" % t
                        for t in v)))
            return "\n".join(lines)
        if action == "blackhole":
            # 算力黑洞探测报告：__blackhole__/__w64__ 轮数统计
            rows = [k for k in sess.api_calls if k.startswith("__blackhole__")]
            w64 = [k for k in sess.api_calls if k.startswith("__w64__")]
            if not rows and not w64:
                return "blackhole: 无记录（未执行过子执行器调用）"
            lines = []
            for k in sorted(rows):
                lines.append("  %s: %s（疑似算力黑洞——可 patch ret 跳过或继续 run）" % (k, sess.api_calls[k]))
            for k in sorted(w64)[:10]:
                lines.append("  %s: %s" % (k, sess.api_calls[k]))
            return "blackhole 报告:\n" + "\n".join(lines)
        return (f"[Error] err: 未知 action={action!r}。可用: load/run/regs/mem/dump/write/patch/hook/"
                f"snapshot/restore/replay/trace/dyncode/antidbg/output/status/cleanup/deobf/"
                f"fixcfg/symexec/blackhole")
    except Exception as e:  # noqa: BLE001
        return f"[Error] err: {e}"


def _fmt(d: dict) -> str:
    return "\n".join(f"  {k}: {v}" for k, v in d.items())
