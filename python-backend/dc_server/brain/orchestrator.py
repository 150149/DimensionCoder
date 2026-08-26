
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import PROJECT_ROOT
from ..graceful import graceful
from ..state_machine.state_machine import StateMachine
from ..storage.adapter import StorageAdapter
from .llm_client import LlmAborted, LlmError
from .sse_hub import SseHub

logger = logging.getLogger(__name__)

class OrchestratorBusyError(Exception):
    pass

class _StepGracefulFinish(Exception):
    pass

class _StepGracefulDrain(Exception):

    def __init__(self, step_id: str) -> None:
        super().__init__(step_id)
        self.step_id = step_id

_PROGRESS_SUMMARY_SYSTEM = (
    "你是执行引擎的收尾总结器。当前步骤因工具调用历史过长（压缩到仅剩最近 20 轮"
    "完整仍超 800K 上下文预算）被强制收尾，请把该步骤的**当前进度**总结为一段简洁的"
    "中文说明（≤500 字），供编排 AI 决定是否开新步骤继续。要点：1) 已完成的工作与"
    "结论；2) 未完成的事项；3) 下一步建议。不要调用任何工具。"
)

_CONTEXT_EXCEEDED_RE = re.compile(
    r"maximum context length|context length|reduce the length|exceed.*(?:limit|length)",
    re.IGNORECASE)

def _is_context_exceeded(err: Any) -> bool:
    msg = str(getattr(err, "message", None) or err)
    return bool(_CONTEXT_EXCEEDED_RE.search(msg))

_TOKENIZER: Any = None

def _count_tokens(msgs: List[dict]) -> Optional[int]:
    global _TOKENIZER
    if _TOKENIZER is None:
        try:
            import tiktoken
            _TOKENIZER = tiktoken.get_encoding("cl100k_base")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] tiktoken unavailable, token budget "
                           f"compression degraded: {e}")
            _TOKENIZER = False
    if not _TOKENIZER:
        return None
    try:
        total = 0
        for m in msgs:
            content = m.get("content") or ""
            if content:
                total += len(_TOKENIZER.encode(str(content)))
            tc = m.get("tool_calls")
            if tc:
                total += len(_TOKENIZER.encode(json.dumps(tc, ensure_ascii=False)))
        return total
    except Exception:  # noqa: BLE001
        return None

def _tool(name: str, description: str, properties: dict, required: Optional[list] = None) -> dict:
    d = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties},
        },
    }
    if required:
        d["function"]["parameters"]["required"] = required
    return d

_TOOL_LIST_DIR = _tool("dcflow_list_dir", "列出目录中的文件和子目录",
      {"dir_path": {"type": "string", "description": "路径，默认 '.'"}})
_TOOL_READ_FILE = _tool("dcflow_read_file", "读取文件内容（可用 start_line/end_line 按行范围分段读取，单次上限 30000 字符）",
      {"file_path": {"type": "string"},
       "start_line": {"type": "integer", "description": "起始行号（含），从 1 开始，默认 1"},
       "end_line": {"type": "integer", "description": "结束行号（含），默认到文件末尾"}},
      ["file_path"])

_EXEC_TOOLS: list[dict] = [
    dict(_TOOL_LIST_DIR),
    dict(_TOOL_READ_FILE),
    _tool("dcflow_write_file", "写入/覆盖文件",
          {"file_path": {"type": "string"}, "content": {"type": "string"}},
          ["file_path", "content"]),
    _tool("dcflow_edit_file", "精准替换文件文本：old_string 必须精确匹配文件中的唯一片段；"
          "若文本出现多处，默认替换第 1 处，可用 occurrence 指定第 N 处；replace_all=true 替换全部",
          {"file_path": {"type": "string"}, "old_string": {"type": "string"},
           "new_string": {"type": "string"}, "replace_all": {"type": "boolean"},
           "occurrence": {"type": "integer", "minimum": 1,
                           "description": "替换第 N 处匹配（从 1 开始），默认 1；与 replace_all 互斥"}},
          ["file_path", "old_string", "new_string"]),
    _tool("dcflow_search_code", "搜索代码/文本（pattern 为 Python 正则，逐行 re.search 匹配；path_filter 相对项目根目录的子目录，默认全项目递归），返回 file:line:content 最多 30 条",
          {"pattern": {"type": "string", "description": "Python 正则（如 r\"def \\w+\"）——不是纯文本，需转义特殊字符"},
           "path_filter": {"type": "string", "description": "相对根目录的子目录/文件路径，默认 '.'（全项目递归）"}},
          ["pattern"]),
    _tool("dcflow_run_cmd", "执行 shell 命令（Windows cmd 环境：head/tail 等 Unix 命令不可用——行过滤用 findstr /n、分页查看用 more +N，避免管道 | more 卡住）。同步等待，timeout_seconds 超时后强制终止整棵进程树；长任务请调大 timeout_seconds",
          {"command": {"type": "string"},
           "timeout_seconds": {"type": "integer", "description": "超时秒数，默认 60；超时后命令及其全部子进程会被强制终止，返回已捕获输出尾部"}},
          ["command"]),
    _tool("dcflow_read_doc", "读取知识库文档（filename=\"list\" 返回全部可用文档清单；文档目录: .github/docs、.github/instructions、docs）",
          {"filename": {"type": "string"}}, ["filename"]),
    _tool("dcflow_step_done", "标记步骤完成，完成所有工作后必须调用",
          {"task_id": {"type": "string"}, "step_id": {"type": "string"},
           "summary": {"type": "string"}}, ["task_id", "step_id"]),
]

_REVERSE_ONLY_TOOLS = {"dcflow_sim", "dcflow_get_decompiled_code",
                       "dcflow_extract_constants", "dcflow_search_bytes",
                       "dcflow_solve_z3"}

_CTF_TOOL_FUNCS = {
    "dcflow_get_decompiled_code": ("get_decompiled_code", ("file_path", "address")),
    "dcflow_extract_constants": ("extract_constants", ("file_path",)),
    "dcflow_search_bytes": ("search_bytes", ("file_path", "pattern")),
    "dcflow_solve_z3": ("solve_z3", ("constraint_script",)),
}

_REVERSE_TOOLS: list[dict] = [
    _tool("dcflow_sim", "Windows 程序 Unicorn 模拟器（环境自动准备，天然绕过全部反调试：PEB/IsDebuggerPresent/NtGlobalFlag/时间检测/父进程/异常类等无需处理）：load 加载 PE（自动 PE 加载+导入表+API stub+PEB/TEB 伪造+参数环境；name/serial 或 inputs 数组为模拟输入——按程序 scanf 调用顺序依次消费，程序有几次 scanf（如 name+serial 两次）就传几个输入，否则后面的 scanf 返回 EOF（EAX=0，缓冲为空，后续 strcpy/memcpy 会死循环卡住）；run 执行到 until_addr 即停（断点式推进：停后可用 write/mem 修改内存/寄存器，再 run 下一个 until_addr 继续；洗牌/障眼桥等纯计算循环会被引擎自动快进，run 返回 stop_reason=fast_auto 表示已自动加速推进一段，继续 run 即可）；fast 安全区快跑（快进 fast-forward，旧命令名 ff 兼容：快照兜底+失败自动回滚，纯计算循环区秒过——如 main 前 0x800 次 hex 解析/乘加循环，遇 int3/异常自动回滚状态无损，返回 fast_rollback 时改用 run）；step N 单步 N 条指令（hook 全保留，诊断逐段观察）；regs/mem/dump 查看；write/patch 改字节；hook 开启执行流；snapshot/restore/replay 快照与输入重放（黑盒推导：replay 改 name/serial 重跑对比）；trace 执行流；dyncode 输出模拟内动态解密代码；antidbg 反调试检测报告；deobf 去混淆规则管道（R1-R5：短跳垃圾/pushfd 包裹/恒定条件跳转/向后扫描/跳转表）；fixcfg 控制流矫正（分发器出口推演）；symexec 混合符号执行（z3 默认/angr 深度，卡点求解）；blackhole 算力黑洞探测报告；output 读程序输出（stdout/OutputDebugString）；status 会话状态；cleanup 结束。参数 {action, ...}。",
          {"action": {"type": "string"}, "exe": {"type": "string"},
           "name": {"type": "string"}, "serial": {"type": "string"},
           "inputs": {"type": "array", "items": {"type": "string"},
                       "description": "模拟输入数组（按 scanf 调用顺序消费，空串占位）——优先于 name/serial"},
           "addr": {"type": "string"}, "size": {"type": "integer"},
           "data": {"type": "string"}, "until_addr": {"type": "string"},
           "steps_limit": {"type": "integer"}, "timeout_seconds": {"type": "integer"},
           "out_file": {"type": "string"}, "trace": {"type": "boolean"}},
          ["action"]),
     _tool("dcflow_get_decompiled_code", "angr 反编译指定地址所属函数（file_path 为 PE 文件路径，address 为函数内任意地址）——返回函数伪代码（含栈帧/导入表/VM 提示）",
           {"file_path": {"type": "string"},
            "address": {"type": "string", "description": "目标函数内地址（hex 如 0x401000 或整数）"}},
           ["file_path", "address"]),
     _tool("dcflow_extract_constants", "扫描提取密码学常量并比对算法特征（file_path 为二进制文件路径）——dword/word 常量 + S-Box 匹配（MD5/SHA/AES/DES/CRC/RC4/TEA 等）",
           {"file_path": {"type": "string"}}, ["file_path"]),
     _tool("dcflow_search_bytes", "通配符字节搜索（file_path 二进制文件，pattern 为十六进制字节空格分隔，?? 通配）——返回命中偏移与前后文",
           {"file_path": {"type": "string"},
            "pattern": {"type": "string", "description": "十六进制字节，空格分隔，如 '41 41 ?? 00'"}},
           ["file_path", "pattern"]),
     _tool("dcflow_solve_z3", "独立进程执行 Z3 约束求解脚本（constraint_script 为完整 Python 脚本：from z3 import * + Solver + 末尾 print 结果）——返回 stdout",
           {"constraint_script": {"type": "string"}}, ["constraint_script"]),
]

_REVERSE_TOOLS = _REVERSE_TOOLS + [dict(t) for t in _EXEC_TOOLS]

