# -*- coding: utf-8 -*-
import io
import json
import os
import re
import threading
import time
import traceback
from typing import Optional

from unicorn import x86_const

from .engine import SimSession

_BATCH_ITEM_CHARS = 300
_BATCH_ITEM_CHARS_VERBOSE = 2000
_BATCH_TOTAL_CHARS = 12000
_SCRIPT_TOTAL_CHARS = 12000


class SimBudgetExceeded(RuntimeError):
    pass


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
    action = _arg(args, "action", "")
    if action == "batch":
        return _run_batch(task_id, sessions, args)
    if action == "script":
        return _run_script(task_id, sessions, args)
    return _dispatch(task_id, sessions, args)


def _dispatch(task_id: str, sessions: dict, args: dict) -> str:
    action = _arg(args, "action", "")
    sess = sessions.get(task_id)
    if action != "cleanup" and sess is not None \
            and getattr(sess, "_script_runaway", False):
        return ("[Error] err: 上一次 script 超时未终止，仍在后台线程操作本会话——"
                "为避免并发操作同一 unicorn 实例（非线程安全）已拦下本次调用；"
                "用 action=cleanup 丢弃该会话后重新 load")
    try:
        if action == "load":
            exe = _arg(args, "exe", "")
            if not exe:
                return "[Error] err: load 需要 exe 路径"
            if sess is not None:
                sess.cleanup()
            sess = SimSession(task_id)
            sessions[task_id] = sess
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
            addr = _int_addr(_arg(args, "until_addr")) or _int_addr(_arg(args, "addr"))
            if not addr:
                return "[Error] err: fast 需要 until_addr（快进目标地址）"
            return _fmt(sess.fast_forward(
                addr,
                count=int(_arg(args, "count") or 0),
                timeout_ms=int(_arg(args, "timeout_seconds") or 30) * 1000))
        if action == "step":
            n = max(1, int(_arg(args, "count") or _arg(args, "size") or 1))
            _rip = x86_const.UC_X86_REG_RIP if sess.image.is64 else x86_const.UC_X86_REG_EIP
            eip0 = sess.uc.reg_read(_rip)
            try:
                sess.uc.emu_start(eip0, 0, count=n)
                eip1 = sess.uc.reg_read(_rip)
                return f"step {n}: 0x{eip0:X} -> 0x{eip1:X}（可 regs/mem 查看）"
            except Exception as e:  # noqa: BLE001
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
            raw_inputs = _arg(args, "inputs")
            if isinstance(raw_inputs, list):
                inputs = [str(s) for s in raw_inputs]
            else:
                inputs = [s for s in (_arg(args, "name"), _arg(args, "serial")) if s]
            if not inputs:
                return "[Error] err: replay 需要 inputs 数组（或 name/serial）"
            return _fmt(sess.replay(
                inputs,
                until_addr=_int_addr(_arg(args, "until_addr")),
                timeout_ms=int(_arg(args, "timeout_seconds") or 0) * 1000))
        if action == "reset":
            raw_inputs = _arg(args, "inputs")
            inputs = [str(s) for s in raw_inputs] if isinstance(raw_inputs, list) else None
            return sess.reset(inputs=inputs, exe_path=_arg(args, "exe"))
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
                    f"{'rip' if sess.image.is64 else 'eip'}: "
                    f"{sess.regs().get('rip') if sess.image.is64 else sess.regs().get('eip')}",
                    f"inputs: {sess.inputs}",
                    f"output: {sess.output_text()[:200]!r}",
                    f"api_calls: {len(sess.api_calls)}",
                    f"antidbg:\n{sess.antidbg_report()}",
                ])
        if action == "deobf":
            addr = _int_addr(_arg(args, "addr"))
            size = _int_addr(_arg(args, "size")) or 0x200
            if not addr:
                return "[Error] err: deobf 需要 addr"
            try:
                from .deobf import analyze as _deobf_analyze
                hits = _deobf_analyze(sess.uc, addr, size,
                                      mode=64 if sess.image.is64 else 32)
            except Exception as e:
                return "[Error] err: %r" % e
            if not hits:
                return "deobf 0x%X..0x%X: 无规则命中" % (addr, addr + size)
            return "\n".join("0x%X: %s -> 0x%X" % (a, r, t) for a, r, t in hits)
        if action == "fixcfg":
            addr = _int_addr(_arg(args, "addr"))
            if not addr:
                return "[Error] err: fixcfg 需要 addr"
            try:
                import capstone as cs
                from .deobf import dispatch_guess
                md = cs.Cs(cs.CS_ARCH_X86,
                           cs.CS_MODE_64 if sess.image.is64 else cs.CS_MODE_32)
                md.detail = True
                tgt, steps = dispatch_guess(sess.uc, addr, md, 100)
            except Exception as e:
                return "[Error] err: %r" % e
            if tgt is None:
                return "fixcfg 0x%X: %d 步未找到出口（可能 ret/乱码）" % (addr, steps)
            return "fixcfg 0x%X: 出口 0x%X（%d 步）" % (addr, tgt, steps)
        if action == "symexec":
            addr = _int_addr(_arg(args, "addr"))
            if not addr:
                return "[Error] err: symexec 需要 addr"
            engine = _arg(args, "size") or "z3"
            if engine not in ("z3", "angr"):
                engine = "z3"
            try:
                import capstone as cs
                from .symexec import symexec as _sym
                md = cs.Cs(cs.CS_ARCH_X86,
                           cs.CS_MODE_64 if sess.image.is64 else cs.CS_MODE_32)
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
                f"snapshot/restore/replay/reset/trace/dyncode/antidbg/output/status/cleanup/deobf/"
                f"fixcfg/symexec/blackhole/batch/script"
                f"（batch=一轮内顺序执行多条命令；script=一段 Python 驱动会话，可轮内条件分支）")
    except Exception as e:  # noqa: BLE001
        return f"[Error] err: {e}"


