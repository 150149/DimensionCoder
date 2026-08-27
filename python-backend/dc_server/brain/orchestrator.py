"""
dc_server.brain.orchestrator — 执行引擎（WP3 T3.4，旧 DimensionCodingOrchestrator.ts Python 化）

职责（全量移植旧 TS 逻辑，D8：按规格翻译重建，方法 snake_case + 行尾注释保留旧 TS 方法名）：
- 执行循环：next-step → 步骤执行 → Monitor 编排 → 最终审查（100 次迭代上限 + stuck 兜底）
- 步骤执行：prep 缓存 / 对话续接 / 介入消费 / LLM 调用（10 轮工具上限）/
  gate 审查（只读工具 + 不 submit，V5）/ submit / __DC_FULL__
- Monitor：每步骤完成后触发一次（并行组整组完成后触发一次，H5）；
  B1：读取 task 级 _flow intervention（flow_pending）注入 system_message 后清空
- Gate：审查完成后 pause_task（H11）+ 返回，等待人工审批
- 介入：_inject_interventions 消费 {pre_tool_injection, force_inject}（N1/N2），注入一次即移除；
  取消标志三值 stop/force_inject/immediate
- 上下文压缩（_check_and_compress）：触发依据 = API usage.prompt_tokens 精确统计
  （>400K，非字符估算）；压缩目标 = 摘要化早期工具轮直到总 token ≤ 800K（tiktoken
  确定性计数，用户方案：不是固定保留 20 轮）；压到 20 轮底线仍超 800K → 总结进度 +
  步骤 completed（编排 AI 接手开新步骤）；发送 400 上下文超限 → 激进压缩到 20 轮
  底线重试、压无可压才收尾（tiktoken 可能低估，不能以 ≤800K 判定跳过压缩）
- 步骤完成语义：执行步骤必须调用 dcflow_step_done 显式完成（纯文本轮注入确认
  引导，模型可继续工具/确认结论；引导用尽才按确认提交）——不再"纯文本即完成"
- 429 退避重试：min(1000*2^n, 30000) ≤10 次，__DC_RETRY__N/10 标记
- llmError：错误卡事件 + 步骤 stopped + 循环终止（H6）
- running 归属：_running dict 由本类维护（C4），wait_stopped 供 deleteTask（V-13）

SSE：所有旧 callbacks 事件经 sse_hub.publish 发布（事件表见 WP3 §2.1，只发 data 行由 hub 处理）。
REST/存储：同进程直调 storage / state_machine / rest_api（延迟导入避免循环依赖）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..config import PROJECT_ROOT
from ..graceful import graceful
from ..state_machine.state_machine import StateMachine
from ..storage.adapter import StorageAdapter
from .llm_client import LlmAborted, LlmError
from .sse_hub import SseHub

logger = logging.getLogger(__name__)


class OrchestratorBusyError(Exception):
    """任务执行中（running=true）不允许的并发操作（approve/reject 409，V-03②）。"""


class _StepGracefulFinish(Exception):
    """上下文压缩到极限（工具轮仅剩最近 KEEP_RECENT_TOOL_ROUNDS 轮）仍超
    COMPRESS_TOKENS（400K）：进度已总结、步骤已标记 completed，终止本轮执行
    （编排 AI 自然接手开新步骤）。收尾阈值 2026-08-16 修订：压到底线保留 20 轮
    完整工具轮后 >400K 才收尾（200K-400K 区间继续跑）。"""


class _StepGracefulDrain(Exception):
    """优雅重启排空信号：draining 期间立即终止当前步骤执行（工具不再执行、
    LLM 对话不再继续——否则 AI 收到拒绝消息会一直重试），步骤置回 pending
    待重启后自动恢复（可重试，无进度损失）。"""

    def __init__(self, step_id: str) -> None:
        super().__init__(step_id)
        self.step_id = step_id


# 优雅收尾总结指令（上下文超限时总结步骤当前进度，供编排 AI 决定是否开新步骤）
_PROGRESS_SUMMARY_SYSTEM = (
    "你是执行引擎的收尾总结器。当前步骤因工具调用历史过长（压缩到仅剩最近 20 轮"
    "完整仍超 800K 上下文预算）被强制收尾，请把该步骤的**当前进度**总结为一段简洁的"
    "中文说明（≤500 字），供编排 AI 决定是否开新步骤继续。要点：1) 已完成的工作与"
    "结论；2) 未完成的事项；3) 下一步建议。不要调用任何工具。"
)

# 上下文超限错误识别（发送 400 时服务端裁决：压缩后仍超模型上限 → 触发优雅收尾）
_CONTEXT_EXCEEDED_RE = re.compile(
    r"maximum context length|context length|reduce the length|exceed.*(?:limit|length)",
    re.IGNORECASE)


def _is_context_exceeded(err: Any) -> bool:
    """是否为上下文超限错误（400 maximum context length 等）。"""
    msg = str(getattr(err, "message", None) or err)
    return bool(_CONTEXT_EXCEEDED_RE.search(msg))


# tiktoken（cl100k_base）确定性 token 计数：压缩目标 ≤900K 的量度（非字符估算）。
# 懒加载 + 容错：不可用时返回 None，压缩回退旧 keep 模式 + 400 裁决兜底。
_TOKENIZER: Any = None


def _count_tokens(msgs: List[dict]) -> Optional[int]:
    """消息列表的 token 总量（cl100k_base 编码：content + tool_calls JSON）。
    tiktoken 不可用/编码失败 → None（调用方降级处理）。"""
    global _TOKENIZER
    if _TOKENIZER is None:
        try:
            import tiktoken  # 延迟导入（仅压缩需要）
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


# ═══════════════════════════════════════════════════════════════════
# 工具 schema（OpenAI functions 格式；名称/描述/参数从旧 TS 工具集翻译）
# ═══════════════════════════════════════════════════════════════════


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


# 共用工具 schema（executor/gate/monitor 复用）：目录浏览 + 读文件
_TOOL_LIST_DIR = _tool("dcflow_list_dir", "列出目录中的文件和子目录",
      {"dir_path": {"type": "string", "description": "路径，默认 '.'"}})
_TOOL_READ_FILE = _tool("dcflow_read_file", "读取文件内容（可用 start_line/end_line 按行范围分段读取，单次上限 30000 字符）",
      {"file_path": {"type": "string"},
       "start_line": {"type": "integer", "description": "起始行号（含），从 1 开始，默认 1"},
       "end_line": {"type": "integer", "description": "结束行号（含），默认到文件末尾"}},
      ["file_path"])

# 执行者工具集：只干活（读/写/搜/跑/提交）。不含流程修改工具——dcflow_adjust_flow/
# dcflow_list_steps 是 Monitor 的决策权（DB 实证：执行者历史零调用，纯噪音且诱导越权）
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

# 逆向专家专用工具（2026-08-24）：dcflow_sim 模拟器 + ctf_tool 4 个逆向工具——
# 仅 type=reverse 步骤可用（工具集注入 + _invoke_tool 执行层双保险）
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

# Monitor 专用工具集（只读 + 流程调整 + 显式完成，不含写文件工具）：list_steps/adjust_flow 决策 +
# list_dir/read_file 自查产出文件（信息核查——Monitor 曾抱怨无法读取步骤产出文件）+
# dcflow_step_done 显式完成（2026-08-22：Monitor 无完成工具是 monitor-2 无限核对死循环根因——
# AI 无法声明"审查结论完毕"，只能继续调只读工具）
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

# Gate 审查员专用工具集（只读，不含写文件工具）
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

# 研究员专用工具集（只读调研——不含写文件/运行命令工具）：
# list_dir/read_file 浏览与读取 + search_code 代码搜索 + read_doc 知识库 + step_done 完成
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

# 文本解析兜底：AI 输出 JSON {"tool":"...","arguments":{...}} 时自动识别为 tool call
_TEXT_TOOL_CALL_RE = re.compile(r'\{\s*"tool"\s*:\s*"(dcflow_\w+)"\s*,\s*"arguments"\s*:\s*(\{[\s\S]*?\})\s*\}')

# 2026-08-27（用户反馈 5b2519ef：AI 输出 add_steps JSON 没效果）：裸
# dcflow_adjust_flow JSON（orchestrator.md 教的输出格式 {"action":...}，未走
# tool_calls）自动包装为 dcflow_adjust_flow 工具调用——action 白名单防幻觉 JSON
# 误执行（与 dcflow_adjust_flow 支持的动作一致）
_ADJUST_FLOW_ACTIONS = {"add_steps", "skip_steps", "remove_steps",
                        "reorder_steps", "mark_complete", "no_change"}
_NAKED_ADJUST_RE = re.compile(
    r'\{\s*"action"\s*:\s*"(' + "|".join(sorted(_ADJUST_FLOW_ACTIONS)) +
    r')"\s*,')


def _extract_balanced_json(text: str, start: int) -> int:
    """从 start（'{" 处）做括号平衡扫描（字符串感知），返回配对的 '}' 之后的下标；
    未闭合返回 -1。裸 JSON 兕底用——steps_json 等嵌套数组内的 '}' 不能截断。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _gate_report_present(msgs: list[dict], round_text: str) -> bool:
    """gate 审批步骤是否已输出方案审批报告（报告主体/JSON options/人类决策确认）。
    只扫 assistant 消息 + 本轮文本——system 提示词（gate-reporter.md）含"方案审批报告"
    字样，不能作为判定依据（2026-08-24 DB 实证 10092ff1 step-12：AI 未输出报告
    直接 step_done → 页面停在"人工审批"但无报告可看）。"""
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
        return True  # 畸形参数（无效 JSON）→ 回传网关必 400，拦截让 AI 重出
    if not isinstance(obj, dict):
        return True  # 工具参数必须是对象；数组/标量非法
    return len(obj) == 0


def _parse_text_tool_calls(text: str, task_id: str = "") -> list[dict]:
    """文本解析：从 AI 输出提取工具调用（旧 _parseTextToolCalls）。

    - `{"tool":"dcflow_x","arguments":{...}}` 包装格式（原生）；
    - 2026-08-27（用户反馈 5b2519ef）：裸 dcflow_adjust_flow JSON
      （{"action":"add_steps",...}——orchestrator.md 教的输出格式，AI 未走
      tool_calls）自动包装为 dcflow_adjust_flow（action 白名单；task_id 缺失时
      注入——仅执行路径传 task_id，重建历史路径不传则不解析裸 JSON，避免凭空
      造 tool 调用）。

    call_id 确定性生成（flaky 根治）：旧实现用时间戳毫秒——同一 content 被独立
    解析两次（_build_lm_messages 的 assistant/tool 两个分支）时跨毫秒边界生成不同
    call_id，导致 tool 消息与 assistant.tool_calls 配对失败（OpenAI 400）。
    改为 hash(name+arguments) + 序号：同内容必同 id，且天然唯一。
    """
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
    # 裸 dcflow_adjust_flow JSON（仅执行路径：task_id 非空；匹配前移除 tool 包装
    # 区防内层 arguments 里的 action 被误匹配）
    if task_id:
        text2 = _TEXT_TOOL_CALL_RE.sub("", text or "")
        for match in _NAKED_ADJUST_RE.finditer(text2):
            end = _extract_balanced_json(text2, match.start())
            if end < 0:
                continue
            try:
                obj = json.loads(text2[match.start():end])
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            action = obj.get("action")
            if action not in _ADJUST_FLOW_ACTIONS:
                continue
            if not obj.get("task_id"):
                obj["task_id"] = task_id
            digest = hashlib.md5(
                ("dcflow_adjust_flow" + json.dumps(obj, sort_keys=True)).encode("utf-8")
            ).hexdigest()[:12]
            results.append({
                "name": "dcflow_adjust_flow",
                "call_id": f"text-{digest}-{len(results)}",
                "input": obj,
            })
    return results


# 空响应重试引导消息（完成闸门：LLM 上一轮未输出任何内容时注入，提示直接输出结论）
_EMPTY_NUDGE = ("【系统提示】你上一轮未输出任何内容。"
                "请基于已有工具结果直接输出本步骤的结论与总结（工作已完成可调用 dcflow_step_done）。")

# 纯文本轮确认引导（执行步骤完成语义：纯文本 ≠ 完成，必须 dcflow_step_done 显式完成）
_TEXT_FINISH_NUDGE = ("【系统提示】你输出了文本但未调用完成工具（dcflow_step_done），步骤不会提交。"
                      "若工作已完成，请调用 dcflow_step_done 提交结论；若需继续分析，请直接调用工具。")

# ── 关键发现捕获 ──
# 关键词集合（基于真实数据统计 + 用户确认）：关键发现 / key finding(s) / 重大发现 /
# 核心发现 / 突破了 / 核心突破 / 关键突破。取关键词后内容直到句号（。.）或换行。
_KEY_FINDING_RE = re.compile(
    r"(?:关键发现|重大发现|核心发现|突破了|核心突破|关键突破|key\s*findings?)"
    r"\s*[:：]?\s*([^。.\n]+)",
    re.IGNORECASE)
# 系统提示复述特征：AI 思考（reasoning）常复述 prompts 里「关键发现记录」小节原文
# （“输出「关键发现：<结论>」（同义关键词：…），系统自动捕获（到句号为止）”），
# 这类内容是提示词而非真实发现，命中任一特征即丢弃
_SYSTEM_PROMPT_MARKERS = (
    "同义关键词", "系统自动捕获", "自动捕获", "到句号为止",
    "关键发现.txt", "<结论>", "关键发现文件",
)
_KEY_FINDING_ARTIFACT = "key_findings"          # task 级 artifact 类型（step_id="_flow"）
_KEY_FINDING_MAX_LEN = 200                        # 单条最大长度（字符）
_KEY_FINDING_MAX_LINES = 100                      # 最大行数（超出仅保留最新）
_STEP_REPORT_ARTIFACT = "step_report"            # task 级 artifact：流程报告.md 的 DB 权威版
                                                # （2026-08-16 起多步骤共享一份，存 (_flow, step_report)；
                                                # AI 读写经 _invoke_tool 拦截；重启 .dc_tmp 清理后不丢）
_FLOW_REPORT_FILENAME = "流程报告.md"          # 流程级共享报告文件名（任务目录 .dc_tmp/{task_id}/ 下，
                                                # 所有步骤读写同一份——多步骤共享，替代旧 {step_id}-步骤报告.md）
_COMPRESS_MAP_ARTIFACT = "compress_map"          # task 级 artifact：{step_id: {"summaries": [摘要文本...]}}
                                                # 压缩点记录（压缩只作用于内存——重启后重建时应用，
                                                # 上下文与压缩时一致，不再重建全量超窗 400）
# 步骤报告注入块（HTML 注释包裹：渲染不可见、AI 读原文可见；注入前清理旧块保证单一）
_KEY_FINDINGS_BLOCK_START = "<!-- DC-KEY-FINDINGS-START -->"
_KEY_FINDINGS_BLOCK_END = "<!-- DC-KEY-FINDINGS-END -->"
# 注入块删除正则：只删「文件末尾、带标题」的最后一块（2026-08-16 修复——AI 全量
# 写报告会复制/挪动注入块标记（实证：c942812e 报告 START 被挪到正文中部 L523、
# END 在 L623），非锚定正则 START.*?END 会把中间正文（含 AI 新写章节）当注入区
# 误删。贪婪前缀 (.*) 定位最后一个 START → 中间复制的块（含标题的完整块）保留在
# 正文里，由剥离逻辑清掉其碎片与标记骨架，AI 真正写的内容不受影响）
_KEY_FINDINGS_BLOCK_RE = re.compile(
    rf"(.*){_KEY_FINDINGS_BLOCK_START}\s*## 系统注入：关键发现.*?{_KEY_FINDINGS_BLOCK_END}\s*$", re.S)
_LAST_PROMPT_TOKENS_ARTIFACT = "last_prompt_tokens"  # task 级 artifact：{step_id: 最近一次
                                                      # LLM 调用 usage.prompt_tokens}（压缩判断依据）
_FLOW_REPORT_ANCHOR_CHARS = 1500   # 流程报告提示注入的开头锚点字符数（步骤/Monitor 共用）
_STEP_REPORT_ROUNDS_ARTIFACT = "step_report_rounds"  # task 级 artifact：{step_id: 已累计轮数}
                                                    # （50 轮报告提醒计数，跨重启持久）
_STEP_REPORT_READ_ROUND_ARTIFACT = "step_report_read_round"  # task 级 artifact：{step_id: 最近一次读取流程报告的轮次}
                                                             # （未读先写阻挡依据，跨重启持久）
_MONITOR_ANCHORS_ARTIFACT = "monitor_anchors"  # task 级 artifact：{instance_id: 触发步骤 id}
                                                # （2026-08-23：monitor sort_order 被后续插入
                                                # 挤压漂移，FlowOverview 眼睛按锚点归属）

# 防放弃提醒（2026-08-16 用户需求）：AI 丧气话模式（DB 全量实证——c942812e step-4 穷尽 9 次/
# 诚实交付 8 次、fea66d3d step-5 穷尽 8 次；隐蔽说法「做最后的决定/最终交付决策」）。
# 命中 → 以用户口吻提醒看报告找突破口；连续出现时最多提醒一次（有实质进展后解除冷却）
GIVE_UP_PATTERN = re.compile(
    r"(穷尽|无法突破|无法继续|无法穿透|无法静态|无法逆推|超出.{0,6}能力|无能为力|无解|死路"
    r"|瓶颈|做不到|走不通|行不通|诚实交付|诚实结论|标记完成|最终交付|最终确认交付"
    r"|做最后的决定|到此为止|就此打住)"
)