_MONITOR_TOOLS: list[dict] = [
    _tool("dcflow_list_steps", "查看 Task 所有步骤状态",
          {"task_id": {"type": "string"}}, ["task_id"]),
    _tool("dcflow_adjust_flow",
          "修改流程: skip/add/remove/reorder/mark_complete。注意: steps_json 必须为步骤对象数组（JSON 数组，禁止字符串形式——字符串内层转义易错导致步骤丢失）",
          {"task_id": {"type": "string"}, "action": {"type": "string"},
           "step_ids": {"description": "步骤ID列表，逗号分隔字符串或字符串数组",
                        "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
           "steps_json": {"description": "新增步骤的JSON对象数组，每个元素含 step_id/title/description/model_tier/required/parallel_with/human_attention/type 等字段（human_attention=gate 可插入人工审批步骤）；每个元素可带 after_step_id 指定插入到该步骤之后（优先于顶层 after_step_id；不传则按顶层/默认位置）",
                          "type": "array", "items": {"type": "object"}},
           "after_step_id": {"description": "新增步骤统一插入到该步骤之后（可省略；steps_json 元素内嵌的 after_step_id 优先；不传则追加末尾真实步骤之后、review 之前）",
                              "type": "string"},
           "order_json": {"description": "步骤ID顺序数组或JSON字符串",
                          "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
           "reasoning": {"type": "string"}},
          ["task_id", "action", "reasoning"]),
    dict(_TOOL_LIST_DIR),
    dict(_TOOL_READ_FILE),
    _tool("dcflow_step_done", "提交审查结论摘要，完成审查后必须调用",
          {"task_id": {"type": "string"}, "step_id": {"type": "string"},
           "summary": {"type": "string"}}, ["task_id", "step_id"]),
]

_GATE_TOOLS: list[dict] = [
    _tool("dcflow_read_file", "读取项目文件（可用 start_line/end_line 按行范围分段读取，单次上限 30000 字符）",
          {"file_path": {"type": "string"},
           "start_line": {"type": "integer", "description": "起始行号（含），从 1 开始，默认 1"},
           "end_line": {"type": "integer", "description": "结束行号（含），默认到文件末尾"}},
          ["file_path"]),
    _tool("dcflow_search_code", "搜索代码/文本（pattern 为 Python 正则，逐行匹配；path_filter 相对根目录，默认全项目递归）",
          {"pattern": {"type": "string", "description": "Python 正则（如 r\"def \\w+\"）——不是纯文本，需转义特殊字符"},
           "path_filter": {"type": "string", "description": "相对根目录的子目录/文件路径，默认 '.'"}},
          ["pattern"]),
    _tool("dcflow_read_doc", "读取知识库文档（filename=\"list\" 返回全部可用文档清单）",
          {"filename": {"type": "string"}}, ["filename"]),
    _tool("dcflow_list_dir", "列出目录",
          {"dir_path": {"type": "string"}}),
    _tool("dcflow_list_steps", "查看步骤状态",
          {"task_id": {"type": "string"}}, ["task_id"]),
    _tool("dcflow_step_done", "提交审批摘要，完成审查后必须调用",
          {"task_id": {"type": "string"}, "step_id": {"type": "string"},
           "summary": {"type": "string"}}, ["task_id", "step_id"]),
]

_RESEARCHER_TOOLS: list[dict] = [
    dict(_TOOL_LIST_DIR),
    dict(_TOOL_READ_FILE),
    _tool("dcflow_search_code", "搜索代码/文本（pattern 为 Python 正则，逐行 re.search 匹配；path_filter 相对项目根目录的子目录，默认全项目递归），返回 file:line:content 最多 30 条",
          {"pattern": {"type": "string", "description": "Python 正则（如 r\"def \\w+\"）——不是纯文本，需转义特殊字符"},
           "path_filter": {"type": "string", "description": "相对根目录的子目录/文件路径，默认 '.'（全项目递归）"}},
          ["pattern"]),
    _tool("dcflow_read_doc", "读取知识库文档（filename=\"list\" 返回全部可用文档清单；文档目录: .github/docs、.github/instructions、docs）",
          {"filename": {"type": "string"}}, ["filename"]),
    _tool("dcflow_step_done", "标记步骤完成，完成所有工作后必须调用",
          {"task_id": {"type": "string"}, "step_id": {"type": "string"},
           "summary": {"type": "string"}}, ["task_id", "step_id"]),
]

_TEXT_TOOL_CALL_RE = re.compile(r'\{\s*"tool"\s*:\s*"(dcflow_\w+)"\s*,\s*"arguments"\s*:\s*(\{[\s\S]*?\})\s*\}')

def _gate_report_present(msgs: list[dict], round_text: str) -> bool:
    recent = "".join(
        str(m.get("content") or "") for m in msgs if m.get("role") == "assistant"
    ) + (round_text or "")
    return ("方案审批报告" in recent or '"options"' in recent
            or '"recommendation"' in recent or "已确认人类决策" in recent)

def _is_empty_arguments(raw: Any) -> bool:
    s = (raw or "").strip()
    if not s or s == "null":
        return True
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return True
    if not isinstance(obj, dict):
        return True
    return len(obj) == 0

def _parse_text_tool_calls(text: str) -> list[dict]:
    import hashlib

    results: list[dict] = []
    for match in _TEXT_TOOL_CALL_RE.finditer(text or ""):
        try:
            args_obj = json.loads(match.group(2))
        except (ValueError, TypeError):
            continue
        digest = hashlib.md5(
            (match.group(1) + json.dumps(args_obj, sort_keys=True)).encode("utf-8")
        ).hexdigest()[:12]
        results.append({
            "name": match.group(1),
            "call_id": f"text-{digest}-{len(results)}",
            "input": args_obj,
        })
    return results

_EMPTY_NUDGE = ("【系统提示】你上一轮未输出任何内容。"
                "请基于已有工具结果直接输出本步骤的结论与总结（工作已完成可调用 dcflow_step_done）。")

_TEXT_FINISH_NUDGE = ("【系统提示】你输出了文本但未调用完成工具（dcflow_step_done），步骤不会提交。"
                      "若工作已完成，请调用 dcflow_step_done 提交结论；若需继续分析，请直接调用工具。")

_KEY_FINDING_RE = re.compile(
    r"(?:关键发现|重大发现|核心发现|突破了|核心突破|关键突破|key\s*findings?)"
    r"\s*[:：]?\s*([^。.\n]+)",
    re.IGNORECASE)
_SYSTEM_PROMPT_MARKERS = (
    "同义关键词", "系统自动捕获", "自动捕获", "到句号为止",
    "关键发现.txt", "<结论>", "关键发现文件",
)
_KEY_FINDING_ARTIFACT = "key_findings"
_KEY_FINDING_MAX_LEN = 200
_KEY_FINDING_MAX_LINES = 100
_STEP_REPORT_ARTIFACT = "step_report"
_FLOW_REPORT_FILENAME = "流程报告.md"
_COMPRESS_MAP_ARTIFACT = "compress_map"
_KEY_FINDINGS_BLOCK_START = "<!-- DC-KEY-FINDINGS-START -->"
_KEY_FINDINGS_BLOCK_END = "<!-- DC-KEY-FINDINGS-END -->"
_KEY_FINDINGS_BLOCK_RE = re.compile(
    rf"(.*){_KEY_FINDINGS_BLOCK_START}\s*## 系统注入：关键发现.*?{_KEY_FINDINGS_BLOCK_END}\s*$", re.S)
_LAST_PROMPT_TOKENS_ARTIFACT = "last_prompt_tokens"
_FLOW_REPORT_ANCHOR_CHARS = 1500
_STEP_REPORT_ROUNDS_ARTIFACT = "step_report_rounds"
_STEP_REPORT_READ_ROUND_ARTIFACT = "step_report_read_round"
_MONITOR_ANCHORS_ARTIFACT = "monitor_anchors"

GIVE_UP_PATTERN = re.compile(
    r"(穷尽|无法突破|无法继续|无法穿透|无法静态|无法逆推|超出.{0,6}能力|无能为力|无解|死路"
    r"|瓶颈|做不到|走不通|行不通|诚实交付|诚实结论|标记完成|最终交付|最终确认交付"
    r"|做最后的决定|到此为止|就此打住)"
)

class Orchestrator:

    MAX_ITERATIONS = 100
    MAX_RETRY = 10
    COMPRESS_TOKENS = 400_000
    CONTEXT_BUDGET_TOKENS = 200_000
    KEEP_RECENT_TOOL_ROUNDS = 20
    MAX_TEXT_FINISH_NUDGE = 1

    def __init__(self, storage: StorageAdapter, state_machine: StateMachine,
                 sse_hub: Optional[SseHub] = None,
                 tool_invoke: Optional[Any] = None) -> None:
        self._storage = storage
        self._sm = state_machine
        self.sse_hub = sse_hub if sse_hub is not None else SseHub()
        self._tool_invoke = tool_invoke

        self._running: Dict[str, bool] = {}
        self._cancelled: Dict[str, bool] = {}
        self._cancel_kind: Dict[str, Optional[str]] = {}
        self._cancel_events: Dict[str, asyncio.Event] = {}
        self._prep_cache: Dict[str, dict] = {}
        self._last_saved_round: Dict[str, int] = {}
        self._step_t0: Dict[str, float] = {}
        self._initial_orchestrating: set = set()
        self._sim_sessions: dict = {}
        self._sim_locks: dict = {}

    def is_running(self, task_id: str) -> bool:
        return bool(self._running.get(task_id, False))

    async def _ensure_initial_orchestration(self, task_id: str) -> bool:
        if task_id in self._initial_orchestrating:
            return True
        self._initial_orchestrating.add(task_id)
        try:
            task = await self._storage.get_task(task_id, include_hidden=True)
            if not task:
                return False
            if task.get("steps"):
                return False
            await self._storage.ensure_step(task_id, "monitor-init", "初始编排", "monitor")
            logger.info(f"[DC:orch] initial orchestration step monitor-init inserted for {task_id}")
            return True
        finally:
            self._initial_orchestrating.discard(task_id)

    async def start_task(self, task_id: str) -> None:
        if self._running.get(task_id, False):
            if not self._cancelled.get(task_id, False):
                return
            logger.warning(f"[DC:orch] zombie running for task {task_id} (cancelled), "
                           f"waiting for previous loop to exit before restart")
            await self.wait_stopped(task_id, timeout=180.0)
            if self._running.get(task_id, False):
                logger.error(f"[DC:orch] task {task_id} loop still stuck after cancel, "
                             f"give up restart (click resume again later)")
                return
        try:
            task0 = await self._storage.get_task(task_id)
            if task0 and not task0.get("steps"):
                await self._ensure_initial_orchestration(task_id)
        except Exception:
            pass
        self._running[task_id] = True
        self._cancelled[task_id] = False
        self._cancel_kind[task_id] = None
        self._cancel_events.pop(task_id, None)
        try:
            from ..config import get_memory_config, PROJECT_ROOT
            if get_memory_config().get("enabled"):
                from ..rest_api import _get_memory_storage
                ms = _get_memory_storage()
                if ms is not None:
                    ms.get_or_create_bank_for_project(PROJECT_ROOT)
        except Exception:
            pass
        asyncio.create_task(self._run_loop(task_id))

    async def approve_gate_and_run(self, task_id: str, step_id: str, reason: Optional[str] = None) -> None:
        if self._running.get(task_id, False):
            raise OrchestratorBusyError("该步骤正在审查中，请稍后再审批")
        await self._sm.handle_gate(task_id, step_id, "approved", reason or "")
        await self._insert_monitor_step(task_id, step_id)
        await self._sm._resume_after_intervention(task_id)
        await self.start_task(task_id)

    async def reject_gate_and_run(self, task_id: str, step_id: str, reason: str) -> None:
        if self._running.get(task_id, False):
            raise OrchestratorBusyError("该步骤正在审查中，请稍后再审批")
        await self._sm.reject_gate(task_id, step_id, reason)

    async def stop_task(self, task_id: str, level: str = "user") -> None:
        self._cancel_kind[task_id] = "stop"
        await self.abort(task_id)
        task = await self._storage.get_task(task_id)
        if not task:
            return
        for s in task.get("steps", []):
            if s["status"] == "active":
                await self._sm.advance_step(task_id, s["step_id"], "stopped")
                break
        await self._sm.pause_task(task_id)

    async def abort(self, task_id: str, kind: Optional[str] = None) -> None:
        if kind is not None:
            self._cancel_kind[task_id] = kind
        if self._cancel_kind.get(task_id) is None:
            self._cancel_kind[task_id] = "stop"
        self._cancelled[task_id] = True
        ev = self._cancel_events.get(task_id)
        if ev is not None:
            ev.set()
        try:
            from .. import rest_api
            await rest_api.kill_task_cmds(task_id)
        except Exception:  # noqa: BLE001
            pass
        await self.wait_stopped(task_id)

    async def wait_stopped(self, task_id: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._running.get(task_id, False):
                return
            await asyncio.sleep(0.2)

    def _step_is_review_kind(self, step_id: str, step_type: str = "") -> bool:
        return (step_type in ("monitor", "review", "report")
                or step_id.startswith("monitor-") or step_id in ("review", "report"))

    async def _monitor_prep(self, task_id: str, step: dict) -> dict:
        from .. import rest_api
        from ..prompts import load_prompt
        from ..prompts.registry import prompt_for_step
        from ..step_context import list_prior_step_outputs, get_task_root
        step_id = step.get("step_id", "")
        step_type = step.get("type") or (
            "monitor" if step_id.startswith("monitor-")
            else "review" if step_id == "review" else "report")
        is_final = step_type in ("review", "report")
        after_step_id = ""
        if not is_final:
            try:
                task_all = await self._storage.get_task(task_id, include_hidden=True)
                before = [s for s in (task_all or {}).get("steps", [])
                          if not self._step_is_review_kind(
                              s.get("step_id", ""), s.get("type", ""))
                          and s.get("status") == "completed"
                          and s.get("sort_order", 0) < step.get("sort_order", 0)]
                if before:
                    after_step_id = (max(before,
                                         key=lambda s: s.get("sort_order", 0))
                                     .get("step_id", ""))
            except Exception:  # noqa: BLE001
                after_step_id = ""
        try:
            ctx = await rest_api.monitor_export({
                "task_id": task_id,
                "step_id": "" if is_final else after_step_id,
                "final_review": is_final,
                "step_type": step_type,
            }, storage=self._storage)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] monitor export failed for task {task_id}: {e}")
            ctx = {}
        system_prompt = load_prompt(prompt_for_step(step_id, step_type))
        if not is_final and step_type == "monitor":
            system_prompt += "\n\n" + load_prompt("flow-templates")
        user_context = ctx.get("user_context", "") or ""
        try:
            task_for_prior = await self._storage.get_task(task_id, include_hidden=True)
            prior_outputs = list_prior_step_outputs(
                task_id, (task_for_prior or {}).get("steps", []), step_id)
            if prior_outputs:
                user_context += (
                    "\n\n## 前序步骤产物文件（AI 产出，用 dcflow_read_file 按相对路径读取）\n"
                    + "\n".join(prior_outputs))
        except Exception:  # noqa: BLE001
            pass
        flow_msg = await self._consume_flow_intervention(task_id)
        if flow_msg:
            user_context += f"\n\n## 用户流程级请求\n{flow_msg}"
        if step_type == "review":
            task_now = await self._storage.get_task(task_id)
            if task_now and task_now.get("best_effort"):
                user_context += ("\n\n## 尽力模式（用户暂时不在线）\n"
                                 "用户不在线，无法审批。请先核对任务描述要求的最终交付物是否已全部"
                                 "产出（如 CTF 的 flag/答案、修复完成、报告产出）；若未达成，"
                                 "请用 add_steps 追加求解/验证步骤继续推进，不要收尾。"
                                 "只有确认全部目标完成时才选择收尾。")
        return {"system_prompt": system_prompt, "step_context": user_context,
                "model_tier": "power", "step_title": step_id}

    async def _ensure_tail_steps(self, task_id: str) -> None:
        conn = await self._storage._get_conn()
        exists = {r["step_id"] for r in conn.execute(
            "SELECT step_id FROM task_steps WHERE task_id = ? "
            "AND step_id IN ('review','report')", (task_id,)).fetchall()}
        if "review" not in exists:
            await self._storage.ensure_step(task_id, "review", "最终审查", "review")
        if "report" not in exists:
            await self._storage.ensure_step(task_id, "report", "产出报告", "report")

    async def _next_monitor_instance_id(self, task_id: str) -> str:
        conn = await self._storage._get_conn()
        rows = conn.execute(
            "SELECT step_id FROM task_steps WHERE task_id = ? "
            "AND step_id GLOB 'monitor-[0-9]*'", (task_id,)).fetchall()
        seq = 0
        for r in rows:
            m = re.match(r"^monitor-(\d+)$", r["step_id"])
            if m:
                seq = max(seq, int(m.group(1)))
        return f"monitor-{seq + 1}"

    async def _insert_monitor_step(self, task_id: str, step_id: str) -> None:
        task = await self._storage.get_task(task_id)
        if not task or task.get("status") in ("completed", "abandoned"):
            return
        if self._step_is_review_kind(step_id, ""):
            return
        instance_id = await self._next_monitor_instance_id(task_id)
        try:
            await self._storage.add_steps(task_id, [{
                "step_id": instance_id, "title": f"Monitor 审查 {instance_id}",
                "type": "monitor", "human_attention": "none", "model_tier": "power",
                "required": 1, "parallel_with": None, "description": "",
            }], after_step_id=step_id)
            await self._record_monitor_anchor(task_id, instance_id, step_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] insert monitor step {instance_id} failed: {e}")

    async def _record_monitor_anchor(self, task_id: str, instance_id: str,
                                     trigger_step_id: str) -> None:
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", _MONITOR_ANCHORS_ARTIFACT)
            anchors: dict = {}
            if raw and raw.get("content"):
                try:
                    data = json.loads(raw["content"])
                    anchors = data if isinstance(data, dict) else {}
                except (ValueError, TypeError):
                    anchors = {}
            anchors[instance_id] = trigger_step_id
            await self._storage.save_artifact(
                task_id, "_flow", _MONITOR_ANCHORS_ARTIFACT,
                json.dumps(anchors, ensure_ascii=False), "json")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] record monitor anchor {instance_id} failed: {e}")

    async def _save_intervention(self, task_id: str, step_id: str, message: str) -> None:
        raw = await self._storage.get_artifact(task_id, step_id, "intervention")
        items: list = []
        if raw and raw.get("content"):
            try:
                data = json.loads(raw["content"])
                items = data if isinstance(data, list) else [data]
            except (json.JSONDecodeError, TypeError):
                items = []
        items.append({"message": message, "role": "user",
                      "timestamp": datetime.now(timezone.utc).isoformat()})
        await self._storage.save_artifact(
            task_id, step_id, "intervention", json.dumps(items, ensure_ascii=False), "json")

    async def _next_intervene_instance_id(self, task_id: str) -> str:
        conn = await self._storage._get_conn()
        rows = conn.execute(
            "SELECT step_id FROM task_steps WHERE task_id = ? "
            "AND step_id GLOB 'monitor-intervene-*'", (task_id,)).fetchall()
        seq = 0
        for r in rows:
            m = re.match(r"^monitor-intervene-(\d+)$", r["step_id"])
            if m:
                seq = max(seq, int(m.group(1)))
        return f"monitor-intervene-{seq + 1}"

    async def trigger_monitor(self, task_id: str, reason: str, mode: str = "send") -> None:
        self._cancelled[task_id] = False
        self._cancel_kind[task_id] = None
        self._cancel_events.pop(task_id, None)
        instance_id = await self._next_intervene_instance_id(task_id)
        await self._storage.add_steps(task_id, [{
            "step_id": instance_id, "title": f"介入审查 {instance_id}",
            "type": "monitor", "human_attention": "none", "model_tier": "power",
            "required": 1, "parallel_with": None, "description": "",
        }], after_step_id=None)
        task = await self._storage.get_task(task_id, include_hidden=True)
        if not task:
            return
        if task.get("status") == "paused" and task.get("pause_level") == "gate":
            gate = next((s for s in task["steps"]
                         if s.get("human_attention") == "gate"
                         and s.get("status") == "active"), None)
            if gate:
                await self._save_intervention(task_id, gate["step_id"], reason)
                await self.start_task(task_id)
                return
        actives = [s for s in task["steps"] if s.get("status") == "active"]
        if actives:
            anchor = min(actives, key=lambda s: s.get("sort_order", 0))
        else:
            queued = [s for s in task["steps"]
                      if s.get("status") in ("pending", "stopped")
                      and not self._step_is_review_kind(
                          s.get("step_id", ""), s.get("type", ""))]
            anchor = min(queued, key=lambda s: s.get("sort_order", 0)) if queued else None
        if mode == "force_inject":
            if actives:
                cur = max(actives, key=lambda s: s.get("sort_order", 0))
                await self._sm.advance_step(task_id, cur["step_id"], "pending")
        if anchor:
            conn = await self._storage._get_conn()
            conn.execute(
                "UPDATE task_steps SET sort_order = sort_order + 10 "
                "WHERE task_id = ? AND sort_order >= ?",
                (task_id, anchor["sort_order"]))
            conn.execute(
                "UPDATE task_steps SET sort_order = ? "
                "WHERE task_id = ? AND step_id = ?",
                (anchor["sort_order"] - 5, task_id, instance_id))
            conn.commit()
            await self._record_monitor_anchor(task_id, instance_id, anchor["step_id"])
        await self._save_intervention(task_id, instance_id, reason)
        self._cancelled[task_id] = False
        await self.start_task(task_id)

    async def get_step_prep(self, task_id: str, step_id: str) -> dict:
        key = f"{task_id}:{step_id}"
        if key in self._prep_cache:
            return self._prep_cache[key]
        from .. import rest_api
        task = await self._storage.get_task(task_id, include_hidden=True)
        step_row = None
        if task:
            step_row = next((s for s in task.get("steps", [])
                             if s.get("step_id") == step_id), None)
        if step_row and self._step_is_review_kind(step_id, step_row.get("type", "")):
            prep = await self._monitor_prep(task_id, step_row)
        else:
            prep = await rest_api.prepare_step({"task_id": task_id, "step_id": step_id})
        self._prep_cache[key] = prep
        return prep

    async def _run_loop(self, task_id: str) -> None:
        delays = (5, 30, 120)
        try:
            for attempt in range(len(delays) + 1):
                try:
                    await self._execution_loop(task_id)
                    return
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e).lower() or attempt >= len(delays):
                        logger.exception(f"[DC:orch] execution loop crashed for task {task_id}: {e}")
                        return
                    logger.warning(f"[DC:orch] execution loop DB locked for task {task_id} "
                                   f"(attempt {attempt + 1}/{len(delays) + 1}) — park steps, "
                                   f"retry in {delays[attempt]}s")
                    try:
                        await self._park_active_steps(task_id)
                    except Exception:
                        pass
                    await asyncio.sleep(delays[attempt])
                    task = await self._storage.get_task(task_id)
                    if (not task or task.get("status") not in ("active", "paused")
                            or self._cancelled.get(task_id, False)):
                        return
                except Exception as e:
                    logger.exception(f"[DC:orch] execution loop crashed for task {task_id}: {e}")
                    try:
                        from ..config import get_memory_config, PROJECT_ROOT
                        if get_memory_config().get("enabled"):
                            from ..rest_api import _get_memory_storage
                            ms = _get_memory_storage()
                            if ms is not None:
                                from ..memory import get_retainer
                                bank_id = ms.get_or_create_bank_for_project(PROJECT_ROOT)
                                retainer = get_retainer(ms, self._make_llm_client("light"), get_memory_config())
                                asyncio.create_task(retainer.retain(
                                    bank_id=bank_id,
                                    text=f"Task {task_id} execution loop crashed: {e}",
                                    tags=["error", "loop-crash"],
                                    source_ref={"task_id": task_id},
                                ))
                    except Exception:
                        pass
                    return
        finally:
            self._running[task_id] = False

    async def _park_active_steps(self, task_id: str) -> None:
        task = await self._storage.get_task(task_id, include_hidden=True)
        if not task:
            return
        for s in task.get("steps", []):
            if s.get("status") == "active":
                await self._storage.update_step_status(task_id, s["step_id"], "pending")
                await self._storage.append_event(task_id, {
                    "event_type": "step_status_change",
                    "step_id": s["step_id"],
                    "actor": "system",
                    "content": {
                        "what_happened": f"Step {s['step_id']} status: active → pending (graceful drain)",
                        "from_status": "active",
                        "to_status": "pending",
                    },
                })
                logger.info(f"[DC:orch] task {task_id} step {s['step_id']} active → pending (graceful drain)")

    async def _execution_loop(self, task_id: str) -> None:
        iterations = 0
        while not self._is_cancelled(task_id) and iterations < self.MAX_ITERATIONS:
            iterations += 1

            if graceful.is_draining():
                await self._park_active_steps(task_id)
                logger.info(f"[DC:orch] task {task_id} graceful drain — execution loop exited")
                return

            task = await self._storage.get_task(task_id)
            if not task:
                logger.info(f"[DC:orch] task {task_id} no longer exists, stopping")
                return

            status = task.get("status")
            if status in ("completed", "abandoned"):
                if status == "abandoned":
                    return
                task_h = await self._storage.get_task(task_id, include_hidden=True)
                hsteps = (task_h or {}).get("steps", [])
                review = next((s for s in hsteps if s.get("step_id") == "review"), None)
                report = next((s for s in hsteps if s.get("step_id") == "report"), None)
                tail_done = (review and review.get("status") == "completed"
                             and report and report.get("status") == "completed")
                if tail_done:
                    return
                await self._ensure_tail_steps(task_id)
                logger.info(f"[DC:orch] task {task_id} completed but tail steps "
                            f"pending — backfilling without status rollback")
            if status == "paused":
                task = await self._storage.get_task(task_id, include_hidden=True)
                gate_wait = [s for s in task.get("steps", [])
                             if s.get("human_attention") == "gate"
                             and s.get("status") in ("active", "stopped")]
                if task.get("pause_level") == "gate" and gate_wait:
                    pending_monitors = [s["step_id"] for s in task.get("steps", [])
                                        if s.get("status") == "pending"
                                        and (s.get("type") in ("monitor", "review", "report")
                                             or str(s.get("step_id", "")).startswith("monitor-"))]
                    if await self._has_pending_interventions(
                            task_id, [s["step_id"] for s in gate_wait] + pending_monitors):
                        for s in gate_wait:
                            if s["status"] == "pending":
                                continue
                            logger.info(
                                f"[DC:orch] gate {s['step_id']} has pending intervention "
                                f"— reset to pending for re-decision")
                            await self._sm.advance_step(task_id, s["step_id"], "pending")
                        await self._sm._resume_after_intervention(task_id)
                        continue
                    logger.info(f"[DC:orch] task {task_id} paused with gate waiting "
                                f"({len(gate_wait)} step(s)) — waiting for approval")
                    return
                active_steps = [s for s in task.get("steps", []) if s["status"] == "active"]
                if active_steps:
                    gate_steps = [s for s in active_steps if s.get("human_attention") == "gate"]
                    if gate_steps and await self._has_pending_interventions(
                        task_id, [s["step_id"] for s in gate_steps]
                    ):
                        for s in gate_steps:
                            logger.info(
                                f"[DC:orch] gate {s['step_id']} has pending intervention — "
                                f"reset to pending for re-decision"
                            )
                            await self._sm.advance_step(task_id, s["step_id"], "pending")
                        continue
                    if gate_steps:
                        logger.info(f"[DC:orch] task {task_id} paused with gate active — "
                                    f"waiting for approval/intervention")
                        return
                    for s in active_steps:
                        if s.get("human_attention") == "gate":
                            continue
                        logger.info(f"[DC:orch] paused with active exec step {s['step_id']} "
                                    f"— reset to pending for resume")
                        await self._sm.advance_step(task_id, s["step_id"], "pending")
                    continue
                await self._sm._resume_after_intervention(task_id)

            steps = await self._sm.get_next_steps(task_id)
            if not steps:
                task = await self._storage.get_task(task_id, include_hidden=True)
                stuck_steps = [s for s in task.get("steps", [])
                               if s["status"] == "active"]
                if stuck_steps and task.get("status") == "active":
                    for s in stuck_steps:
                        logger.info(f"[DC:orch] resetting stuck active step {s['step_id']} → pending")
                        await self._sm.advance_step(task_id, s["step_id"], "pending")
                    continue
                if task.get("status") == "completed":
                    return
                if not task.get("steps"):
                    logger.warning(f"[DC:orch] task {task_id} has no steps — ensure monitor-init")
                    await self._ensure_initial_orchestration(task_id)
                    return
                return

            first = steps[0]
            if len(steps) == 1 and not first.get("parallel_with"):
                task_cur = await self._storage.get_task(task_id)
                if task_cur:
                    sid0 = first["step_id"]
                    for s in task_cur.get("steps", []):
                        if (s["status"] == "pending" and s["step_id"] != sid0
                                and sid0 in (s.get("parallel_with") or [])):
                            logger.info(f"[DC:parallel] task={task_id} merge {s['step_id']} "
                                        f"into group of {sid0} (parallel_with)")
                            steps.append(s)
                            break

            if first.get("human_attention") == "gate":
                await self._execute_step(task_id, first["step_id"], is_gate=True)
                if self._is_cancelled(task_id):
                    return
                task_after = await self._storage.get_task(task_id)
                step_after = self._find_step(task_after, first["step_id"]) if task_after else None
                if step_after and step_after.get("status") == "completed":
                    logger.info(f"[DC:orch] gate {first['step_id']} completed (human confirmed) — continuing flow")
                    continue
                task_before_pause = await self._storage.get_task(task_id)
                if task_before_pause and task_before_pause.get("best_effort"):
                    await self._submit_step(task_id, first["step_id"])
                    await self._insert_monitor_step(task_id, first["step_id"])
                    logger.info(f"[DC:orch] best-effort: gate {first['step_id']} "
                                f"auto-submitted (user delegated decision)")
                    continue
                logger.info(f"[DC:orch] gate step {first['step_id']} reviewed, pausing for human approval")
                await self._sm.pause_task(task_id)
                return

            group_ids = [s["step_id"] for s in steps]
            is_parallel = len(steps) > 1 or bool(first.get("parallel_with"))
            if is_parallel:
                logger.info(f"[DC:parallel] task={task_id} group={group_ids[0]} start")
                results = await asyncio.gather(
                    *[self._execute_step(task_id, sid, skip_monitor=True) for sid in group_ids],
                    return_exceptions=True)
                for sid, res in zip(group_ids, results):
                    if isinstance(res, BaseException):
                        logger.info(f"[DC:parallel] task={task_id} group={group_ids[0]} failed step={sid}: {res}")
                        try:
                            await self._storage.append_event(task_id, {
                                "event_type": "parallel_failed", "actor": "system",
                                "content": {"step_id": sid, "error": str(res)[:200]},
                            })
                        except Exception:
                            pass
                logger.info(f"[DC:parallel] task={task_id} group={group_ids[0]} done")
                if self._is_cancelled(task_id):
                    return
                await self._insert_monitor_step(task_id, group_ids[-1])
            else:
                await self._execute_step(task_id, group_ids[0])
                if self._is_cancelled(task_id):
                    return

        if iterations >= self.MAX_ITERATIONS:
            logger.warning(f"[DC:orch] Max iterations reached for task {task_id}, stopping execution")

    async def _step_is_active(self, task_id: str, step_id: str) -> bool:
        try:
            task = await self._storage.get_task(task_id, include_hidden=True)
            if not task:
                return False
            row = next((s for s in task.get("steps", [])
                        if s.get("step_id") == step_id), None)
            return bool(row and row.get("status") == "active")
        except Exception:  # noqa: BLE001
            return False

    async def _safe_mark_stopped(self, task_id: str, step_id: str) -> None:
        if await self._step_is_active(task_id, step_id):
            await self._sm.advance_step(task_id, step_id, "stopped")
        else:
            logger.info(f"[DC:orch] step={step_id} stop skipped — external already changed state")

    async def _execute_step(self, task_id: str, step_id: str, is_gate: bool = False,
                            skip_monitor: bool = False,
                            prep_override: Optional[dict] = None,
                            save_trigger_step: str = "") -> None:
        _st_row = None
        try:
            _t0 = await self._storage.get_task(task_id, include_hidden=True)
            if _t0:
                _st_row = next((s for s in _t0.get("steps", [])
                                if s.get("step_id") == step_id), None)
        except Exception:  # noqa: BLE001
            pass
        _st_type = (_st_row or {}).get("type") or "executor"
        is_review_kind = self._step_is_review_kind(step_id, _st_type)
        is_reverse = _st_type == "reverse"
        is_researcher = _st_type == "researcher"

        self._prep_cache.pop(f"{task_id}:{step_id}", None)

        self._last_saved_round[f"{task_id}:{step_id}"] = 0

        existing_messages = await self._storage.get_step_messages(task_id, step_id)
        has_existing = len(existing_messages) > 0
        if not has_existing:
            await self._storage.clear_step_messages(task_id, step_id)
            await self._clear_compress_points(task_id, step_id)
            await self._save_step_report_round(task_id, step_id, 0)
            await self._save_step_report_read_round(task_id, step_id, None)
        else:
            logger.info(f"[DC:orch] continuing conversation for step={step_id}, existing msgs={len(existing_messages)}")

        prep = prep_override
        if prep is None:
            if is_review_kind:
                prep = await self._monitor_prep(task_id, _st_row or {"step_id": step_id, "type": _st_type})
            else:
                prep = await self.get_step_prep(task_id, step_id)
        system_prompt = prep.get("system_prompt", "") or ""
        step_context = prep.get("step_context", "") or ""
        model_tier = prep.get("model_tier") or ("power" if is_gate else "light")
        step_title = prep.get("step_title") or step_id

        self._publish(task_id, "stepStart", {"stepId": step_id})
        self._step_t0[f"{task_id}:{step_id}"] = time.monotonic()

        if not has_existing:
            await self._append_message(task_id, step_id,
                                       {"role": "system", "content": system_prompt, "round_num": 0})

        if is_review_kind:
            mon_status = await self._sm._get_step_status(task_id, step_id)
            if mon_status == "completed":
                await self._sm.reset_step_for_continuation(
                    task_id, step_id, "审查步骤重跑续接")
        await self._sm.advance_step(task_id, step_id, "active")

        user_msgs = await self._read_interventions(task_id, step_id)

        base_msgs: list[dict] = [{"role": "system", "content": system_prompt},
                                 {"role": "user", "content": step_context}]
        if has_existing:
            compress_points = await self._get_compress_points(task_id, step_id)
            messages = base_msgs + self._sanitize_tool_pairs(
                self._build_lm_messages(existing_messages, compress_points)) + user_msgs
        else:
            messages = base_msgs + user_msgs

        last_tokens = await self._get_last_prompt_tokens(task_id, step_id)
        if has_existing:
            measured = _count_tokens(messages)
            if measured is not None and measured > self.COMPRESS_TOKENS:
                logger.info(f"[DC:orch] resume rebuild measured={measured} > "
                            f"{self.COMPRESS_TOKENS} — pre-compress before send")
                last_tokens = measured
        compressed = await self._check_and_compress(task_id, step_id, messages, last_tokens)
        if compressed is None:
            await self._graceful_finish_step(task_id, step_id, messages)
            if not skip_monitor:
                await self._insert_monitor_step(task_id, step_id)
            return
        messages = compressed

        logger.info(f"[DC:orch] start step={step_id} tier={model_tier} gate={is_gate} existing={has_existing}")

        try:
            result = await self._call_llm(
                task_id, step_id, model_tier, messages,
                (self._get_monitor_tools() if is_review_kind
                 else self._get_reverse_tools() if is_reverse
                 else self._get_researcher_tools() if is_researcher
                 else self._get_gate_tools() if is_gate and _st_type != "plan"
                 else self._get_exec_tools()),
                self._cancel_event(task_id),
                empty_ok=is_review_kind,
                text_finish=is_gate or is_review_kind,
                give_up_check=not is_gate and not is_review_kind,
                is_gate=is_gate)
            logger.info(f"[DC:orch] done step={step_id} text={len(result.get('text', ''))}ch "
                        f"toolCalls={len(result.get('toolCalls', []))}")
            self._publish(task_id, "streamEnd", {"stepId": step_id})

            if self._is_cancelled(task_id):
                await self._sm.advance_step(task_id, step_id, "stopped")
                return

            if result.get("empty"):
                text = result.get("text") or ""
                if text.strip():
                    err_msg = "AI 连续输出文本但未显式调用完成工具（dcflow_step_done），步骤未完成"
                    empty_msg = ("❌ AI 连续输出文本但未显式调用完成工具（dcflow_step_done），"
                                 "步骤未完成，请人工检查后重试")
                else:
                    err_msg = "AI 未输出任何结论，步骤未完成"
                    empty_msg = "❌ AI 未输出步骤结论（空响应），步骤未完成，请人工检查后重试"
                await self._sm.advance_step(task_id, step_id, "stopped")
                await self._append_message(task_id, step_id,
                                           {"role": "system", "content": empty_msg,
                                            "round_num": -1})
                self._publish(task_id, "llmError", {
                    "stepId": step_id, "code": "empty_response",
                    "message": err_msg, "retryable": True, "retryCount": 0})
                logger.warning(f"[DC:orch] empty response for step={step_id} — stopped, not completed")
                self._cancelled[task_id] = True
                return

            if is_gate:
                if await self._gate_confirmed_by_human(task_id, step_id):
                    logger.info(f"[DC:orch] gate {step_id} confirmed by human — submitting and continuing")
                    await self._submit_step(task_id, step_id)
                    self._publish_full_conversation(task_id, step_id, system_prompt, step_title, result)
                    if not skip_monitor:
                        await self._insert_monitor_step(task_id, step_id)
                    return
                self._publish_full_conversation(task_id, step_id, system_prompt, step_title, result)
                return

            if (is_review_kind and user_msgs
                    and not (result.get("text") or "").strip()
                    and not result.get("toolCalls")):
                await self._sm.advance_step(task_id, step_id, "stopped")
                await self._append_message(
                    task_id, step_id,
                    {"role": "system",
                     "content": "⚠️ AI 未产生任何回复，用户消息尚未处理。请人工恢复后重试。",
                     "round_num": -1})
                self._publish(task_id, "llmError", {
                    "stepId": step_id, "code": "empty_response",
                    "message": "AI 未输出任何结论（空响应），步骤未完成",
                    "retryable": True, "retryCount": 0})
                self._cancelled[task_id] = True
                return

            if is_review_kind:
                await self._sm.advance_step(task_id, step_id, "completed")
                self._publish_full_conversation(task_id, step_id, system_prompt, step_title, result)
                from .. import rest_api as _rest_api
                conversation = []
                try:
                    _msgs = await self._storage.get_step_messages(task_id, step_id)
                    conversation = [
                        {"role": m.get("role", ""), "content": m.get("content", "")}
                        for m in (_msgs or [])
                        if not (m.get("role") == "system"
                                and "LLM Error" in str(m.get("content", "")))
                    ]
                except Exception:  # noqa: BLE001
                    pass
                if not conversation:
                    conversation = [{"role": "system", "content": system_prompt}] \
                        + self._result_to_conversation(result)
                try:
                    await _rest_api.monitor_save_conversation({
                        "task_id": task_id,
                        "trigger_step_id": save_trigger_step or step_id,
                        "conversation": conversation}, storage=self._storage)
                except Exception as e:
                    logger.warning(f"[DC:orch] save monitor conversation failed for {step_id}: {e}")
                if step_id == "monitor-init":
                    await self._ensure_tail_steps(task_id)
                elif step_id == "report":
                    await self._sm.complete_task(task_id)
                return

            await self._submit_step(task_id, step_id)

            task = await self._storage.get_task(task_id)
            step = self._find_step(task, step_id) if task else None
            if step and step.get("status") != "completed":
                logger.info(f"[DC:orch] step {step_id} status={step.get('status')} after submit — skipping monitor")
                self._publish_full_conversation(task_id, step_id, system_prompt, step_title, result)
                return

            self._publish_full_conversation(task_id, step_id, system_prompt, step_title, result)
            if not skip_monitor:
                await self._insert_monitor_step(task_id, step_id)

        except _StepGracefulFinish:
            if not skip_monitor:
                await self._insert_monitor_step(task_id, step_id)
            return

        except _StepGracefulDrain:
            if await self._step_is_active(task_id, step_id):
                logger.info(f"[DC:orch] step={step_id} drained — active → pending")
                await self._sm.advance_step(task_id, step_id, "pending")
            else:
                logger.info(f"[DC:orch] step={step_id} drain skipped — external already changed state")
            return

        except (LlmError, LlmAborted) as e:
            if self._is_cancelled(task_id):
                await self._safe_mark_stopped(task_id, step_id)
                return

            await self._safe_mark_stopped(task_id, step_id)
            err_msg = getattr(e, "message", None) or str(e)
            await self._append_message(task_id, step_id,
                                       {"role": "system", "content": f"❌ LLM Error: {err_msg}", "round_num": -1})
            info = self._classify_error(e)
            retry_count = getattr(e, "retry_count", 0)
            self._publish(task_id, "llmError", {
                "stepId": step_id, "code": info["code"], "message": info["message"],
                "retryable": info["retryable"], "retryCount": retry_count,
            })
            logger.warning(f"[DC:orch] llmError step={step_id} code={info['code']} "
                           f"retryable={info['retryable']} retryCount={retry_count}: {err_msg}")
            self._cancelled[task_id] = True

    async def _call_llm(self, task_id: str, step_id: str, tier: str, msgs: list[dict],
                        tools: list[dict], cancel_event: asyncio.Event,
                        empty_ok: bool = False, text_finish: bool = True,
                        give_up_check: bool = False, is_gate: bool = False) -> dict:
        full_content = ""
        recorded_tool_calls: list[dict] = []
        rounds: list[dict] = []
        retry_count = 0
        give_up_reminded = False
        give_up_progress = False
        try:
            _task_row = await self._storage.get_task(task_id)
            best_effort = bool(_task_row and _task_row.get("best_effort"))
        except Exception:  # noqa: BLE001
            best_effort = False
        done_checked = False
        gate_report_nudges = 0
        _round = await self._get_step_report_round(task_id, step_id)
        pending_chunks: list[dict] = []
        empty_retries = 0
        text_nudges = 0
        done_via_tool = False

        if graceful.is_draining():
            raise _StepGracefulDrain(step_id)

        async def _maybe_retry_empty() -> bool:
            nonlocal empty_retries
            if not full_content.strip() and not empty_ok:
                if empty_retries < 3:
                    empty_retries += 1
                    msgs.append({"role": "user", "content": _EMPTY_NUDGE})
                    await self._append_message(task_id, step_id,
                                               {"role": "user", "content": _EMPTY_NUDGE, "round_num": -1})
                    logger.warning(f"[DC:orch] empty response for step={step_id} — "
                                   f"auto retry {empty_retries}/3 with nudge")
                    return True
            return False

        no_change_cnt = 0
        review_kind = self._step_is_review_kind(step_id, "")

        while True:
            _t_check = await self._storage.get_task(task_id)
            if step_id != "report" and (not _t_check
                                        or _t_check.get("status") in ("completed", "abandoned")):
                logger.info(f"[DC:orch] task {task_id} ended (status="
                            f"{_t_check.get('status') if _t_check else 'gone'}) during "
                            f"step {step_id} — graceful finish")
                await self._graceful_finish_step(task_id, step_id, msgs,
                                                 reason="task_ended")
                raise _StepGracefulFinish(step_id)
            _round += 1
            try:
                await self._save_step_report_round(task_id, step_id, _round)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DC:orch] step report round save failed step={step_id}: {e}")
            if _round > 0 and _round % 50 == 0 and not review_kind:
                report_nudge = (f"流程报告：{_FLOW_REPORT_FILENAME}（在你的临时文件夹，所有步骤共享"
                                "同一份，不存在则用 dcflow_write_file 创建）\n"
                                "已经跑了一段时间了，请先停下来，重新读取一次流程报告，然后合并当前尝试过的东西，碰壁的"
                                "东西，成功的东西，有用的结论到流程报告中，不要重复内容，不要有多处工作焦点，然后各个章节深度整合一下，不要只读一部分整合，理顺思路。"
                                "**整合完成后必须继续推进当前步骤的剩余任务（未完成的工作/验证），不要整理完报告就结束步骤**")
                msgs.append({"role": "user", "content": report_nudge})
                await self._append_message(task_id, step_id,
                                           {"role": "user", "content": report_nudge,
                                            "round_num": -1})
                logger.info(f"[DC:orch] report nudge at round {_round} for step={step_id}")
            last_tokens = await self._get_last_prompt_tokens(task_id, step_id)
            compressed = await self._check_and_compress(task_id, step_id, msgs, last_tokens)
            if compressed is None:
                await self._graceful_finish_step(task_id, step_id, msgs)
                raise _StepGracefulFinish(step_id)
            msgs = compressed
            await self._inject_interventions(task_id, step_id, msgs)

            client = self._make_llm_client(tier)
            logger.info(f"[DC:step] step={step_id} tier={tier} model={client._model} msgs={len(msgs)}")

            round_text = ""
            round_reasoning = ""
            round_usage = {"prompt": 0, "cached": 0, "completion": 0}
            round_tool_slots: list[dict] = []
            t_req = time.monotonic()
            first_chunk_t: Optional[float] = None
            last_chunk_t: Optional[float] = None
            _delta_calls: dict = {}
            try:
                async for ev in client.stream_chat(msgs, tools, signal=cancel_event):
                    if ev["type"] == "text":
                        text = ev["text"]
                        full_content += text
                        round_text += text
                        _t_now = time.monotonic()
                        if first_chunk_t is None:
                            first_chunk_t = _t_now
                        last_chunk_t = _t_now
                        self._emit_chunk(task_id, step_id, text)
                        pending_chunks.append({"chunk_type": "text", "content": text})
                    elif ev["type"] == "reasoning":
                        _t_now = time.monotonic()
                        if first_chunk_t is None:
                            first_chunk_t = _t_now
                        last_chunk_t = _t_now
                        round_reasoning += ev["text"]
                        self._publish(task_id, "thinkingChunk", {"stepId": step_id, "chunk": ev["text"]})
                    elif ev["type"] == "tool_call_delta":
                        _t_now = time.monotonic()
                        last_chunk_t = _t_now
                        idx = ev["index"]
                        slot = _delta_calls.get(idx) or {"id": None, "name": None, "buf": []}
                        if ev.get("id"):
                            slot["id"] = ev["id"]
                        if ev.get("name"):
                            slot["name"] = ev["name"]
                        if slot["id"] and slot["name"]:
                            if not slot.get("started"):
                                slot["started"] = True
                                self._publish(task_id, "toolCallStart", {
                                    "stepId": step_id, "callId": slot["id"],
                                    "toolName": slot["name"], "input": ""})
                                for d in slot["buf"]:
                                    self._publish(task_id, "toolCallParam", {
                                        "stepId": step_id, "callId": slot["id"], "delta": d})
                                slot["buf"] = []
                            if ev["delta"]:
                                self._publish(task_id, "toolCallParam", {
                                    "stepId": step_id, "callId": slot["id"], "delta": ev["delta"]})
                        elif ev["delta"]:
                            slot["buf"].append(ev["delta"])
                        _delta_calls[idx] = slot
                    elif ev["type"] == "usage":
                        round_usage["prompt"] += ev["prompt"]
                        round_usage["cached"] += ev["cached"]
                        round_usage["completion"] += ev["completion"]
                        await self._add_step_tokens(task_id, step_id, ev["prompt"],
                                                    ev["cached"], ev["completion"])
                    else:
                        last_chunk_t = time.monotonic()
                        round_tool_slots.append(ev)
                retry_count = 0
            except LlmAborted:
                if self._cancel_kind.get(task_id) == "force_inject":
                    logger.info(f"[DC:ai] force_inject abort at round {_round}, step={step_id}, continuing")
                    cancel_event.clear()
                    self._cancelled[task_id] = False
                    self._cancel_kind[task_id] = None
                    continue
                raise
            except LlmError as e:
                logger.warning(f"[DC:orch] LLM error step={step_id} round={_round} "
                               f"status={getattr(e, 'status', '?')} msg={getattr(e, 'message', e)!r}")
                if _is_context_exceeded(e):
                    collected: list[str] = []
                    compressed = self._summarize_to_token_budget(
                        msgs, self.CONTEXT_BUDGET_TOKENS, self.KEEP_RECENT_TOOL_ROUNDS,
                        out_summaries=collected)
                    if compressed is not msgs:
                        await self._record_compress_points(task_id, step_id, collected)
                        msgs[:] = self._sanitize_tool_pairs(compressed)
                        logger.info(f"[DC:orch] context exceeded step={step_id} — "
                                    f"compressed to {len(msgs)} msgs (≤{self.CONTEXT_BUDGET_TOKENS}), "
                                    f"retrying")
                        continue
                    collected = []
                    compressed = self._summarize_old_tool_calls(
                        msgs, keep=self.KEEP_RECENT_TOOL_ROUNDS, out_summaries=collected)
                    if compressed is not msgs:
                        await self._record_compress_points(task_id, step_id, collected)
                        msgs[:] = self._sanitize_tool_pairs(compressed)
                        logger.info(f"[DC:orch] context exceeded step={step_id} — tiktoken "
                                    f"underestimates, aggressive compress to "
                                    f"{self.KEEP_RECENT_TOOL_ROUNDS} rounds, retrying")
                        continue
                    await self._graceful_finish_step(task_id, step_id, msgs)
                    raise _StepGracefulFinish(step_id)
                info = self._classify_error(e)
                if info["retryable"] and retry_count < self.MAX_RETRY:
                    if self._is_cancelled(task_id):
                        logger.info(f"[DC:ai] cancelled during retry for step={step_id} "
                                    f"— aborting instead of retrying")
                        raise e
                    retry_count += 1
                    delay = min(1000 * (2 ** (retry_count - 1)), 30000)
                    marker = f"__DC_RETRY__{retry_count}/10__DC_RETRY__"
                    self._emit_chunk(task_id, step_id, marker)
                    pending_chunks.append({"chunk_type": "text", "content": marker})
                    await self._save_chunks(task_id, step_id, pending_chunks)
                    pending_chunks.clear()
                    logger.info(f"[DC:ai] rate limited, retry {retry_count}/10 in {delay}ms")
                    for _ in range(int(delay / 1000)):
                        if self._is_cancelled(task_id):
                            logger.info(f"[DC:ai] cancelled during retry wait for "
                                        f"step={step_id} — aborting")
                            raise e
                        await asyncio.sleep(1.0)
                    continue
                e.retry_count = retry_count  # type: ignore[attr-defined]
                raise

            if not round_usage["prompt"] and not round_usage["completion"]:
                est_prompt = _count_tokens(msgs)
                if est_prompt is not None:
                    comp_text = round_reasoning + round_text
                    if round_tool_slots:
                        comp_text += "\n" + json.dumps(
                            [tc.get("arguments", "") for tc in round_tool_slots],
                            ensure_ascii=False)
                    est_comp = _count_tokens([{"role": "assistant", "content": comp_text}]) or 0
                    await self._add_step_tokens(task_id, step_id, est_prompt, 0, est_comp,
                                                record_last=False, context_tokens=est_prompt)
                    logger.info(f"[DC:orch] estimated tokens for step={step_id}: "
                                f"prompt={est_prompt} completion={est_comp} (no API usage)")

            if not round_tool_slots and round_text:
                parsed = _parse_text_tool_calls(round_text)
                if parsed:
                    logger.info(f"[DC:tool] text-parsed {len(parsed)}: "
                                f"{', '.join(p['name'] for p in parsed)}")
                    for p in parsed:
                        round_tool_slots.append({
                            "id": p["call_id"], "name": p["name"],
                            "arguments": json.dumps(p["input"], ensure_ascii=False)})
                    round_text = _TEXT_TOOL_CALL_RE.sub("", round_text).strip()

            _t_end = time.monotonic()
            _ttft = int((first_chunk_t - t_req) * 1000) if first_chunk_t is not None else 0
            _out = int((last_chunk_t - first_chunk_t) * 1000) if first_chunk_t is not None else 0
            _run = int((_t_end - self._step_t0.get(f"{task_id}:{step_id}", _t_end)) * 1000)
            try:
                await self._update_step_stats(
                    task_id, step_id, requests=1,
                    ttft_ms=_ttft if _ttft > 0 else None,
                    output_ms=_out, run_ms=_run)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DC:orch] step stats save failed step={step_id}: {e}")

            if round_tool_slots:
                if self._is_cancelled(task_id):
                    logger.info(f"[DC:orch] cancelled during tool execution, step={step_id}")
                    return {"text": full_content, "toolCalls": recorded_tool_calls, "rounds": rounds}

                if any(_is_empty_arguments(tc.get("arguments"))
                       for tc in round_tool_slots):
                    names = ", ".join(tc["name"] for tc in round_tool_slots)
                    feedback = (f"【系统提示】你调用的 {names} 参数为空/缺少必填参数，"
                                f"工具未执行。请补齐必填参数（参考工具说明）后重新调用。")
                    msgs.append({"role": "user", "content": feedback})
                    await self._append_message(task_id, step_id,
                                               {"role": "user", "content": feedback,
                                                "round_num": -1})
                    self._publish(task_id, "userMessage",
                                  {"stepId": step_id, "message": feedback})
                    self._emit_chunk(task_id, step_id,
                                     "__DC_USER_MSG__" + json.dumps(
                                         {"stepId": step_id, "content": feedback},
                                         ensure_ascii=False) + "__DC_USER_MSG__")
                    logger.warning(f"[DC:orch] empty-arguments tool call step={step_id} "
                                   f"round={_round} — skipped, feedback injected: {names}")
                    continue

                if graceful.is_draining():
                    logger.info(f"[DC:orch] graceful drain during tool execution, step={step_id}")
                    raise _StepGracefulDrain(step_id)

                give_up_progress = True

                logger.info(f"[DC:tool] executing {len(round_tool_slots)} tool(s): "
                            f"{', '.join(tc['name'] for tc in round_tool_slots)}")
                self._publish(task_id, "toolExecuting", {
                    "stepId": step_id,
                    "callIds": [tc["id"] for tc in round_tool_slots],
                    "toolNames": [tc["name"] for tc in round_tool_slots]})
                if round_text.strip():
                    rounds.append({"text": round_text, "toolCalls": []})
                    full_content = ""

                assist_msg: dict = {"role": "assistant", "content": round_text,
                                    "reasoning_content": round_reasoning}
                assist_msg["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in round_tool_slots
                ]
                msgs.append(assist_msg)

                round_tcs: list[dict] = []
                for tc in round_tool_slots:
                    self._publish(task_id, "toolCallStart", {
                        "stepId": step_id, "callId": tc["id"], "toolName": tc["name"],
                        "input": tc["arguments"]})
                    pending_chunks.append({
                        "chunk_type": "tool_call_start", "content": tc["name"], "call_id": tc["id"]})
                    output_text = await self._invoke_tool(task_id, step_id, tc["name"], tc["arguments"], tc["id"])
                    if review_kind and tc["name"] == "dcflow_adjust_flow":
                        if "action=no_change" in output_text:
                            no_change_cnt += 1
                        else:
                            no_change_cnt = 0
                    try:
                        input_obj = json.loads(tc["arguments"] or "{}")
                    except (ValueError, TypeError):
                        input_obj = {}
                    recorded_tool_calls.append({"name": tc["name"], "input": input_obj, "output": output_text})
                    round_tcs.append({"name": tc["name"], "input": input_obj, "output": output_text,
                                      "call_id": tc["id"]})
                    logger.info(f"[DC:tool] {tc['name']} OK: {output_text[:80]!r}")
                    self._publish(task_id, "toolCallResult", {
                        "stepId": step_id, "callId": tc["id"], "toolName": tc["name"],
                        "output": output_text[:5000]})
                    pending_chunks.append({
                        "chunk_type": "tool_call_result", "content": output_text, "call_id": tc["id"]})
                    msgs.append({"role": "tool", "content": output_text, "tool_call_id": tc["id"]})

                rounds.append({"text": "", "toolCalls": round_tcs})

                await self._append_round_messages(task_id, step_id, rounds)
                await self._save_chunks(task_id, step_id, pending_chunks)
                pending_chunks.clear()

                await self._capture_key_findings(task_id, step_id,
                                                 round_text + "\n" + round_reasoning)

                if no_change_cnt >= 2:
                    logger.info(f"[DC:orch] review step {step_id} converged "
                                f"(2× no_change) — ending rounds")
                    break

                if any(tc.get("name") == "dcflow_step_done" for tc in round_tool_slots):
                    if is_gate and not _gate_report_present(msgs, round_text):
                        if gate_report_nudges < 2:
                            gate_report_nudges += 1
                            nudge = (
                                "【系统提示】你请求完成 gate 审批步骤，但尚未输出「方案审批报告」。"
                                "请先按输出格式整理完整报告（报告主体：方案概述/逐条根因/测试方案/"
                                "决策点/审批建议，含尾部 JSON options 数据块），供人类审批；"
                                "或若人类已在对话中拍板，则输出「✅ 已确认人类决策：<决策内容>」后"
                                "再调用 dcflow_step_done。")
                            msgs.append({"role": "user", "content": nudge})
                            await self._append_message(task_id, step_id,
                                                       {"role": "user", "content": nudge,
                                                        "round_num": -1})
                            self._publish(task_id, "userMessage",
                                          {"stepId": step_id, "message": nudge})
                            self._emit_chunk(task_id, step_id,
                                             "__DC_USER_MSG__" + json.dumps(
                                                 {"stepId": step_id, "content": nudge},
                                                 ensure_ascii=False) + "__DC_USER_MSG__")
                            logger.info(f"[DC:orch] gate report nudge for step={step_id} "
                                        f"at round {_round}")
                            continue
                        logger.warning(f"[DC:orch] gate step {step_id} done without report "
                                       f"after {gate_report_nudges} nudges — keeping gate wait")
                    if give_up_check and best_effort and not done_checked:
                        done_checked = True
                        done_msg = (
                            "【系统提示-尽力模式】你请求完成当前步骤，但请先核对：步骤目标"
                            "（任务描述/步骤定义中的目标与路径）是否已全部达成？若存在尚未尝试"
                            "的路径或可继续的方向（如步骤定义中列出的其他路径、可复用的中间值"
                            "捕获手段、尚未验证的假设），请继续推进而不是收尾；若确实所有路径"
                            "都已尝试且确认不可行，请再次调用 dcflow_step_done 完成步骤。")
                        msgs.append({"role": "user", "content": done_msg})
                        await self._append_message(task_id, step_id,
                                                   {"role": "user", "content": done_msg,
                                                    "round_num": -1})
                        self._publish(task_id, "userMessage",
                                      {"stepId": step_id, "message": done_msg})
                        self._emit_chunk(task_id, step_id,
                                         "__DC_USER_MSG__" + json.dumps(
                                             {"stepId": step_id, "content": done_msg},
                                             ensure_ascii=False) + "__DC_USER_MSG__")
                        logger.info(f"[DC:orch] best-effort done-check for step={step_id} "
                                    f"at round {_round}")
                        continue
                    done_via_tool = True
                    logger.info(f"[DC:orch] dcflow_step_done called for step={step_id} — "
                                f"completing step")
                    break

                compressed = await self._check_and_compress(task_id, step_id, msgs, round_usage["prompt"])
                if compressed is None:
                    await self._graceful_finish_step(task_id, step_id, msgs)
                    raise _StepGracefulFinish(step_id)
                if compressed is not msgs:
                    msgs[:] = compressed
                    logger.info(f"[DC:ai] compressed to {len(msgs)} messages after round {_round}")
                continue

            if round_text.strip() or round_reasoning.strip():
                await self._capture_key_findings(task_id, step_id,
                                                 round_text + "\n" + round_reasoning)
            if give_up_check and round_text.strip() and GIVE_UP_PATTERN.search(round_text) and (
                    not give_up_reminded or give_up_progress or best_effort):
                give_up_reminded = True
                give_up_progress = False
                give_up_msg = (f"任务还没完成，先别急着收尾。请重新读一遍当前步骤报告"
                               f"（{_FLOW_REPORT_FILENAME}，在你的临时文件夹）和最近的关键发现，"
                               f"认真研究里面的线索和未验证的假设，找出新的突破口再继续。"
                               f"你之前几次说\"穷尽\"之后换个角度都还有新发现，我不在线，"
                               f"你继续独立完成我指定的目标，直到所有目标都完成。")
                msgs.append({"role": "user", "content": give_up_msg})
                await self._append_message(task_id, step_id,
                                           {"role": "user", "content": give_up_msg,
                                            "round_num": -1})
                self._publish(task_id, "userMessage", {"stepId": step_id, "message": give_up_msg})
                self._emit_chunk(task_id, step_id,
                                 "__DC_USER_MSG__" + json.dumps(
                                     {"stepId": step_id, "content": give_up_msg},
                                     ensure_ascii=False) + "__DC_USER_MSG__")
                logger.info(f"[DC:orch] give-up nudge for step={step_id} at round {_round}")
            elif round_text.strip():
                give_up_progress = True
            if await _maybe_retry_empty():
                continue
            if not full_content.strip():
                break
            if text_finish:
                break
            if text_nudges < self.MAX_TEXT_FINISH_NUDGE:
                text_nudges += 1
                msgs.append({"role": "user", "content": _TEXT_FINISH_NUDGE})
                await self._append_message(task_id, step_id,
                                           {"role": "user", "content": _TEXT_FINISH_NUDGE,
                                            "round_num": -1})
                logger.info(f"[DC:orch] text without step_done for step={step_id} — "
                            f"confirm nudge {text_nudges}/{self.MAX_TEXT_FINISH_NUDGE}")
            continue

        if full_content.strip():
            rounds.append({"text": full_content, "toolCalls": []})
            await self._append_message(task_id, step_id,
                                       {"role": "assistant", "content": full_content,
                                        "round_num": len(rounds) - 1})
        await self._save_chunks(task_id, step_id, pending_chunks)
        pending_chunks.clear()

        await self._capture_key_findings(task_id, step_id,
                                         full_content + "\n" + round_reasoning)

        if not text_finish and not done_via_tool:
            empty = not bool(full_content.strip())
        else:
            empty = not bool(full_content.strip()) and not empty_ok and not done_via_tool
        return {"text": full_content, "toolCalls": recorded_tool_calls,
                "rounds": rounds, "empty": empty}

    async def _save_intervention_message(self, task_id: str, step_id: str, message: str) -> None:
        try:
            raw = await self._storage.get_artifact(task_id, step_id, "intervention")
            iv_list: list = []
            if raw and raw.get("content"):
                try:
                    data = json.loads(raw["content"])
                    iv_list = data if isinstance(data, list) else [data]
                except (ValueError, TypeError):
                    iv_list = []
            iv_list.append({"type": "force_inject", "message": message})
            await self._storage.save_artifact(task_id, step_id, "intervention",
                                              json.dumps(iv_list, ensure_ascii=False), "json")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] save intervention failed step={step_id}: {e}")

    async def _has_pending_interventions(self, task_id: str, step_ids: list[str]) -> bool:
        for sid in step_ids:
            try:
                raw = await self._storage.get_artifact(task_id, sid, "intervention")
            except Exception:
                continue
            if not raw or not raw.get("content"):
                continue
            try:
                ivs = json.loads(raw["content"])
            except (ValueError, TypeError):
                continue
            iv_list = ivs if isinstance(ivs, list) else [ivs]
            for iv in iv_list:
                if (
                    isinstance(iv, dict)
                    and iv.get("type") in ("pre_tool_injection", "force_inject")
                    and iv.get("message")
                ):
                    return True
        return False

    async def _gate_confirmed_by_human(self, task_id: str, step_id: str) -> bool:
        try:
            msgs = await self._storage.get_step_messages(task_id, step_id, limit=20)
        except Exception:
            return False
        for m in msgs:
            if m.get("role") == "assistant" and "已确认人类决策" in str(m.get("content") or ""):
                return True
        return False

    async def _inject_interventions(self, task_id: str, step_id: str, msgs: list[dict]) -> None:
        try:
            raw = await self._storage.get_artifact(task_id, step_id, "intervention")
            if not raw or not raw.get("content"):
                return
            try:
                interventions = json.loads(raw["content"])
            except (ValueError, TypeError):
                return
            iv_list = interventions if isinstance(interventions, list) else [interventions]
            if not iv_list:
                return

            remaining: list[dict] = []
            for iv in iv_list:
                itype = iv.get("type", "") if isinstance(iv, dict) else ""
                msg = iv.get("message", "") if isinstance(iv, dict) else ""
                if itype in ("pre_tool_injection", "force_inject") and msg:
                    msgs.append({"role": "user", "content": "[用户介入]: " + msg})
                    await self._append_message(task_id, step_id,
                                               {"role": "user", "content": msg, "round_num": -1})
                    self._publish(task_id, "userMessage", {"stepId": step_id, "message": msg})
                    self._emit_chunk(task_id, step_id,
                                     "__DC_USER_MSG__" + json.dumps(
                                         {"stepId": step_id, "content": msg}, ensure_ascii=False)
                                     + "__DC_USER_MSG__")
                    logger.info(f"[DC:orch] injected intervention [{itype}]: {msg[:50]}")
                else:
                    remaining.append(iv)

            await self._storage.save_artifact(task_id, step_id, "intervention",
                                              json.dumps(remaining, ensure_ascii=False), "json")
        except Exception:
            pass

    async def _read_interventions(self, task_id: str, step_id: str) -> list[dict]:
        user_msgs: list[dict] = []
        try:
            raw = await self._storage.get_artifact(task_id, step_id, "intervention")
            if raw and raw.get("content"):
                data = json.loads(raw["content"])
                iv_list = data if isinstance(data, list) else [data]
                for iv in iv_list:
                    msg = iv.get("message") if isinstance(iv, dict) else None
                    if msg:
                        user_msgs.append({"role": "user", "content": msg})
                        await self._append_message(task_id, step_id,
                                                   {"role": "user", "content": msg, "round_num": -1})
                        self._publish(task_id, "userMessage", {"stepId": step_id, "message": msg})
                        self._emit_chunk(task_id, step_id,
                                         "__DC_USER_MSG__" + json.dumps(
                                             {"stepId": step_id, "content": msg}, ensure_ascii=False)
                                         + "__DC_USER_MSG__")
                if user_msgs:
                    await self._storage.save_artifact(task_id, step_id, "intervention", "[]", "json")
        except Exception:
            pass
        return user_msgs

    async def _consume_flow_intervention(self, task_id: str) -> Optional[str]:
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", "intervention")
            if not raw or not raw.get("content"):
                return None
            data = json.loads(raw["content"])
            items = data if isinstance(data, list) else [data]
            msgs = []
            remaining = []
            for it in items:
                if isinstance(it, dict) and it.get("type") == "flow_pending":
                    msgs.append(it.get("reason") or it.get("message") or "")
                else:
                    remaining.append(it)
            await self._storage.save_artifact(task_id, "_flow", "intervention",
                                              json.dumps(remaining, ensure_ascii=False), "json")
            return "\n".join(m for m in msgs if m) or None
        except Exception:
            return None

    def _build_lm_messages(self, db_messages: list[dict],
                           compress_summaries: Optional[list[str]] = None) -> list[dict]:
        out: list[dict] = []
        skip_n = len(compress_summaries) if compress_summaries else 0
        tool_round_seen = 0
        skip_tool_tail = False
        win_end = len(db_messages)
        _hist0 = next((i for i, m in enumerate(db_messages)
                       if m.get("role") in ("assistant", "tool")), None)
        if _hist0 is not None:
            win_end = _hist0 + 10
        for idx, m in enumerate(db_messages):
            role = m.get("role", "")
            if role not in ("user", "assistant", "tool"):
                continue
            content = str(m.get("content", "") or "")
            if role == "assistant":
                tc_raw = m.get("tool_calls")
                has_tc = bool(tc_raw and str(tc_raw).strip())
                parsed_calls: list[dict] = [] if has_tc else _parse_text_tool_calls(content)
                if (has_tc or parsed_calls) and idx < win_end \
                        and self._is_plan_read_round(db_messages, idx):
                    skip_tool_tail = False
                elif (has_tc or parsed_calls) and tool_round_seen < skip_n:
                    tool_round_seen += 1
                    skip_tool_tail = True
                    if compress_summaries and tool_round_seen <= len(compress_summaries):
                        out.append({"role": "user",
                                    "content": compress_summaries[tool_round_seen - 1]})
                    continue
                else:
                    skip_tool_tail = False
                calls: list[dict] = []
                if has_tc:
                    try:
                        parsed = json.loads(tc_raw) if isinstance(tc_raw, str) else tc_raw
                        if isinstance(parsed, list):
                            calls = [c for c in parsed if isinstance(c, dict)]
                    except (ValueError, TypeError):
                        calls = []
                entry: dict = {"role": "assistant", "content": content}
                entry["reasoning_content"] = ""
                calls = [c for c in calls
                         if not _is_empty_arguments(
                             (c.get("function") or {}).get("arguments", ""))]
                if calls:
                    entry["tool_calls"] = calls
                elif parsed_calls:
                    entry["tool_calls"] = [
                        {"id": pc["call_id"], "type": "function",
                         "function": {"name": pc["name"],
                                      "arguments": json.dumps(pc["input"], ensure_ascii=False)}}
                        for pc in parsed_calls
                    ]
                out.append(entry)
            elif role == "tool":
                if skip_tool_tail:
                    continue
                tcid = str(m.get("tool_call_id") or "")
                if not tcid:
                    if out and out[-1]["role"] == "assistant" and out[-1].get("tool_calls"):
                        tcid = out[-1]["tool_calls"][0]["id"]
                    else:
                        parsed_calls = _parse_text_tool_calls(content)
                        if parsed_calls:
                            tcid = parsed_calls[0]["call_id"]
                if not tcid:
                    continue
                out.append({"role": "tool", "content": content, "tool_call_id": tcid})
            else:
                out.append({"role": role, "content": content})
        return out

    def _classify_error(self, err: Any) -> dict:
        status = getattr(err, "status", 0) or 0
        msg = str(getattr(err, "message", None) or err).lower()
        if status == 429 or re.search(r"429|rate.?limit|quota|too many", msg):
            return {"code": "rate_limit", "message": getattr(err, "message", None) or str(err),
                    "retryable": True}
        if status in (401, 403) or re.search(r"403|forbidden|unauthorized|401", msg):
            return {"code": "forbidden", "message": getattr(err, "message", None) or str(err),
                    "retryable": False}
        if re.search(r"请求失败|响应中断|连接|readerror|httpx|超时|reset|broken pipe|network", msg):
            return {"code": "network", "message": getattr(err, "message", None) or str(err),
                    "retryable": True}
        return {"code": "unknown", "message": getattr(err, "message", None) or str(err),
                "retryable": False}

    def _summarize_old_tool_calls(self, msgs: list[dict], keep: int = 10,
                                  budget_chars: Optional[int] = None,
                                  out_summaries: Optional[list[str]] = None) -> list[dict]:
        if budget_chars:
            total = 0
            for m in msgs:
                total += len(m.get("content", "") or "")
                tc = m.get("tool_calls")
                if tc:
                    total += len(json.dumps(tc, ensure_ascii=False))
            if total <= budget_chars:
                return msgs
            out = list(msgs)
            protected = self._protected_plan_read_hashes(msgs)
            while total > budget_chars:
                start = next((i for i, m in enumerate(out)
                              if m.get("role") == "assistant" and m.get("tool_calls")
                              and self._msg_hash(m) not in protected), None)
                if start is None:
                    break
                summaries = []
                for tc in out[start].get("tool_calls") or []:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args_obj = json.loads(fn.get("arguments", "{}") or "{}")
                    except (ValueError, TypeError):
                        args_obj = {}
                    summaries.append(self._summarize_tool_call(name, args_obj))
                removed = len(out[start].get("content", "") or "") + len(
                    json.dumps(out[start].get("tool_calls") or [], ensure_ascii=False))
                end = start + 1
                while end < len(out) and out[end].get("role") == "tool":
                    removed += len(out[end].get("content", "") or "")
                    end += 1
                items = "；".join(f"{j + 1}. {s}" for j, s in enumerate(summaries))
                summary_msg = {"role": "user",
                               "content": f"[早期工具调用已摘要] {items}"}
                if out_summaries is not None:
                    out_summaries.append(summary_msg["content"])
                out[start:end] = [summary_msg]
                total = total - removed + len(summary_msg["content"])
            return out
        tool_starts = [i for i, m in enumerate(msgs)
                       if m.get("role") == "assistant" and m.get("tool_calls")]
        protected = {i for i in tool_starts if self._is_plan_read_round(msgs, i)}
        tool_starts_f = [i for i in tool_starts if i not in protected]
        if len(tool_starts_f) <= keep:
            return msgs
        keep_start = tool_starts_f[-keep]
        prefix: list[dict] = []
        protect_tail = False
        for i, m in enumerate(msgs[:keep_start]):
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                if i in protected:
                    prefix.append(m)
                    protect_tail = True
                    continue
                protect_tail = False
                summaries = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args_obj = json.loads(fn.get("arguments", "{}") or "{}")
                    except (ValueError, TypeError):
                        args_obj = {}
                    summaries.append(self._summarize_tool_call(name, args_obj))
                items = "；".join(f"{i + 1}. {s}" for i, s in enumerate(summaries))
                summary_msg = {"role": "user",
                               "content": f"[早期工具调用已摘要] {items}"}
                if out_summaries is not None:
                    out_summaries.append(summary_msg["content"])
                prefix.append(summary_msg)
                continue
            if role == "tool":
                if protect_tail:
                    prefix.append(m)
                continue
            prefix.append(m)
        return prefix + msgs[keep_start:]

    @staticmethod
    def _msg_hash(m: dict) -> str:
        import hashlib
        payload = {"role": m.get("role"), "content": m.get("content"),
                   "tool_calls": m.get("tool_calls")}
        return hashlib.md5(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                      default=str).encode("utf-8")).hexdigest()

    @staticmethod
    def _is_plan_read_round(msgs: list[dict], idx: int, window: int = 10) -> bool:
        m = msgs[idx] if 0 <= idx < len(msgs) else None
        if not m or m.get("role") != "assistant":
            return False
        start = next((i for i, x in enumerate(msgs)
                      if x.get("role") in ("assistant", "tool")), None)
        if start is None or not (start <= idx < start + window):
            return False
        calls = m.get("tool_calls") or []
        if isinstance(calls, str):
            try:
                calls = json.loads(calls)
            except (ValueError, TypeError):
                calls = []
        for tc in calls:
            fn = (tc or {}).get("function", {}) or {}
            if fn.get("name") == "dcflow_read_file":
                try:
                    fp = (json.loads(fn.get("arguments") or "{}") or {}).get("file_path", "")
                except (ValueError, TypeError):
                    fp = ""
                if str(fp).lower().endswith("plan.md"):
                    return True
        try:
            parsed = _parse_text_tool_calls(str(m.get("content") or ""))
        except Exception:
            parsed = []
        for pc in parsed:
            if pc.get("name") == "dcflow_read_file" and str(
                    (pc.get("input") or {}).get("file_path", "")).lower().endswith("plan.md"):
                return True
        return False

    def _protected_plan_read_hashes(self, msgs: list[dict],
                                    window: int = 10) -> set:
        start = next((i for i, m in enumerate(msgs)
                      if m.get("role") in ("assistant", "tool")), None)
        if start is None:
            return set()
        return {self._msg_hash(msgs[i])
                for i in range(start, min(start + window, len(msgs)))
                if self._is_plan_read_round(msgs, i, window)}

    def _summarize_to_token_budget(self, msgs: list[dict], budget_tokens: int,
                                   keep: int, out_summaries: Optional[list[str]] = None) -> list[dict]:
        total = _count_tokens(msgs)
        if total is None:
            return self._summarize_old_tool_calls(msgs, keep=keep, out_summaries=out_summaries)
        if total <= budget_tokens:
            return msgs
        out = list(msgs)
        protected = self._protected_plan_read_hashes(msgs)
        while total > budget_tokens:
            tool_starts = [i for i, m in enumerate(out)
                           if m.get("role") == "assistant" and m.get("tool_calls")
                           and self._msg_hash(m) not in protected]
            if len(tool_starts) <= keep:
                break
            start = tool_starts[0]
            removed = _count_tokens([out[start]])
            end = start + 1
            while end < len(out) and out[end].get("role") == "tool":
                removed = (removed or 0) + (_count_tokens([out[end]]) or 0)
                end += 1
            summaries = []
            for tc in out[start].get("tool_calls") or []:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args_obj = json.loads(fn.get("arguments", "{}") or "{}")
                except (ValueError, TypeError):
                    args_obj = {}
                summaries.append(self._summarize_tool_call(name, args_obj))
            items = "；".join(f"{j + 1}. {s}" for j, s in enumerate(summaries))
            summary_msg = {"role": "user", "content": f"[早期工具调用已摘要] {items}"}
            if out_summaries is not None:
                out_summaries.append(summary_msg["content"])
            out[start:end] = [summary_msg]
            added = _count_tokens([summary_msg])
            total = total - (removed or 0) + (added or 0)
        return out

    @staticmethod
    def _summarize_tool_call(name: str, args: dict) -> str:
        if name == "dcflow_read_file":
            fp = args.get("file_path") or ""
            start = args.get("start_line")
            end = args.get("end_line")
            if start and end:
                s = f"读取了 {fp}（第 {start}-{end} 行）"
            else:
                s = f"读取了 {fp} 开头部分"
        else:
            param = json.dumps(args, ensure_ascii=False)
            s = f"调用 {name}({param})"
        return s if len(s) <= 80 else s[:77] + "..."

    async def _check_and_compress(self, task_id: str, step_id: str, msgs: list[dict],
                                  last_tokens: Optional[int]) -> Optional[list[dict]]:
        if last_tokens is not None and last_tokens <= self.COMPRESS_TOKENS:
            logger.info(f"[DC:orch] compress check step={step_id} last_tokens={last_tokens} "
                        f"-> skip ({self.COMPRESS_TOKENS})")
            return msgs
        collected: list[str] = []
        if last_tokens is None:
            total_now = _count_tokens(msgs)
            if total_now is None:
                logger.info(f"[DC:orch] compress check step={step_id} last_tokens=None "
                            f"-> skip (tokenizer unavailable, 400 fallback)")
                return msgs
            if total_now <= self.COMPRESS_TOKENS:
                logger.info(f"[DC:orch] compress check step={step_id} last_tokens=None "
                            f"-> skip (measured {total_now} ≤ {self.COMPRESS_TOKENS})")
                return msgs
            logger.info(f"[DC:orch] compress check step={step_id} last_tokens=None "
                        f"-> measured {total_now} > {self.COMPRESS_TOKENS}, "
                        f"compress to ≤{self.CONTEXT_BUDGET_TOKENS}")
            msgs2 = self._summarize_to_token_budget(
                msgs, self.CONTEXT_BUDGET_TOKENS, self.KEEP_RECENT_TOOL_ROUNDS,
                out_summaries=collected)
            total = _count_tokens(msgs2)
            if total is not None and total > self.COMPRESS_TOKENS:
                logger.info(f"[DC:orch] compress check step={step_id} last_tokens=None "
                            f"-> exhausted at {total} tokens (> {self.COMPRESS_TOKENS}), "
                            f"graceful finish")
                return None
            if msgs2 is not msgs:
                await self._record_compress_points(task_id, step_id, collected)
            return msgs2 if msgs2 is msgs else self._sanitize_tool_pairs(msgs2)
        msgs2 = self._summarize_to_token_budget(
            msgs, self.CONTEXT_BUDGET_TOKENS, self.KEEP_RECENT_TOOL_ROUNDS,
            out_summaries=collected)
        total = _count_tokens(msgs2)
        if total is not None and total > self.COMPRESS_TOKENS:
            if msgs2 is not msgs:
                await self._record_compress_points(task_id, step_id, collected)
            logger.info(f"[DC:orch] compress check step={step_id} last_tokens={last_tokens} "
                        f"-> exhausted at {total} tokens (> {self.COMPRESS_TOKENS}), "
                        f"graceful finish")
            return None
        if msgs2 is msgs:
            logger.info(f"[DC:orch] compress check step={step_id} last_tokens={last_tokens} "
                        f"-> already within budget ({total})")
            return msgs
        await self._record_compress_points(task_id, step_id, collected)
        logger.info(f"[DC:orch] compress step={step_id} last_tokens={last_tokens} "
                    f"tokens {total} msgs {len(msgs)} -> {len(msgs2)}")
        return self._sanitize_tool_pairs(msgs2)

    async def _get_compress_points(self, task_id: str, step_id: str) -> list[str]:
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", _COMPRESS_MAP_ARTIFACT)
            if raw and raw.get("content"):
                mapping = json.loads(raw["content"])
                return [str(s) for s in (mapping.get(step_id, {}).get("summaries") or [])]
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning(f"[DC:orch] compress map read failed step={step_id}: {e}")
        return []

    async def _record_compress_points(self, task_id: str, step_id: str,
                                      new_points: list[str]) -> None:
        if not new_points:
            return
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", _COMPRESS_MAP_ARTIFACT)
            mapping: dict = {}
            if raw and raw.get("content"):
                try:
                    parsed = json.loads(raw["content"])
                    if isinstance(parsed, dict):
                        mapping = parsed
                except (ValueError, TypeError):
                    mapping = {}
            entry = mapping.get(step_id)
            if not isinstance(entry, dict):
                entry = {}
            existing = entry.get("summaries")
            if not isinstance(existing, list):
                existing = []
            mapping[step_id] = {"summaries": existing + list(new_points)}
            await self._storage.save_artifact(
                task_id, "_flow", _COMPRESS_MAP_ARTIFACT,
                json.dumps(mapping, ensure_ascii=False), "json")
        except Exception as e:
            logger.warning(f"[DC:orch] compress map write failed step={step_id}: {e}")

    async def _clear_compress_points(self, task_id: str, step_id: str) -> None:
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", _COMPRESS_MAP_ARTIFACT)
            if not (raw and raw.get("content")):
                return
            mapping = json.loads(raw["content"])
            if step_id in mapping:
                del mapping[step_id]
                await self._storage.save_artifact(
                    task_id, "_flow", _COMPRESS_MAP_ARTIFACT,
                    json.dumps(mapping, ensure_ascii=False), "json")
        except Exception as e:
            logger.warning(f"[DC:orch] compress map clear failed step={step_id}: {e}")

    async def _graceful_finish_step(self, task_id: str, step_id: str,
                                    msgs: list[dict],
                                    reason: str = "context_limit") -> None:
        finish_label = ("[任务已完成，步骤收尾]" if reason == "task_ended"
                        else "[上下文超限，步骤已收尾]")
        summary = ""
        try:
            client = self._make_llm_client("power")
            user_content = self._build_progress_input(msgs)
            parts: list[str] = []
            async for ev in client.stream_chat(
                    [{"role": "system", "content": _PROGRESS_SUMMARY_SYSTEM},
                     {"role": "user", "content": user_content}],
                    [], signal=self._cancel_event(task_id)):
                if ev["type"] == "text":
                    parts.append(ev["text"])
            summary = "".join(parts).strip()
        except (LlmError, LlmAborted) as e:
            logger.warning(f"[DC:orch] progress summary failed for step={step_id}: {e}")
        if not summary:
            for m in reversed(msgs):
                if m.get("role") == "assistant" and not m.get("tool_calls") \
                        and (m.get("content") or "").strip():
                    summary = m["content"].strip()[:500]
                    break
        if summary:
            try:
                raw = await self._storage.get_artifact(task_id, "_flow", _KEY_FINDING_ARTIFACT)
                existing: list[str] = []
                if raw and raw.get("content"):
                    existing = [ln for ln in str(raw["content"]).splitlines() if ln.strip()]
                merged = existing + [f"[步骤 {step_id} 进度总结] {summary}"]
                merged = merged[-_KEY_FINDING_MAX_LINES:]
                content = "\n".join(merged)
                await self._storage.save_artifact(task_id, "_flow", _KEY_FINDING_ARTIFACT,
                                                  content, "text")
                await self._append_message(task_id, step_id,
                                           {"role": "assistant",
                                            "content": f"{finish_label} {summary}",
                                            "round_num": -1})
            except Exception:
                pass
        await self._sm.advance_step(task_id, step_id, "completed")
        if step_id == "report":
            await self._sm.complete_task(task_id)
        self._publish(task_id, "streamEnd", {"stepId": step_id})
        logger.info(f"[DC:orch] step {step_id} {reason} — progress summarized, "
                    f"marked completed (orchestrator will open next step)")

    @staticmethod
    def _build_progress_input(msgs: list[dict], max_chars: int = 120000) -> str:
        lines: list[str] = []
        tool_summaries: list[str] = []
        for m in msgs:
            if m.get("role") == "tool":
                continue
            if m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args_obj = json.loads(fn.get("arguments", "{}") or "{}")
                    except (ValueError, TypeError):
                        args_obj = {}
                    tool_summaries.append(Orchestrator._summarize_tool_call(name, args_obj))
                continue
            content = (m.get("content") or "").strip()
            if content:
                lines.append(f"[{m.get('role')}] {content}")
        head: list[str] = []
        total = 0
        for ln in lines:
            if total + len(ln) > max_chars:
                break
            head.append(ln)
            total += len(ln)
        body = "\n".join(head)
        if tool_summaries:
            body += "\n\n[最近工具调用]\n" + "\n".join(
                f"- {s}" for s in tool_summaries[-40:])
        return body

    def _sanitize_tool_pairs(self, messages: list[dict]) -> list[dict]:
        declared: set[str] = set()
        responded: set[str] = set()
        for m in messages:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    if tc.get("id"):
                        declared.add(tc["id"])
            elif m.get("role") == "tool" and m.get("tool_call_id"):
                if m["tool_call_id"] in declared:
                    responded.add(m["tool_call_id"])
        out: list[dict] = []
        for m in messages:
            role = m.get("role")
            if role == "assistant":
                tcs = m.get("tool_calls") or []
                if tcs:
                    remain = [tc for tc in tcs if tc.get("id") in responded]
                    if remain:
                        out.append({**m, "tool_calls": remain})
                    elif m.get("content"):
                        out.append({k: v for k, v in m.items()
                                    if k not in ("tool_calls", "reasoning_content")})
                else:
                    out.append(m)
            elif role == "tool":
                if m.get("tool_call_id") in responded:
                    out.append(m)
            else:
                out.append(m)
        return out

    def _trim_tool_results(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m.get("role") == "tool":
                content = m.get("content", "") or ""
                if len(content) > 2000:
                    m2 = dict(m)
                    m2["content"] = content[:2000] + "...[trimmed]"
                    out.append(m2)
                    continue
            out.append(m)
        return out

    def _smart_compress(self, messages: list[dict]) -> list[dict]:
        if len(messages) <= 8:
            return self._trim_tool_results(messages)
        early = messages[:-6]
        recent = messages[-6:]
        return [{"role": "user", "content": f"[早期对话已压缩，共 {len(early)} 条消息]"}] + recent

    def _get_exec_tools(self) -> list[dict]:
        return list(_EXEC_TOOLS)

    def _get_monitor_tools(self) -> list[dict]:
        return list(_MONITOR_TOOLS)

    def _get_gate_tools(self) -> list[dict]:
        return list(_GATE_TOOLS)

    def _get_reverse_tools(self) -> list[dict]:
        return list(_REVERSE_TOOLS)

    def _get_researcher_tools(self) -> list[dict]:
        return list(_RESEARCHER_TOOLS)

    def _make_llm_client(self, tier: str):
        from ..config import get_llm_config
        from .llm_client import LlmClient
        cfg = get_llm_config()
        if tier == "light":
            base_url, api_key, model = (cfg.get("light_base_url") or "",
                                        cfg.get("light_api_key") or "",
                                        cfg.get("light_model") or "")
        else:
            base_url, api_key, model = (cfg.get("power_base_url") or "",
                                        cfg.get("power_api_key") or "",
                                        cfg.get("power_model") or "")
        if not base_url or not api_key:
            raise LlmError(0, f"LLM not configured, 请检查设置页 "
                              f"{'Light' if tier == 'light' else 'Power'} 组 Base URL / API Key")
        return LlmClient({"base_url": base_url, "api_key": api_key, "model": model or "mock-model"})

    async def _invoke_tool(self, task_id: str, step_id: str, name: str,
                           arguments_str: str, call_id: str) -> str:
        from ..step_context import get_task_root
        if name in _REVERSE_ONLY_TOOLS:
            _step_type = "executor"
            try:
                _trow = await self._storage.get_task(task_id, include_hidden=True)
                _st = next((s for s in (_trow or {}).get("steps", [])
                            if s.get("step_id") == step_id), None)
                if _st:
                    _step_type = _st.get("type") or "executor"
            except Exception:  # noqa: BLE001
                pass
            if _step_type != "reverse":
                return f"[Error] 工具 {name} 仅逆向专家步骤（type=reverse）可用"
        try:
            args = json.loads(arguments_str or "{}")
            if not isinstance(args, dict):
                args = {}
        except (ValueError, TypeError):
            args = {}
        if name in ("dcflow_write_file", "dcflow_edit_file") \
                and get_task_root(task_id) == PROJECT_ROOT:
            fp = str(args.get("file_path") or "")
            if fp and not os.path.isabs(fp) \
                    and not fp.replace("\\", "/").startswith(".dc_tmp"):
                return ("[Error] 拒绝裸相对路径 file_path（解析基准是项目根，裸相对路径"
                        "会落到项目根而非步骤目录，后续步骤读不到）：请传完整路径 —— 推荐 "
                        f".dc_tmp/{task_id}/{step_id}/artifacts/<文件名>（产物目录）；"
                        "或绝对路径。产物统一放步骤产物目录 artifacts/ 下。")
        action, block_msg = await self._intercept_step_report(task_id, step_id, name, args)
        if action == "block":
            logger.info(f"[DC:orch] flow report write blocked step={step_id}: {block_msg[:60]}")
            return block_msg
        if name == "dcflow_sim":
            from ..simulator.tool import run_sim_tool
            lock = self._sim_locks.setdefault(task_id, asyncio.Lock())
            async with lock:
                return await asyncio.to_thread(run_sim_tool, task_id, self._sim_sessions, args)
        if name in _CTF_TOOL_FUNCS:
            from .. import ctf_tool
            func_name, params = _CTF_TOOL_FUNCS[name]
            fn_args = [str(args.get(k) or "") for k in params]
            return await asyncio.to_thread(getattr(ctf_tool, func_name), *fn_args)
        if self._tool_invoke is not None:
            result = await self._tool_invoke({"name": name, "args": args,
                                              "task_id": task_id})
        else:
            from .. import rest_api
            result = await rest_api.invoke_tool({"name": name, "args": args,
                                                 "task_id": task_id})
        if action == "mirror":
            await self._mirror_step_report(task_id, step_id)
        if isinstance(result, dict):
            return str(result.get("result", "(无输出)"))
        return str(result)

    async def _intercept_step_report(self, task_id: str, step_id: str, name: str,
                                     args: dict) -> tuple[str, str]:
        fp = str(args.get("file_path") or "")
        base = os.path.basename(fp.replace("\\", "/"))
        if base not in (_FLOW_REPORT_FILENAME, f"{step_id}-步骤报告.md", f"{step_id}-step_report.md"):
            return "", ""
        if base == f"{step_id}-step_report.md" and name in ("dcflow_write_file", "dcflow_edit_file"):
            return "", ""
        if name == "dcflow_read_file":
            report_path = os.path.normpath(os.path.join(PROJECT_ROOT, fp))
            if not os.path.isfile(report_path):
                raw = await self._storage.get_artifact(task_id, "_flow", _STEP_REPORT_ARTIFACT)
                if raw and raw.get("content"):
                    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
                    with open(report_path, "w", encoding="utf-8") as f:
                        f.write(raw["content"])
                    logger.info(f"[DC:orch] flow report restored from DB before read "
                                f"step={step_id} ({len(raw['content'])} chars)")
            try:
                await self._save_step_report_read_round(
                    task_id, step_id, await self._get_step_report_round(task_id, step_id))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DC:orch] flow report read round save failed step={step_id}: {e}")
            return "", ""
        if name in ("dcflow_write_file", "dcflow_edit_file"):
            report_path = os.path.normpath(os.path.join(PROJECT_ROOT, fp))
            if os.path.isfile(report_path):
                cur_round = await self._get_step_report_round(task_id, step_id)
                read_round = await self._get_step_report_read_round(task_id, step_id)
                if read_round is None or cur_round - read_round >= 10:
                    return ("block",
                            f"❌ 写入被阻止：你最近 10 轮内没有读取过流程报告"
                            f"（{_FLOW_REPORT_FILENAME}，请用 dcflow_read_file 读取完整内容后再写）。"
                            f"当前轮次 {cur_round}，上次读取轮次 {read_round or '从未'}。")
            return "mirror", ""
        return "", ""

    async def _mirror_step_report(self, task_id: str, step_id: str) -> None:
        try:
            report_path = os.path.join(PROJECT_ROOT, ".dc_tmp", task_id,
                                       _FLOW_REPORT_FILENAME)
            legacy_path = os.path.join(PROJECT_ROOT, ".dc_tmp", task_id, step_id,
                                       f"{step_id}-步骤报告.md")
            if os.path.isfile(report_path) and os.path.isfile(legacy_path):
                if os.path.getmtime(legacy_path) >= os.path.getmtime(report_path):
                    report_path = legacy_path
            elif os.path.isfile(legacy_path):
                report_path = legacy_path
            elif not os.path.isfile(report_path):
                return
            with open(report_path, "r", encoding="utf-8") as f:
                content = f.read()
            file_content = content
            cleaned = _KEY_FINDINGS_BLOCK_RE.sub(r"\1", content)
            raw_kf = await self._storage.get_artifact(task_id, "_flow", _KEY_FINDING_ARTIFACT)
            all_items: list[str] = []
            if raw_kf and raw_kf.get("content"):
                all_items = [ln.strip() for ln in raw_kf["content"].splitlines()
                             if ln.strip()]
            cleaned = self._strip_findings_fragments(cleaned, all_items)
            missing = [it for it in all_items if it not in cleaned]
            if missing:
                content = (cleaned.rstrip()
                           + f"\n\n{_KEY_FINDINGS_BLOCK_START}\n## 系统注入：关键发现\n"
                           + "\n".join(f"- {it}" for it in missing)
                           + f"\n{_KEY_FINDINGS_BLOCK_END}\n")
            if content != file_content:
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(content)
                flow_path = os.path.join(PROJECT_ROOT, ".dc_tmp", task_id,
                                         _FLOW_REPORT_FILENAME)
                if os.path.normpath(report_path) != os.path.normpath(flow_path):
                    with open(flow_path, "w", encoding="utf-8") as f2:
                        f2.write(content)
            await self._storage.save_artifact(task_id, "_flow", _STEP_REPORT_ARTIFACT,
                                              content, "markdown")
        except OSError as e:
            logger.warning(f"[DC:orch] flow report mirror failed step={step_id}: {e}")

    async def _append_message(self, task_id: str, step_id: str, message: dict) -> None:
        await self._storage.append_message(task_id, step_id, message)

    async def _update_step_stats(self, task_id: str, step_id: str, requests: int = 0,
                                 ttft_ms: Optional[int] = None, output_ms: int = 0,
                                 run_ms: Optional[int] = None) -> None:
        await self._storage.update_step_stats(
            task_id, step_id, requests=requests, ttft_ms=ttft_ms,
            output_ms=output_ms, run_ms=run_ms)

    async def _add_step_tokens(self, task_id: str, step_id: str,
                               prompt: int, cached: int, completion: int,
                               record_last: bool = True,
                               context_tokens: Optional[int] = None) -> None:
        try:
            await self._storage.add_step_tokens(
                task_id, step_id, prompt, cached, completion,
                context_tokens=context_tokens if context_tokens is not None else prompt)
            if not record_last:
                return
            raw = await self._storage.get_artifact(task_id, "_flow", _LAST_PROMPT_TOKENS_ARTIFACT)
            data: dict = {}
            if raw and raw.get("content"):
                try:
                    data = json.loads(raw["content"])
                except (ValueError, TypeError):
                    data = {}
            data[step_id] = prompt
            await self._storage.save_artifact(task_id, "_flow", _LAST_PROMPT_TOKENS_ARTIFACT,
                                              json.dumps(data), "text")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] add_step_tokens failed step={step_id}: {e}")

    async def _get_last_prompt_tokens(self, task_id: str, step_id: str) -> Optional[int]:
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", _LAST_PROMPT_TOKENS_ARTIFACT)
            if not raw or not raw.get("content"):
                return None
            val = json.loads(raw["content"]).get(step_id)
            return int(val) if val else None
        except (ValueError, TypeError):
            return None

    async def _get_step_report_round(self, task_id: str, step_id: str) -> int:
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", _STEP_REPORT_ROUNDS_ARTIFACT)
            if not raw or not raw.get("content"):
                return 0
            val = json.loads(raw["content"]).get(step_id)
            return int(val) if val else 0
        except (ValueError, TypeError):
            return 0

    async def _save_step_report_round(self, task_id: str, step_id: str, n: int) -> None:
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", _STEP_REPORT_ROUNDS_ARTIFACT)
            data: dict = {}
            if raw and raw.get("content"):
                try:
                    data = json.loads(raw["content"])
                except (ValueError, TypeError):
                    data = {}
            data[step_id] = n
            await self._storage.save_artifact(task_id, "_flow", _STEP_REPORT_ROUNDS_ARTIFACT,
                                              json.dumps(data), "text")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] step report round save failed step={step_id}: {e}")

    async def _get_step_report_read_round(self, task_id: str, step_id: str) -> Optional[int]:
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", _STEP_REPORT_READ_ROUND_ARTIFACT)
            if not raw or not raw.get("content"):
                return None
            val = json.loads(raw["content"]).get(step_id)
            return int(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    async def _save_step_report_read_round(self, task_id: str, step_id: str,
                                           n: Optional[int]) -> None:
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", _STEP_REPORT_READ_ROUND_ARTIFACT)
            data: dict = {}
            if raw and raw.get("content"):
                try:
                    data = json.loads(raw["content"])
                except (ValueError, TypeError):
                    data = {}
            if n is None:
                data.pop(step_id, None)
            else:
                data[step_id] = n
            await self._storage.save_artifact(task_id, "_flow", _STEP_REPORT_READ_ROUND_ARTIFACT,
                                              json.dumps(data), "text")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] flow report read round save failed step={step_id}: {e}")

    async def _append_round_messages(self, task_id: str, step_id: str, rounds: list[dict]) -> None:
        key = f"{task_id}:{step_id}"
        saved = self._last_saved_round.get(key, 0)
        for i in range(saved, len(rounds)):
            r = rounds[i]
            tool_calls = r.get("toolCalls", [])
            if (r.get("text") or "").strip() or tool_calls:
                entry: dict = {"role": "assistant", "content": r.get("text", "") or "",
                               "round_num": i}
                if tool_calls:
                    entry["tool_calls"] = json.dumps([
                        {"id": tc.get("call_id") or f"call_{i}_{j}", "type": "function",
                         "function": {"name": tc.get("name", ""),
                                       "arguments": json.dumps(tc.get("input") or {},
                                                                ensure_ascii=False)}}
                        for j, tc in enumerate(tool_calls)
                    ], ensure_ascii=False)
                await self._append_message(task_id, step_id, entry)
            for tc in tool_calls:
                await self._append_message(task_id, step_id, {
                    "role": "tool", "content": tc.get("output", "") or "",
                    "tool_call_id": tc.get("call_id"),
                    "toolName": tc.get("name", ""), "input": tc.get("input"),
                    "output": tc.get("output", "") or "", "round_num": i,
                })
            saved = i + 1
        self._last_saved_round[key] = saved

    _KF_QUOTE_PREFIX_RE = re.compile(
        r"^(?:.{0,4}?"
        r"(?:报告|文件|对话|步骤|节选|注入|上文|前面|上面|里面|里头|总结|内容|段落|注释|部分|地方|该|这|那|其|里|中)"
        r"(?:里|中|部分|地方)?"
        r"(?:说|提到|里有|中有|里说|里写|显示|提示|表明|说明|指出|意味着|提到过|提到，|提到：|提到:))")

    @staticmethod
    def _extract_key_findings(text: str, max_len: int = _KEY_FINDING_MAX_LEN) -> list[str]:
        if not text:
            return []
        found: list[str] = []
        for m in _KEY_FINDING_RE.finditer(text):
            seg = re.sub(r"[\r\n\t\v\f\u2028\u2029]+", " ", m.group(1))
            seg = seg.strip().rstrip("。.").strip()
            seg = seg.lstrip("*#-:： \t'\"【】")
            seg = seg.strip()
            if not seg:
                continue
            if any(marker in seg for marker in _SYSTEM_PROMPT_MARKERS):
                continue
            if Orchestrator._KF_QUOTE_PREFIX_RE.match(seg):
                continue
            if (not re.search(r"[\u4e00-\u9fff]", seg) and len(seg) >= 30
                    and re.match(r"(?i)^(mention|section|this|these|they|it|talk|say|show|describe|seem|contain|strong|warn|note|comment)",
                                 seg)):
                continue
            found.append(seg[:max_len])
        return found

    @staticmethod
    def _strip_findings_fragments(text: str, items: list[str]) -> str:
        if not text:
            return text
        frags = {f"- {it}" for it in items} | set(items)
        out = []
        for ln in text.splitlines():
            s = ln.strip()
            if s in frags:
                continue
            if s.startswith(_KEY_FINDINGS_BLOCK_START):
                continue
            if s.startswith(_KEY_FINDINGS_BLOCK_END):
                continue
            if s == "## 系统注入：关键发现":
                continue
            out.append(ln)
        return "\n".join(out)

    async def _capture_key_findings(self, task_id: str, step_id: str, text: str) -> None:
        try:
            items = self._extract_key_findings(text)
            if not items:
                return
            raw = await self._storage.get_artifact(task_id, "_flow", _KEY_FINDING_ARTIFACT)
            existing: list[str] = []
            if raw and raw.get("content"):
                existing = [
                    re.sub(r"[\r\n\t\v\f\u2028\u2029]+", " ", ln).strip()
                    for ln in str(raw["content"]).splitlines() if ln.strip()
                ]
            merged = list(existing)
            for it in items:
                if it not in merged:
                    merged.append(it)
            merged = merged[-_KEY_FINDING_MAX_LINES:]
            content = "\n".join(merged)
            await self._storage.save_artifact(task_id, "_flow", _KEY_FINDING_ARTIFACT,
                                              content, "text")
            try:
                report_dir = os.path.join(PROJECT_ROOT, ".dc_tmp", task_id)
                os.makedirs(report_dir, exist_ok=True)
                report_path = os.path.join(report_dir, _FLOW_REPORT_FILENAME)
                existing_text = ""
                if os.path.exists(report_path):
                    with open(report_path, "r", encoding="utf-8") as f:
                        existing_text = f.read()
                else:
                    raw_db = await self._storage.get_artifact(
                        task_id, "_flow", _STEP_REPORT_ARTIFACT)
                    if raw_db and raw_db.get("content"):
                        existing_text = raw_db["content"]
                cleaned = _KEY_FINDINGS_BLOCK_RE.sub(r"\1", existing_text)
                raw_kf = await self._storage.get_artifact(task_id, "_flow", _KEY_FINDING_ARTIFACT)
                all_items: list[str] = []
                if raw_kf and raw_kf.get("content"):
                    all_items = [ln.strip() for ln in raw_kf["content"].splitlines()
                                 if ln.strip()]
                cleaned = self._strip_findings_fragments(cleaned, all_items)
                missing = [it for it in all_items if it not in cleaned]
                if missing:
                    block = (f"\n\n{_KEY_FINDINGS_BLOCK_START}\n## 系统注入：关键发现\n"
                             + "\n".join(f"- {it}" for it in missing)
                             + f"\n{_KEY_FINDINGS_BLOCK_END}\n")
                    with open(report_path, "w", encoding="utf-8") as f:
                        f.write(cleaned.rstrip() + block)
                await self._mirror_step_report(task_id, step_id)
            except OSError as e:
                logger.warning(f"[DC:orch] key_findings report inject failed: {e}")
        except Exception:
            pass

    async def _save_chunk(self, task_id: str, step_id: str, chunk: dict) -> None:
        await self._storage.save_chunk(task_id, step_id, chunk)

    async def _save_chunks(self, task_id: str, step_id: str, chunks: list[dict]) -> None:
        if chunks:
            await self._storage.save_chunks(task_id, step_id, chunks)

    def _publish(self, task_id: str, command: str, payload: dict) -> None:
        self.sse_hub.publish(task_id, command, payload)

    def _emit_chunk(self, task_id: str, step_id: str, chunk: str) -> None:
        self._publish(task_id, "streamChunk", {"stepId": step_id, "chunk": chunk})

    def _publish_full_conversation(self, task_id: str, step_id: str,
                                   system_message: str, step_title: str, result: dict) -> None:
        conversation: list[dict] = []
        if system_message:
            conversation.append({"role": "system", "content": system_message})
        for r in result.get("rounds", []):
            if (r.get("text") or "").strip():
                conversation.append({"role": "assistant", "content": r["text"]})
            for tc in r.get("toolCalls", []):
                conversation.append({"role": "tool", "content": tc.get("output", "") or "",
                                     "toolName": tc.get("name", ""), "input": tc.get("input"),
                                     "output": tc.get("output", "") or ""})
        self._emit_chunk(task_id, step_id,
                         "__DC_FULL__" + json.dumps(
                             {"stepId": step_id, "conversation": conversation, "stepTitle": step_title},
                             ensure_ascii=False) + "__DC_FULL__")

    def _result_to_conversation(self, result: dict) -> list[dict]:
        conversation: list[dict] = []
        for r in result.get("rounds", []):
            if (r.get("text") or "").strip():
                conversation.append({"role": "assistant", "content": r["text"]})
            for tc in r.get("toolCalls", []):
                conversation.append({"role": "tool", "content": tc.get("output", "") or "",
                                     "toolName": tc.get("name", ""), "input": tc.get("input"),
                                     "output": tc.get("output", "") or ""})
        return conversation

    async def _submit_step(self, task_id: str, step_id: str) -> None:
        from .. import rest_api
        await rest_api.submit_step_result({"task_id": task_id, "step_id": step_id})

    def _is_cancelled(self, task_id: str) -> bool:
        return bool(self._cancelled.get(task_id, False))

    def _cancel_event(self, task_id: str) -> asyncio.Event:
        ev = self._cancel_events.get(task_id)
        if ev is None:
            ev = asyncio.Event()
            self._cancel_events[task_id] = ev
        return ev

    @staticmethod
    def _find_step(task: Optional[dict], step_id: str) -> Optional[dict]:
        if not task:
            return None
        for s in task.get("steps", []):
            if s.get("step_id") == step_id:
                return s
        return None