def _fmt(d: dict) -> str:
    return "\n".join(f"  {k}: {v}" for k, v in d.items())




def _clip(text: str, cap: int, mark: str = "") -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + (mark or f"...(+{len(text) - cap} chars)")


def _cmd_brief(cmd: dict) -> str:
    act = _arg(cmd, "action", "")
    for k in ("until_addr", "addr", "exe", "size", "count", "inputs"):
        v = cmd.get(k)
        if v not in (None, ""):
            return f"{act} {k}={v}"
    return act


def _as_bool(v, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    return s in ("true", "1", "yes", "y", "on") if s else default


def _run_batch(task_id: str, sessions: dict, args: dict) -> str:
    cmds = _arg(args, "commands")
    if not isinstance(cmds, list) or not cmds:
        return ("[Error] err: batch 需要 commands（子命令对象数组，每项形如 "
                '{"action": "run", "until_addr": "0x401000"}）')
    budget = float(int(_arg(args, "timeout_seconds") or 60))
    stop_on_error = _as_bool(_arg(args, "stop_on_error"), True)
    verbose = _as_bool(_arg(args, "verbose"), False)
    item_cap = _BATCH_ITEM_CHARS_VERBOSE if verbose else _BATCH_ITEM_CHARS

    t0 = time.monotonic()
    lines: list = []
    done = 0
    halted = ""
    for i, cmd in enumerate(cmds):
        if time.monotonic() - t0 > budget:
            halted = f"[budget] 剩余 {len(cmds) - i} 条未执行（预算 {budget:g}s 用尽）"
            break
        if not isinstance(cmd, dict) or not _arg(cmd, "action"):
            done += 1
            lines.append(f"  {i + 1}. <非法命令> -> [Error] 需要对象且含 action")
            if stop_on_error:
                halted = (f"[stopped] 第 {i + 1} 条非法，剩余 {len(cmds) - i - 1} "
                          f"条未执行（stop_on_error=true）")
                break
            continue
        out = _dispatch(task_id, sessions, cmd)
        done += 1
        lines.append(f"  {i + 1}. {_cmd_brief(cmd)} -> {_clip(out, item_cap)}")
        if stop_on_error and out.startswith("[Error]"):
            halted = (f"[stopped] 第 {i + 1} 条报错，剩余 {len(cmds) - i - 1} "
                      f"条未执行（stop_on_error=true）")
            break
    head = (f"[batch] {len(cmds)} 条命令 / 耗时 {time.monotonic() - t0:.2f}s / "
            f"预算 {budget:g}s / 已执行 {done}")
    body = "\n".join(lines)
    if halted:
        body = (body + "\n" + halted) if body else halted
    return _clip(head + "\n" + body, _BATCH_TOTAL_CHARS,
                 mark="\n[截断] 批量输出超上限（用 verbose=false 或拆成多批）")


_DEADLINE_METHODS = ("run", "fast_forward", "replay", "reset", "dump", "snapshot")


def _arm_deadline(sess, deadline: float) -> dict:
    saved = {}
    for m in _DEADLINE_METHODS:
        orig = getattr(sess, m, None)
        if not callable(orig):
            continue
        saved[m] = True

        def _wrap(orig=orig):
            def _f(*a, **kw):
                if time.monotonic() > deadline:
                    raise SimBudgetExceeded("超出 script 时间预算")
                return orig(*a, **kw)
            return _f

        setattr(sess, m, _wrap())
    return saved


def _disarm_deadline(sess, saved: dict) -> None:
    for m in saved:
        try:
            delattr(sess, m)
        except AttributeError:
            pass


def _script_print(buf):
    def _p(*a, **kw):
        kw.pop("flush", None)
        kw.pop("file", None)
        print(*a, file=buf, **kw)
    return _p


def _run_script(task_id: str, sessions: dict, args: dict) -> str:
    sess = sessions.get(task_id)
    if sess is None:
        return "[Error] err: 会话未创建（先 load）"
    code = _arg(args, "code", "")
    if not code.strip():
        return ("[Error] err: script 需要 code（Python 代码；命名空间已注入 "
                "sess/uc/cs/x86_const/int_addr/re/json/os，print 的内容即返回结果）")
    budget = float(int(_arg(args, "timeout_seconds") or 30))
    try:
        compiled = compile(code, "<dcflow_sim script>", "exec")
    except SyntaxError:
        return "[Error] 脚本语法错误:\n" + traceback.format_exc()[-1500:]

    buf = io.StringIO()
    ns = {
        "__name__": "__dcflow_sim_script__",
        "sess": sess, "uc": sess.uc, "x86_const": x86_const,
        "int_addr": _int_addr, "re": re, "json": json, "os": os, "time": time,
        "print": _script_print(buf),
        "SimBudgetExceeded": SimBudgetExceeded,
    }
    try:
        import capstone as _cs
        ns["cs"] = _cs
    except Exception:  # noqa: BLE001
        ns["cs"] = None

    holder: dict = {}

    def _target():
        try:
            exec(compiled, ns)
        except BaseException as e:  # noqa: BLE001
            holder["exc"] = e
            holder["tb"] = traceback.format_exc()

    saved = _arm_deadline(sess, time.monotonic() + budget)
    th = threading.Thread(target=_target, daemon=True, name="dcflow-sim-script")
    try:
        th.start()
        th.join(budget)
        if th.is_alive():
            sess._script_runaway = True
            buf.write(f"\n[budget exceeded after {budget:g}s：代码仍在后台线程运行，"
                      f"本会话已标记为占用——用 action=cleanup 丢弃后重新 load]")
        elif "exc" in holder:
            if isinstance(holder["exc"], SimBudgetExceeded):
                buf.write(f"\n[budget exceeded after {budget:g}s]")
            else:
                buf.write("\n[Error] 脚本异常:\n" + str(holder["tb"])[-2000:])
    finally:
        _disarm_deadline(sess, saved)
    return _clip(buf.getvalue().strip() or "(脚本无输出)", _SCRIPT_TOTAL_CHARS,
                 mark="\n[截断] 脚本输出超上限（大结果请写文件后只 print 路径与摘要）")