class Orchestrator:
    """执行引擎（旧 DimensionCodingOrchestrator）。

    公共方法（REST 接线唯一入口，WP3 T3.4 H2）：
        start_task / approve_gate_and_run / reject_gate_and_run / stop_task /
        abort / wait_stopped / trigger_monitor / is_running / get_step_prep
    """

    MAX_ITERATIONS = 100          # 单任务 100 次迭代上限
    # 工具轮无上限（用户决策：无论跑多少次工具都不停）——退出只依赖：
    # 1) 显式 dcflow_step_done；2) 纯文本引导用尽（防假死）；3) 空响应预算耗尽；
    # 4) 上下文压缩压无可压（_check_and_compress 返回 None → 优雅收尾）。
    MAX_RETRY = 10                # 429 退避重试 ≤10 次
    # 上下文压缩（用户方案，2026-08-15 修订：400K 触发 → 200K 目标；2026-08-16
    # 修订收尾线：压到底线保留最近 20 轮完整工具轮，≤400K 继续跑，>400K 才收尾）：
    # - 触发：最近一次 API usage.prompt_tokens > COMPRESS_TOKENS（400K，精确统计）；
    #   无 usage 记录（last_tokens=None）→ tiktoken 实测 > 400K 同样触发（统一阈值）；
    # - 压缩目标：摘要化早期工具轮直到总 token ≤ CONTEXT_BUDGET_TOKENS（200K，
    #   tiktoken 确定性计数，非固定保留轮数）；
    # - 底线：完整工具轮至少保留最近 KEEP_RECENT_TOOL_ROUNDS 轮（硬性保留，不再
    #   继续压缩）；压到底线仍 > 400K（COMPRESS_TOKENS）→ 总结进度 + 步骤 completed
    #   （_graceful_finish_step）；发送 400 上下文超限 → 激进压缩到 20 轮底线重试、
    #   压无可压才收尾。
    COMPRESS_TOKENS = 400_000       # 压缩触发 + 收尾阈值：usage.prompt_tokens > 400K 才检查压缩；压到底线仍 >400K 才收尾
    CONTEXT_BUDGET_TOKENS = 200_000  # 压缩目标：总 token ≤ 200K（能压就压，压不到不强行收尾）
    KEEP_RECENT_TOOL_ROUNDS = 20    # 压缩底线：完整工具轮至少保留最近 20 轮（硬性，不继续压缩）
    MAX_TEXT_FINISH_NUDGE = 1       # 纯文本轮确认引导上限（用户决策 2026-08-15：连续纯文本只提示一次，不停止）

    def __init__(self, storage: StorageAdapter, state_machine: StateMachine,
                 sse_hub: Optional[SseHub] = None,
                 tool_invoke: Optional[Any] = None) -> None:
        self._storage = storage
        self._sm = state_machine
        self.sse_hub = sse_hub if sse_hub is not None else SseHub()
        # tool_invoke: async callable(payload: dict) -> dict（{result: str}）；None → rest_api.invoke_tool
        self._tool_invoke = tool_invoke

        self._running: Dict[str, bool] = {}            # C4：running 归属 orchestrator 维护
        self._cancelled: Dict[str, bool] = {}          # 取消标志
        self._cancel_kind: Dict[str, Optional[str]] = {}  # 取消标志三值 stop/force_inject/immediate
        self._cancel_events: Dict[str, asyncio.Event] = {}  # 中断当前 LLM 流（signal）
        self._prep_cache: Dict[str, dict] = {}         # prepare 只调一次缓存（旧 _prepCache）
        self._last_saved_round: Dict[str, int] = {}    # 逐轮消息保存游标
        self._step_t0: Dict[str, float] = {}           # 步骤执行开始（monotonic，运行时长落库；key=task:step）
        self._initial_orchestrating: set = set()       # 初始编排进行中的任务（防重入：创建/start 双触发并发 add_steps）
        self._sim_sessions: dict = {}                  # dcflow_sim 会话（task_id -> SimSession，跨工具轮保持）
        self._sim_locks: dict = {}                    # 2026-08-19：per-task 模拟器锁（并行 step 并发调同一会话防护）
        # 2026-08-27（详情页首屏进行中状态）：per-(task,step) 实时状态快照——
        # thinking/text 累积、当前执行中工具、streaming 与最后事件 seq。
        # 供 getStep 附带 + SSE 补发过滤（从总览进详情页时进行中事件已被 200 条
        # 缓冲冲掉，快照保证首屏可渲染「AI 正在思考」/「正在执行的命令」）
        self._step_live: Dict[Tuple[str, str], dict] = {}

    # ═══════════════════════════════════════════════════════════════
    # 公共方法（REST 接线调用）
    # ═══════════════════════════════════════════════════════════════

    def is_running(self, task_id: str) -> bool:
        """该任务是否正在执行（409 检查数据源，C4）。"""
        return bool(self._running.get(task_id, False))

    async def _ensure_initial_orchestration(self, task_id: str) -> bool:
        """确保初始编排步骤存在（2026-08-21 实体化）：任务无步骤时插入
        monitor-init（type=monitor，空任务时 sort_order=1 即最前）。

        任务已有步骤 → False（无需编排）；已存在 monitor-init → True（幂等）。
        不启动执行循环（由 start_task/create 流程启动）。"""
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
        """启动 Task 执行循环（旧 scheduleTask/scheduleAndRun）。幂等：已 running 直接返回。

        僵尸 running 兜底（恢复卡死修复）：abort 的 wait_stopped 有界等待（10s）可能
        超时返回——暂停瞬间 AI 正在跑长命令时，工具子进程不会因 cancel 立即退出，
        旧执行循环仍挂在工具调用上（_running 保持 True）。此时恢复（resumeStep + 
        startTask）若直接幂等返回，旧循环工具结束后 cancel 检查退出、_running 归零，
        但新循环从未启动 → 步骤停在 pending，整个流程永久卡死。检测到 cancelled 标记
        的僵尸 running → 等旧循环收尾（工具超时/完成后必退，_kill_cmd_tree 兜底）
        再启动新循环，避免双循环并发。
        """
        if self._running.get(task_id, False):
            if not self._cancelled.get(task_id, False):
                return  # 正常运行中，幂等返回
            logger.warning(f"[DC:orch] zombie running for task {task_id} (cancelled), "
                           f"waiting for previous loop to exit before restart")
            # 有界等待：旧循环若卡在工具调用（to_thread 线程不可取消，工具默认
            # 超时 60s + 进程树清理 ~30s）需等其自然退出；180s 足够覆盖，超时后
            # 放弃本次启动（用户可再点恢复），避免无限等待无反馈
            await self.wait_stopped(task_id, timeout=180.0)
            if self._running.get(task_id, False):
                # 极端：工具超时极大仍未退出——放弃本次启动，用户可再次点恢复
                logger.error(f"[DC:orch] task {task_id} loop still stuck after cancel, "
                             f"give up restart (click resume again later)")
                return
        # 初始编排兜底：任务仍无步骤（创建时编排失败/未触发）→ start 时补触发一次
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
        # 2026-08-25（Hindsight 记忆模块 B-5）：启动时创建项目记忆 bank（幂等）
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
        """Gate 审批通过后重启执行循环（旧 approveGateAndRun）。
        running 检查→advance(approved)→resume(after_intervention)→start_task。"""
        if self._running.get(task_id, False):
            raise OrchestratorBusyError("该步骤正在审查中，请稍后再审批")
        await self._sm.handle_gate(task_id, step_id, "approved", reason or "")
        # 2026-08-23（用户反馈）：审批通过 = 选项确认完成（最后一个 step_done）——
        # gate 步骤完成即创建审查实例，否则方案审批→下一步之间无线上的眼睛；
        # 在完成点创建（不在进入下一步时创建），各完成路径互斥天然幂等
        await self._insert_monitor_step(task_id, step_id)
        await self._sm._resume_after_intervention(task_id)
        await self.start_task(task_id)

    async def reject_gate_and_run(self, task_id: str, step_id: str, reason: str) -> None:
        """Gate 审批拒绝（旧语义，SWP2-B 断言为准）：步骤回 pending + 任务暂停，
        不自动重跑（去掉 SWP3-B1 的自动 resume+start_task）。拒绝原因由 reject_gate
        写入 intervention artifact；人工 resume + start 后重跑时经 _read_interventions
        注入为 user 消息（flow gate reject 用例链路）。"""
        if self._running.get(task_id, False):
            raise OrchestratorBusyError("该步骤正在审查中，请稍后再审批")
        await self._sm.reject_gate(task_id, step_id, reason)

    async def stop_task(self, task_id: str, level: str = "user") -> None:
        """abort + 置 stopped（端点 30 用户暂停用，J3）。"""
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
        """置 cancel 标志 + 等当前轮退出（旧 cancel + scheduleTask 竞态处理）。

        kind: stop/force_inject/immediate（取消标志三值）；缺省保持已置值，None 时按 stop。
        """
        if kind is not None:
            self._cancel_kind[task_id] = kind
        if self._cancel_kind.get(task_id) is None:
            self._cancel_kind[task_id] = "stop"
        self._cancelled[task_id] = True
        ev = self._cancel_events.get(task_id)
        if ev is not None:
            ev.set()
        # 2026-08-25（用户需求）：中断/暂停时杀掉该任务正在运行的命令进程树
        # （run_cmd 不响应 cancel_event，不杀则命令继续跑到 60s 超时；与超时
        # 同路径 _kill_cmd_tree，taskkill /T 连孙进程）
        try:
            from .. import rest_api
            await rest_api.kill_task_cmds(task_id)
        except Exception:  # noqa: BLE001
            pass
        await self.wait_stopped(task_id)

    async def wait_stopped(self, task_id: str, timeout: float = 10.0) -> None:
        """await running 归零（deleteTask 用，V-13）。有界等待（默认最多 10s）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._running.get(task_id, False):
                return
            await asyncio.sleep(0.2)

    # ═══════════════════════════════════════════════════════════════
    # 实体化（2026-08-21）：审查/收尾步骤（monitor-*/review/report）生命周期
    # ═══════════════════════════════════════════════════════════════

    def _step_is_review_kind(self, step_id: str, step_type: str = "") -> bool:
        """审查/收尾步骤判定（type 优先，id 模式兜底）。"""
        return (step_type in ("monitor", "review", "report")
                or step_id.startswith("monitor-") or step_id in ("review", "report"))

    async def _monitor_prep(self, task_id: str, step: dict) -> dict:
        """审查/收尾步骤的上下文构建（2026-08-21 实体化）：system = 按类型选
        提示词（orchestrator / final-reviewer / final-reporter），step_context =
        monitor_export 动态摘要 + flow_pending 消费（保留 best_effort 收尾复核提示）。"""
        from .. import rest_api
        from ..prompts import load_prompt
        from ..prompts.registry import prompt_for_step
        from ..step_context import list_prior_step_outputs, get_task_root
        step_id = step.get("step_id", "")
        step_type = step.get("type") or (
            "monitor" if step_id.startswith("monitor-")
            else "review" if step_id == "review" else "report")
        is_final = step_type in ("review", "report")
        # 2026-08-21 去绑定普通化：上下文锚点不再解析命名（monitor-step-X →
        # step-X）——monitor 是独立编号步骤，按自身 sort_order 自动推导：取
        # sort_order 小于本实例的最大 completed 真实步骤（位置即锚点）
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
            # 2026-08-23（用户定方案）：预设流程模板拼入 system——两个静态 md
            # 拼接跨任务/跨步骤字节级恒定，前缀缓存命中率最高（放 user 则跟在
            # 「当前任务」后，每任务不同 → 模板只同任务内命中）；非初始编排
            # （monitor-N）也能参考模板调整流程（修复前仅 monitor-init 注入
            # user，monitor-N 看不到模板）。flow-templates.md 自带标题，不重复拼
            system_prompt += "\n\n" + load_prompt("flow-templates")
        user_context = ctx.get("user_context", "") or ""
        # 2026-08-22：前序步骤 AI 产出文件清单（Monitor 自查产出需要路径线索）
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
        # 流程级请求注入（flow_pending，消费后清空）
        flow_msg = await self._consume_flow_intervention(task_id)
        if flow_msg:
            user_context += f"\n\n## 用户流程级请求\n{flow_msg}"
        # 尽力模式收尾复核（原 _final_review 语义）
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
        """确保任务尾部有 review+report（流程创建时系统自动插入，幂等）。"""
        conn = await self._storage._get_conn()
        exists = {r["step_id"] for r in conn.execute(
            "SELECT step_id FROM task_steps WHERE task_id = ? "
            "AND step_id IN ('review','report')", (task_id,)).fetchall()}
        if "review" not in exists:
            await self._storage.ensure_step(task_id, "review", "最终审查", "review")
        if "report" not in exists:
            await self._storage.ensure_step(task_id, "report", "产出报告", "report")

    async def _next_monitor_instance_id(self, task_id: str) -> str:
        """任务内下一个审查实例 id（去绑定普通化 2026-08-21）：monitor-N 独立
        编号（monitor-init / monitor-intervene-* 为独立命名空间，不占序号）。"""
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
        """步骤完成后插入审查步骤（去绑定普通化 2026-08-21）：独立编号
        monitor-N（任务内自增，不与步骤命名绑定）；插到 step_id 之后；每次
        完成都新建实例（旧行 completed 终态保持，不复用）。

        2026-08-23（DB 实证 60b8e589：monitor-9~98 空壳死循环 90 个）双防御：
        1) 任务已终态（completed/abandoned）不插——任务完成后执行循环补尾阶段
           （收尾链未完成时继续拾取）不再产生新 monitor；
        2) 审查/收尾步骤（monitor-*/review/report）完成不插——monitor-N graceful
           收尾（上下文超限快进 5-10s/个）曾触发 monitor-N+1 无限链，直到
           MAX_ITERATIONS(100) 耗尽才停，流程总览连线上几十个眼睛。"""
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
            # 2026-08-23（DB 实证 10092ff1 monitor-6 眼睛错位）：记录锚点——
            # monitor 的 sort_order 会被后续插入的步骤挤压后移（执行顺序正确），
            # 前端眼睛归属按 sort_order 区间会随之漂移（cr-r1→step-6 无线段、
            # 末尾 fallback 重复挂载）——锚点 = 触发步骤 id（稳定不漂移），
            # FlowOverview 按锚点步骤的当前 sort_order 归属眼睛
            await self._record_monitor_anchor(task_id, instance_id, step_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] insert monitor step {instance_id} failed: {e}")

    async def _record_monitor_anchor(self, task_id: str, instance_id: str,
                                     trigger_step_id: str) -> None:
        """记录 monitor 实例的触发步骤锚点（_flow/monitor_anchors artifacts，
        {instance_id: trigger_step_id}；多次插入幂等合并）。失败仅告警。"""
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
        """介入消息写入步骤 intervention artifact（执行时 _read_interventions 消费落库）。"""
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
        """任务内下一个介入审查实例 id（去绑定普通化 2026-08-21）：
        monitor-intervene-N 按轮次编号（N = 既有最大序号 + 1）。"""
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
        """介入触发（2026-08-21 去绑定普通化）：每次介入创建新实例
        monitor-intervene-N（独立编号，不复用旧行；旧行 completed 终态保持）。
        - send：新实例插入当前运行步骤（active，含 monitor 类型）之前的线段排队
          ——当前无 active 时插队首（最小 sort_order 的真实 pending/stopped 之前）；
        - force_inject：打断当前步骤（置 pending）+ 新实例队首优先执行。
        gate 待审批（paused(gate)+gate active）时 send → 消息写给 gate（重跑纳入），
        不创建介入实例。"""
        # 介入是新阶段：清除上一执行阶段的取消状态
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
        # gate 待审批场景：消息写给 gate（重跑纳入），不创建介入审查
        if task.get("status") == "paused" and task.get("pause_level") == "gate":
            gate = next((s for s in task["steps"]
                         if s.get("human_attention") == "gate"
                         and s.get("status") == "active"), None)
            if gate:
                await self._save_intervention(task_id, gate["step_id"], reason)
                await self.start_task(task_id)
                return
        # 锚点 = 当前运行步骤（active，含 monitor 类型）之前——介入挂在其
        # 前面的线段（用户决策 2026-08-21）；无 active 时取最小 sort_order 的
        # 真实 pending/stopped（队首——排队介入也要尽快处理）
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
            # 打断当前步骤 → pending（恢复执行时重跑）
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
            # 2026-08-24（根因修复 10092ff1）：介入实例记录锚点（当前运行步骤）——
            # 前端按锚点归属（monitor-intervene-* 前缀 → 锚点步骤之前的线段），
            # 免疫后续 reorder 造成的 sort_order 漂移（此前从不写锚点，漂移后
            # 介入眼睛错位/堆叠）
            await self._record_monitor_anchor(task_id, instance_id, anchor["step_id"])
        await self._save_intervention(task_id, instance_id, reason)
        self._cancelled[task_id] = False
        await self.start_task(task_id)

    async def get_step_prep(self, task_id: str, step_id: str) -> dict:
        """步骤 prompt 准备（只调一次，缓存到 _prepCache；端点 32 与执行循环共用）。
        2026-08-21 实体化：审查/收尾步骤（type=monitor/review/report）→ _monitor_prep
        （提示词按类型选 + 动态摘要）；普通步骤走 prepare_step。"""
        key = f"{task_id}:{step_id}"
        if key in self._prep_cache:
            return self._prep_cache[key]
        from .. import rest_api  # 延迟导入避免循环依赖
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

    # ═══════════════════════════════════════════════════════════════
    # 执行循环
    # ═══════════════════════════════════════════════════════════════

    async def _run_loop(self, task_id: str) -> None:
        """执行循环包装：running 标志归零（含异常路径）。

        DB 锁冲突自动恢复（2026-08-15 修复）：外部进程（DB 查看工具/杀软扫描/
        文件同步）短暂持锁时 sqlite3 报 "database is locked"——busy_timeout 30s
        等待后仍冲突 → active 步骤回 pending（安全点，_park_active_steps）→
        退避重跑整个循环（5s/30s/120s），不再永久卡死；用户暂停/任务完成则退出。
        """
        delays = (5, 30, 120)  # 锁冲突退避（秒）：共 1 次立即执行 + 3 次重试
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
                    # 2026-08-25（Hindsight 记忆模块 B-5）：执行循环崩溃 → 错误 retain
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
            # 2026-08-27：执行循环退出（完成/暂停/打断/崩溃/优雅排空）→ 清空该任务
            # 全部 live 快照（防 stopped/pending 步骤重新进入页面渲染残留执行状态；
            # 正常完成路径快照已由 streamEnd 清空，此处幂等）
            self._clear_task_live(task_id)

    async def _park_active_steps(self, task_id: str) -> None:
        """优雅重启排空：所有 active 步骤置回 pending（可重试），重启后自动续跑。
        2026-08-21 实体化：include_hidden=True——monitor/review/report 也是真实步骤，
        重启排空必须覆盖（否则残留 active 阻塞执行循环）。"""
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
        """核心执行循环（旧 _executionLoop）：首轮校验 task.status + stuck 兜底 + 100 次上限。"""
        iterations = 0
        while not self._is_cancelled(task_id) and iterations < self.MAX_ITERATIONS:
            iterations += 1

            # 优雅重启检查点（用户定义的安全点）：draining 期间不再启动新步骤/新命令——
            # 当前 active 步骤置回 pending（可重试），退出循环（重启后启动恢复自动续跑）
            if graceful.is_draining():
                await self._park_active_steps(task_id)
                logger.info(f"[DC:orch] task {task_id} graceful drain — execution loop exited")
                return

            # 0. 验证 task 仍然存在（防止被删除后继续执行）
            task = await self._storage.get_task(task_id)
            if not task:
                logger.info(f"[DC:orch] task {task_id} no longer exists, stopping")
                return

            # 首轮校验 task.status（T3.4）：completed/abandoned 终止；paused 时——
            # 有 active 步骤（gate 审查等待/执行中被打断）→ 区分处理；任务级暂停（J3）→ 恢复 active 继续
            status = task.get("status")
            if status in ("completed", "abandoned"):
                if status == "abandoned":
                    return
                # 补尾兜底（2026-08-21 规范化）：任务 completed 但收尾链未完成
                # （Monitor 提前 mark_complete / 旧数据缺行）→ 只 ensure review/report
                # 并继续执行，不再把任务 completed→active 回退（终态一致：report
                # 完成后仍 complete_task 保持 completed，下一轮校验收尾完成自然退出）
                task_h = await self._storage.get_task(task_id, include_hidden=True)
                hsteps = (task_h or {}).get("steps", [])
                review = next((s for s in hsteps if s.get("step_id") == "review"), None)
                report = next((s for s in hsteps if s.get("step_id") == "report"), None)
                tail_done = (review and review.get("status") == "completed"
                             and report and report.get("status") == "completed")
                if tail_done:
                    # 2026-08-27（用户反馈：已完成流程总览页发消息没反应）：收尾链
                    # 已完成但可能有新插入的 pending 介入步骤（completed 任务发
                    # pending 消息 → intervene_flow 创建 monitor-intervene-N）——
                    # 有 pending 继续执行（介入处理用户消息），无 pending 才退出
                    if not any(s.get("status") == "pending" for s in hsteps):
                        return
                    logger.info(f"[DC:orch] task {task_id} completed tail_done but "
                                f"pending intervene steps — continuing")
                await self._ensure_tail_steps(task_id)
                logger.info(f"[DC:orch] task {task_id} completed but tail steps "
                            f"pending — backfilling without status rollback")
                # 不 return：继续 get_next_steps 拾取 review/report 执行补尾
            if status == "paused":
                # 2026-08-22：paused 分支需要隐藏步骤（monitor/review/report）判断
                # 介入消息——顶部 get_task 不含隐藏行，monitor-init 等不在 steps 中
                # （否则 pending_monitors 恒空，gate 等待期间 monitor 消息永不消费）
                task = await self._storage.get_task(task_id, include_hidden=True)
                # 2026-08-21（DB 实证 e726f3e6 19:05）：gate 审批等待判断不能只看
                # active——决策包已提交时 gate 步骤被置 stopped（19:05:57），active
                # 判断漏过 → 走到 _resume_after_intervention 恢复任务 → 收尾步骤
                # （report）被误拾取，AI 乱调 adjust_flow（remove/add/skip/
                # mark_complete）把流程彻底改乱（任务被 mark_complete 结束）
                # 2026-08-23（DB 实证 99248a9f）：pending 的 gate 不算等待审批——
                # 未执行过、无决策包；用户手动暂停（pause_task 也写 pause_level='gate'）
                # 时 pending gate 被误判 waiting for approval → 点「继续」静默无反应
                gate_wait = [s for s in task.get("steps", [])
                             if s.get("human_attention") == "gate"
                             and s.get("status") in ("active", "stopped")]
                if task.get("pause_level") == "gate" and gate_wait:
                    # 2026-08-22（DB 实证 e726f3e6 05:04）：gate 审批等待中用户给
                    # monitor 步骤（monitor-init / monitor-N / monitor-intervene-N /
                    # review / report）发消息 → 同样要恢复消费——此前只查 gate_wait
                    # 自身 intervention，monitor 消息永不消费（monitor-init 卡
                    # pending、页面无任何变化）
                    pending_monitors = [s["step_id"] for s in task.get("steps", [])
                                        if s.get("status") == "pending"
                                        and (s.get("type") in ("monitor", "review", "report")
                                             or str(s.get("step_id", "")).startswith("monitor-"))]
                    # 存在未消费介入消息（用户发消息/做出选择）→ 将 gate 步骤重置回
                    # pending 重新执行（AI 纳入新信息后重新整理决策请求包）
                    if await self._has_pending_interventions(
                            task_id, [s["step_id"] for s in gate_wait] + pending_monitors):
                        for s in gate_wait:
                            if s["status"] == "pending":
                                continue
                            logger.info(
                                f"[DC:orch] gate {s['step_id']} has pending intervention "
                                f"— reset to pending for re-decision")
                            await self._sm.advance_step(task_id, s["step_id"], "pending")
                        # 2026-08-21：介入消息交给 gate 重跑消费——恢复任务继续执行
                        # （不恢复则任务仍 paused，下一轮循环再次命中同一分支死循环，
                        # 永远走不到 get_next_steps）
                        await self._sm._resume_after_intervention(task_id)
                        continue  # get_next_steps 将拾取该 gate 步骤重新执行
                    logger.info(f"[DC:orch] task {task_id} paused with gate waiting "
                                f"({len(gate_wait)} step(s)) — waiting for approval")
                    return
                active_steps = [s for s in task.get("steps", []) if s["status"] == "active"]
                if active_steps:
                    # gate 审查等待中：存在未消费介入消息（用户发消息/做出选择）→ 将 gate
                    # 步骤重置回 pending 重新执行（AI 纳入新信息后重新整理决策请求包），
                    # 而不是直接退出——否则介入消息无人消费，流程表现为点了没反应
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
                        continue  # get_next_steps 将拾取该 gate 步骤重新执行
                    if gate_steps:
                        logger.info(f"[DC:orch] task {task_id} paused with gate active — "
                                    f"waiting for approval/intervention")
                        return
                    # 非 gate 的 active 步骤（2026-08-16 修复：J3 任务级暂停时步骤执行中被打断，
                    # 残留 active；恢复（start_task）若直接退出则永久卡死）→ 重置 pending 续跑，
                    # 不视为审批等待（只有 gate 步骤才等待人工审批）。
                    for s in active_steps:
                        if s.get("human_attention") == "gate":
                            continue
                        logger.info(f"[DC:orch] paused with active exec step {s['step_id']} "
                                    f"— reset to pending for resume")
                        await self._sm.advance_step(task_id, s["step_id"], "pending")
                    continue  # get_next_steps 将重新拾取该步骤
                await self._sm._resume_after_intervention(task_id)

            # 1. 获取下一个待执行步骤（含并行组）
            steps = await self._sm.get_next_steps(task_id)
            if not steps:
                # 没有待执行步骤 → 检查是否有卡住的 active 步骤（force_inject 后遗留）
                task = await self._storage.get_task(task_id, include_hidden=True)
                stuck_steps = [s for s in task.get("steps", [])
                               if s["status"] == "active"]
                if stuck_steps and task.get("status") == "active":
                    for s in stuck_steps:
                        logger.info(f"[DC:orch] resetting stuck active step {s['step_id']} → pending")
                        await self._sm.advance_step(task_id, s["step_id"], "pending")
                    continue  # 重新进入循环，get_next_steps 会返回该步骤
                # 2026-08-21：completed 补尾已由首轮校验分支处理（不再回退任务状态）——
                # 此处若仍 completed 且无 pending 收尾步骤则正常退出
                if task.get("status") == "completed":
                    return
                if not task.get("steps"):
                    # 实体化：空任务（无 monitor-init）→ 兜底插入初始编排步骤后退出
                    # （用户 start 时拾取；不再循环等待）
                    logger.warning(f"[DC:orch] task {task_id} has no steps — ensure monitor-init")
                    await self._ensure_initial_orchestration(task_id)
                    return
                return  # 无 pending/active（正常收尾前状态）——退出等待外部触发

            first = steps[0]
            # 1b. 并行组补充（H5/P0-2）：首步未声明 parallel_with 时，扫描声明了
            #     parallel_with 含首步 id 的 pending 步骤并入组（并行声明可能挂在
            #     组内后续步骤上，如 incident-check step-3 parallel_with=["step-2"]）
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

            # 2. Gate 步骤 → 独立审查员角色执行，完成后暂停等待审批（H11）
            if first.get("human_attention") == "gate":
                await self._execute_step(task_id, first["step_id"], is_gate=True)
                if self._is_cancelled(task_id):
                    return
                # 人类已拍板（_execute_step 内 AI 确认决策并 submit 完成）→ 不暂停，
                # 继续循环执行编排者规划的新步骤（否则用户输入决策后流程再次卡在 gate）
                task_after = await self._storage.get_task(task_id)
                step_after = self._find_step(task_after, first["step_id"]) if task_after else None
                if step_after and step_after.get("status") == "completed":
                    logger.info(f"[DC:orch] gate {first['step_id']} completed (human confirmed) — continuing flow")
                    continue
                # 尽力模式（2026-08-16 用户需求扩展）：gate 弹审批时自动走"用户决策"路径。
                # 2026-08-20 修复：直接提交完成——原实现（注入"尽力模式"消息 + 重置
                # pending 重跑，期望 AI 输出「已确认人类决策」标记走 _gate_confirmed_by_human
                # 链路）不成立：gate-reporter 角色将介入消息解读为系统提示而非人类决策，
                # 坚持"gate 步骤等待人类审批"永不输出确认标记（DB 实证 step-8 摘要
                # 原文"gate 步骤等待人类审批，非执行"）→ 永不 submit → 编排循环无限
                # active↔pending（step-8 每 20-40s 一轮 ×10+ 轮，用户手动 stopped 才停）。
                # 直接 submit 语义等价"用户已授权 AI 自行决策"，且省一轮 AI 调用。
                task_before_pause = await self._storage.get_task(task_id)
                if task_before_pause and task_before_pause.get("best_effort"):
                    await self._submit_step(task_id, first["step_id"])
                    # 2026-08-23（用户反馈）：尽力模式自动放行 = 授权决策完成——
                    # 与 approve_gate_and_run 一致，在完成点创建审查实例
                    await self._insert_monitor_step(task_id, first["step_id"])
                    logger.info(f"[DC:orch] best-effort: gate {first['step_id']} "
                                f"auto-submitted (user delegated decision)")
                    continue  # get_next_steps 将跳过已完成的 gate 步骤
                logger.info(f"[DC:orch] gate step {first['step_id']} reviewed, pausing for human approval")
                await self._sm.pause_task(task_id)
                return

            # 3. 并行组（H5）：组内 asyncio.gather 并发（skip_monitor=True——
            #    Monitor 由本循环在整组完成后触发一次）；任一步失败记录
            #    parallel_failed，其余继续
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
                # 实体化（2026-08-21）：整组完成后插入一个审查步骤（组内最后步骤之后）
                await self._insert_monitor_step(task_id, group_ids[-1])
            else:
                # 4. 普通步骤（Monitor 由 _execute_step 在步骤完成后触发，旧 TS 同构）
                await self._execute_step(task_id, group_ids[0])
                if self._is_cancelled(task_id):
                    return

        if iterations >= self.MAX_ITERATIONS:
            logger.warning(f"[DC:orch] Max iterations reached for task {task_id}, stopping execution")

    async def _step_is_active(self, task_id: str, step_id: str) -> bool:
        """宽容化辅助：读取步骤当前状态是否为 active（2026-08-21 竞态修复）。
        外部操作（暂停/停止/恢复/排空）可能已在执行器异常处理前修改了状态，
        异常路径不得假设步骤仍 active。"""
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
        """宽容化收尾：仅当步骤仍为 active 时置 stopped（幂等）。
        19:53 崩溃实证：暂停链把 step-18 置 pending 后，异常处理再 advance(stopped)
        触发非法转移（pending→stopped）导致执行循环崩溃——非 active 时跳过仅日志。"""
        if await self._step_is_active(task_id, step_id):
            await self._sm.advance_step(task_id, step_id, "stopped")
        else:
            logger.info(f"[DC:orch] step={step_id} stop skipped — external already changed state")

    async def _execute_step(self, task_id: str, step_id: str, is_gate: bool = False,
                            skip_monitor: bool = False,
                            prep_override: Optional[dict] = None,
                            save_trigger_step: str = "") -> None:
        """执行单个步骤（旧 _executeStep）。开头无条件 advance(active)（T3.4 规格，advance 幂等）。
        skip_monitor: 并行组内调用传 True（H5：Monitor 整组完成后触发一次，不逐步骤触发）。
        审查/收尾步骤（2026-08-21 实体化：type=monitor/review/report）：普通步骤执行器全复用，
        仅差异——上下文（_monitor_prep 动态摘要，不走 prepare_step）、工具组（monitor 工具）、
        完成语义（不 dcflow_step_done，直接 completed + artifact 保存；monitor-init 完成自动
        插 review+report；report 完成 complete_task）；其余（状态/消息续接/压缩/llmError/打断）
        与普通步骤完全一致。"""
        # 审查/收尾步骤判定（type 优先，id 模式兜底）
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
        # 2026-08-24（逆向专家）：type=reverse 步骤用逆向工具集（模拟器+ctf 工具）
        is_reverse = _st_type == "reverse"
        # 研究员步骤（只读调研专家）：用只读工具集
        is_researcher = _st_type == "researcher"

        # 清除 prep cache（恢复执行时不要用旧缓存）
        self._prep_cache.pop(f"{task_id}:{step_id}", None)

        # 每次 _execute_step 是新的 LLM 会话（rounds 从 0 开始），保存游标必须重置
        # （否则 stop/resume、reject 重跑等续接场景新消息被游标跳过不落 DB）
        self._last_saved_round[f"{task_id}:{step_id}"] = 0

        # 检查是否有已有对话（对话续接模式）
        existing_messages = await self._storage.get_step_messages(task_id, step_id)
        has_existing = len(existing_messages) > 0
        if not has_existing:
            await self._storage.clear_step_messages(task_id, step_id)
            # 消息已清空 → 旧压缩点作废（重启重建不再应用）
            await self._clear_compress_points(task_id, step_id)
            # 新会话 → 50 轮报告提醒计数清零（2026-08-16：计数持久化到 DB，
            # 续接恢复继续累加；首次/重跑从 0 开始）
            await self._save_step_report_round(task_id, step_id, 0)
            # 未读先写阻挡的读取轮次同样清零（新会话从"未读取"重新约束；
            # None = 删除记录——0 是有效轮次（读发生在第 0 轮），不能用作"未读"）
            await self._save_step_report_read_round(task_id, step_id, None)
        else:
            logger.info(f"[DC:orch] continuing conversation for step={step_id}, existing msgs={len(existing_messages)}")

        # 1. 准备 prompt（只调一次，缓存到 _prepCache）
        # 2026-08-21 实体化：审查/收尾步骤走 _monitor_prep（动态摘要）；普通步骤
        # 走 prepare_step；prep_override（测试/兼容）优先
        prep = prep_override
        if prep is None:
            if is_review_kind:
                prep = await self._monitor_prep(task_id, _st_row or {"step_id": step_id, "type": _st_type})
            else:
                prep = await self.get_step_prep(task_id, step_id)
        # Task 8/§4.3.1：静态规则（system）+ 动态上下文（user）严格分离——
        # system 前缀稳定可缓存；system_message 拼装值仅作兼容展示字段，LLM 不用
        system_prompt = prep.get("system_prompt", "") or ""
        step_context = prep.get("step_context", "") or ""
        model_tier = prep.get("model_tier") or ("power" if is_gate else "light")
        step_title = prep.get("step_title") or step_id

        # stepStart：新执行周期 → 重置快照（streaming=True，LLM 请求进行中）
        seq_start = self._publish(task_id, "stepStart", {"stepId": step_id})
        self._step_live[(task_id, step_id)] = {
            "seq": seq_start, "streaming": True, "thinking": "", "text": "",
            "tool": None, "completed_tools": []}
        # 运行统计（2026-08-21 落库）：每次执行周期重置 t0（重跑=新计时）。
        # 2026-08-21 修复：key = task:step（此前 per-task 单点，多步骤执行周期
        # 交错时被后启动步骤覆盖 → run_duration_ms 偏小/错位，DB 实证 e726f3e6
        # step-14/15 的 out(API 时长) > run(步骤时长)）
        self._step_t0[f"{task_id}:{step_id}"] = time.monotonic()

        # 保存 system message 到 DB（仅首次；存纯规则，动态上下文由前端从 prep 渲染）
        if not has_existing:
            await self._append_message(task_id, step_id,
                                       {"role": "system", "content": system_prompt, "round_num": 0})
        # 介入消息（用户真实消息）由 _read_interventions 消费落库（2026-08-20 流修复：
        # 系统注入 step_context 不落库，用户消息只落一次；实体化后同一语义）

        # 开头无条件 advance(active)（T3.4；同状态幂等 no-op，SWP2-C）
        # 2026-08-21 实体化：审查/收尾步骤重跑（任务完成后再触发）时实例可能仍
        # completed——completed → active 非法转移：先走豁免入口置回 pending
        # （续接）→ advance active（状态机 pending → active 合法）
        if is_review_kind:
            mon_status = await self._sm._get_step_status(task_id, step_id)
            if mon_status == "completed":
                await self._sm.reset_step_for_continuation(
                    task_id, step_id, "审查步骤重跑续接")
        await self._sm.advance_step(task_id, step_id, "active")

        # 2a. 读取介入消息（恢复执行时的用户输入）→ 作为 user 消息注入对话
        # （_read_interventions 内部消费后落库 user 消息，审查/普通步骤同一语义）
        user_msgs = await self._read_interventions(task_id, step_id)

        # 2c/2d 已移除（2026-08-26 用户决策）：不再每轮注入「任务关键发现」「流程报告
        # 锚点」——即使独立 user 消息，内容随任务进展变化仍破坏前缀缓存。关键发现已
        # 在流程报告中（AI 读报告即得）；每 50 轮 report_nudge（下方 L1677）已提示
        # AI 重新读取流程报告并合并结论，无需重复注入。system + step_context 全程
        # 固定 → 前缀完全可缓存。
        
        # 3. 调用 LLM（对话续接：如有历史消息则加载并续接；DB system 已过滤不重复注入）
        # 2026-08-26：base_msgs（system 纯静态 + step_context 固定）→ 历史重建 → 介入消息
        base_msgs: list[dict] = [{"role": "system", "content": system_prompt},
                                 {"role": "user", "content": step_context}]
        if has_existing:
            # 压缩点应用（2026-08-15 用户决策）：重启重建不再发 DB 全量（实测
            # 1.26M > 1M 窗口 400）——压缩时记录的最早 N 个工具轮摘要在此按序替换，
            # 重建上下文与压缩时一致（重启前 375K，重启后重建仍是 375K）
            compress_points = await self._get_compress_points(task_id, step_id)
            # 重建历史过 _sanitize_tool_pairs 兜底：旧数据可能缺 tool_call_id / 配对不完整
            # （_build_lm_messages 已丢弃无 id tool；此处再清理尾部无响应的 assistant tool_calls）
            messages = base_msgs + self._sanitize_tool_pairs(
                self._build_lm_messages(existing_messages, compress_points)) + user_msgs
        else:
            messages = base_msgs + user_msgs

        # 上下文压缩（触发 = 最近一次 API usage.prompt_tokens >400K；压缩目标 =
        # 总 token ≤200K）。返回 None = 压到 20 轮底线仍超 200K → 直接收尾
        # （不发送 400 浪费一次请求）。
        last_tokens = await self._get_last_prompt_tokens(task_id, step_id)
        if has_existing:
            # resume 重建全量复核（2026-08-15 实证：压缩只作用于内存不落库，重启后
            # 重建 = DB 全量——step-4 实测 1.26M > 1M 窗口，但 last_tokens 记录的是
            # 旧压缩版的 345K → 本地 skip → 发送 400 后再压缩。此处实测提前压缩，
            # 避免失败往返（首轮一次，tiktoken 编码 <3s，远小于 400+压缩重试成本）
            measured = _count_tokens(messages)
            if measured is not None and measured > self.COMPRESS_TOKENS:
                logger.info(f"[DC:orch] resume rebuild measured={measured} > "
                            f"{self.COMPRESS_TOKENS} — pre-compress before send")
                last_tokens = measured
        compressed = await self._check_and_compress(task_id, step_id, messages, last_tokens)
        if compressed is None:
            await self._graceful_finish_step(task_id, step_id, messages)
            # 步骤已 completed → 仍需 Monitor 编排（2026-08-15 实证：此前直接 return
            # 跳过编排 → graceful 收尾的步骤（c942812e step-4）无 monitor 记录，
            # 前端 monitor 页面空白；正常 submit 路径 L885-887 已编排）
            if not skip_monitor:
                await self._insert_monitor_step(task_id, step_id)
            return
        messages = compressed

        logger.info(f"[DC:orch] start step={step_id} tier={model_tier} gate={is_gate} existing={has_existing}")

        try:
            result = await self._call_llm(
                task_id, step_id, model_tier, messages,
                # 2026-08-21（用户定论）：类型决定工具集——plan 步骤可写（产出
                # 计划文档，即使误带 human_attention=gate 也按 plan 处理，否则
                # planner 提示词配只读工具集会空转——DB 实证 step-14 487 条消息）；
                # gate 审批步骤（type=gate 或 executor+gate）只读：整理给人类看
                # 的审批摘要走 step_done summary 在 UI 展示，不落文件。
                (self._get_monitor_tools() if is_review_kind
                 else self._get_reverse_tools() if is_reverse
                 else self._get_researcher_tools() if is_researcher
                 else self._get_gate_tools() if is_gate and _st_type != "plan"
                 else self._get_exec_tools()),
                self._cancel_event(task_id),
                empty_ok=is_review_kind,
                # 2026-08-20：完成语义——executor 必须 dcflow_step_done 显式完成
                # （text_finish=False，纯文本轮 nudge 引导）；gate（审查文本即完成，
                # 保持 active 等审批）与 review/report（最终审查/报告是文本产出）用
                # text_finish=True——否则 mock/真实模型的纯文本收尾轮被误判未完成 →
                # 无限轮死循环
                # 2026-08-27（用户设计定案：monitor 只有两种收尾——dcflow_step_done
                # 或 adjust_flow(no_change)）：monitor-* 改 False——纯文本不完成
                # （注入确认引导重试，直到显式收尾）；裸 JSON 兜底解析为工具轮执行
                # 后继续轮。极端情况（AI 顽固纯文本）由上下文压缩优雅收尾兜底。
                # monitor 调 no_change 即收尾（_call_llm 收敛：1 次 no_change →
                # 结束轮次 → 此处按 completed 收尾，不再引导 step_done）
                text_finish=(is_gate
                             or (is_review_kind and not step_id.startswith("monitor-"))),
                give_up_check=not is_gate and not is_review_kind,
                # 2026-08-24：gate 步骤 step_done 强制报告校验（未输出方案审批报告
                # 则注入提示重试——见 _call_llm step_done 分支）
                is_gate=is_gate)
            logger.info(f"[DC:orch] done step={step_id} text={len(result.get('text', ''))}ch "
                        f"toolCalls={len(result.get('toolCalls', []))}")
            self._publish(task_id, "streamEnd", {"stepId": step_id})
            # 2026-08-27：流结束 → DB 已落库，清空 live 快照（后续靠 getStep 全量渲染）
            self._clear_step_live(task_id, step_id)

            if self._is_cancelled(task_id):
                # force_inject 已由 _call_llm 内部处理（清除 event 后继续），
                # 此处仅 stop/immediate 会到达
                await self._sm.advance_step(task_id, step_id, "stopped")
                return

            # 2026-08-24（DB 实证 99248a9f rebuild 后 monitor 无反应）：monitor
            # 空响应（无文本无工具）+ 本轮消费过用户介入消息 → 不静默 completed——
            # 用户消息必须得到回应，否则 UI 无任何更新、后续步骤照常重跑
            # （用户看到"发消息没反应、后面步骤又开跑"）
            # 2026-08-27（monitor 收尾收紧后）：该分支移到 empty 判定之前——收紧后
            # monitor 空响应也判 empty（_call_llm L2308），若先走通用 empty 分支会
            # 丢失「用户消息尚未处理」的专属语义（介入场景必须优先）
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
                self._clear_step_live(task_id, step_id)
                self._cancelled[task_id] = True
                return

            if result.get("empty"):
                # 完成闸门：AI 未输出任何结论（空响应重试 / 纯文本引导用尽后仍无文本）
                # → 不提交步骤，置 stopped + llmError 提示，等人工 resume（H6 循环终止语义复用）。
                # 工具轮无上限——工具链不会因轮次耗尽被截断，不在此判定。
                # monitor 空响应（无介入）2026-08-27 收紧后也走此分支（未显式收尾 = 未完成）
                text = result.get("text") or ""
                if text.strip():
                    # 输出文本但从未显式调用完成工具（引导/轮次用尽）→ 不提交，等人工
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
                self._clear_step_live(task_id, step_id)
                logger.warning(f"[DC:orch] empty response for step={step_id} — stopped, not completed")
                self._cancelled[task_id] = True
                return

            if is_gate:
                # V5：gate 审查完默认不 submit、保持 active，等待人工审批（H11 pause 由执行循环做）。
                # 但若人类已拍板（对话中出现「✅ 已确认人类决策」——gate-instruction 硬性格式，
                # 用户在详情页选择选项/输入决策后重新执行时输出）→ 直接提交完成步骤，
                # 交给编排者继续，不再等待审批（否则流程表现为再次暂停无反应）。
                # 注意：确认文本可能在 step_done 工具轮（_call_llm 工具轮清空 full_content，
                # result.text 拿不到）→ 从已落库的消息流判定
                if await self._gate_confirmed_by_human(task_id, step_id):
                    logger.info(f"[DC:orch] gate {step_id} confirmed by human — submitting and continuing")
                    await self._submit_step(task_id, step_id)
                    self._publish_full_conversation(task_id, step_id, system_prompt, step_title, result)
                    if not skip_monitor:
                        await self._insert_monitor_step(task_id, step_id)
                    return
                self._publish_full_conversation(task_id, step_id, system_prompt, step_title, result)
                return

            if is_review_kind:
                # 2026-08-21 实体化：审查/收尾步骤完成——不 dcflow_step_done 提交，
                # 直接 completed + 保存对话 artifact（key=步骤 id）；monitor-init 完成
                # 自动插 review+report；report 完成 complete_task（收尾链）
                await self._sm.advance_step(task_id, step_id, "completed")
                self._publish_full_conversation(task_id, step_id, system_prompt, step_title, result)
                from .. import rest_api as _rest_api
                # 快照 = DB 完整消息（system 提示词 + 用户消息 + 全部 assistant/tool 轮）
                # ——rebuild 重跑后快照此前只含本轮输出，历史对话丢失；llmError 等失败
                # system 消息不保留在快照（前端展示噪音）
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
                    # 兜底：DB 读不到（异常）→ 仅 system + 本轮 result
                    conversation = [{"role": "system", "content": system_prompt}] \
                        + self._result_to_conversation(result)
                try:
                    await _rest_api.monitor_save_conversation({
                        "task_id": task_id,
                        "trigger_step_id": save_trigger_step or step_id,
                        "conversation": conversation}, storage=self._storage)
                except Exception as e:
                    logger.warning(f"[DC:orch] save monitor conversation failed for {step_id}: {e}")
                # 收尾链（用户决策）：monitor-init 完成 → 系统自动插 review+report；
                # report 完成 → complete_task（唯一收尾出口）
                if step_id == "monitor-init":
                    await self._ensure_tail_steps(task_id)
                elif step_id == "report":
                    await self._sm.complete_task(task_id)
                return

            # 提交步骤结果
            await self._submit_step(task_id, step_id)

            # 确认步骤是否真的 completed（介入/异常可能使 submit 未生效）
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
            # 上下文超限优雅收尾（_call_llm 工具轮内增长触发）：步骤已 completed，
            # 仍需 Monitor 审查步骤（2026-08-15 实证：跳过会导致该步骤无 monitor 记录）
            if not skip_monitor:
                await self._insert_monitor_step(task_id, step_id)
            return

        except _StepGracefulDrain:
            # 优雅重启排空：步骤置回 pending（可重试），不提交、不标记错误——
            # 执行循环顶部的 draining 检查随后退出（重启后自动恢复）
            # 2026-08-21（19:53 竞态）：外部（暂停/停止/恢复）可能已把步骤从
            # active 改为其他状态——宽容化：仅当仍为 active 时才置回 pending
            if await self._step_is_active(task_id, step_id):
                logger.info(f"[DC:orch] step={step_id} drained — active → pending")
                await self._sm.advance_step(task_id, step_id, "pending")
            else:
                logger.info(f"[DC:orch] step={step_id} drain skipped — external already changed state")
            return

        except (LlmError, LlmAborted) as e:
            if self._is_cancelled(task_id):
                # 用户打断（stop/immediate）：状态机已置 stopped（幂等），直接退出。
                # 2026-08-21（19:53 崩溃实证）：外部（如暂停后执行循环 paused 分支把
                # 步骤重置 pending）可能已改状态——advance(stopped) 会非法转移崩溃，
                # 宽容化：仅当仍为 active 时才置 stopped，否则跳过（外部已处理）
                await self._safe_mark_stopped(task_id, step_id)
                return

            # llmError：步骤 stopped + 错误消息 + llmError 事件 + 循环终止（H6）
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
            self._clear_step_live(task_id, step_id)
            logger.warning(f"[DC:orch] llmError step={step_id} code={info['code']} "
                           f"retryable={info['retryable']} retryCount={retry_count}: {err_msg}")
            self._cancelled[task_id] = True  # H6：llmError 循环终止

    # ═══════════════════════════════════════════════════════════════
    # LLM 调用（旧 _callVscodeLm：10 轮工具上限 + 介入注入 + 压缩 + 429 重试）
    # ═══════════════════════════════════════════════════════════════

    async def _call_llm(self, task_id: str, step_id: str, tier: str, msgs: list[dict],
                        tools: list[dict], cancel_event: asyncio.Event,
                        empty_ok: bool = False, text_finish: bool = True,
                        give_up_check: bool = False, is_gate: bool = False) -> dict:
        """LLM 多轮调用循环。返回 {text, toolCalls, rounds, empty}（与旧 _callVscodeLm 同形）。
        empty_ok=True（Monitor/_final_review）：空响应仅日志警告，不重试、不标记 empty。
        text_finish=False（执行步骤）：纯文本不是完成信号——必须 dcflow_step_done 显式
        完成；纯文本轮注入确认引导（≤3 次），引导用尽后不再注入也绝不提交，继续轮次等
        显式完成。工具轮无上限（用户决策），退出只依赖显式完成 / 纯文本引导用尽 /
        空响应预算耗尽 / 上下文压缩压无可压（优雅收尾）。"""
        full_content = ""
        recorded_tool_calls: list[dict] = []
        rounds: list[dict] = []
        retry_count = 0
        # 防放弃提醒状态（2026-08-16 用户需求）：连续出现丧气话时最多提醒一次——
        # 提醒后 AI 有实质进展（工具调用/正常文本轮）→ 解除冷却，再次丧气话可再提醒
        give_up_reminded = False
        give_up_progress = False
        # 尽力模式（2026-08-16 用户需求扩展）：best_effort 任务下——① 防放弃提醒放开
        # （AI 有实质进展后再说"穷尽"仍提醒）；② step_done 前目标核对（仅注入一次，
        # 防止"一条路径穷尽就收尾"）。仅 executor 步骤（give_up_check=True）生效。
        try:
            _task_row = await self._storage.get_task(task_id)
            best_effort = bool(_task_row and _task_row.get("best_effort"))
        except Exception:  # noqa: BLE001
            best_effort = False
        done_checked = False          # step_done 目标核对已注入标记（防循环）
        # 2026-08-24（DB 实证 10092ff1 step-12）：gate 步骤未输出方案审批报告就
        # step_done 的提示注入计数（最多 2 次，跨轮累计；限流重试 continue 不重置）
        gate_report_nudges = 0
        # 50 轮报告提醒计数：从 DB 恢复（跨重启持久，2026-08-16 用户修正——
        # 重启清零会导致长步骤永远等不到报告提醒）；新会话已在 _execute_step 清零
        _round = await self._get_step_report_round(task_id, step_id)
        pending_chunks: list[dict] = []  # 流式 chunk 累积（批量落盘，避免逐条 commit 拖慢 UI）
        empty_retries = 0
        text_nudges = 0                # 纯文本轮确认引导已用次数（text_finish=False）
        done_via_tool = False          # 本轮已调用 dcflow_step_done（显式完成标记）

        # 优雅重启排空检查：draining 时立即终止（不等 LLM 自然结束——AI 收到
        # 拒绝消息会一直重试 run_cmd，导致步骤永不结束、循环不退、重启卡住）
        if graceful.is_draining():
            raise _StepGracefulDrain(step_id)

        async def _maybe_retry_empty() -> bool:
            """空响应（无最终文本）处理：真空响应（无文本且无工具调用）→ 注入
            引导消息自动重试，最多 3 次。返回 True 表示已处理（调用方 continue）。
            （工具轮无上限——上轮执行了工具但本轮无文本不是空响应，直接继续轮次。）"""
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

        # 2026-08-21：审查/收尾步骤收敛——AI 反复 list_steps + adjust_flow(no_change)
        # 无实质变更时无限轮次（DB 实证 e726f3e6 06:16：monitor-step-11 每 10-15s
        # 一轮 no_change 死循环）；连续 2 次 no_change → 结束轮次（_execute_step
        # 按 completed 收尾；有实质 action 则重置计数）
        # 2026-08-27（用户设计定案）：monitor-* 1 次 no_change 即收尾（见下方收敛）
        no_change_cnt = 0
        review_kind = self._step_is_review_kind(step_id, "")

        while True:
            # 2026-08-21（DB 实证 e726f3e6 19:07）：任务已进入终态（如收尾步骤
            # 调 mark_complete）→ 停止当前步骤轮次——此前任务 completed 后 report
            # 步骤继续空转 3+ 分钟，AI 乱调 adjust_flow（remove/add/skip/
            # mark_complete）把已完成任务的流程状态再次改乱
            _t_check = await self._storage.get_task(task_id)
            # 2026-08-24（DB 实证 99248a9f）：report 是收尾链唯一交付步骤——review
            # 提前 mark_complete 后 task completed，report 被拾取时不得空收尾（无
            # LLM、无产出）；report 必须正常执行产出最终报告（is_review_kind
            # 完成路径 L1353 幂等 complete_task）
            if step_id != "report" and (not _t_check
                                        or _t_check.get("status") in ("completed", "abandoned")):
                logger.info(f"[DC:orch] task {task_id} ended (status="
                            f"{_t_check.get('status') if _t_check else 'gone'}) during "
                            f"step {step_id} — graceful finish")
                await self._graceful_finish_step(task_id, step_id, msgs,
                                                 reason="task_ended")
                raise _StepGracefulFinish(step_id)
            _round += 1
            # 每轮落盘轮数（跨重启持久；失败仅告警——丢了本轮计数不影响正确性）
            try:
                await self._save_step_report_round(task_id, step_id, _round)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DC:orch] step report round save failed step={step_id}: {e}")
            # 每 50 轮报告提醒（用户决策）：停下合并已尝试/碰壁/成功/结论到步骤报告，
            # 再继续（落盘 round_num=-1，与 nudge 同构；幂等：50 的倍数）。
            # 2026-08-21：审查/收尾步骤（monitor-*/review/report）不发——它们是
            # 只读节点，不应去改流程报告；提示只面向普通执行/plan 步骤（用户反馈）
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
            # 每轮发送前：最近一次 API usage.prompt_tokens > 400K → 压缩到 ≤200K
            # （同一 step 内跨轮增长也能触发；resume 首轮由 _execute_step 开头兜底）。
            # None = 压到 20 轮底线仍超 200K → 优雅收尾
            last_tokens = await self._get_last_prompt_tokens(task_id, step_id)
            compressed = await self._check_and_compress(task_id, step_id, msgs, last_tokens)
            if compressed is None:
                await self._graceful_finish_step(task_id, step_id, msgs)
                raise _StepGracefulFinish(step_id)
            msgs = compressed
            # 检查介入消息（💬发送 / ⛔强制插入）→ 注入为 user 消息
            await self._inject_interventions(task_id, step_id, msgs)

            client = self._make_llm_client(tier)
            logger.info(f"[DC:step] step={step_id} tier={tier} model={client._model} msgs={len(msgs)}")  # N9

            round_text = ""
            round_reasoning = ""  # 仅用于关键发现捕获，不落盘（保持 reasoning 不落盘原则）
            round_usage = {"prompt": 0, "cached": 0, "completion": 0}  # Token 展示：本轮用量
            round_tool_slots: list[dict] = []
            # 轮统计（2026-08-21 落库）：本轮请求开始/首字/最后 chunk（monotonic）
            t_req = time.monotonic()
            first_chunk_t: Optional[float] = None
            last_chunk_t: Optional[float] = None
            # V-18：工具参数流式（index → {id, name, buf, started}）；每轮重置
            _delta_calls: dict = {}
            try:
                async for ev in client.stream_chat(msgs, tools, signal=cancel_event):
                    if ev["type"] == "text":
                        text = ev["text"]
                        full_content += text
                        round_text += text
                        # 轮统计：首字时间 + 最后 chunk 时间（纯 API 输出时长）
                        _t_now = time.monotonic()
                        if first_chunk_t is None:
                            first_chunk_t = _t_now
                        last_chunk_t = _t_now
                        self._emit_chunk(task_id, step_id, text)
                        pending_chunks.append({"chunk_type": "text", "content": text})
                    elif ev["type"] == "reasoning":
                        # 2026-08-22（DB 实证 0269b09a step-1）：thinking 模型
                        # 工具轮无文本输出（39 轮全部 reasoning+工具），首字/输出
                        # 时长计时只认 text → 落库全 0 → 输出速度/首字延迟显示 --。
                        # 思考首块也算模型开始输出，纳入首字计时
                        _t_now = time.monotonic()
                        if first_chunk_t is None:
                            first_chunk_t = _t_now
                        last_chunk_t = _t_now
                        # 思考过程：仅 SSE 流式推送（thinkingChunk）+ 累积供关键发现捕获，不落盘、不参与文本累计
                        round_reasoning += ev["text"]
                        seq_think = self._publish(task_id, "thinkingChunk",
                                                  {"stepId": step_id, "chunk": ev["text"]})
                        # 2026-08-27：思考分片累积进 live 快照（详情页首屏可渲染）
                        self._update_step_live(task_id, step_id, seq=seq_think,
                                               thinking=ev["text"], streaming=True)
                    elif ev["type"] == "tool_call_delta":
                        # 工具参数流式（V-18）：delta 逐片到达 → 首个 delta 发布 toolCallStart
                        # （input 空，前端插卡），后续 delta 发布 toolCallParam（前端逐字渲染）。
                        # id/name 通常先于 arguments 就绪（OpenAI delta 顺序）；未就绪时积压 buf，
                        # 就绪后补发（真实场景基本不走）。工具执行前的 toolCallStart（带完整 input）
                        # 保留作兜底，前端按 callId 去重不覆盖。
                        # 2026-08-24（输出时长纯吐字口径）：工具参数吐字计入输出时长——
                        # 每 delta 更新 last_chunk_t（reasoning_only 轮 last 可到工具参数）
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
                                seq_ts = self._publish(task_id, "toolCallStart", {
                                    "stepId": step_id, "callId": slot["id"],
                                    "toolName": slot["name"], "input": ""})
                                # 2026-08-27：参数流式开始 → 工具卡进 live 快照
                                self._update_step_live(
                                    task_id, step_id, seq=seq_ts,
                                    tool={"callId": slot["id"], "name": slot["name"], "input": ""})
                                for d in slot["buf"]:
                                    seq_tp = self._publish(task_id, "toolCallParam", {
                                        "stepId": step_id, "callId": slot["id"], "delta": d})
                                    self._append_step_live_tool_param(
                                        task_id, step_id, slot["id"], d, seq=seq_tp)
                                slot["buf"] = []
                            if ev["delta"]:
                                seq_tp = self._publish(task_id, "toolCallParam", {
                                    "stepId": step_id, "callId": slot["id"], "delta": ev["delta"]})
                                self._append_step_live_tool_param(
                                    task_id, step_id, slot["id"], ev["delta"], seq=seq_tp)
                        elif ev["delta"]:
                            slot["buf"].append(ev["delta"])
                        _delta_calls[idx] = slot
                    elif ev["type"] == "usage":
                        # Token 展示：流末尾 usage chunk（每轮一次）。累计后立即落库——
                        # 与消息落盘同步（resume 重放不产生 usage 事件，天然幂等不重复计费）
                        round_usage["prompt"] += ev["prompt"]
                        round_usage["cached"] += ev["cached"]
                        round_usage["completion"] += ev["completion"]
                        await self._add_step_tokens(task_id, step_id, ev["prompt"],
                                                    ev["cached"], ev["completion"])
                    else:
                        # 2026-08-24（输出时长纯吐字口径）：完整工具槽参数吐字计入
                        # 输出时长（reasoning_only 轮 last 可到工具参数，保证 out > 0）
                        last_chunk_t = time.monotonic()
                        round_tool_slots.append(ev)
                # 2026-08-23（用户反馈）：本轮流正常结束 → 限流重试计数归零——
                # 否则限流恢复后下一轮再次限流从上次计数继续（6/10→7/10），
                # 应每轮从 1/10 重新开始（retry_count 定义在 while 外，跨轮累加）
                retry_count = 0
            except LlmAborted:
                # 取消：force_inject 中断当前流后继续（下一轮注入介入消息）；
                # stop/immediate 向上抛，由 _execute_step 置 stopped
                if self._cancel_kind.get(task_id) == "force_inject":
                    logger.info(f"[DC:ai] force_inject abort at round {_round}, step={step_id}, continuing")
                    cancel_event.clear()
                    # force_inject 不终止步骤：清除取消标志，下一轮注入后继续
                    self._cancelled[task_id] = False
                    self._cancel_kind[task_id] = None
                    continue
                raise
            except LlmError as e:
                # 先记录真实错误内容（400 取证：message 含具体原因，如上下文超限/
                # 消息序列非法/参数错误——2026-08-15 重启后 345K 被 400 但窗口 1M，
                # 需日志确认真实原因）
                logger.warning(f"[DC:orch] LLM error step={step_id} round={_round} "
                               f"status={getattr(e, 'status', '?')} msg={getattr(e, 'message', e)!r}")
                if _is_context_exceeded(e):
                    # 上下文超限（服务端裁决，已证明 >1M）：先按 800K 预算压缩
                    # （tiktoken 判定）重试；判已 ≤800K 仍 400 → 计数低估实际
                    # tokenizer → 激进压缩到 20 轮底线重试；压无可压才收尾。
                    # 压缩点同步记录（重启重建时应用，上下文与此处一致）
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
                    # 2026-08-21：用户打断（stop/immediate 介入）时不再重试——
                    # 此前重试循环不响应 cancel_event，旧执行循环卡在 LLM 重试
                    # （30s 等待 + 120s 空闲超时 ×N），start_task 的 zombie 等待
                    # （180s）超时后放弃重启 → 介入的 monitor 步骤永远不被拾取，
                    # 流程表现为"没反应"（DB 实证 e726f3e6 07:01 介入后
                    # monitor-intervene 卡 pending、第二个眼睛无内容）
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
                    # 可中断等待：分段 1s 检查取消（打断后不再死等 30s）
                    for _ in range(int(delay / 1000)):
                        if self._is_cancelled(task_id):
                            logger.info(f"[DC:ai] cancelled during retry wait for "
                                        f"step={step_id} — aborting")
                            raise e
                        await asyncio.sleep(1.0)
                    continue  # 重试本轮
                e.retry_count = retry_count  # type: ignore[attr-defined]
                raise

            # Token 兜底（真实流式 API 无 usage chunk，如 DeepSeek）：tiktoken 估算
            # 输入=发送 msgs、输出=本轮文本+工具参数。仅累加展示列（record_last=False
            # 不写 last_prompt_tokens——估算会低估实际 tokenizer，压缩判断仍走
            # last_tokens=None 实测兜底，不被低估的估算值污染）
            if not round_usage["prompt"] and not round_usage["completion"]:
                est_prompt = _count_tokens(msgs)
                if est_prompt is not None:
                    # 2026-08-22：思考吐字、工具参数吐字均算输出速度——估算
                    # completion 含 reasoning（此前只含文本+工具参数，thinking
                    # 模型无文本轮 completion 低估/偏 0）
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

            # 文本解析兜底：模型不支持原生 tool calling 时，解析 JSON {"tool":...} 输出
            # （2026-08-27：+ 裸 {"action":...} dcflow_adjust_flow JSON——orchestrator.md
            # 教的输出格式，task_id 由调用点注入）
            if not round_tool_slots and round_text:
                parsed = _parse_text_tool_calls(round_text, task_id)
                if parsed:
                    logger.info(f"[DC:tool] text-parsed {len(parsed)}: "
                                f"{', '.join(p['name'] for p in parsed)}")
                    for p in parsed:
                        round_tool_slots.append({
                            "id": p["call_id"], "name": p["name"],
                            "arguments": json.dumps(p["input"], ensure_ascii=False)})
                    round_text = _TEXT_TOOL_CALL_RE.sub("", round_text).strip()
                    # 裸 JSON 移除（完整对象——嵌套 steps_json 内的 } 不截断）
                    while True:
                        _m = _NAKED_ADJUST_RE.search(round_text)
                        if not _m:
                            break
                        _end = _extract_balanced_json(round_text, _m.start())
                        if _end < 0:
                            break
                        round_text = round_text[:_m.start()] + round_text[_end:]
                    round_text = round_text.strip()

            # 轮统计落库（2026-08-21 用户需求：统计与 token 明细同表同位置，步骤级）：
            # 每轮 LLM 流正常结束 = 一次大模型请求；时序 = 本轮 API 调用开始 → 流结束
            # （含首字等待 TTFT——卡顿后一次性吐字时按"首字→最后 chunk"会虚高，
            # 用户反馈 2026-08-21 改为 API 总耗时）；run_ms = 本轮结束 - 步骤开始（定格值）
            # 2026-08-24（用户反馈：输出速度虚低）：输出时长改为纯吐字 = 最后 chunk -
            # 首字（文本/思考/工具参数吐字），排除首字等待 TTFT/限流/工具等待——
            # 工具轮多的步骤每轮 TTFT 累积导致速度比预期慢很多；首字延迟另有独立指标
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
                # 检查是否被取消（⏹打断 / 🛑流程介入）
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

                # 优雅重启排空检查（工具执行前）：draining 时立即中断本轮执行
                if graceful.is_draining():
                    logger.info(f"[DC:orch] graceful drain during tool execution, step={step_id}")
                    raise _StepGracefulDrain(step_id)

                # 工具轮 = 实质进展（防放弃提醒冷却解除）
                give_up_progress = True

                logger.info(f"[DC:tool] executing {len(round_tool_slots)} tool(s): "
                            f"{', '.join(tc['name'] for tc in round_tool_slots)}")
                # V-18：工具轮前不再发 streamEnd（会清空前端流式文本缓存导致"突然蹦出"）；
                # 改发 toolExecuting 供前端显示「正在执行工具」状态。streamEnd 保留在步骤
                # 完成路径（_execute_step）做最终全量重拉。
                seq_te = self._publish(task_id, "toolExecuting", {
                    "stepId": step_id,
                    "callIds": [tc["id"] for tc in round_tool_slots],
                    "toolNames": [tc["name"] for tc in round_tool_slots]})
                # 2026-08-27：工具开始执行 → streaming=False（占位消失，工具卡 spinner）
                if round_tool_slots:
                    self._update_step_live(task_id, step_id, seq=seq_te, streaming=False)
                if round_text.strip():
                    rounds.append({"text": round_text, "toolCalls": []})
                    full_content = ""

                # 构建 Assistant 消息（含 tool call parts）
                # 2026-08-22：thinking 段真实回传——工具调用轮必须带 reasoning_content
                # （此前空串占位仅过 400 校验「reasoning_content must be passed back」）。
                # DeepSeek 官方要求工具轮回传真实 reasoning（否则 400）、OpenAI 官方
                # 建议"回传工具轮 reasoning 让模型以最 token 高效方式继续推理过程"、
                # DeepSeek-V3.2 论文"推理状态贯穿多步轨迹"——空串 = 每轮从头推理。
                # 跨请求/重启重建（_build_lm_messages）保持空串：reasoning 不落盘
                # （对齐 Reasonix「临时推理不污染上下文」+ 官方用户消息边界丢弃）
                assist_msg: dict = {"role": "assistant", "content": round_text,
                                    "reasoning_content": round_reasoning}
                assist_msg["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in round_tool_slots
                ]
                msgs.append(assist_msg)

                # 执行工具并构建 tool result parts
                round_tcs: list[dict] = []
                for tc in round_tool_slots:
                    seq_ts = self._publish(task_id, "toolCallStart", {
                        "stepId": step_id, "callId": tc["id"], "toolName": tc["name"],
                        "input": tc["arguments"]})
                    # 2026-08-27：兜底 toolCallStart（完整 input）→ 覆盖快照工具卡
                    # （流式路径 input 已累积时整体替换为完整参数）
                    self._update_step_live(
                        task_id, step_id, seq=seq_ts,
                        tool={"callId": tc["id"], "name": tc["name"],
                              "input": tc["arguments"] or ""})
                    pending_chunks.append({
                        "chunk_type": "tool_call_start", "content": tc["name"], "call_id": tc["id"]})
                    output_text = await self._invoke_tool(task_id, step_id, tc["name"], tc["arguments"], tc["id"])
                    # 2026-08-21 收敛：审查/收尾步骤 adjust_flow 无实质变更
                    # （no_change，无 action 参数时后端默认 no_change）计数；
                    # 有实质 action（add/remove/reorder 等）重置计数
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
                    seq_res = self._publish(task_id, "toolCallResult", {
                        "stepId": step_id, "callId": tc["id"], "toolName": tc["name"],
                        "output": output_text[:5000]})
                    # 2026-08-27：工具结果到达 → 快照清工具 + 恢复 streaming=True
                    # （与前端 handleToolResult 对齐：工具执行完立即显示「AI 正在思考」
                    # 等待下一轮首字——否则工具完成后的等待期重新进入页面无任何渲染）
                    self._update_step_live(task_id, step_id, seq=seq_res, tool=None,
                                            streaming=True)
                    # 2026-08-27（用户反馈：刷新丢已完成工具卡）：已完成工具记录到
                    # completed_tools——多工具轮中消息要等整轮结束才落库，窗口内刷新
                    # 时 DB 无该工具消息，快照必须保留（整轮落库后 _clear 清空）
                    self._append_step_live_completed(
                        task_id, step_id, tc["id"], tc["name"],
                        tc.get("arguments") or "", output_text[:5000])
                    pending_chunks.append({
                        "chunk_type": "tool_call_result", "content": output_text, "call_id": tc["id"]})
                    msgs.append({"role": "tool", "content": output_text, "tool_call_id": tc["id"]})

                rounds.append({"text": "", "toolCalls": round_tcs})

                # 逐条追加消息到 DB
                await self._append_round_messages(task_id, step_id, rounds)
                # 批量落盘本轮流式 chunk（单事务；逐条 commit 会阻塞流式循环）
                await self._save_chunks(task_id, step_id, pending_chunks)
                pending_chunks.clear()
                # 2026-08-27：整轮落库完成 → 清空 completed_tools（DB 已可渲染，
                # 防后续刷新重复渲染 live 卡 + DB 卡）
                self._clear_step_live_completed(task_id, step_id)
                # 2026-08-27：整轮落库完成 → 清空 text/thinking（已落库轮文本 DB
                # 已可渲染；不清则跨轮拼接，刷新时 initLive 重复渲染「一坨」）
                self._clear_step_live_round(task_id, step_id)

                # 关键发现捕获（工具轮：该轮文本 + 思考）
                await self._capture_key_findings(task_id, step_id,
                                                 round_text + "\n" + round_reasoning)

                # 2026-08-21 收敛：审查/收尾步骤连续 2 次 no_change → 结束轮次
                # （_execute_step 按 completed 收尾，不等待显式完成工具——审查/收尾
                # 步骤无 step_done，AI 反复核对时会无限轮次，见函数开头注释；放落库
                # 之后保证工具轮消息/产物已保存）
                # 2026-08-27（用户设计定案：monitor 只有两种收尾——dcflow_step_done
                # 或 adjust_flow(no_change)；DB 实证 5b2519ef monitor-18 08:19）：
                # monitor-* 的 no_change 即收尾——1 次即结束轮次（不再 nudge 引导
                # step_done——否则真实 LLM 多绕 3 轮：no_change → nudge → 错误
                # mark_complete → 错误 step_id 才收尾，耗时 4.4 分钟）；
                # review/report 保持 2 次（8.21 防死循环防护）
                if no_change_cnt >= (1 if step_id.startswith("monitor-") else 2):
                    logger.info(f"[DC:orch] review step {step_id} converged "
                                f"({no_change_cnt}× no_change) — ending rounds")
                    # 收敛也是显式收尾（no_change 是 monitor 两种收尾之一）——必须标记
                    # done_via_tool，否则 text_finish=False（monitor 收紧后）的 empty
                    # 判定（L2318：not text_finish and not done_via_tool）把工具轮误判
                    # 为空响应 → stopped（2026-08-27 实证 5b2519ef monitor 场景）
                    done_via_tool = True
                    break

                # 显式完成：模型调用了 dcflow_step_done（summary 已存 artifact）→
                # 终止循环，不再等后续轮次（完成语义：只有此工具才算步骤完成）
                if any(tc.get("name") == "dcflow_step_done" for tc in round_tool_slots):
                    # 2026-08-24（DB 实证 10092ff1 step-12）：gate 步骤未输出方案审批
                    # 报告就 step_done → 页面停在"人工审批"但无报告可看。强制先输出
                    # 完整报告（含尾部 JSON options）或人类决策确认才能完成；最多提醒
                    # 2 次（AI 顽固不输出则放行，停在审批等人工，对话有提示记录）
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
                    # 尽力模式 step_done 目标核对（2026-08-16 用户需求扩展）：executor
                    # 请求完成但步骤目标可能未达（如 desc 列了多条路径只走了一条）——
                    # 注入核对消息（仅一次），继续轮次；AI 核对后再调 step_done 才完成
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

                # 惰性压缩检查（触发 = 本轮 API usage >400K；目标 = 总 token ≤800K）。
                # None = 压到 20 轮底线仍超 900K → 优雅收尾
                compressed = await self._check_and_compress(task_id, step_id, msgs, round_usage["prompt"])
                if compressed is None:
                    await self._graceful_finish_step(task_id, step_id, msgs)
                    raise _StepGracefulFinish(step_id)
                if compressed is not msgs:
                    msgs[:] = compressed
                    logger.info(f"[DC:ai] compressed to {len(msgs)} messages after round {_round}")
                continue

            # 无 tool_calls：文本轮。空响应（无最终文本）→ 引导重试
            # 纯文本轮同样捕获关键发现（此前只在工具轮/最终轮捕获——纯文本轮的
            # "关键发现：xxx"结论丢失，AI 在长会话中反复重识，DB 实证 kctf4/KCTF2）
            if round_text.strip() or round_reasoning.strip():
                await self._capture_key_findings(task_id, step_id,
                                                 round_text + "\n" + round_reasoning)
            # 防放弃提醒（2026-08-16 用户需求）：AI 输出丧气话（穷尽/无法/诚实交付/标记完成
            # 等，DB 实证模式）→ 以用户口吻提醒看报告找突破口。频率 = 连续出现时最多一次：
            # 提醒后 AI 有实质进展（工具调用 / 非信号文本轮）→ 解除冷却再提醒；仍连续丧气
            # 无进展 → 不重复骚扰。gate 步骤不提醒（决策包整理是本职，尽力模式自动放行）
            if give_up_check and round_text.strip() and GIVE_UP_PATTERN.search(round_text) and (
                    not give_up_reminded or give_up_progress or best_effort):
                # 尽力模式：有实质进展后再次丧气仍提醒（普通模式不重复骚扰）
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
                # 用户口吻渲染：userMessage SSE + 内联标记（与介入消息同链路，
                # 前端显示为用户气泡而非系统消息——系统检测式口吻会被 AI 忽略）
                self._publish(task_id, "userMessage", {"stepId": step_id, "message": give_up_msg})
                self._emit_chunk(task_id, step_id,
                                 "__DC_USER_MSG__" + json.dumps(
                                     {"stepId": step_id, "content": give_up_msg},
                                     ensure_ascii=False) + "__DC_USER_MSG__")
                logger.info(f"[DC:orch] give-up nudge for step={step_id} at round {_round}")
            elif round_text.strip():
                # 正常文本轮（未命中信号）= 实质进展 → 解除提醒冷却
                give_up_progress = True
            if await _maybe_retry_empty():
                continue
            if not full_content.strip():
                # 空响应预算耗尽（nudge×3 后模型仍无任何输出）→ 停止语义（empty），
                # 不进文本确认引导（引导只针对"有文本但未调完成工具"的轮次）
                break
            if text_finish:
                break
            # 执行步骤（text_finish=False）：纯文本不是完成信号——步骤只能通过
            # dcflow_step_done 显式完成。注入确认引导：模型可继续调工具 / 调完成
            # 工具；连续纯文本只提示一次（用户决策 2026-08-15：可提示但不要停止，
            # 重复提示无意义），之后不再注入也不停止——继续轮次等显式完成（工具轮
            # 无上限，上下文压缩兜底 graceful）。
            if text_nudges < self.MAX_TEXT_FINISH_NUDGE:
                text_nudges += 1
                msgs.append({"role": "user", "content": _TEXT_FINISH_NUDGE})
                await self._append_message(task_id, step_id,
                                           {"role": "user", "content": _TEXT_FINISH_NUDGE,
                                            "round_num": -1})
                logger.info(f"[DC:orch] text without step_done for step={step_id} — "
                            f"confirm nudge {text_nudges}/{self.MAX_TEXT_FINISH_NUDGE}")
            continue  # 不停止、不再提示，继续轮次（AI 可继续调工具/输出文本；压缩兜底）

        # Save final round text (no tool calls)
        if full_content.strip():
            rounds.append({"text": full_content, "toolCalls": []})
            await self._append_message(task_id, step_id,
                                       {"role": "assistant", "content": full_content,
                                        "round_num": len(rounds) - 1})
        # 批量落盘剩余流式 chunk（最终文本轮）
        await self._save_chunks(task_id, step_id, pending_chunks)
        pending_chunks.clear()

        # 关键发现捕获（最终文本轮：全量文本 + 最后思考）
        await self._capture_key_findings(task_id, step_id,
                                         full_content + "\n" + round_reasoning)

        # 完成语义（执行步骤 text_finish=False）：必须显式 dcflow_step_done 才算完成——
        # 任何退出路径（引导用尽继续后轮次耗尽 / 空响应预算耗尽）只要未调完成工具
        # 都视为未完成（empty → _execute_step 置 stopped 等人工介入，绝不自动提交）
        if not text_finish and not done_via_tool:
            # 用户决策 2026-08-15：有文本但未显式调用完成工具 → 不再判 empty
            # （纯文本轮不再 break，仅真·空响应——_maybe_retry_empty 耗尽后仍无
            # 输出——判 empty → _execute_step 置 stopped 等人工，绝不自动提交）
            empty = not bool(full_content.strip())
        else:
            empty = not bool(full_content.strip()) and not empty_ok and not done_via_tool
        return {"text": full_content, "toolCalls": recorded_tool_calls,
                "rounds": rounds, "empty": empty}

    async def _save_intervention_message(self, task_id: str, step_id: str, message: str) -> None:
        """追加一条介入消息（force_inject 类型）到步骤 intervention artifact。
        尽力模式 gate 自动放行复用：gate 重置 pending 后 _read_interventions
        把它注入为 user 消息（AI 按"人类已给出明确决策"规则确认并 submit）。"""
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
        """步骤是否存在未消费的介入消息（pre_tool_injection / force_inject 类型）。
        gate 审查等待中据此判定是否需要重启步骤重新整理决策（_execution_loop 首轮校验）。"""
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
        """gate 步骤对话中是否已出现「已确认人类决策」标记（人类已拍板 → 直接完成步骤）。
        确认文本可能在 step_done 工具轮（_call_llm 工具轮清空 full_content，result.text 拿不到）
        → 从已落库的消息流判定；历史确认文本同样有效（再次执行只会再次确认）。"""
        try:
            msgs = await self._storage.get_step_messages(task_id, step_id, limit=20)
        except Exception:
            return False
        for m in msgs:
            if m.get("role") == "assistant" and "已确认人类决策" in str(m.get("content") or ""):
                return True
        return False

    async def _inject_interventions(self, task_id: str, step_id: str, msgs: list[dict]) -> None:
        """检查并注入步骤介入消息（💬发送 / ⛔强制插入，旧 _injectInterventions）。
        消费 {pre_tool_injection, force_inject}，注入一次即移除（N1/N2），其余类型保留。"""
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
            pass  # 介入注入失败不阻断执行

    async def _read_interventions(self, task_id: str, step_id: str) -> list[dict]:
        """恢复执行时读取介入消息 → 注入为 user 消息并清空（旧 _executeStep L352-372）。

        注入后发布 userMessage SSE 事件（与 _inject_interventions 一致）：否则 stopped
        步骤发送后恢复的场景，前端「待发送」气泡永远等不到清理事件（用户实证：
        消息已投递但 UI 卡待发送，刷新才正常）。
        """
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
                        # 前端清理「待发送」气泡的信号（同 _inject_interventions 的 userMessage）
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

    # ═══════════════════════════════════════════════════════════════
    # 介入消息（task 级 _flow intervention，flow_pending）
    # ═══════════════════════════════════════════════════════════════

    async def _consume_flow_intervention(self, task_id: str) -> Optional[str]:
        """读取 task 级 _flow intervention（flow_pending，B1 修订）→ 返回文案，注入后清空（保留其他类型）。"""
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

    # ═══════════════════════════════════════════════════════════════
    # 消息构建 / 错误分类 / 压缩（单测目标函数）
    # ═══════════════════════════════════════════════════════════════

    def _build_lm_messages(self, db_messages: list[dict],
                           compress_summaries: Optional[list[str]] = None) -> list[dict]:
        """把 DB step_messages 重建为 OpenAI messages（工具配对由 orchestrator 重建）。

        - 原生配对：assistant.tool_calls（JSON 字符串）与 tool.tool_call_id 直接使用；
        - 正则兜底：assistant content 内嵌 {"tool":"dcflow_x","arguments":{...}}（文本调用格式）
          解析为 tool_calls 写入该 assistant 消息；紧随的 tool 消息无 tool_call_id 时，
          从自身 content 或前一个 assistant 的 content 解析出 call_id 补上。
        - 压缩点应用（2026-08-15 用户决策）：compress_summaries 非空时，最早的
          len(summaries) 个工具轮（assistant+tool 对）被压缩——重建时在对应位置替换为
          摘要 user 消息，后续轮次原样（DB 全量保留，前端历史不变；重启后上下文
          与压缩时一致）。"""
        out: list[dict] = []
        skip_n = len(compress_summaries) if compress_summaries else 0
        tool_round_seen = 0
        skip_tool_tail = False  # 当前工具轮被摘要 → 紧随的 tool 消息跳过
        # 2026-08-26：plan.md 读取保护——db 历史前 window 条内读取 *.plan.md 的
        # 工具轮原样输出且不占用 skip_n 计数（与压缩侧 _protected_plan_read_hashes
        # 同一窗口语义：第一条 assistant/tool 起数 10 条，两侧判定一致防替换错位）
        win_end = len(db_messages)
        _hist0 = next((i for i, m in enumerate(db_messages)
                       if m.get("role") in ("assistant", "tool")), None)
        if _hist0 is not None:
            win_end = _hist0 + 10
        for idx, m in enumerate(db_messages):
            role = m.get("role", "")
            if role not in ("user", "assistant", "tool"):
                continue  # system 消息由调用方显式注入（Task 8 分离结构），DB 历史 system 不重复注入
            content = str(m.get("content", "") or "")
            if role == "assistant":
                tc_raw = m.get("tool_calls")
                has_tc = bool(tc_raw and str(tc_raw).strip())
                # 文本调用轮（DB 无 tool_calls 字段）同样算工具轮：压缩点按轮
                # 计数，识别必须一致，否则摘要替换错位（2026-08-15 压缩点持久化）
                parsed_calls: list[dict] = [] if has_tc else _parse_text_tool_calls(content)
                if (has_tc or parsed_calls) and idx < win_end \
                        and self._is_plan_read_round(db_messages, idx):
                    # 步骤开始 plan.md 读取轮（2026-08-26 保护）：原样输出且不
                    # 占用 skip_n 计数，tool 尾部不跳过——与压缩侧保护判定对称
                    skip_tool_tail = False
                elif (has_tc or parsed_calls) and tool_round_seen < skip_n:
                    # 被压缩的早期工具轮：位置输出摘要 user 消息，跳过本轮的 tool 尾部
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
                # DeepSeek V4 thinking：所有 assistant 轮必须回传 reasoning_content
                # （空串可）——工具轮与纯文本轮都要（2026-08-20 DB 实证：
                # _monitor:step-7a resume 重建时纯文本 assistant 漏补 → 400
                # 「reasoning_content must be passed back」；此前只给工具轮补）
                entry["reasoning_content"] = ""

                calls = [c for c in calls
                         if not _is_empty_arguments(
                             (c.get("function") or {}).get("arguments", ""))]
                if calls:
                    entry["tool_calls"] = calls
                elif parsed_calls:
                    # 正则兜底：content 内嵌 {"tool":"dcflow_x","arguments":{...}} → 文本调用
                    entry["tool_calls"] = [
                        {"id": pc["call_id"], "type": "function",
                         "function": {"name": pc["name"],
                                      "arguments": json.dumps(pc["input"], ensure_ascii=False)}}
                        for pc in parsed_calls
                    ]
                out.append(entry)
            elif role == "tool":
                if skip_tool_tail:
                    continue  # 该工具轮已被压缩摘要，tool 结果跳过
                tcid = str(m.get("tool_call_id") or "")
                if not tcid:
                    # 正则兜底：优先取前一 assistant 已生成的 tool_calls[0].id——
                    # 独立二次解析会生成不同时间戳 call_id（跨毫秒边界不一致 →
                    # tool 消息与 assistant.tool_calls 配对失败，OpenAI 400）；
                    # 仅当无前一 assistant 时才从自身 content 解析补上
                    if out and out[-1]["role"] == "assistant" and out[-1].get("tool_calls"):
                        tcid = out[-1]["tool_calls"][0]["id"]
                    else:
                        parsed_calls = _parse_text_tool_calls(content)
                        if parsed_calls:
                            tcid = parsed_calls[0]["call_id"]
                if not tcid:
                    # 无 tool_call_id 且无法兜底（旧数据落盘缺失）→ 丢弃，
                    # 避免发出无 tool_call_id 的非法 tool 消息（OpenRouter 400）
                    continue
                out.append({"role": "tool", "content": content, "tool_call_id": tcid})
            else:
                out.append({"role": role, "content": content})
        return out

    def _classify_error(self, err: Any) -> dict:
        """分类 LLM 错误（旧 _classifyError）：429/限流 → rate_limit 可重试；403/401 → forbidden。"""
        status = getattr(err, "status", 0) or 0
        msg = str(getattr(err, "message", None) or err).lower()
        if status == 429 or re.search(r"429|rate.?limit|quota|too many", msg):
            return {"code": "rate_limit", "message": getattr(err, "message", None) or str(err),
                    "retryable": True}
        if status in (401, 403) or re.search(r"403|forbidden|unauthorized|401", msg):
            return {"code": "forbidden", "message": getattr(err, "message", None) or str(err),
                    "retryable": False}
        # 网络层/流中断（llm_client 已把 httpx.ReadError 等转为 LlmError(0, "...中断/失败...")）
        # → 瞬时错误可自动重试（不置 stopped、不 crash 执行循环）
        if re.search(r"请求失败|响应中断|连接|readerror|httpx|超时|reset|broken pipe|network", msg):
            return {"code": "network", "message": getattr(err, "message", None) or str(err),
                    "retryable": True}
        return {"code": "unknown", "message": getattr(err, "message", None) or str(err),
                "retryable": False}

    def _summarize_old_tool_calls(self, msgs: list[dict], keep: int = 10,
                                  budget_chars: Optional[int] = None,
                                  out_summaries: Optional[list[str]] = None) -> list[dict]:
        """工具调用历史摘要化。两种模式：
        - keep 给定（兼容旧调用/测试；生产路径已由 _summarize_to_token_budget 替代）：
          只保留最近 keep 次完整工具结果，更早的工具轮替换为一条"工具名+参数"摘要
          （纯文本 assistant 保留）；工具轮 ≤ keep 次时完全不动（压缩底线内）。
        - budget_chars 给定（惰性模式，兼容旧调用/测试）：仅当消息总字符超过预算时
          从最早工具轮开始逐组替换，直到总字符 ≤ budget_chars 或无可替换。
        仅作用于发给 LLM 的 msgs，DB step_messages 与 SSE 展示不受影响。
        out_summaries（可选）：本次实际压缩生成的摘要消息 content 按序收集——
        压缩点持久化用（2026-08-15 起 keep 模式逐工具轮独立摘要，每条 = 一轮，
        重建时按轮替换可精确复现）。"""
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
                # 找最早一组未摘要的 assistant(tool_calls) 轮（保护轮跳过）
                start = next((i for i, m in enumerate(out)
                              if m.get("role") == "assistant" and m.get("tool_calls")
                              and self._msg_hash(m) not in protected), None)
                if start is None:
                    break  # 无可摘要（全部纯文本/已摘要/仅剩保护轮）
                summaries = []
                for tc in out[start].get("tool_calls") or []:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args_obj = json.loads(fn.get("arguments", "{}") or "{}")
                    except (ValueError, TypeError):
                        args_obj = {}
                    summaries.append(self._summarize_tool_call(name, args_obj))
                # 替换代价 = assistant 消息 + 紧随的 tool 结果消息
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
        # 2026-08-26：步骤开始 plan.md 读取轮保护——从候选剔除（不摘要、不占
        # keep 名额），保留区内原样；保留区外的保护轮在 prefix 循环中原样输出
        protected = {i for i in tool_starts if self._is_plan_read_round(msgs, i)}
        tool_starts_f = [i for i in tool_starts if i not in protected]
        if len(tool_starts_f) <= keep:
            return msgs
        # 保留区起点：最近 keep 个非保护工具轮中最老的 assistant 消息（含其
        # tool_calls），保证保留区内 tool 配对完整（不产生孤立 tool）
        keep_start = tool_starts_f[-keep]
        prefix: list[dict] = []
        protect_tail = False  # 当前 tool 结果归属被保护的 plan 读取轮 → 原样保留
        for i, m in enumerate(msgs[:keep_start]):
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                if i in protected:
                    prefix.append(m)  # 保护轮 assistant 原样（含 tool_calls）
                    protect_tail = True
                    continue
                protect_tail = False
                # 逐工具轮独立摘要（2026-08-15 压缩点持久化：每条 = 一轮，
                # 重建按轮替换可精确复现；不再合并成单条大摘要）
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
                continue  # 工具轮 assistant 不保留（已被摘要）
            if role == "tool":
                if protect_tail:
                    prefix.append(m)  # 保护轮 tool 结果完整保留
                continue  # 非保护轮工具结果不保留（已被摘要）
            prefix.append(m)  # 其余 user/assistant 纯文本保留（对话连贯性）
        return prefix + msgs[keep_start:]

    @staticmethod
    def _msg_hash(m: dict) -> str:
        """消息内容稳定指纹（plan.md 保护判定用）：role/content/tool_calls 参与，
        与消息索引无关——压缩循环 out 切片替换后索引漂移，hash 过滤不受影响。"""
        import hashlib
        payload = {"role": m.get("role"), "content": m.get("content"),
                   "tool_calls": m.get("tool_calls")}
        return hashlib.md5(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                      default=str).encode("utf-8")).hexdigest()

    @staticmethod
    def _is_plan_read_round(msgs: list[dict], idx: int, window: int = 10) -> bool:
        """步骤开始 window 条历史内读取 *.plan.md 的工具轮（压缩/重建共用判定）：
        history 起点 = 跳过 system 后第一条 assistant/tool；idx 在 [start, start+window)
        内且 assistant.tool_calls（或文本调用解析）含 dcflow_read_file 且 file_path
        以 plan.md 结尾（大小写不敏感）→ True。保护轮压缩不摘要、重建不替换——
        两侧必须用同一窗口语义，否则摘要替换错位（400/内容错乱）。"""
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
        # 文本调用格式兜底（旧数据 DB 无 tool_calls 字段，content 内嵌 {"tool":...}）
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
        """步骤开始 window 条历史内 plan.md 读取轮的内容指纹集合（压缩用）：
        history 起点 = 第一条 assistant/tool；窗口内 _is_plan_read_round 为真的消息
        全部纳入——压缩时跳过这些轮，重建时按同一窗口原样输出。"""
        start = next((i for i, m in enumerate(msgs)
                      if m.get("role") in ("assistant", "tool")), None)
        if start is None:
            return set()
        return {self._msg_hash(msgs[i])
                for i in range(start, min(start + window, len(msgs)))
                if self._is_plan_read_round(msgs, i, window)}

    def _summarize_to_token_budget(self, msgs: list[dict], budget_tokens: int,
                                   keep: int, out_summaries: Optional[list[str]] = None) -> list[dict]:
        """压缩目标 = 总 token ≤ budget_tokens（用户方案：不是固定保留最近 keep 轮）：
        从最早工具轮开始逐组摘要化，直到总 token ≤ budget 或工具轮 ≤ keep（底线）。
        增量计数（初始全量 + 每组替换差值），避免每轮 O(n) 重算。
        返回 msgs 原对象 = 已在预算内 / 无可压缩（纯文本或已到底线）。
        tiktoken 不可用（_count_tokens 返回 None）→ 降级旧 keep 模式（压到 keep 底线，
        发送 400 裁决兜底收尾）。
        out_summaries（可选）：本次实际压缩生成的摘要消息 content 按序收集——
        压缩点持久化（compress_map artifact）用：重启重建时逐轮替换复现。
        2026-08-26：步骤开始 plan.md 读取轮（_protected_plan_read_hashes）不摘要——
        保护轮不占 keep 名额（候选过滤后计数）。"""
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
                break  # 底线：保留最近 keep 轮完整（保护轮不占名额）
            start = tool_starts[0]
            # 移除代价 = assistant(tool_calls) + 紧随的 tool 结果（增量计数）
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
        """单次工具调用 → 自然语言摘要。输出统一 ≤80 字符（DB 审计实证：step-5b
        场景 1697 条摘要因完整路径/长参数撑到 30 万字符，压不进 280K 预算导致
        force 兜底丢光对话上下文）。"""
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
        """上下文压缩（用户方案，2026-08-15 修订：400K 触发 → 200K 目标；2026-08-16
        修订收尾线：压到底线保留最近 20 轮完整工具轮，≤400K 继续跑，>400K 才收尾）：
        - 触发：last_tokens（该步骤最近一次 LLM 调用的 usage.prompt_tokens）为 None
          或 ≤ COMPRESS_TOKENS（400K）→ 完全不动（正常轮次零开销）；
        - > 400K → 摘要化早期工具轮完整内容（"工具名+参数"），直到总 token
          ≤ CONTEXT_BUDGET_TOKENS（200K，tiktoken 确定性计数；不是固定保留 20 轮）；
        - last_tokens=None（resume 无 usage 记录：真实 API 流式响应不含 usage chunk）
          → tiktoken 实测兜底：明确超 400K（COMPRESS_TOKENS，与主分支统一触发）
          才压缩，压到 ≤ 200K（CONTEXT_BUDGET_TOKENS）；tiktoken 不可用 →
          交发送 400 兜底。
        - 返回 None = 已压缩到 20 轮底线（KEEP_RECENT_TOOL_ROUNDS，保留最近 20 轮
          完整工具轮不再压缩）/ 无可压缩（纯文本）仍 > 400K（COMPRESS_TOKENS）
          → 调用方负责 _graceful_finish_step（总结进度 + 步骤 completed，不等发送 400）；
        - 发送 400 上下文超限（tiktoken 可能低估实际 tokenizer）→ 调用方激进压缩
          到 20 轮底线重试（不经本方法，见 _call_llm），压无可压才收尾。
        压缩只作用于发给 LLM 的 msgs，DB step_messages 与 SSE 展示不受影响。
        压缩实际发生时把本次摘要按序记录到 compress_map artifact（task 级压缩点）——
        重启重建时 _build_lm_messages 应用压缩点，重建上下文与压缩时一致（不再发
        DB 全量：2026-08-15 实证 step-4 重启后 1.26M > 1M 窗口 400，但 last_tokens
        记录的旧压缩版 345K → 本地 skip → 发送才 400）。
        结果统一过 _sanitize_tool_pairs 保证配对合法（避免 OpenAI 400：
        tool 无前置 tool_calls）。"""
        if last_tokens is not None and last_tokens <= self.COMPRESS_TOKENS:
            logger.info(f"[DC:orch] compress check step={step_id} last_tokens={last_tokens} "
                        f"-> skip ({self.COMPRESS_TOKENS})")
            return msgs
        collected: list[str] = []
        if last_tokens is None:
            # resume 无 usage 记录（流式 API 无 usage chunk）：tiktoken 实测兜底——
            # 明确超 400K（COMPRESS_TOKENS，与主分支统一触发阈值）才压缩，压到
            # ≤200K（CONTEXT_BUDGET_TOKENS）；tiktoken 不可用 → 交 400 兜底
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
                # 2026-08-16 修订：None 分支（resume 无 usage）与主分支统一——
                # 压到底线（保留 20 轮）仍 >400K 才收尾；≤400K 继续跑
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
            # 已到 20 轮底线（保留最近 20 轮完整工具轮，不再继续压缩）/ 纯文本无可
            # 压缩，仍超 400K → 收尾（调用方 graceful）；2026-08-16 修订：收尾线从
            # 200K 提到 400K——200K-400K 区间压到底线也继续跑（20 轮仍可工作）
            # 部分压缩已发生 → 压缩点照常记录（重建保持该状态）
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
        """读压缩点（task 级 compress_map artifact）：该步骤最早 N 个工具轮的摘要
        文本，按序；无记录/损坏 → 空列表（重建全量，实测复核兜底）。"""
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
        """追加压缩点（压缩发生时调用，幂等追加；异常仅告警不阻断执行）。"""
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
        """清除该步骤旧压缩点（步骤首次开始消息已清空时调用，旧映射作废）。"""
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
        """优雅收尾（用户方案，替代旧 _force_compress 硬压）。触发时机：
        1) 压缩到 20 轮底线 / 无可压缩仍超 CONTEXT_BUDGET_TOKENS（800K，本地判定）；
        2) 发送 400 maximum context length（tiktoken 低估兜底）；
        3) task_ended：步骤执行中任务进入终态（review 提前 mark_complete 等，
           _call_llm 每轮检查）——文案区分，避免误报"上下文超限"（DB 实证
           99248a9f：实际仅 22k 却显示超限）。
        流程：
        1) LLM 总结当前进度（输入裁剪：纯文本进展脉络 + 工具轮摘要，直调
           stream_chat 不落盘、不递归压缩）；
        2) 总结写入 task 级关键发现（_flow/key_findings，新步骤 system 自动注入）
           + 步骤对话最后一条 assistant 消息（用户在步骤详情可见）；
        3) 步骤标记 completed——编排 AI 自然接手开新步骤（用户明确：编排部分
           不在此实现、不改动）。
        总结失败不阻断收尾（降级：取最后一条 assistant 纯文本为进度）。"""
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
                pass  # 收尾落盘失败不阻断完成
        await self._sm.advance_step(task_id, step_id, "completed")
        # 2026-08-23：report 优雅收尾也要 complete_task（收尾链唯一出口）——
        # 否则 report graceful 后任务悬在 active（正常路径 L1314 才有 complete_task，
        # 优雅收尾不经过），执行循环补尾反复拾取收尾链
        if step_id == "report":
            await self._sm.complete_task(task_id)
        self._publish(task_id, "streamEnd", {"stepId": step_id})
        logger.info(f"[DC:orch] step {step_id} {reason} — progress summarized, "
                    f"marked completed (orchestrator will open next step)")

    @staticmethod
    def _build_progress_input(msgs: list[dict], max_chars: int = 120000) -> str:
        """总结输入裁剪：纯文本进展脉络（user/assistant 非工具轮）全保留（防御
        截断 max_chars）+ 最近工具轮摘要（_summarize_tool_call，≤40 条）。
        tool 结果过大不直接进总结输入（已被摘要或裁剪）。"""
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
        """消息序列合法化（配对完整性，任意位置悬空均可修复）：
        - 孤立 tool 消息（配对被裁）→ 丢弃；
        - assistant 声明的 tool_calls 若无对应 tool 响应（中间悬空/尾部被裁）→
          移除未响应的 tool_calls（全部无响应则移除 tool_calls 字段，纯工具发起轮丢弃）。
        保证任何消息序列都能通过 OpenAI 工具消息校验（400 防护）。
        修订（V-15）：原实现只清理尾部悬空——删除中间段消息（如清理重试轮）
        产生的中间悬空 assistant 会漏过导致 400，改为两遍全局校验。
        """
        # 第一遍：收集所有 assistant 声明过且确有 tool 响应的 call_id
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
        # 第二遍：重建（仅保留配对完整的消息/工具项）
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
                    # 无文本且工具调用全无响应 → 整条丢弃（纯工具发起轮已无意义）
                else:
                    out.append(m)
            elif role == "tool":
                if m.get("tool_call_id") in responded:
                    out.append(m)
                # 孤立 tool（配对被裁/未声明）→ 丢弃
            else:
                out.append(m)
        return out

    def _trim_tool_results(self, messages: list[dict]) -> list[dict]:
        """工具结果超 2000 字符截断（旧 _trimToolResults，只处理 tool 消息；
        smart 压缩回退路径内部使用——不再作为独立压缩级别）。"""
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
        """60K 级别：早期消息合并为一条摘要，保留最近 6 条（旧 _smartCompress）。"""
        if len(messages) <= 8:
            return self._trim_tool_results(messages)
        early = messages[:-6]
        recent = messages[-6:]
        return [{"role": "user", "content": f"[早期对话已压缩，共 {len(early)} 条消息]"}] + recent

    # ═══════════════════════════════════════════════════════════════
    # 工具集 / LLM 客户端
    # ═══════════════════════════════════════════════════════════════

    def _get_exec_tools(self) -> list[dict]:
        return list(_EXEC_TOOLS)

    def _get_monitor_tools(self) -> list[dict]:
        return list(_MONITOR_TOOLS)

    def _get_gate_tools(self) -> list[dict]:
        return list(_GATE_TOOLS)

    def _get_reverse_tools(self) -> list[dict]:
        """逆向专家工具集（2026-08-24）：exec 基础 + dcflow_sim + ctf 逆向工具。"""
        return list(_REVERSE_TOOLS)

    def _get_researcher_tools(self) -> list[dict]:
        """研究员工具集（只读调研）：目录浏览 + 读文件 + 代码搜索 + 知识库 + 完成。
        不含写文件/编辑/运行命令工具——researcher 是只读调研专家。"""
        return list(_RESEARCHER_TOOLS)

    def _make_llm_client(self, tier: str):
        """构建 LlmClient（V-L7：每次实时读 config.json，env 优先）。
        V-09 修订：仅 tier=='light' 用轻量模型，其余（power/reasoning 等人工
        加注的深度推理 tier）一律用强力模型——否则 step-5b 这类 tier=reasoning
        的步骤会落入 light_model（flash），与设置页「强力模型」配置不符。
        2026-08-23 双模型拆分：light/power 各自独立端点与 Key（设置页分组配置），
        按 tier 选组；未配置时回退共享旧字段（get_llm_config 内回退）。"""
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
        """执行工具调用（统一走 REST /api/tool/invoke 同进程处理器，旧 L726）。

        步骤报告拦截（2026-08-15 用户方案）：{step_id}-步骤报告.md 的读写走 DB
        持久化（artifacts step_report，重启 .dc_tmp 清理不丢）——
        - 写报告：直接执行（不拦截 AI 全量写），执行后镜像 DB（含系统注入块）；
        - 读报告：文件缺失时先从 DB 导出到文件，再正常读。
        """
        from ..step_context import get_task_root, get_task_workspace  # 延迟导入（模块级有循环风险）
        # 2026-08-24（逆向专家限定）：dcflow_sim/ctf 工具仅 type=reverse 步骤可调——
        # 工具集注入 + 执行层双保险（防提示词诱导/越权调用）；步骤查不到同样拒绝
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
        # 2026-08-23（用户定方案）：修改类工具拒绝裸相对路径。AI 写「artifacts/xxx.md」
        # 时以为相对步骤目录，实际解析基准是 PROJECT_ROOT（workspace/）→ 落到项目根，
        # 后续步骤 list_prior_step_outputs（只列步骤目录）看不到 → 产物断链（实证
        # dd5a2d1d：step1_root_cause.md 落到 workspace/artifacts/）。拒绝并要求完整路径：
        # .dc_tmp/<任务ID>/<步骤ID>/...（推荐，产物目录）或绝对路径（safe_resolve 校验），
        # 落点无歧义；read 不拒（只读无破坏，前序产物清单已给完整路径）。
        # 2026-08-24（任务隔离）：任务根存在（新任务 workspace/<task_id>/）时裸相对允许——
        # 落任务根（代码等持久化产物，互不干扰）；无任务根（旧任务）保持拒绝（防落 workspace 根）。
        # 2026-08-26（自定义工作目录）：workspace_dir 非空 → 基准是自定义目录，同样放行
        if name in ("dcflow_write_file", "dcflow_edit_file") \
                and await get_task_workspace(self._storage, task_id) == PROJECT_ROOT:
            fp = str(args.get("file_path") or "")
            if fp and not os.path.isabs(fp) \
                    and not fp.replace("\\", "/").startswith(".dc_tmp"):
                return ("[Error] 拒绝裸相对路径 file_path（解析基准是项目根，裸相对路径"
                        "会落到项目根而非步骤目录，后续步骤读不到）：请传完整路径 —— 推荐 "
                        f".dc_tmp/{task_id}/{step_id}/artifacts/<文件名>（产物目录）；"
                        "或绝对路径。产物统一放步骤产物目录 artifacts/ 下。")
        action, block_msg = await self._intercept_step_report(task_id, step_id, name, args)
        if action == "block":
            # 未读先写阻挡（2026-08-16 用户需求）：写入前 10 轮内未读取过流程报告
            # → 不执行写入，直接返回提示让 AI 重新读取
            logger.info(f"[DC:orch] flow report write blocked step={step_id}: {block_msg[:60]}")
            return block_msg
        if name == "dcflow_sim":
            # 模拟器会话在 orchestrator 内存中保持（不经过 REST 工具分发）。
            # 2026-08-19 修复：run_sim_tool 是同步函数（uc.emu_start 最长 180s），
            # 直接在事件循环调用会阻塞整个 asyncio——所有 HTTP/SSE 请求排队
            # （UI 全卡、刷新页面未响应，日志可见 ConnectionResetError 断连噪音）
            # → 放线程池执行 + per-task 锁（parallel_with 并行 step 可能并发调同一会话）。
            from ..simulator.tool import run_sim_tool
            lock = self._sim_locks.setdefault(task_id, asyncio.Lock())
            async with lock:
                return await asyncio.to_thread(run_sim_tool, task_id, self._sim_sessions, args)
        if name in _CTF_TOOL_FUNCS:
            # 2026-08-24：ctf 逆向工具（线程池执行——angr 反编译/常量扫描可能数秒~数十秒）
            from .. import ctf_tool
            func_name, params = _CTF_TOOL_FUNCS[name]
            fn_args = [str(args.get(k) or "") for k in params]
            return await asyncio.to_thread(getattr(ctf_tool, func_name), *fn_args)
        if self._tool_invoke is not None:
            result = await self._tool_invoke({"name": name, "args": args,
                                              "task_id": task_id})
        else:
            from .. import rest_api  # 延迟导入避免循环依赖
            result = await rest_api.invoke_tool({"name": name, "args": args,
                                                 "task_id": task_id})
        if action == "mirror":
            await self._mirror_step_report(task_id, step_id)
        if isinstance(result, dict):
            return str(result.get("result", "(无输出)"))
        return str(result)

    async def _intercept_step_report(self, task_id: str, step_id: str, name: str,
                                     args: dict) -> tuple[str, str]:
        """流程报告读写拦截（返回 (action, message)）：
        - ("mirror", "")：写/编辑报告 → 工具执行后镜像 DB（含关键发现注入块）；
        - ("", "")：读报告（文件缺失时先从 DB 导出到文件，再正常读）/ 不相关文件。
        2026-08-16 流程级改造：多步骤共享一份 `{_FLOW_REPORT_FILENAME}`（任务目录根），
        DB 存 (_flow, step_report)；旧名 {step_id}-步骤报告.md 兼容（读写仍拦截映射同份）。"""
        fp = str(args.get("file_path") or "")
        base = os.path.basename(fp.replace("\\", "/"))
        # 匹配：新名（流程报告）+ 旧名（AI 维护的中文名）+ 英文名（export_for_ai 导出
        # artifacts/{step_id}-step_report.md）都算报告；写/编辑只认中文名/新名
        # （英文名是系统导出快照，非 AI 维护目标，不拦截）
        if base not in (_FLOW_REPORT_FILENAME, f"{step_id}-步骤报告.md", f"{step_id}-step_report.md"):
            return "", ""
        if base == f"{step_id}-step_report.md" and name in ("dcflow_write_file", "dcflow_edit_file"):
            return "", ""
        if name == "dcflow_read_file":
            # 用户方案（2026-08-15/16）：读报告前先从 DB 把数据导出到文件，再运行正常
            # 读取——重启 .dc_tmp 清理后文件自动恢复，AI 读到的就是完整报告（DB 无内容
            # 则保持原样，工具报"文件不存在"）；流程级后 DB 权威版存 (_flow, step_report)
            report_path = os.path.normpath(os.path.join(PROJECT_ROOT, fp))
            if not os.path.isfile(report_path):
                raw = await self._storage.get_artifact(task_id, "_flow", _STEP_REPORT_ARTIFACT)
                if raw and raw.get("content"):
                    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
                    with open(report_path, "w", encoding="utf-8") as f:
                        f.write(raw["content"])
                    logger.info(f"[DC:orch] flow report restored from DB before read "
                                f"step={step_id} ({len(raw['content'])} chars)")
            # 记录本次读取轮次（未读先写阻挡依据；读其他文件不记录）
            try:
                await self._save_step_report_read_round(
                    task_id, step_id, await self._get_step_report_round(task_id, step_id))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DC:orch] flow report read round save failed step={step_id}: {e}")
            return "", ""  # 放行：正常读文件（导出后必然存在）
        if name in ("dcflow_write_file", "dcflow_edit_file"):
            # 未读先写阻挡（2026-08-16 用户需求）：写入前 10 轮内未读取过流程报告
            # → 阻挡并提示重新读取（防止 AI 基于过时认知全量覆盖报告）；文件不存在
            # （首次创建）豁免——无可读内容。
            report_path = os.path.normpath(os.path.join(PROJECT_ROOT, fp))
            if os.path.isfile(report_path):
                cur_round = await self._get_step_report_round(task_id, step_id)
                read_round = await self._get_step_report_read_round(task_id, step_id)
                if read_round is None or cur_round - read_round >= 10:
                    return ("block",
                            f"❌ 写入被阻止：你最近 10 轮内没有读取过流程报告"
                            f"（{_FLOW_REPORT_FILENAME}，请用 dcflow_read_file 读取完整内容后再写）。"
                            f"当前轮次 {cur_round}，上次读取轮次 {read_round or '从未'}。")
            # 用户方案（2026-08-15/16）：写报告不拦截（AI 全量写不会出问题——
            # 读拦截自动恢复 + 镜像兜底；底部关键发现注入区由镜像后写回文件维持）
            # → 直接执行，执行后镜像 DB（_flow，多步骤共享同一份）
            return "mirror", ""
        return "", ""

    async def _mirror_step_report(self, task_id: str, step_id: str) -> None:
        """流程报告文件 → DB（(_flow, step_report)，权威持久化，多步骤共享）：读文件
        （优先任务目录流程报告.md，兼容旧名路径）→ 合并缺失的关键发现注入块 → 覆盖写
        artifact，并把带块版本写回文件（用户决策 2026-08-15/16：AI 全量写报告会覆盖
        文件底部系统注入区 → 镜像后恢复，保证文件始终含完整关键发现；重启 .dc_tmp
        清理后 DB 不丢）。"""
        try:
            report_path = os.path.join(PROJECT_ROOT, ".dc_tmp", task_id,
                                       _FLOW_REPORT_FILENAME)
            legacy_path = os.path.join(PROJECT_ROOT, ".dc_tmp", task_id, step_id,
                                       f"{step_id}-步骤报告.md")
            # 新旧两路径都存在时取 mtime 最新（AI 刚写过的那个是权威——旧名兼容期
            # 避免旧名写入内容被新路径旧内容覆盖）；仅新路径 → 新；仅旧路径 → 旧。
            # 2026-08-18 修复：Windows mtime 秒级粒度下新旧路径 mtime 可能完全相等，
            # 严格 `>` 比较会漏选刚写入的旧名（内容被旧 DB 覆盖）——相等时旧名优先
            # （旧名存在意味着旧名兼容期写入，其内容为最新权威，镜像后会同步新路径）
            if os.path.isfile(report_path) and os.path.isfile(legacy_path):
                if os.path.getmtime(legacy_path) >= os.path.getmtime(report_path):
                    report_path = legacy_path
            elif os.path.isfile(legacy_path):
                report_path = legacy_path
            elif not os.path.isfile(report_path):
                return
            with open(report_path, "r", encoding="utf-8") as f:
                content = f.read()
            file_content = content  # 原始文件内容（镜像后对比：注入块是否被覆盖）
            cleaned = _KEY_FINDINGS_BLOCK_RE.sub(r"\1", content)
            raw_kf = await self._storage.get_artifact(task_id, "_flow", _KEY_FINDING_ARTIFACT)
            all_items: list[str] = []
            if raw_kf and raw_kf.get("content"):
                all_items = [ln.strip() for ln in raw_kf["content"].splitlines()
                             if ln.strip()]
            # 2026-08-16 修复：剥离正文中复制的碎片（agent 常把注入区复制进节选）
            cleaned = self._strip_findings_fragments(cleaned, all_items)
            missing = [it for it in all_items if it not in cleaned]
            if missing:
                content = (cleaned.rstrip()
                           + f"\n\n{_KEY_FINDINGS_BLOCK_START}\n## 系统注入：关键发现\n"
                           + "\n".join(f"- {it}" for it in missing)
                           + f"\n{_KEY_FINDINGS_BLOCK_END}\n")
            if content != file_content:
                # AI 全量写覆盖了底部注入区 → 把带块版本写回文件
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(content)
                # 旧名镜像后同步新路径（共享报告权威文件保持最新，避免两份不同步）
                flow_path = os.path.join(PROJECT_ROOT, ".dc_tmp", task_id,
                                         _FLOW_REPORT_FILENAME)
                if os.path.normpath(report_path) != os.path.normpath(flow_path):
                    with open(flow_path, "w", encoding="utf-8") as f2:
                        f2.write(content)
            await self._storage.save_artifact(task_id, "_flow", _STEP_REPORT_ARTIFACT,
                                              content, "markdown")
        except OSError as e:
            logger.warning(f"[DC:orch] flow report mirror failed step={step_id}: {e}")

    # ═══════════════════════════════════════════════════════════════
    # 持久化辅助（旧 _appendMessage/_appendRoundMessages/_saveChunk）
    # ═══════════════════════════════════════════════════════════════

    async def _append_message(self, task_id: str, step_id: str, message: dict) -> None:
        await self._storage.append_message(task_id, step_id, message)

    async def _update_step_stats(self, task_id: str, step_id: str, requests: int = 0,
                                 ttft_ms: Optional[int] = None, output_ms: int = 0,
                                 run_ms: Optional[int] = None) -> None:
        """步骤运行统计落库（2026-08-21）：requests/输出时长累加、首字延迟累计
        （读取端算平均）、运行时长覆盖定格。失败仅告警，不影响主流程。"""
        await self._storage.update_step_stats(
            task_id, step_id, requests=requests, ttft_ms=ttft_ms,
            output_ms=output_ms, run_ms=run_ms)

    async def _add_step_tokens(self, task_id: str, step_id: str,
                               prompt: int, cached: int, completion: int,
                               record_last: bool = True,
                               context_tokens: Optional[int] = None) -> None:
        """累计步骤 token 用量到 DB（Token 展示）+ 记录最近一次 prompt_tokens
        （压缩判断依据：API 精确统计）；失败仅告警，不影响主流程。
        record_last=False（tiktoken 估算兜底）：只累加展示列，不写
        last_prompt_tokens——估算值会低估实际 tokenizer，压缩判断保持
        last_tokens=None 实测兜底（压到 400K）更安全。
        context_tokens：当前上下文长度（最近一次请求的输入 tokens，覆盖写）——
        API usage 与估算两条路径都写入，供前端上下文占用条展示。"""
        try:
            await self._storage.add_step_tokens(
                task_id, step_id, prompt, cached, completion,
                context_tokens=context_tokens if context_tokens is not None else prompt)
            if not record_last:
                return
            # 最近一次 usage.prompt_tokens（_flow/last_prompt_tokens artifact，
            # {step_id: prompt}）——resume 发送前压缩判断用，非字符估算
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
        """该步骤最近一次成功 LLM 调用的 usage.prompt_tokens（API 精确统计）。
        无记录（首轮/从未成功）→ None：发送前不压缩，交由 400 兜底。"""
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", _LAST_PROMPT_TOKENS_ARTIFACT)
            if not raw or not raw.get("content"):
                return None
            val = json.loads(raw["content"]).get(step_id)
            return int(val) if val else None
        except (ValueError, TypeError):
            return None

    async def _get_step_report_round(self, task_id: str, step_id: str) -> int:
        """50 轮报告提醒计数（跨重启持久）：该步骤已累计的 LLM 轮数。无记录/损坏 → 0。"""
        try:
            raw = await self._storage.get_artifact(task_id, "_flow", _STEP_REPORT_ROUNDS_ARTIFACT)
            if not raw or not raw.get("content"):
                return 0
            val = json.loads(raw["content"]).get(step_id)
            return int(val) if val else 0
        except (ValueError, TypeError):
            return 0

    async def _save_step_report_round(self, task_id: str, step_id: str, n: int) -> None:
        """落盘 50 轮报告提醒计数（_flow/step_report_rounds artifact，{step_id: n}）。
        失败仅告警（丢本轮计数不影响正确性，下次 nudge 迟到几轮）。"""
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
        """最近一次读取流程报告的轮次（未读先写阻挡依据）。无记录/损坏 → None（视为未读）；
        0 是有效值（读发生在第 0 轮）。"""
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
        """落盘最近一次读取流程报告的轮次（_flow/step_report_read_round artifact）。
        n=None → 删除该步骤记录（新会话清零，恢复"未读"状态）。
        失败仅告警（读轮次丢失 → 下一次写可能被阻挡，AI 重新读取后恢复）。"""
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
        """追加一轮或多轮的消息到 DB（旧 _appendRoundMessages，_lastSavedRound 增量）。
        工具轮同时落盘 assistant(tool_calls) 消息（OpenAI tool_calls JSON 文本）与带
        tool_call_id 的 tool 消息——DB 历史可完整重建，续接恢复不再产生孤儿 tool 消息（400 修复）。"""
        key = f"{task_id}:{step_id}"
        saved = self._last_saved_round.get(key, 0)
        for i in range(saved, len(rounds)):
            r = rounds[i]
            tool_calls = r.get("toolCalls", [])
            if (r.get("text") or "").strip() or tool_calls:
                # assistant 消息：纯文本轮只有 content；工具轮附 tool_calls（含并行多工具）
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

    # 引用性片段前缀（2026-08-16 修复：Monitor/gate 评述常以「X里说/提到/显示」
    # 引用报告内容，不是新结论——捕获后污染报告）
    _KF_QUOTE_PREFIX_RE = re.compile(
        r"^(?:.{0,4}?"
        r"(?:报告|文件|对话|步骤|节选|注入|上文|前面|上面|里面|里头|总结|内容|段落|注释|部分|地方|该|这|那|其|里|中)"
        r"(?:里|中|部分|地方)?"
        r"(?:说|提到|里有|中有|里说|里写|显示|提示|表明|说明|指出|意味着|提到过|提到，|提到：|提到:))")

    @staticmethod
    def _extract_key_findings(text: str, max_len: int = _KEY_FINDING_MAX_LEN) -> list[str]:
        """从 AI 输出（content 或 reasoning）中提取关键发现：关键词后内容直到句号（。.）
        或换行；同一段文本多个关键词全部提取；去掉前导符号（**、-、冒号等）与尾部句号。
        换行类字符（\r \n \t 及 Unicode 行分隔符）一律替换为空格——保证关键发现文件
        一行一个发现（否则 \r 在 Windows 编辑器会显示为折行）。
        2026-08-16 过滤（报告污染修复）：引用性片段（「X里说/提到/显示…」引述报告内容）
        与纯英文长评述（Monitor 思考过程）不是新结论 → 丢弃，避免 key_findings 与报告
        被评审碎片占满。"""
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
                continue  # 系统提示/指令原文复述，不是真实发现
            if Orchestrator._KF_QUOTE_PREFIX_RE.match(seg):
                continue  # 「报告里提到/文件显示…」= 引用而非新结论
            if (not re.search(r"[\u4e00-\u9fff]", seg) and len(seg) >= 30
                    and re.match(r"(?i)^(mention|section|this|these|they|it|talk|say|show|describe|seem|contain|strong|warn|note|comment)",
                                 seg)):
                continue  # 纯英文长评述（Monitor 思考元语言），非稳定结论；英文真实结论（无评述词开头）保留
            found.append(seg[:max_len])
        return found

    @staticmethod
    def _strip_findings_fragments(text: str, items: list[str]) -> str:
        """剥离正文中复制的关键发现碎片（2026-08-16 修复：agent 读报告后常把系统
        注入区复制进正文节选——整行等于『- {item}』或裸 item 的行、以及孤立注入块
        标记行）——碎片由系统注入区统一承载，正文只保留 agent 真正写的内容。"""
        if not text:
            return text
        # 2026-08-16 修复：剥离正文中复制的关键发现碎片（agent 常把系统
        # 注入区复制进正文节选——整行等于『- {item}』或裸 item 的行、以及孤立注入块
        # 标记行/标题行）——碎片由系统注入区统一承载，正文只保留 agent 真正写的内容。
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
        """解析 assistant 输出中的关键发现 → task 级 artifact（"_flow"/key_findings）
        + 步骤报告文件注入 + DB 镜像（step_report artifact；2026-08-15 起不再双写
        关键发现.txt——重启 .dc_tmp 清理会丢，DB 才是权威）。
        去重（字符串匹配）、单条 ≤200 字、总行数 ≤100（超出仅保留最新）。
        捕获失败仅跳过，不阻断执行。"""
        try:
            items = self._extract_key_findings(text)
            if not items:
                return
            raw = await self._storage.get_artifact(task_id, "_flow", _KEY_FINDING_ARTIFACT)
            existing: list[str] = []
            if raw and raw.get("content"):
                # 存量行也做一次换行符清理（历史数据可能残留 \r 等）
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
            # 报告文件自动注入（用户决策 2026-08-15 修订）：注入块 = 关键发现
            # artifact 全量（正文中已存在的跳过）——此前只注入本次新增且清理旧块
            # 时把历史发现一并删除，报告里只剩最新一条；现保证系统注入区始终
            # 包含全部已捕获结论。渲染不可见、AI 读原文可见；旧块清理保证单一。
            try:
                # 流程级（2026-08-16）：共享报告在任务目录根 .dc_tmp/{task_id}/流程报告.md
                report_dir = os.path.join(PROJECT_ROOT, ".dc_tmp", task_id)
                os.makedirs(report_dir, exist_ok=True)
                report_path = os.path.join(report_dir, _FLOW_REPORT_FILENAME)
                existing_text = ""
                if os.path.exists(report_path):
                    with open(report_path, "r", encoding="utf-8") as f:
                        existing_text = f.read()
                else:
                    # 重启后文件被清（cleanup_dc_tmp）→ 以 DB 权威内容为基底重建，
                    # 否则"空文件+注入块"会覆盖丢失（2026-08-15 实证：DB 正文被
                    # 残缺文件覆盖）——与读拦截恢复互为兜底；流程级后 DB 存 _flow
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
                # 2026-08-16 修复：剥离正文中复制的碎片（agent 常把注入区复制进
                # 节选）——剥离后 missing 必然命中，注入块稳定重建在文件末尾，
                # 节选不再被碎片占满
                cleaned = self._strip_findings_fragments(cleaned, all_items)
                missing = [it for it in all_items if it not in cleaned]
                if missing:
                    block = (f"\n\n{_KEY_FINDINGS_BLOCK_START}\n## 系统注入：关键发现\n"
                             + "\n".join(f"- {it}" for it in missing)
                             + f"\n{_KEY_FINDINGS_BLOCK_END}\n")
                    with open(report_path, "w", encoding="utf-8") as f:
                        f.write(cleaned.rstrip() + block)
                # DB 镜像：报告（含注入块）持久化到 step_report artifact（重启不丢）
                await self._mirror_step_report(task_id, step_id)
            except OSError as e:
                logger.warning(f"[DC:orch] key_findings report inject failed: {e}")
        except Exception:
            pass  # 捕获失败不阻断执行

    async def _save_chunk(self, task_id: str, step_id: str, chunk: dict) -> None:
        """实时落盘流式 chunk（旧 _saveChunk）。"""
        await self._storage.save_chunk(task_id, step_id, chunk)

    async def _save_chunks(self, task_id: str, step_id: str, chunks: list[dict]) -> None:
        """批量落盘流式 chunk（单事务）：流式输出逐条 commit 阻塞流式循环（实测
        1.79ms/条，1600 条拖 2.8s）；批量 0.01ms/条。stream_chunks 仅审计用途。"""
        if chunks:
            await self._storage.save_chunks(task_id, step_id, chunks)

    # ═══════════════════════════════════════════════════════════════
    # SSE / 辅助
    # ═══════════════════════════════════════════════════════════════

    def _publish(self, task_id: str, command: str, payload: dict) -> int:
        """发布 SSE 事件（command/taskId/seq 由 hub 统一注入），返回事件 seq。"""
        return self.sse_hub.publish(task_id, command, payload)

    # ═══════════════════════════════════════════════════════════════
    # 步骤实时状态快照（2026-08-27：详情页首屏进行中状态）
    # ═══════════════════════════════════════════════════════════════

    def _update_step_live(self, task_id: str, step_id: str, seq: Optional[int] = None,
                          **fields) -> None:
        """更新 (task, step) 实时状态快照：thinking/text 为增量累积（调用处传
        分片），tool 整体替换（None 清空），streaming 覆盖；seq 单调前进。"""
        key = (task_id, step_id)
        live = self._step_live.get(key)
        if live is None:
            live = {"seq": 0, "streaming": False, "thinking": "", "text": "",
                    "tool": None, "completed_tools": []}
            self._step_live[key] = live
        if seq is not None and seq > live["seq"]:
            live["seq"] = seq
        for k, v in fields.items():
            if k in ("thinking", "text") and isinstance(v, str):
                live[k] = live[k] + v
            else:
                live[k] = v

    def _append_step_live_tool_param(self, task_id: str, step_id: str,
                                     call_id: str, delta: str,
                                     seq: Optional[int] = None) -> None:
        """工具参数增量累积（callId 匹配当前快照工具时拼接 input）。"""
        live = self._step_live.get((task_id, step_id))
        if live and live["tool"] and live["tool"].get("callId") == call_id:
            live["tool"]["input"] = live["tool"]["input"] + delta
        if seq is not None:
            self._update_step_live(task_id, step_id, seq=seq)

    def _append_step_live_completed(self, task_id: str, step_id: str,
                                    call_id: str, name: str, input_: str,
                                    output: str) -> None:
        """2026-08-27：记录已完成工具（整轮未落库期间供刷新渲染）。同 callId
        去重（工具轮内每个工具只记录一次）。"""
        live = self._step_live.get((task_id, step_id))
        if live is None:
            # 快照不存在（如刷新后首次执行到达工具结果）→ 初始化再记录
            live = {"seq": 0, "streaming": False, "thinking": "", "text": "",
                    "tool": None, "completed_tools": []}
            self._step_live[(task_id, step_id)] = live
        for item in live["completed_tools"]:
            if item["callId"] == call_id:
                return
        live["completed_tools"].append(
            {"callId": call_id, "name": name, "input": input_, "output": output})

    def _clear_step_live_completed(self, task_id: str, step_id: str) -> None:
        """2026-08-27：整轮落库后清空已完成工具记录（DB 已可渲染，防重复）。"""
        live = self._step_live.get((task_id, step_id))
        if live is not None:
            live["completed_tools"] = []

    def _clear_step_live_round(self, task_id: str, step_id: str) -> None:
        """2026-08-27：整轮落库后清空 live.text/thinking——已落库轮的文本 DB 已可
        渲染，不清会跨轮持续拼接（实测 live.text 含 DB 已落库轮内容，刷新时
        initLive 重复渲染「一坨」且思考历史累积一大段）。streaming 保留（工具轮后
        等待下一轮首字，前端显示「AI 正在思考」）。"""
        live = self._step_live.get((task_id, step_id))
        if live is not None:
            live["text"] = ""
            live["thinking"] = ""

    def _clear_step_live(self, task_id: str, step_id: str) -> None:
        """清空快照（streamEnd/llmError：DB 已落库，后续靠 getStep 全量渲染）。"""
        self._step_live.pop((task_id, step_id), None)

    def _clear_task_live(self, task_id: str) -> None:
        """2026-08-27：清空该任务全部 (task, step) live 快照。执行循环退出
        （stop/暂停/崩溃/优雅重启排空/正常完成）后调用——否则 stopped/pending
        步骤重新进入详情页会渲染残留的「思考中/命令执行中」假状态。"""
        for key in [k for k in self._step_live if k[0] == task_id]:
            del self._step_live[key]

    def get_step_live(self, task_id: str, step_id: str) -> Optional[dict]:
        """读步骤实时状态快照（getStep 附带；无 → None）。"""
        return self._step_live.get((task_id, step_id))

    async def stream_events(self, task_id: str, last_seq: int = 0):
        """SSE 订阅 + live 快照过滤（2026-08-27）：补发阶段跳过快照已覆盖的
        同步骤事件（详情页首屏快照初始化后防重复渲染）；实时阶段不过滤——
        2026-08-27 修复：过滤若对实时事件生效（live.seq 在发布时同步更新为
        事件自身 seq，event.seq <= live.seq 恒成立）会把全部实时事件丢弃，
        页面收不到任何更新。过滤经 sse_hub.subscribe 的 should_skip 回调
        （仅补发阶段应用）。"""

        def _skip(event: dict) -> bool:
            sid = event.get("stepId")
            if not sid:
                return False
            # 2026-08-27：429 重试标记（streamChunk 变体）放行——快照无法重建
            # 「正在重试」进度条，补发必须送达前端（否则重新进入页面进度条消失）
            if (event.get("command") == "streamChunk"
                    and "__DC_RETRY__" in str(event.get("chunk", ""))):
                return False
            live = self._step_live.get((task_id, sid))
            return bool(live and event["seq"] <= live.get("seq", 0))

        async for event in self.sse_hub.subscribe(task_id, last_seq=last_seq,
                                                  should_skip=_skip):
            yield event

    def _emit_chunk(self, task_id: str, step_id: str, chunk: str) -> None:
        seq = self._publish(task_id, "streamChunk", {"stepId": step_id, "chunk": chunk})
        # 内联标记（__DC_FULL__ 全量重拉 / __DC_RETRY__ 限流重试 / __DC_USER_MSG__
        # 介入与反馈消息）不进 text 累积——标记若进快照，重新进入页面 initLive
        # 会渲染原始标记文本（2026-08-27 实证：live.text 含 USER_MSG 原始标记）
        if not chunk.startswith(("__DC_FULL__", "__DC_RETRY__", "__DC_USER_MSG__")):
            self._update_step_live(task_id, step_id, seq=seq, text=chunk)

    def _publish_full_conversation(self, task_id: str, step_id: str,
                                   system_message: str, step_title: str, result: dict) -> None:
        """__DC_FULL__ 内联标记：完整对话一次推送给前端（旧 onChunk __DC_FULL__）。"""
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
        """从 LLM 调用结果构建 Monitor 对话记录（旧 triggerMonitor/_monitorOrchestrate 同构）。"""
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
        """提交步骤结果（旧 POST /api/step/submit）。"""
        from .. import rest_api  # 延迟导入避免循环依赖
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
