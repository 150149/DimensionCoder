from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from uuid import uuid4
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..config import PROJECT_ROOT
from ..graceful import graceful
from ..state_machine.state_machine import StateMachine
from ..storage.adapter import StorageAdapter
from ..step_context import STEP_TYPE_MODEL_TIER as _STEP_TYPE_MODEL_TIER
from ..step_context import (
    get_project_key, get_project_path,
    CODE_DIAGRAM_FILENAME as _CODE_DIAGRAM_FILENAME,
    PROJECT_CONTEXT_FILENAME as _PROJECT_CONTEXT_FILENAME,
    PROJECT_ASSET_FILES as _PROJECT_ASSET_FILES,
    PROJECT_ASSET_CANON as _PROJECT_ASSET_CANON,
    asset_ts as _asset_ts,
    load_project_asset as _load_project_asset_shared,
    project_asset_local_path as _project_asset_local_path,
    read_asset_file as _read_asset_file,
    write_asset_file as _write_asset_file,
    get_step_temp_dir,
)
from .llm_client import LlmAborted, LlmError
from .mcp_client import mcp_manager, McpError
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
            reasoning = m.get("reasoning_content")
            if reasoning:
                total += len(_TOKENIZER.encode(str(reasoning)))
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
    _tool("dcflow_write_file", "写入/覆盖文件（**一次写完整内容**：不要先写框架再分多次补写；"
          "同一文件有多处改动时合并到一次调用，或改用 dcflow_run_code 在一段代码里"
          "读全文→批量替换→写回）",
          {"file_path": {"type": "string"}, "content": {"type": "string"}},
          ["file_path", "content"]),
    _tool("dcflow_edit_file", "精准替换文件文本：old_string 必须精确匹配文件中的唯一片段；"
          "若文本出现多处，默认替换第 1 处，可用 occurrence 指定第 N 处；replace_all=true 替换全部。"
          "**同一文件有 2 处以上改动时不要分成多次调用**——用 dcflow_run_code 一段代码"
          "批量替换后写回，一次调用完成全部改动",
          {"file_path": {"type": "string"}, "old_string": {"type": "string"},
           "new_string": {"type": "string"}, "replace_all": {"type": "boolean"},
           "occurrence": {"type": "integer", "minimum": 1,
                           "description": "替换第 N 处匹配（从 1 开始），默认 1；与 replace_all 互斥"}},
          ["file_path", "old_string", "new_string"]),
    _tool("dcflow_search_code", "搜索代码/文本（pattern 为 Python 正则，逐行 re.search 匹配；path_filter 相对项目根目录的子目录，默认全项目递归），返回 file:line:content 最多 30 条",
          {"pattern": {"type": "string", "description": "Python 正则（如 r\"def \\w+\"）——不是纯文本，需转义特殊字符"},
           "path_filter": {"type": "string", "description": "相对根目录的子目录/文件路径，默认 '.'（全项目递归）"}},
          ["pattern"]),
    _tool("dcflow_run_code", "执行一段 Python 代码（独立子进程；工作目录 = 任务工作目录；解释器与后端同一个——capstone/pefile/unicorn/z3/angr 等已装库直接 import）。**干活首选**：一轮里可以循环、分支、调 subprocess、读写文件、把中间结果留在代码里，只有 print 出来的内容会回到上下文（console.log 语义）——大结果自己写文件（工作目录或 .dc_tmp/<任务ID>/<步骤ID>/artifacts/）后只 print 路径 + 行数 + 命中摘要（输出上限 10000 字符）。跑 shell 用 subprocess.run(cmd, shell=True, capture_output=True, text=True)（Windows cmd 环境：dir/type/findstr，无 ls/cat/grep）。**每轮是全新进程、变量不跨轮**：要复用的逻辑写成工作目录里的 helper 模块（如 revlib.py），下一轮直接 import revlib——不要每轮重写同类脚本。timeout_seconds 超时后强制终止整棵进程树并返回已捕获输出尾部；长任务请调大。**一次调用做完一件事**：同一目标的读取/多处修改/写回/校验放在同一段代码里，不要分开多次调用改同一个文件，也不要把本可一次做完的同类工作拆成多轮",
          {"code": {"type": "string", "description": "完整 Python 代码（顶层直接执行，无需 if __name__ 包裹）"},
           "description": {"type": "string", "description": "一句话说明这段代码要干什么——落库与上下文压缩后的历史摘要只用它，务必写清（如「统计 0x401000 的全部交叉引用」）"},
           "timeout_seconds": {"type": "integer", "description": "超时秒数，默认 60；超时后代码进程及其全部子进程会被强制终止，返回已捕获输出尾部"}},
          ["code", "description"]),
    _tool("dcflow_read_doc", "读取知识库文档（filename=\"list\" 返回全部可用文档清单；文档目录: .github/docs、.github/instructions、docs）",
          {"filename": {"type": "string"}}, ["filename"]),
    _tool("dcflow_step_done", "标记步骤完成，完成所有工作后必须调用（summary 写结论，自动挂到当前步骤）。完成声明必须结构化：携带 completion 四态——done/verified 至少一项，或显式声明 unfinished/blocked；blocked 需写明 tried 失败证据与 variants_left 未尝试变体清单（存在未尝试变体不得以受阻为由结束）。unfinished 只写**本步骤职责范围内**尚未完成的事项（不属于本步骤的后续工作写进 summary 建议，不要进 unfinished）",
          {"summary": {"type": "string",
                       "description": "一句话结论（写进步骤报告摘要：做了什么、验证结果、落地位置）"},
           "completion": {"type": "object",
                           "description": "完成声明四态（必填）：done/verified 至少一项；unfinished 仅本步骤职责范围内未完成的事项；受阻走 blocked",
                           "properties": {
                               "done": {"type": "array", "items": {"type": "string"},
                                        "description": "已完成项 → 落地位置（文件/产物/块名）"},
                               "verified": {"type": "array", "items": {"type": "string"},
                                            "description": "验证命令/测试 → 实际结果摘要（必须真跑过）"},
                               "unfinished": {"type": "array", "items": {"type": "string"},
                                              "description": "仅本步骤职责范围内、尚未完成的事项（系统会拒收并要求继续做完；不属于本步骤的后续工作/其他角色事项不要写进来，写进 summary 的建议即可）"},
                               "blocked": {"type": "object", "properties": {
                                   "goal": {"type": "string"},
                                   "tried": {"type": "array", "items": {"type": "string"}},
                                   "variants_left": {"type": "array", "items": {"type": "string"}},
                                   "conclusion_level": {"type": "string",
                                                         "enum": ["implementation", "route"]}}}}}},
          ["summary", "completion"]),
]

_REVERSE_ONLY_TOOLS = {"dcflow_sim", "dcflow_get_decompiled_code",
                       "dcflow_extract_constants", "dcflow_search_bytes",
                       "dcflow_solve_z3"}

_SIM_LOCK_WAIT = 10

_CTF_TOOL_FUNCS = {
    "dcflow_get_decompiled_code": ("get_decompiled_code", ("file_path", "address")),
    "dcflow_extract_constants": ("extract_constants", ("file_path",)),
    "dcflow_search_bytes": ("search_bytes", ("file_path", "pattern")),
    "dcflow_solve_z3": ("solve_z3", ("constraint_script",)),
}

_REVERSE_TOOLS: list[dict] = [
    _tool("dcflow_sim",
          "Windows 程序 Unicorn 模拟器（32/64 位 PE，load 自动识别架构：x64 走 "
          "RCX/RDX/R8/R9 传参 + .pdata SEH/AMD64 CONTEXT/VEH/UEF 异常分发 + "
          "GS/TEB64/PEB64 反调试伪造；环境自动准备，天然绕过全部反调试：PEB/"
          "IsDebuggerPresent/NtGlobalFlag/时间检测/父进程/异常类等无需处理）。"
          "参数 {action, ...}。\n"
          "【轮次纪律（硬规则）】每个 action 单独调用 = 一整轮 LLM 往返（实测均 "
          "218K 上下文）。① 同一目标连续 ≥3 次观察（run/regs/mem/dump 任意组合）"
          "必须用 batch 或 script 一次做完；② 需要「看上一步结果再决定下一步」"
          "（条件链：run → 读寄存器 → 据此定下个断点）时用 script，一段代码里分支"
          "即可——这是 batch 与并行工具调用都做不到的；③ 换输入重跑用 "
          "reset(inputs=[...]) 或 replay(inputs=[...])，禁止 cleanup + load（重建会话"
          "是纯浪费）。\n"
          "【会话】load 加载 PE（自动导入表+API stub+PEB/TEB 伪造+参数环境；"
          "inputs 数组为模拟输入，按程序 scanf 调用顺序依次消费，程序有几次 scanf"
          "（如 name+serial 两次）就传几个输入，否则后面的 scanf 返回 EOF（EAX=0、"
          "缓冲为空，后续 strcpy/memcpy 会死循环卡住））；reset 原地重启（不重建"
          "会话对象，释放旧引擎；可选 inputs/exe 覆盖）；status 会话状态；cleanup "
          "结束会话。\n"
          "【推进】run 执行到 until_addr 即停（断点式推进：停后可用 write/mem 修改"
          "内存，再 run 下一个 until_addr 继续；洗牌/障眼桥等纯计算循环会被引擎"
          "自动快进，stop_reason=fast_auto 表示已自动加速推进一段，继续 run 即可）；"
          "fast 安全区快跑（快照保护 + 失败自动回滚，纯计算循环区秒过——如 main 前 "
          "0x800 次 hex 解析/乘加循环，遇 int3/异常自动回滚状态无损，返回 "
          "fast_rollback 时改用 run）；step 单步 count 条指令（hook 全保留，诊断"
          "逐段观察）。\n"
          "【观察】regs 寄存器；mem 读内存（addr/size）；dump 结构化转储；trace "
          "执行流；output 读程序输出（stdout/OutputDebugString）；dyncode 模拟内"
          "动态解密代码；antidbg 反调试检测报告；blackhole 算力黑洞探测报告。\n"
          "【修改】write 写内存；patch 改字节；hook 开启执行流。\n"
          "【快照】snapshot 存快照；restore 回到快照点（中途回滚）；replay 从头重跑"
          "（黑盒输入对比：inputs 数组优先，回退 name/serial；透传 until_addr/"
          "timeout_seconds）。两者分工明确不混用：replay = 重启程序换输入对比，"
          "restore = 回到已存快照点。\n"
          "【分析管道】deobf 去混淆规则管道（R1-R5：短跳垃圾/pushfd 包裹/恒定条件"
          "跳转/向后扫描/跳转表）；fixcfg 控制流矫正（分发器出口推演）；symexec "
          "混合符号执行（z3 默认/angr 深度，卡点求解）。\n"
          "【批量（优先用这两个，别一次只发一个 action）】batch 声明式命令序列："
          "commands=[{action,...},...] 顺序执行并返回聚合摘要（stop_on_error 默认 "
          "true 遇错即停；timeout_seconds 整体预算默认 60，用尽则停止派发后续命令"
          "并标注剩余条数；verbose 放宽单条上限）——目标场景：load+run+regs+mem+dump "
          "5 轮压成 1 轮、N 组输入扫描压成 1 轮。script 进程内 Python 驱动实时会话："
          "code 里可用 sess（SimSession 全部方法）/uc/cs（capstone）/x86_const/"
          "int_addr/re/json/os/time，只有 print 的内容会返回（timeout_seconds 默认 "
          "30，超时返回已 print 内容并标记会话占用，需 cleanup 重建）——条件链、"
          "循环推进 N 个断点并收集每点寄存器都靠它。",
          {"action": {"type": "string",
                      "description": "子命令名（见描述里的分组清单）；批量优先用 "
                                     "batch/script"},
           "exe": {"type": "string"},
           "name": {"type": "string"}, "serial": {"type": "string"},
           "inputs": {"type": "array", "items": {"type": "string"},
                       "description": "模拟输入数组（按 scanf 调用顺序消费，空串占位）"
                                      "——优先于 name/serial；load/reset/replay 均适用"},
           "addr": {"type": "string"}, "size": {"type": "integer"},
           "count": {"type": "integer",
                     "description": "step 单步指令条数 / fast 快进循环次数上限"},
           "data": {"type": "string"}, "until_addr": {"type": "string"},
           "steps_limit": {"type": "integer"}, "timeout_seconds": {"type": "integer"},
           "out_file": {"type": "string"}, "trace": {"type": "boolean"},
           "commands": {"type": "array", "items": {"type": "object"},
                        "description": "batch 专用：命令对象数组，每项形如 "
                                       "{\"action\": \"run\", \"until_addr\": \"0x401000\"}，"
                                       "按顺序执行（load/cleanup 也可入列）"},
           "stop_on_error": {"type": "boolean",
                             "description": "batch 专用：遇 [Error] 是否即停，默认 true"},
           "verbose": {"type": "boolean",
                       "description": "batch 专用：放宽单条结果截断上限，默认 false"},
           "code": {"type": "string",
                    "description": "script 专用：驱动实时会话的 Python 代码（可用 "
                                   "sess/uc/cs/x86_const/int_addr/re/json/os/time，"
                                   "只有 print 的内容返回）"},
           "description": {"type": "string",
                           "description": "一句话说明本次 batch/script 要干什么——上下文"
                                          "压缩后的历史摘要只用它，务必写清"}},
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
    _tool("dcflow_list_steps", "查看 Task 所有步骤状态（无需传任务，自动归属当前任务）",
          {}),
    _tool("dcflow_adjust_flow",
          "修改流程: skip/add/remove/reorder/mark_complete（无需传任务，自动归属当前任务）。注意: steps_json 必须为步骤对象数组（JSON 数组，禁止字符串形式——字符串内层转义易错导致步骤丢失）",
          {"action": {"type": "string"},
           "step_ids": {"description": "步骤ID列表，逗号分隔字符串或字符串数组",
                        "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
           "steps_json": {"description": "新增步骤的JSON对象数组，每个元素含 step_id/title/description/required/parallel_with/human_attention/type 等字段（human_attention=gate 可插入人工审批步骤；模型档位由系统按类型决定，无需指定）；每个元素可带 after_step_id 指定插入到该步骤之后（优先于顶层 after_step_id；不传则跟随同批次上一个新步骤之后，批次首步按顶层/默认位置）",
                          "type": "array", "items": {"type": "object"}},
           "after_step_id": {"description": "新增步骤统一插入到该步骤之后（可省略；steps_json 元素内嵌的 after_step_id 优先；不传则追加末尾真实步骤之后、review 之前）",
                              "type": "string"},
           "order_json": {"description": "步骤ID顺序数组或JSON字符串",
                          "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
           "reasoning": {"type": "string"}},
          ["action", "reasoning"]),
    dict(_TOOL_LIST_DIR),
    dict(_TOOL_READ_FILE),
    _tool("dcflow_step_done", "提交审查结论并收尾：决策完成（adjust_flow 已执行成功或结论已定）后必须调用。审查结论会自动挂到被审查步骤名下（无需传步骤）；summary 写审查结论摘要。已成功执行的调整（add_steps 等）不得重复调用（系统幂等拦截）",
          {"summary": {"type": "string"}}),
]

_TOOL_STEP_DONE_GATE = _tool("dcflow_step_done",
      "提交审批请求，整理完方案文档后必须调用（summary 写一句话结论，自动挂到当前步骤）。"
      "必须携带 approval 参数：options（供人类点选的决策选项）+ doc_path（方案文档"
      "路径，系统读取原文展示给人类审批）。禁止在对话里输出方案正文/审批报告"
      "——系统直接读文档展示，重复输出浪费且干扰人类阅读",
      {"summary": {"type": "string"},
       "approval": {"type": "object",
                    "description": "审批请求包（系统据此渲染审批界面：文档原文 + 选项）",
                    "required": ["options", "doc_path"],
                    "properties": {
                        "options": {"type": "array", "items": {"type": "object"},
                                    "description": "决策选项数组，每项含 option（一句话方案名）/cost（代价）/risk（风险）/pros（收益）；多方案时逐方案一项，末尾加「拒绝并调整」项"},
                        "doc_path": {"type": "string",
                                     "description": "方案文档路径——直接照抄「前序步骤产物文件」清单里的写法（如 .dc_tmp/<任务ID>/<步骤ID>/artifacts/step2_plan.md），绝对路径也可；文件必须真实存在（系统只校存在性，不校是否在工作区内）；无方案文档的 gate（需求澄清类）传空字符串"},
                        "recommendation": {"type": "string",
                                           "description": "一句话推荐（推荐哪个选项及理由）"},
                        "questions": {"type": "array", "items": {"type": "string"},
                                      "description": "需要人类回答的决策点问题（逐条）"}}}},
      ["summary", "approval"])

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
    _tool("dcflow_list_steps", "查看步骤状态（无需传任务，自动归属当前任务）",
          {}),
    _TOOL_STEP_DONE_GATE,
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
    _tool("dcflow_write_file", "写入/覆盖文件（仅限调研报告等产物，必须写到自己的步骤产物目录 "
          ".dc_tmp/<任务ID>/<步骤ID>/artifacts/ 下，推荐文件名 research-report.md；"
          "不得修改项目/任务代码；文档一次写完整，不要分多次补写同一文件）",
          {"file_path": {"type": "string"}, "content": {"type": "string"}},
          ["file_path", "content"]),
    _tool("dcflow_step_done", "标记步骤完成，完成所有工作后必须调用（summary 写结论，自动挂到当前步骤）。完成声明必须结构化：携带 completion 四态——done/verified 至少一项，或显式声明 unfinished/blocked；blocked 需写明 tried 失败证据与 variants_left 未尝试变体清单（存在未尝试变体不得以受阻为由结束）。unfinished 只写**本步骤职责范围内**尚未完成的事项（不属于本步骤的后续工作写进 summary 建议，不要进 unfinished）",
          {"summary": {"type": "string",
                       "description": "一句话结论（写进步骤报告摘要：做了什么、验证结果、落地位置）"},
           "completion": {"type": "object",
                           "description": "完成声明四态（必填）：done/verified 至少一项；unfinished 仅本步骤职责范围内未完成的事项；受阻走 blocked",
                           "properties": {
                               "done": {"type": "array", "items": {"type": "string"},
                                        "description": "已完成项 → 落地位置（文件/产物/块名）"},
                               "verified": {"type": "array", "items": {"type": "string"},
                                            "description": "验证命令/测试 → 实际结果摘要（必须真跑过）"},
                               "unfinished": {"type": "array", "items": {"type": "string"},
                                              "description": "仅本步骤职责范围内、尚未完成的事项（系统会拒收并要求继续做完；不属于本步骤的后续工作/其他角色事项不要写进来，写进 summary 的建议即可）"},
                               "blocked": {"type": "object", "properties": {
                                   "goal": {"type": "string"},
                                   "tried": {"type": "array", "items": {"type": "string"}},
                                   "variants_left": {"type": "array", "items": {"type": "string"}},
                                   "conclusion_level": {"type": "string",
                                                         "enum": ["implementation", "route"]}}}}}},
          ["summary", "completion"]),
]


_TOOL_ASK_USER = _tool("dcflow_ask_user",
      "向用户提问以澄清需求（仅需求分析师/计划制定者可用）：一次只问一个关键问题，"
      "question 写清要确认什么与为什么需要确认；options 可选（给用户点选的方向）。"
      "调用后步骤暂停等待用户回答，回答会注入对话供你继续；需求已清晰时禁止调用",
      {"question": {"type": "string"},
       "options": {"type": "array", "items": {"type": "string"},
                   "description": "可选：供用户点选的方向（不传则用户自由输入）"}},
      ["question"])

_TOOL_STEP_DONE_WITH_SCORE = _tool("dcflow_step_done",
      "标记步骤完成，完成所有工作后必须调用（summary 写结论）。"
      "必须携带 score 评分参数（6 个字段只填数字/布尔，不写解释文字）——"
      "系统据此自动决定是否需要架构分析师；你不需要也不得自己安排架构步骤",
      {"summary": {"type": "string"},
       "score": {"type": "object",
                 "description": "任务评分（6 个字段，只填数字与布尔值，不写解释）",
                 "required": ["functionPoints", "fileCount", "isMultiModule",
                              "isArchitectural", "clarity", "difficulty"],
                 "properties": {
                     "functionPoints": {"type": "number", "description": "功能点数量"},
                     "fileCount": {"type": "number", "description": "涉及文件数量"},
                     "isMultiModule": {"type": "boolean", "description": "是否多模块"},
                     "isArchitectural": {"type": "boolean", "description": "是否架构改造"},
                     "clarity": {"type": "integer", "description": "需求明确度 1-10（1-3 非常明确/4-6 部分明确/7-10 模糊探索）"},
                     "difficulty": {"type": "integer", "description": "技术难度 1-10（1-3 简单/4-6 中等/7-10 困难）"}}}},
      ["summary"])

_REQUIREMENT_TOOLS: list[dict] = [
    dict(_TOOL_LIST_DIR),
    dict(_TOOL_READ_FILE),
    _tool("dcflow_search_code", "搜索代码/文本（pattern 为 Python 正则，逐行 re.search 匹配），返回 file:line:content 最多 30 条",
          {"pattern": {"type": "string"},
           "path_filter": {"type": "string"}}, ["pattern"]),
    _tool("dcflow_read_doc", "读取知识库文档（filename=\"list\" 返回全部可用文档清单）",
          {"filename": {"type": "string"}}, ["filename"]),
    _tool("dcflow_write_file", "写入/覆盖文件（用户旅程文档等产物；不做代码实现）"
          "（文档一次写完整，不要分多次补写同一文件）",
          {"file_path": {"type": "string"}, "content": {"type": "string"}},
          ["file_path", "content"]),
    _TOOL_ASK_USER,
    _TOOL_STEP_DONE_WITH_SCORE,
]

_PLAN_TOOLS: list[dict] = [*_EXEC_TOOLS, _TOOL_ASK_USER]

_TOOL_ASK_USER_SINGLE = _tool("dcflow_ask_user",
      "向用户提问以澄清需求：一次只问一个关键问题，question 写清要确认什么与"
      "为什么需要确认；options 可选（给用户点选的方向）。调用后等待用户回答，"
      "回答会注入对话供你继续；需求已清晰时禁止调用",
      {"question": {"type": "string"},
       "options": {"type": "array", "items": {"type": "string"},
                   "description": "可选：供用户点选的方向（不传则用户自由输入）"}},
      ["question"])

_SINGLE_CHAT_TOOLS: list[dict] = [*_EXEC_TOOLS, _TOOL_ASK_USER_SINGLE]

_ARCHITECT_TOOLS: list[dict] = [
    dict(_TOOL_LIST_DIR),
    dict(_TOOL_READ_FILE),
    _tool("dcflow_search_code", "搜索代码/文本（pattern 为 Python 正则，逐行 re.search 匹配），返回 file:line:content 最多 30 条",
          {"pattern": {"type": "string"},
           "path_filter": {"type": "string"}}, ["pattern"]),
    _tool("dcflow_read_doc", "读取知识库文档（filename=\"list\" 返回全部可用文档清单）",
          {"filename": {"type": "string"}}, ["filename"]),
    _tool("dcflow_write_file", "写入/覆盖文件（架构方案文档等产物；不做代码实现）"
          "（文档一次写完整，不要分多次补写同一文件）",
          {"file_path": {"type": "string"}, "content": {"type": "string"}},
          ["file_path", "content"]),
    _tool("dcflow_step_done", "标记步骤完成，完成所有工作后必须调用（summary 写结论，自动挂到当前步骤）",
          {"summary": {"type": "string"}}),
]

_ASK_USER_ONLY = {"dcflow_ask_user"}
_ASK_USER_TYPES = ("requirement", "plan")

_REVIEW_ARRAY = {"type": "array", "items": {"type": "string"}}
_TOOL_STEP_DONE_REVIEW = _tool("dcflow_step_done",
      "标记审查完成，审查结束后必须调用（summary 写审查结论摘要）。"
      "必须按分类声明发现的问题（每类字符串数组，无问题传空数组 []）："
      "residual_* = 执行步骤已按方案做完、方案本身仍未解决的问题（非执行偏差，"
      "流程编排者会安排新一轮研究/计划/实现/测试）；execution_* = 没有按方案执行"
      "出现的漏做/做错（系统自动打回：分类清单与审查报告转交开发步骤重做，"
      "无需你安排后续步骤）",
      {"summary": {"type": "string"},
       "residual_business_logic": {**_REVIEW_ARRAY, "description": "按方案做完仍未修复的业务逻辑问题"},
       "residual_architecture_security": {**_REVIEW_ARRAY, "description": "按方案做完仍未修复的架构安全问题"},
       "execution_business_logic": {**_REVIEW_ARRAY, "description": "未按方案执行漏做/做错的业务逻辑问题"},
       "execution_architecture_security": {**_REVIEW_ARRAY, "description": "未按方案执行漏做/做错的架构安全问题"},
       "execution_code_style": {**_REVIEW_ARRAY, "description": "未按方案执行漏做/做错的代码风格问题"},
       "execution_compile_check": {**_REVIEW_ARRAY, "description": "未按方案执行漏做/做错的编译检查问题"}},
      ["summary", "residual_business_logic", "residual_architecture_security",
       "execution_business_logic", "execution_architecture_security",
       "execution_code_style", "execution_compile_check"])

_TOOL_STEP_DONE_PLAN_REVIEW = _tool("dcflow_step_done",
      "标记方案审查完成，审查结束后必须调用（summary 写审查结论摘要）。"
      "必须按分类声明审查发现（每类字符串数组，无内容传空数组 []）："
      "plan_issues = 方案必须修复的缺陷（文档结构缺失/与代码现状不符/用户旅程"
      "未覆盖/验收不可测等——非空即系统自动打回方案设计步骤重做：清单与审查"
      "报告转交方案设计者，无需你安排后续步骤）；plan_suggestions = 不阻塞"
      "审批的改进建议（只随报告与 summary 转交，不触发打回）",
      {"summary": {"type": "string"},
       "plan_issues": {**_REVIEW_ARRAY,
                       "description": "方案必须修复的缺陷（非空系统自动打回方案设计步骤）"},
       "plan_suggestions": {**_REVIEW_ARRAY,
                            "description": "不阻塞审批的改进建议（不触发打回）"}},
      ["summary", "plan_issues", "plan_suggestions"])

_CODE_REVIEW_TOOLS: list[dict] = [
    *(dict(t) for t in _EXEC_TOOLS
      if t["function"]["name"] != "dcflow_step_done"
      and t["function"]["name"] != "dcflow_edit_file"),
    _TOOL_STEP_DONE_REVIEW,
]

_PLAN_REVIEW_TOOLS: list[dict] = [
    *(dict(t) for t in _EXEC_TOOLS
      if t["function"]["name"] not in ("dcflow_step_done", "dcflow_edit_file")),
    _TOOL_STEP_DONE_PLAN_REVIEW,
]

_TEXT_TOOL_CALL_RE = re.compile(r'\{\s*"tool"\s*:\s*"(dcflow_\w+)"\s*,\s*"arguments"\s*:\s*(\{[\s\S]*?\})\s*\}')

_ADJUST_FLOW_ACTIONS = {"add_steps", "skip_steps", "remove_steps",
                        "reorder_steps", "mark_complete", "no_change"}
_NAKED_ADJUST_RE = re.compile(
    r'\{\s*"action"\s*:\s*"(' + "|".join(sorted(_ADJUST_FLOW_ACTIONS)) +
    r')"\s*,')


def _extract_balanced_json(text: str, start: int) -> int:
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


_GATE_OPTIONS_RE = re.compile(r'"options"\s*:\s*\[')


def _gate_report_present(msgs: list[dict], round_text: str,
                         done_call: Optional[dict] = None) -> bool:
    if isinstance(done_call, dict):
        if done_call.get("_is_error", False):
            return False
        _in = done_call.get("input")
        ap = _in.get("approval") if isinstance(_in, dict) else None
        if isinstance(ap, dict) and isinstance(ap.get("options"), list) and ap["options"]:
            return True
    recent = "".join(
        str(m.get("content") or "") for m in msgs if m.get("role") == "assistant"
    ) + (round_text or "")
    if "已确认人类决策" in recent:
        return True
    idx = 0
    while (m := _GATE_OPTIONS_RE.search(recent, idx)):
        win_start = max(0, m.start() - 2000)
        candidates = [i for i in range(win_start, m.start()) if recent[i] == "{"]
        for start in reversed(candidates[-5:]):
            end = _extract_balanced_json(recent, start)
            if end <= 0:
                continue
            try:
                obj = json.loads(recent[start:end])
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict) and isinstance(obj.get("options"), list) and obj["options"]:
                return True
        idx = m.end()
    return False


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


def _tool_schema_allows_empty(name: str, tools: list[dict]) -> bool:
    for t in tools:
        if t.get("function", {}).get("name") == name:
            params = t["function"].get("parameters") or {}
            return not params.get("properties")
    return False


def _tool_schema_of(name: str, tools: list[dict]) -> dict:
    for t in tools:
        fn = t.get("function") or {}
        if fn.get("name") == name:
            params = fn.get("parameters")
            return params if isinstance(params, dict) else {}
    return {}


def _must_pass_keys(params: dict, cap: int) -> list[str]:
    props = params.get("properties")
    props = props if isinstance(props, dict) else {}
    required = params.get("required")
    keys: list[str] = []
    if isinstance(required, list):
        keys = [str(k) for k in required if str(k).strip()]
    for k in props:
        if len(keys) >= cap:
            break
        if k not in keys:
            keys.append(k)
    return keys[:cap]


def _placeholder_value(spec: Any) -> Any:
    if not isinstance(spec, dict):
        return "..."
    t = spec.get("type")
    if t in ("integer", "number"):
        return 0
    if t == "boolean":
        return False
    if t == "array":
        items = spec.get("items")
        if isinstance(items, dict) and items.get("type") == "string":
            return ["..."]
        return []
    if t == "object":
        sub = spec.get("properties")
        if isinstance(sub, dict) and sub:
            first = next(iter(sub))
            return {first: _placeholder_value(sub[first])}
        return {}
    return "..."


def _missing_args_feedback(name: str, tools: list[dict], raw: Any) -> str:
    params = _tool_schema_of(name, tools)
    props = params.get("properties")
    props = props if isinstance(props, dict) else {}
    required_raw = params.get("required")
    required = ([str(k) for k in required_raw] if isinstance(required_raw, list) else [])
    body = ("" if raw is None
            else raw if isinstance(raw, str)
            else json.dumps(raw, ensure_ascii=False))
    body = body.strip()
    shown = "（空）" if not body else (body if len(body) <= 80 else body[:80] + "…")
    lines = [f"【系统提示】你调用的 {name} 参数为空/缺少必填参数，工具未执行。",
             f"本次 arguments = {shown}"]
    if props:
        lines.append("该工具参数要求：")
        for k, spec in list(props.items())[:8]:
            if not isinstance(spec, dict):
                spec = {}
            desc = str(spec.get("description") or "").replace("\n", " ").strip()[:120]
            mark = "必填" if (k in required or not required) else "可选"
            lines.append(f"- {k}（{spec.get('type') or 'object'}，{mark}）"
                         + (f"：{desc}" if desc else ""))
    ex_keys = _must_pass_keys(params, 4)
    example = json.dumps({k: _placeholder_value(props.get(k) or {}) for k in ex_keys},
                         ensure_ascii=False) if ex_keys else "{}"
    lines.append(f"正确写法示例：{name}({example})")
    lines.append("请补齐参数后重新调用（参数写在 arguments 中，不能用对话正文替代）。")
    return "\n".join(lines)[:1500]


def _safe_arguments(name: str, tools: list[dict]) -> str:
    params = _tool_schema_of(name, tools)
    props = params.get("properties")
    props = props if isinstance(props, dict) else {}
    obj = {k: _placeholder_value(props.get(k) or {})
           for k in _must_pass_keys(params, 8)}
    if not obj:
        obj = {"_": ""}
    return json.dumps(obj, ensure_ascii=False)


class _RepetitionDetector:

    MIN_PERIOD = 24
    MAX_PERIOD = 2048
    K = 4
    MAX_TAIL = 8192
    SCAN_STEP = 64
    MIN_DISTINCT = 8

    def __init__(self) -> None:
        self._tail = ""
        self._scanned = 0

    def feed(self, delta: str) -> bool:
        self._tail = (self._tail + (delta or ""))[-self.MAX_TAIL:]
        if len(self._tail) - self._scanned < self.SCAN_STEP:
            return False
        self._scanned = len(self._tail)
        t = self._tail
        max_p = min(self.MAX_PERIOD, len(t) // self.K)
        for p in range(self.MIN_PERIOD, max_p + 1):
            block = t[-p:]
            if len(set(block)) < self.MIN_DISTINCT:
                continue
            for k in range(2, self.K + 1):
                if t[-k * p:-(k - 1) * p] != block:
                    break
            else:
                return True
        return False


def _parse_text_tool_calls(text: str, task_id: str = "") -> list[dict]:
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


_EMPTY_NUDGE = ("【系统提示】你上一轮未输出任何内容。"
                "请基于已有工具结果直接输出本步骤的结论与总结（工作已完成可调用 dcflow_step_done）。")

_SILENT_NUDGE = ("【系统提示】上一轮有输出但没有落到正文，也没有可用的工具调用，"
                 "流程无法推进（本轮实测：completion={completion} token、"
                 "思考 {reasoning_chars} 字）。请把结论写进正文，并调用工具推进或用 "
                 "dcflow_step_done 提交本步骤收尾。")

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
_FILE_WRITE_TOOLS = {"dcflow_write_file", "dcflow_edit_file"}
_FILE_WRITE_NUDGE_FROM = 2
_NUDGE_EXEMPT_FILES = frozenset({_FLOW_REPORT_FILENAME, _CODE_DIAGRAM_FILENAME,
                                 _PROJECT_CONTEXT_FILENAME})
_COMPRESS_MAP_ARTIFACT = "compress_map"
_KEY_FINDINGS_BLOCK_START = "<!-- DC-KEY-FINDINGS-START -->"
_KEY_FINDINGS_BLOCK_END = "<!-- DC-KEY-FINDINGS-END -->"
_KEY_FINDINGS_BLOCK_RE = re.compile(
    rf"(.*){_KEY_FINDINGS_BLOCK_START}\s*## 系统注入：关键发现.*?{_KEY_FINDINGS_BLOCK_END}\s*$", re.S)


def _strip_trailing_kf_block(text: str) -> str:
    if not text or not text.rstrip().endswith(_KEY_FINDINGS_BLOCK_END):
        return text
    return _KEY_FINDINGS_BLOCK_RE.sub(r"\1", text)



_FLOW_REPORT_MAX_CHARS = 17233
_DOC_CHAR_LIMITS = {
    _FLOW_REPORT_FILENAME: _FLOW_REPORT_MAX_CHARS,
    _CODE_DIAGRAM_FILENAME: 8865,
    _PROJECT_CONTEXT_FILENAME: 20262,
}


def _validate_code_diagram_content(content: str) -> str:
    mermaid_blocks = re.findall(r'```(?:mermaid)?[^\n]*\n[\s\S]*?```', content)
    if len(mermaid_blocks) > 1:
        return (f"[Error] {_CODE_DIAGRAM_FILENAME} 只能包含一张 Mermaid 图"
                f"（当前 {len(mermaid_blocks)} 张），请用 subgraph 在同一张图内分区")
    outside_lines = []
    in_fence = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and stripped and not stripped.startswith("#") \
                and not stripped.startswith(">"):
            outside_lines.append(stripped)
    if outside_lines:
        return (f"[Error] {_CODE_DIAGRAM_FILENAME} 的 Mermaid 图之外不能有描述性段落"
                f"（发现 {len(outside_lines)} 行非标题/非引用文本），"
                f"所有描述请放入图内节点标签或 subgraph 标题")
    return ""


def _doc_length_error(filename: str, content: str) -> str:
    limit = _DOC_CHAR_LIMITS.get(filename)
    if not limit:
        return ""
    text = _strip_trailing_kf_block(content) if filename == _FLOW_REPORT_FILENAME else content
    n = len(text.rstrip())
    if n <= limit:
        return ""
    return (f"{filename} 长度超限：写入后总长 {n} 字符 > 上限 {limit} 字符"
            f"（按字符数判定）。请先蒸馏压缩（删过程叙事与重复口径，"
            f"只留结论/读数/残余与未决）再写入。")


def _doc_write_intent(code: str, filename: str) -> Tuple[bool, bool]:
    if not filename or filename not in code:
        return False, False
    lines = code.splitlines()
    win: set = set()
    for i, ln in enumerate(lines):
        if filename in ln:
            win.update(range(i, min(i + 3, len(lines))))
    window = "\n".join(lines[i] for i in sorted(win))
    write_re = re.compile(
        r"open\s*\([^)]*['\"][waxb+]{1,3}['\"]|\.write\s*\(|write_text\s*\(|"
        r"write_bytes\s*\(|writelines\s*\(|os\.replace\s*\(|"
        r"shutil\.(?:copyfile|copy|copy2|move)\s*\(|>>")
    read_re = re.compile(
        r"\.read\s*\(|read_text\s*\(|read_bytes\s*\(|readlines\s*\(|"
        r"json\.load\s*\(|open\s*\((?![^)]*['\"][waxb+]{1,2}['\"])")
    return bool(write_re.search(window)), bool(read_re.search(window))


def _doc_referenced_paths(code: str, filename: str) -> List[str]:
    out: list[str] = []
    pat = re.compile(r"['\"]([^'\"\n]{1,200}" + re.escape(filename) + r")['\"]")
    for m in pat.finditer(code or ""):
        p = m.group(1)
        if p and p not in out:
            out.append(p)
    return out


def _doc_abs_path(root: str, fp: str) -> str:
    p = fp.replace("\\", "/")
    if (not os.path.isabs(fp) and p.startswith(".dc_tmp/")) \
            or (os.path.isabs(fp) and "/.dc_tmp/" in p):
        return os.path.normpath(os.path.join(PROJECT_ROOT, fp))
    return os.path.normpath(os.path.join(root, fp))


_FLOW_SKELETON = "流程报告骨架.md"
_DIAGRAM_SKELETON = "代码流程图骨架.md"
_CONTEXT_SKELETON = "项目背景与约定骨架.md"
_DOC_SKELETON_TEMPLATES = {
    _FLOW_REPORT_FILENAME: _FLOW_SKELETON,
    _CODE_DIAGRAM_FILENAME: _DIAGRAM_SKELETON,
    _PROJECT_CONTEXT_FILENAME: _CONTEXT_SKELETON,
}
_REPORT_LEGACY_SUFFIX = "-步骤报告.md"
_CONTEXT_SUMMARY_SECTION = "任务总结"
_DOC_SKELETON_MAX_REJECTS = 3


def _doc_canonical_name(filename: str) -> str:
    if filename in _DOC_CHAR_LIMITS:
        return filename
    if filename.endswith(_REPORT_LEGACY_SUFFIX):
        return _FLOW_REPORT_FILENAME
    return ""


def _doc_skeleton_template(filename: str) -> str:
    return _DOC_SKELETON_TEMPLATES.get(_doc_canonical_name(filename), "")


def _skeleton_template_path(template: str) -> str:
    from ..prompts import templates_dir
    return os.path.join(templates_dir(), template)


def _doc_headers(content: str, level: int) -> List[str]:
    out: List[str] = []
    in_fence = False
    for line in _strip_trailing_kf_block(content or "").splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not s.startswith("#"):
            continue
        hashes = len(s) - len(s.lstrip("#"))
        if hashes == level and (len(s) == hashes or s[hashes] in " \t"):
            out.append(s[hashes:].strip())
    return out


def _doc_skeleton_error(filename: str, content: str, step_id: str = "") -> str:
    template = _doc_skeleton_template(filename)
    if not template:
        return ""
    path = _skeleton_template_path(template)
    try:
        with open(path, "r", encoding="utf-8") as f:
            want = _doc_headers(f.read(), 2)
    except OSError:
        logger.warning(f"[DC:orch] doc skeleton template missing: {path}")
        return ""
    got = _doc_headers(content, 2)
    missing = [s for s in want if s not in got]
    extra = [s for s in dict.fromkeys(got) if s not in want]
    h3 = _doc_headers(content, 3) if template == _FLOW_SKELETON else []
    if not (missing or extra or h3):
        return ""
    details: List[str] = []
    if missing:
        details.append("缺少章节 " + "、".join(f"`## {s}`" for s in missing))
    if extra:
        details.append("多出章节 " + "、".join(f"`## {s}`" for s in extra))
    if h3:
        details.append(f"不得使用 `###` 小节（发现 {len(h3)} 个，如 `### {h3[0]}`）")
    return (f"{filename} 章节结构不符骨架：{'；'.join(details)}。骨架模板（唯一事实源）"
            f"：{path} —— 请先用 dcflow_read_file 读它，再按骨架整篇重写，章节不多不少："
            + "、".join(f"`## {s}`" for s in want) + "。")


def _doc_diagram_permission_error(step_type: str, template_path: str) -> str:
    return (f"[拒绝写入] 代码流程图只有 planner 可以修改（当前 type={step_type}）："
            f"本步骤职责是忠实执行计划，发现图与实现不符时在流程报告中注明"
            f"「需更新代码流程图：<具体内容>」。骨架模板：{template_path}。")


def _doc_edit_redirect_error(filename: str, template_path: str) -> str:
    return (f"[拒绝写入] {filename} 是骨架固定文档：dcflow_edit_file 的补丁式修改"
            f"无法预校验章节结构，请改用 dcflow_write_file 整篇重写（先读骨架模板再写）："
            f"{template_path}。")


def _doc_section_body(content: str, section: str) -> str:
    body: List[str] = []
    inside = False
    for line in (content or "").splitlines():
        s = line.strip()
        if s.startswith("## ") or s == "##":
            if inside:
                break
            inside = (s[3:].strip() if s != "##" else "") == section
            continue
        if inside:
            body.append(s)
    return "\n".join(body).strip()


def _doc_task_summary_error(step_type: str) -> str:
    return (f"[拒绝写入] 「{_CONTEXT_SUMMARY_SECTION}」章节只有 final-reporter"
            f"（type=report）可以在收尾时改写（当前 type={step_type}）："
            f"其余角色请保持该章节原样，你的结论写进「开发规范」/「项目信息」/"
            f"「经验教训」即可。")


_LAST_PROMPT_TOKENS_ARTIFACT = "last_prompt_tokens"
_FLOW_REPORT_ANCHOR_CHARS = 1500
_STEP_REPORT_ROUNDS_ARTIFACT = "step_report_rounds"
_STEP_REPORT_READ_ROUND_ARTIFACT = "step_report_read_round"
_MONITOR_ANCHORS_ARTIFACT = "monitor_anchors"

_BG_BY_ROLE = {
    "executor":  "这段时间踩的坑（现象/根因/规避做法）补进「经验教训」章节。",
    "reverse":   "这段时间踩的坑（现象/根因/规避做法）补进「经验教训」章节。",
    "plan":      "新增的开发规范/架构约定补进「开发规范」、项目事实补进「项目信息」章节。",
    "architect": "新增的开发规范/架构约定补进「开发规范」、项目事实补进「项目信息」章节。",
    "researcher":  "调研确认的项目事实补进「项目信息」章节。",
    "requirement": "澄清确认的项目事实补进「项目信息」章节。",
    "code_review": "审查发现的规范偏差补进「开发规范」章节。",
    "plan_review": "审查发现的规范偏差补进「开发规范」章节。",
    "gate":        "你只读，不写——发现不符处在输出里列出即可。",
    "monitor":     "你只读，不写——发现不符处在输出里列出即可。",
}



class Orchestrator:

    MAX_ITERATIONS = 100
    MAX_RETRY = 10
    EMPTY_NUDGE_MAX = 3
    CATCHUP_REPLAY_LIMIT = 500
    _ABORT_WAIT = 10.0
    _ABORT_FORCE_WAIT = 5.0
    _ZOMBIE_WAIT = 180.0
    COMPRESS_TOKENS = 400_000
    CONTEXT_FINISH_MIN_TOKENS = COMPRESS_TOKENS
    CONTEXT_BUDGET_TOKENS = 200_000
    KEEP_RECENT_TOOL_ROUNDS = 20
    REPORT_NUDGE_EVERY = 100
    STEP_DONE_AGAIN_WINDOW_S = 180.0
    HEARTBEAT_INTERVAL_S = 15.0

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
        self._loop_tasks: Dict[str, asyncio.Task] = {}
        self._rewind_restarts: Dict[str, str] = {}
        self._rewind_restart_tasks: Dict[str, asyncio.Task] = {}
        self._ask_user_answers: Dict[str, asyncio.Event] = {}
        self._ask_user_results: Dict[str, str] = {}
        self._queued_user_confirms: Dict[str, dict] = {}
        self._prep_cache: Dict[str, dict] = {}
        self._light_route_avoid: Optional[int] = None
        self._last_saved_round: Dict[str, int] = {}
        self._step_t0: Dict[str, float] = {}
        self._step_run_accounted: Dict[str, float] = {}
        self._initial_orchestrating: set = set()
        self._sim_sessions: dict = {}
        self._sim_locks: dict = {}
        self._file_write_counts: Dict[str, int] = {}
        self._doc_skeleton_rejects: Dict[str, int] = {}
        self._round_in_flight: Dict[str, bool] = {}
        self._mid_round_user_seqs: Dict[str, list] = {}
        self._step_live: Dict[Tuple[str, str], dict] = {}
        self._compress_requests: set = set()


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
            if task.get("mode") == "single":
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
            await self.wait_stopped(task_id, timeout=self._ZOMBIE_WAIT)
            if self._running.get(task_id, False):
                t = self._loop_tasks.get(task_id)
                if t is not None and not t.done():
                    logger.info(f"[DC:orch] task {task_id} loop still stuck after "
                                f"{self._ZOMBIE_WAIT:g}s — force cancelling loop task")
                    t.cancel()
                await self.wait_stopped(task_id, timeout=self._ABORT_FORCE_WAIT)
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
        loop_task = asyncio.create_task(self._run_loop(task_id))
        self._loop_tasks[task_id] = loop_task

    async def approve_gate_and_run(self, task_id: str, step_id: str, reason: Optional[str] = None) -> None:
        if self._running.get(task_id, False):
            raise OrchestratorBusyError("该步骤正在审查中，请稍后再审批")
        await self._sm.handle_gate(task_id, step_id, "approved", reason or "")
        await self._insert_monitor_step(task_id, step_id)
        await self._sm._resume_after_intervention(task_id)
        await self.start_task(task_id)

    async def _resolve_prev_step(self, task_id: str, step_id: str,
                                 skip_types: tuple = ()) -> Optional[str]:
        from ..prompts.registry import is_hidden_step
        task = await self._storage.get_task(task_id, include_hidden=True)
        cur = next((s for s in (task or {}).get("steps", [])
                    if s.get("step_id") == step_id), None)
        if not cur:
            return None
        cur_order = cur.get("sort_order", 0)
        prev, prev_order = None, None
        for s in (task or {}).get("steps", []):
            order = s.get("sort_order", 0)
            if order >= cur_order or is_hidden_step(s) \
                    or s.get("status") == "skipped" \
                    or s.get("type") in skip_types:
                continue
            if prev is None or order > prev_order:
                prev, prev_order = s.get("step_id"), order
        return prev

    async def reject_gate_and_run(self, task_id: str, step_id: str, reason: str) -> None:
        if self._running.get(task_id, False):
            raise OrchestratorBusyError("该步骤正在审查中，请稍后再审批")
        from ..prompts.registry import STEP_TYPE_PLAN_REVIEW
        prev = await self._resolve_prev_step(task_id, step_id,
                                             skip_types=(STEP_TYPE_PLAN_REVIEW,))
        if not prev:
            raise ValueError("前面没有可回退的步骤，无法拒绝回退")
        await self._storage.append_event(task_id, {
            "event_type": "gate_decision", "step_id": step_id, "actor": "human",
            "content": {"what_happened": f"Gate rejected: {reason}",
                        "decision": "rejected", "reason": reason},
        })
        await self._sm.reset_flow_from_step(task_id, prev)
        await self._sm.handle_step_intervention(task_id, prev, "send", f"【审批拒绝】{reason}")
        await self.start_task(task_id)

    async def rewind_code_review(self, task_id: str, step_id: str, reason: str,
                                 summary: str = "",
                                 exec_findings: Optional[dict] = None) -> str:
        prev = await self._resolve_prev_step(task_id, step_id)
        if not prev:
            raise ValueError("前面没有可回退的步骤，无法打回审查")
        from ..step_context import get_step_temp_dir
        artifacts_dir = os.path.join(get_step_temp_dir(task_id, step_id), "artifacts")
        names = os.listdir(artifacts_dir) if os.path.isdir(artifacts_dir) else []
        report_names = [n for n in names if n == "review.md"] \
            or [n for n in sorted(names)
                if n.endswith(".md") and not n.startswith("_")]
        files = [f".dc_tmp/{task_id}/{step_id}/artifacts/{n}" for n in report_names]
        msg = f"【审查打回】{reason}"
        if summary:
            msg += f"\n\n## 审查结论摘要\n{summary}"
        if exec_findings:
            _labels = {"execution_business_logic": "业务逻辑问题",
                       "execution_architecture_security": "架构安全问题",
                       "execution_code_style": "代码风格问题",
                       "execution_compile_check": "编译检查问题"}
            msg += "\n\n## 未按方案执行的漏做/做错（必须全部修复）"
            for k, items in exec_findings.items():
                msg += f"\n### {_labels.get(k, k)}\n" + "\n".join(f"- {i}" for i in items)
        if files:
            msg += ("\n\n## 审查报告文件（已保留，可用 dcflow_read_file 读取）\n"
                    + "\n".join(f"- {p}" for p in files))
        zombies = await self._collect_stale_review_steps(task_id, prev, step_id)
        if zombies:
            await self._storage.remove_steps(task_id, zombies)
        await self._sm.reset_flow_from_step(task_id, prev)
        await self._sm.handle_step_intervention(task_id, prev, "send", msg)
        self._rewind_restarts[task_id] = step_id
        return (f"✓ 审查结论已提交（BLOCKED）——已打回到 {prev}，摘要与报告文件已转交，"
                f"流程将自动重做"
                + (f"；已清理 {len(zombies)} 个被本次打回作废的待复核步骤："
                   f"{','.join(zombies)}" if zombies else ""))

    async def _resolve_prev_plan_step(self, task_id: str, step_id: str) -> Optional[str]:
        from ..prompts.registry import is_hidden_step, STEP_TYPE_PLAN
        task = await self._storage.get_task(task_id, include_hidden=True)
        cur = next((s for s in (task or {}).get("steps", [])
                    if s.get("step_id") == step_id), None)
        if not cur:
            return None
        cur_order = cur.get("sort_order", 0)
        plan, plan_order = None, None
        for s in (task or {}).get("steps", []):
            order = s.get("sort_order", 0)
            if order >= cur_order or is_hidden_step(s) \
                    or s.get("status") == "skipped" \
                    or s.get("type") != STEP_TYPE_PLAN:
                continue
            if plan is None or order > plan_order:
                plan, plan_order = s.get("step_id"), order
        return plan or await self._resolve_prev_step(task_id, step_id)

    async def rewind_plan_review(self, task_id: str, step_id: str, reason: str,
                                 summary: str = "",
                                 issues: Optional[list] = None,
                                 suggestions: Optional[list] = None) -> str:
        target = await self._resolve_prev_plan_step(task_id, step_id)
        if not target:
            raise ValueError("前面没有可回退的方案设计步骤，无法打回方案审查")
        from ..prompts.registry import STEP_TYPE_PLAN_REVIEW
        from ..step_context import get_step_temp_dir
        artifacts_dir = os.path.join(get_step_temp_dir(task_id, step_id), "artifacts")
        names = os.listdir(artifacts_dir) if os.path.isdir(artifacts_dir) else []
        report_names = [n for n in names if n == "plan_review.md"] \
            or [n for n in sorted(names)
                if n.endswith(".md") and not n.startswith("_")]
        files = [f".dc_tmp/{task_id}/{step_id}/artifacts/{n}" for n in report_names]
        msg = f"【方案审查打回】{reason}"
        if summary:
            msg += f"\n\n## 审查结论摘要\n{summary}"
        if issues:
            msg += ("\n\n## 方案必须修复的缺陷（逐条）\n"
                    + "\n".join(f"- {i}" for i in issues))
        if suggestions:
            msg += ("\n\n## 改进建议（不阻塞，可一并处置）\n"
                    + "\n".join(f"- {i}" for i in suggestions))
        if files:
            msg += ("\n\n## 审查报告文件（已保留，可用 dcflow_read_file 读取）\n"
                    + "\n".join(f"- {p}" for p in files))
        zombies = await self._collect_stale_review_steps(
            task_id, target, step_id, review_type=STEP_TYPE_PLAN_REVIEW)
        if zombies:
            await self._storage.remove_steps(task_id, zombies)
        await self._sm.reset_flow_from_step(task_id, target)
        await self._sm.handle_step_intervention(task_id, target, "send", msg)
        self._rewind_restarts[task_id] = step_id
        return (f"✓ 方案审查结论已提交（BLOCKED）——已打回到 {target}，"
                f"问题清单与报告文件已转交，流程将自动重做"
                + (f"；已清理 {len(zombies)} 个被本次打回作废的待复核步骤："
                   f"{','.join(zombies)}" if zombies else ""))

    async def _collect_stale_review_steps(self, task_id: str, prev: str,
                                          keep_step_id: str,
                                          review_type: str = "code_review") -> list[str]:
        task = await self._storage.get_task(task_id, include_hidden=True)
        steps = (task or {}).get("steps", []) or []
        prev_order = next((s.get("sort_order", 0) for s in steps
                           if s.get("step_id") == prev), None)
        if prev_order is None:
            return []
        return [s.get("step_id") for s in steps
                if s.get("step_id") and s.get("step_id") != keep_step_id
                and (s.get("sort_order") or 0) > prev_order
                and s.get("type") == review_type
                and s.get("status") == "pending"]

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
        ask_evt = self._ask_user_answers.get(task_id)
        if ask_evt is not None:
            self._ask_user_results[task_id] = ""
            ask_evt.set()
        try:
            from .. import rest_api
            await rest_api.kill_task_cmds(task_id)
        except Exception:  # noqa: BLE001
            pass
        await self.wait_stopped(task_id, timeout=self._ABORT_WAIT)
        if self._running.get(task_id, False):
            t = self._loop_tasks.get(task_id)
            if t is not None and not t.done():
                logger.warning(f"[DC:orch] abort: loop still running after "
                               f"{self._ABORT_WAIT:g}s — force cancelling loop task")
                t.cancel()
            await self.wait_stopped(task_id, timeout=self._ABORT_FORCE_WAIT)

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
                    "\n\n## 前序步骤产物文件（仅列 .md 报告/方案；其余产物在各步骤 artifacts/ 目录，按需读取）\n"
                    + "\n".join(prior_outputs))
        except Exception:  # noqa: BLE001
            pass
        flow_msg = await self._consume_flow_intervention(task_id)
        if flow_msg:
            user_context += f"\n\n## 用户流程级请求\n{flow_msg}"
        if step_id == "monitor-init":
            try:
                raw_hist = await self._storage.get_artifact(task_id, "_flow", "chat_history")
                hist = (raw_hist or {}).get("content") or ""
                if hist.strip():
                    if len(hist) > 150_000:
                        hist = (hist[:20_000] + f"\n\n…（中段截断，原文 "
                                   f"{len(hist)} 字符，保留头部 2 万与尾部）…\n\n"
                                   + hist[-30_000:])
                    user_context += ("\n\n## 前身上下文（本流程由单步骤对话升级而来）\n"
                                     + hist)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DC:orch] chat_history inject failed for {task_id}: {e}")
        return {"system_prompt": system_prompt, "step_context": user_context,
                "model_tier": _STEP_TYPE_MODEL_TIER.get(
                    step_type, "light"),
                "step_title": step_id}

    async def _ensure_tail_steps(self, task_id: str) -> None:
        conn = await self._storage._get_conn()
        exists = {r["step_id"] for r in conn.execute(
            "SELECT step_id FROM task_steps WHERE task_id = ? "
            "AND step_id IN ('review','report')", (task_id,)).fetchall()}
        if "review" not in exists:
            await self._storage.ensure_step(task_id, "review", "最终审查", "review")
        if "report" not in exists:
            await self._storage.ensure_step(task_id, "report", "产出报告", "report")
        conn.execute("UPDATE task_steps SET model_tier='power' "
                     "WHERE task_id=? AND step_id='review'", (task_id,))
        conn.execute("UPDATE task_steps SET model_tier='light' "
                     "WHERE task_id=? AND step_id='report'", (task_id,))
        conn.commit()

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
        if task.get("mode") == "single":
            return
        if self._step_is_review_kind(step_id, ""):
            return
        instance_id = await self._next_monitor_instance_id(task_id)
        try:
            await self._storage.add_steps(task_id, [{
                "step_id": instance_id, "title": f"Monitor 审查 {instance_id}",
                "type": "monitor", "human_attention": "none", "model_tier": "light",
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
            "type": "monitor", "human_attention": "none", "model_tier": "light",
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
        if task and task.get("mode") == "single" and step_id == "chat":
            prep = {"system_prompt": "", "step_context": "", "messages": [],
                    "model_tier": "light", "step_title": "对话"}
        elif step_row and self._step_is_review_kind(step_id, step_row.get("type", "")):
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
                    return
        finally:
            self._running[task_id] = False
            _cur = asyncio.current_task()
            if _cur is not None and self._loop_tasks.get(task_id) is _cur:
                self._loop_tasks.pop(task_id, None)
            self._clear_task_live(task_id)
            if self._rewind_restarts.pop(task_id, None) is not None:
                _rt = asyncio.get_running_loop().create_task(self.start_task(task_id))
                self._rewind_restart_tasks[task_id] = _rt

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
                    if not any(s.get("status") == "pending" for s in hsteps):
                        return
                    logger.info(f"[DC:orch] task {task_id} completed tail_done but "
                                f"pending intervene steps — continuing")
                await self._ensure_tail_steps(task_id)
                logger.info(f"[DC:orch] task {task_id} completed but tail steps "
                            f"pending — backfilling without status rollback")
            if status == "paused":
                task = await self._storage.get_task(task_id, include_hidden=True)
                if task.get("pause_level") == "question":
                    waiting = [s for s in task.get("steps", [])
                               if s.get("status") == "stopped"]
                    if waiting:
                        logger.info(f"[DC:orch] task {task_id} paused awaiting user "
                                    f"answer ({[s['step_id'] for s in waiting]}) — waiting")
                        return
                    await self._storage.update_task(task_id, {"pause_level": None})
                    await self._publish_task_updated(task_id)
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
                if task_before_pause and task_before_pause.get("auto_approve"):
                    await self._submit_step(task_id, first["step_id"])
                    await self._insert_monitor_step(task_id, first["step_id"])
                    logger.info(f"[DC:orch] auto-approve: gate {first['step_id']} "
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
        self._mid_round_user_seqs.pop(f"{task_id}:{step_id}", None)
        self._round_in_flight[f"{task_id}:{step_id}"] = False
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
        is_requirement = _st_type == "requirement"
        is_architect = _st_type == "architect"
        is_single = bool((_t0 or {}).get("mode") == "single" and step_id == "chat")

        self._prep_cache.pop(f"{task_id}:{step_id}", None)

        self._last_saved_round[f"{task_id}:{step_id}"] = 0
        if self._rewind_restarts.get(task_id) == step_id:
            del self._rewind_restarts[task_id]

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
        model_tier = _STEP_TYPE_MODEL_TIER.get(_st_type, "light")
        step_title = prep.get("step_title") or step_id

        seq_start = self._publish(task_id, "stepStart", {"stepId": step_id})
        self._step_live[(task_id, step_id)] = {
            "seq": seq_start, "streaming": True, "roundUid": None,
            "thinking": "", "text": "", "tools": []}
        _t_cycle = time.monotonic()
        self._step_t0[f"{task_id}:{step_id}"] = _t_cycle
        self._step_run_accounted[f"{task_id}:{step_id}"] = _t_cycle

        if not has_existing and not is_single:
            await self._append_message(task_id, step_id,
                                       {"role": "system", "content": system_prompt, "round_num": 0})

        if is_review_kind:
            mon_status = await self._sm._get_step_status(task_id, step_id)
            if mon_status == "completed":
                await self._sm.reset_step_for_continuation(
                    task_id, step_id, "审查步骤重跑续接")
        await self._sm.advance_step(task_id, step_id, "active")

        user_msgs = await self._read_interventions(task_id, step_id)

        if is_single and not user_msgs:
            logger.info(f"[DC:orch] single chat step={step_id} has no new user message — back to pending")
            self._publish(task_id, "streamEnd", {"stepId": step_id})
            self._clear_step_live(task_id, step_id)
            await self._sm.advance_step(task_id, step_id, "pending")
            return

        
        if is_single:
            base_msgs: list[dict] = []
        else:
            base_msgs = [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": step_context}]
        if has_existing:
            compress_points = await self._get_compress_points(task_id, step_id)
            messages = base_msgs + self._sanitize_tool_pairs(
                self._build_lm_messages(existing_messages, compress_points)) + user_msgs
        else:
            messages = base_msgs + user_msgs

        if self._take_compress_request(task_id, step_id):
            logger.info(f"[DC:orch] manual compress request consumed step={step_id}")
        last_tokens = await self._get_last_prompt_tokens(task_id, step_id)
        if has_existing:
            measured = _count_tokens(messages)
            if measured is not None and measured > self.COMPRESS_TOKENS:
                logger.info(f"[DC:orch] resume rebuild measured={measured} > "
                            f"{self.COMPRESS_TOKENS} — pre-compress before send")
                last_tokens = measured
        compressed = await self._check_and_compress(task_id, step_id, messages, last_tokens)
        if compressed is None:
            _m0 = _count_tokens(messages)
            await self._graceful_finish_step(
                task_id, step_id, messages,
                evidence=f"本地判定压无可压：实测 prompt={last_tokens} 估算={_m0}"
                         f"，已压到 {self.KEEP_RECENT_TOOL_ROUNDS} 轮底线仍 >"
                         f" {self.COMPRESS_TOKENS}")
            if not skip_monitor:
                await self._insert_monitor_step(task_id, step_id)
            return
        messages = compressed

        logger.info(f"[DC:orch] start step={step_id} tier={model_tier} gate={is_gate} existing={has_existing}")

        try:
            mcp_tools = await mcp_manager.openai_tools()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] MCP 工具拉取失败: {e}")
            mcp_tools = []

        try:
            _nudge_role = ("plan" if _st_type == "plan"
                           else "monitor" if is_review_kind
                           else "reverse" if is_reverse
                           else "researcher" if is_researcher
                           else "requirement" if is_requirement
                           else "architect" if is_architect
                           else "gate" if is_gate
                           else "code_review" if _st_type == "code_review"
                           else "plan_review" if _st_type == "plan_review"
                           else "executor")
            result = await self._call_llm(
                task_id, step_id, model_tier, messages,
                (self._get_monitor_tools() if is_review_kind
                 else self._get_reverse_tools() if is_reverse
                 else self._get_researcher_tools() if is_researcher
                 else self._get_requirement_tools() if is_requirement
                 else self._get_architect_tools() if is_architect
                 else self._get_single_chat_tools() if is_single
                 else self._get_gate_tools() if is_gate and _st_type not in ("plan", "plan_review")
                 else self._get_code_review_tools() if _st_type == "code_review"
                 else self._get_plan_review_tools() if _st_type == "plan_review"
                 else self._get_plan_tools() if _st_type == "plan"
                 else self._get_exec_tools()) + mcp_tools,
                self._cancel_event(task_id),
                empty_ok=is_review_kind,
                text_finish=(is_gate
                             or (is_review_kind and not step_id.startswith("monitor-"))
                             or is_single),
                give_up_check=_st_type == "executor",
                is_gate=is_gate,
                role=_nudge_role,
                single_chat=is_single)
            logger.info(f"[DC:orch] done step={step_id} text={len(result.get('text', ''))}ch "
                        f"toolCalls={len(result.get('toolCalls', []))}")
            self._publish(task_id, "streamEnd", {"stepId": step_id})
            self._clear_step_live(task_id, step_id)

            if self._is_cancelled(task_id):
                if is_single:
                    if await self._step_is_active(task_id, step_id):
                        await self._sm.advance_step(task_id, step_id, "pending")
                    return
                await self._sm.advance_step(task_id, step_id, "stopped")
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
                self._clear_step_live(task_id, step_id)
                self._cancelled[task_id] = True
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
                if is_single:
                    if await self._step_is_active(task_id, step_id):
                        await self._sm.advance_step(task_id, step_id, "pending")
                else:
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
                if await self._gate_confirmed_by_human(task_id, step_id):
                    logger.info(f"[DC:orch] gate {step_id} confirmed by human — submitting and continuing")
                    await self._submit_step(task_id, step_id)
                    if not skip_monitor:
                        await self._insert_monitor_step(task_id, step_id)
                    return
                return

            if is_review_kind:
                await self._sm.advance_step(task_id, step_id, "completed")
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

            if self._rewind_restarts.get(task_id) == step_id:
                logger.info(f"[DC:orch] step {step_id} rewound by review "
                            f"(step_done passed=false) — skip submit, loop will restart")
                return
            await self._submit_step(task_id, step_id)

            task = await self._storage.get_task(task_id)
            step = self._find_step(task, step_id) if task else None
            if step and step.get("status") != "completed":
                logger.info(f"[DC:orch] step {step_id} status={step.get('status')} after submit — skipping monitor")
                return

            if not skip_monitor:
                await self._insert_monitor_step(task_id, step_id)

        except _StepGracefulFinish:
            if is_single:
                try:
                    await self._sm.reset_step_for_continuation(
                        task_id, step_id, "对话上下文已达上限")
                    await self._append_message(task_id, step_id,
                                               {"role": "system",
                                                "content": "⚠️ 对话上下文已达上限，无法继续本轮对话。"
                                                           "建议点击右上角「切换为多步骤」把对话转交流程继续。",
                                                "round_num": -1})
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[DC:orch] single graceful finish normalize failed: {e}")
                return
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
                if is_single:
                    if await self._step_is_active(task_id, step_id):
                        await self._sm.advance_step(task_id, step_id, "pending")
                    return
                await self._safe_mark_stopped(task_id, step_id)
                return

            if is_single:
                if await self._step_is_active(task_id, step_id):
                    await self._sm.advance_step(task_id, step_id, "pending")
            else:
                await self._safe_mark_stopped(task_id, step_id)
            err_msg = getattr(e, "message", None) or str(e)
            await self._append_message(task_id, step_id,
                                       {"role": "system", "content": f"❌ LLM Error: {err_msg}", "round_num": -1})
            info = self._classify_error(e)
            retry_count = getattr(e, "retry_count", 0)
            self._publish(task_id, "llmError", {
                "stepId": step_id, "code": info["code"], "message": info["message"],
                "retryable": info["retryable"], "retryCount": retry_count,
                "model": getattr(e, "route_model", None),
            })
            self._clear_step_live(task_id, step_id)
            logger.warning(f"[DC:orch] llmError step={step_id} code={info['code']} "
                           f"retryable={info['retryable']} retryCount={retry_count}: {err_msg}")
            self._cancelled[task_id] = True

        finally:
            await self._reorder_mid_round_user_messages(task_id, step_id)
            self._round_in_flight[f"{task_id}:{step_id}"] = False


    async def _call_llm(self, task_id: str, step_id: str, tier: str, msgs: list[dict],
                        tools: list[dict], cancel_event: asyncio.Event,
                        empty_ok: bool = False, text_finish: bool = True,
                        give_up_check: bool = False, is_gate: bool = False,
                        role: str = "", single_chat: bool = False) -> dict:
        full_content = ""
        saved_text_len = 0
        recorded_tool_calls: list[dict] = []
        rounds: list[dict] = []
        retry_count = 0
        done_streak = 0
        last_done_at: Optional[float] = None
        done_struct_prompts = 0
        gate_report_nudges = 0
        done_reject_nudges = 0
        done_completion_rejects = 0
        _round = await self._get_step_report_round(task_id, step_id)
        pending_report_nudge = False
        _diagram_head = ("顺便对照检查一下代码流程图（" + _CODE_DIAGRAM_FILENAME +
                         "，项目级共享）和实现是否还一致——分支条件、增删的路线。")
        if role == "plan":
            _diagram_block = _diagram_head + (
                "不一致就先判断是实现错了还是图画错了：实现错了修代码，"
                "图画错了用 dcflow_write_file 把图重写为新的蒸馏态。"
                "它是项目代码怎么跑，不是任务步骤流程图。\n")
        else:
            _diagram_block = _diagram_head + (
                "不一致的话不要改图，把偏差按「需更新代码流程图：<具体内容>」"
                "写进你的输出或流程报告。"
                "它是项目代码怎么跑，不是任务步骤流程图。\n")
        pending_chunks: list[dict] = []
        empty_streak = 0
        done_via_tool = False

        if graceful.is_draining():
            raise _StepGracefulDrain(step_id)

        no_change_cnt = 0
        review_kind = self._step_is_review_kind(step_id, "")

        while True:
            round_uid = uuid4().hex
            _t_check = await self._storage.get_task(task_id)
            if step_id != "report" and (not _t_check
                                        or _t_check.get("status") in ("completed", "abandoned")):
                logger.info(f"[DC:orch] task {task_id} ended (status="
                            f"{_t_check.get('status') if _t_check else 'gone'}) during "
                            f"step {step_id} — graceful finish")
                await self._graceful_finish_step(task_id, step_id, msgs,
                                                 reason="task_ended")
                raise _StepGracefulFinish(step_id)
            if pending_report_nudge and not single_chat:
                pending_report_nudge = False
                report_nudge = (
                    f"先停一下，整理一轮再继续：重新读取一次流程报告（{_FLOW_REPORT_FILENAME}，"
                    "在你的临时文件夹，所有步骤共享，没有就用 dcflow_write_file 建），"
                    "把这段时间尝试过的、碰壁的、成功的以及有用的结论合并进去，"
                    "不要重复内容、不要有多处工作焦点，各章节深度整合，不要只读一部分。"
                    "**整合完成后必须继续推进当前步骤的剩余任务，不要整理完报告就结束步骤**\n"
                    + _diagram_block +
                    f"项目背景与约定（{_PROJECT_CONTEXT_FILENAME}，同目录，项目级共享）："
                    + _BG_BY_ROLE.get(role, _BG_BY_ROLE["executor"]))
                msgs.append({"role": "user", "content": report_nudge})
                await self._announce_user_message(task_id, step_id, report_nudge)
                logger.info(f"[DC:orch] report nudge after {_round} tool rounds for step={step_id}")
            if self._take_compress_request(task_id, step_id):
                logger.info(f"[DC:orch] manual compress request consumed step={step_id}")
            last_tokens = await self._get_last_prompt_tokens(task_id, step_id)
            compressed = await self._check_and_compress(task_id, step_id, msgs, last_tokens)
            if compressed is None:
                _m1 = _count_tokens(msgs)
                await self._graceful_finish_step(
                    task_id, step_id, msgs,
                    evidence=f"本地判定压无可压（轮首）：实测 prompt={last_tokens} "
                             f"估算={_m1}，已压到 {self.KEEP_RECENT_TOOL_ROUNDS} 轮底线仍 >"
                             f" {self.COMPRESS_TOKENS}")
                raise _StepGracefulFinish(step_id)
            msgs = compressed
            await self._inject_interventions(task_id, step_id, msgs, no_prefix=single_chat)

            self._round_in_flight[f"{task_id}:{step_id}"] = True
            client = self._make_llm_client(tier)
            logger.info(f"[DC:step] step={step_id} tier={tier} model={client._model} msgs={len(msgs)}")

            round_text = ""
            round_reasoning = ""
            repetition = _RepetitionDetector()
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
                        self._emit_chunk(task_id, step_id, text, entity_id=round_uid)
                        pending_chunks.append({"chunk_type": "text", "content": text})
                        if repetition.feed(text):
                            raise LlmError(0, "LLM 输出重复循环：相同文本连续重复，已断开自动重试")
                    elif ev["type"] == "reasoning":
                        _t_now = time.monotonic()
                        if first_chunk_t is None:
                            first_chunk_t = _t_now
                        last_chunk_t = _t_now
                        round_reasoning += ev["text"]
                        if repetition.feed(ev["text"]):
                            raise LlmError(0, "LLM 思考重复循环：思考内容连续重复，已断开自动重试")
                        seq_think = self._publish(task_id, "thinkingChunk",
                                                  {"stepId": step_id, "chunk": ev["text"],
                                                   "entityId": round_uid})
                        self._update_step_live(task_id, step_id, seq=seq_think,
                                               thinking=ev["text"], streaming=True,
                                               roundUid=round_uid)
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
                                seq_ts = self._publish(task_id, "toolCallStart", {
                                    "stepId": step_id, "callId": slot["id"],
                                    "entityId": slot["id"],
                                    "toolName": slot["name"], "input": ""})
                                self._upsert_step_live_tool(
                                    task_id, step_id, slot["id"],
                                    name=slot["name"], input_="", seq=seq_ts)
                                for d in slot["buf"]:
                                    seq_tp = self._publish(task_id, "toolCallParam", {
                                        "stepId": step_id, "callId": slot["id"],
                                        "entityId": slot["id"], "delta": d})
                                    self._append_step_live_tool_param(
                                        task_id, step_id, slot["id"], d, seq=seq_tp)
                                slot["buf"] = []
                            if ev["delta"]:
                                seq_tp = self._publish(task_id, "toolCallParam", {
                                    "stepId": step_id, "callId": slot["id"],
                                    "entityId": slot["id"], "delta": ev["delta"]})
                                self._append_step_live_tool_param(
                                    task_id, step_id, slot["id"], ev["delta"], seq=seq_tp)
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
                if single_chat and pending_chunks:
                    try:
                        await self._save_chunks(task_id, step_id, pending_chunks)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[DC:orch] save partial chunks failed step={step_id}: {e}")
                    pending_chunks.clear()
                raise
            except LlmError as e:
                logger.warning(f"[DC:orch] LLM error step={step_id} round={_round} "
                               f"status={getattr(e, 'status', '?')} msg={getattr(e, 'message', e)!r}")
                _err_run = self._account_run_delta(task_id, step_id, since=t_req)
                if _err_run > 0:
                    try:
                        await self._update_step_stats(task_id, step_id, run_ms=_err_run)
                    except Exception as _se:  # noqa: BLE001
                        logger.warning(f"[DC:orch] step stats save failed step={step_id}: {_se}")
                info = self._classify_error(e)
                _measured = _count_tokens(msgs)
                _last_tp = await self._get_last_prompt_tokens(task_id, step_id)
                _size = max(_measured or 0, _last_tp or 0)
                if _size >= self.CONTEXT_FINISH_MIN_TOKENS and info["code"] not in (
                        "rate_limit", "forbidden", "server_error", "network"):
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
                        self._abort_round_live(task_id, step_id, round_uid)
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
                        self._abort_round_live(task_id, step_id, round_uid)
                        continue
                    logger.info(f"[DC:orch] context exceeded step={step_id} — 实测规模 "
                                f"{_size} ≥ {self.CONTEXT_FINISH_MIN_TOKENS} 且无可压缩"
                                f" — graceful finish")
                    _err_text = str(getattr(e, "message", None) or e)
                    await self._graceful_finish_step(
                        task_id, step_id, msgs,
                        evidence=f"实测规模={_size}（估算={_measured} / 最近 usage "
                                 f"prompt={_last_tp}）≥ 判据 "
                                 f"{self.CONTEXT_FINISH_MIN_TOKENS}；HTTP status="
                                 f"{getattr(e, 'status', '?')}；服务端原文={_err_text[:200]}")
                    raise _StepGracefulFinish(step_id)
                if (tier == "light"
                        and getattr(client, "_route_group", 0) in (1, 2)
                        and info["code"] in ("rate_limit", "server_error", "network")):
                    self._light_route_avoid = client._route_group
                    logger.info(f"[DC:route] light group={client._route_group} "
                                f"error={info['code']} — next request forced to the other model")
                e.route_model = getattr(client, "_model", None)
                if info["retryable"] and retry_count < self.MAX_RETRY:
                    if self._is_cancelled(task_id):
                        logger.info(f"[DC:ai] cancelled during retry for step={step_id} "
                                    f"— aborting instead of retrying")
                        raise e
                    pending_chunks.clear()
                    if round_text:
                        full_content = full_content[:-len(round_text)]
                        saved_text_len = min(saved_text_len, len(full_content))
                    self._abort_round_live(task_id, step_id, round_uid)
                    retry_count += 1
                    delay = min(1000 * (2 ** (retry_count - 1)), 30000)
                    self._publish(task_id, "retryProgress", {
                        "stepId": step_id, "retryCount": retry_count,
                        "total": self.MAX_RETRY})
                    logger.info(f"[DC:ai] rate limited, retry {retry_count}/{self.MAX_RETRY} "
                                f"in {delay}ms")
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
                parsed = _parse_text_tool_calls(round_text, task_id)
                if parsed:
                    logger.info(f"[DC:tool] text-parsed {len(parsed)}: "
                                f"{', '.join(p['name'] for p in parsed)}")
                    for p in parsed:
                        round_tool_slots.append({
                            "id": p["call_id"], "name": p["name"],
                            "arguments": json.dumps(p["input"], ensure_ascii=False)})
                    round_text = _TEXT_TOOL_CALL_RE.sub("", round_text).strip()
                    while True:
                        _m = _NAKED_ADJUST_RE.search(round_text)
                        if not _m:
                            break
                        _end = _extract_balanced_json(round_text, _m.start())
                        if _end < 0:
                            break
                        round_text = round_text[:_m.start()] + round_text[_end:]
                    round_text = round_text.strip()

            _t_end = time.monotonic()
            _ttft = int((first_chunk_t - t_req) * 1000) if first_chunk_t is not None else 0
            _out = int((last_chunk_t - first_chunk_t) * 1000) if first_chunk_t is not None else 0
            _run = self._account_run_delta(task_id, step_id, since=t_req)
            try:
                await self._update_step_stats(
                    task_id, step_id, requests=1,
                    ttft_ms=_ttft if _ttft > 0 else None,
                    output_ms=_out, run_ms=_run)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DC:orch] step stats save failed step={step_id}: {e}")

            if round_tool_slots:
                _round += 1
                try:
                    await self._save_step_report_round(task_id, step_id, _round)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[DC:orch] step report round save failed step={step_id}: {e}")
                if _round > 0 and _round % self.REPORT_NUDGE_EVERY == 0 and not review_kind:
                    pending_report_nudge = True

                if self._is_cancelled(task_id):
                    logger.info(f"[DC:orch] cancelled during tool execution, step={step_id}")
                    return {"text": full_content, "toolCalls": recorded_tool_calls, "rounds": rounds}

                bad_empty = [tc for tc in round_tool_slots
                             if _is_empty_arguments(tc.get("arguments"))
                             and not _tool_schema_allows_empty(tc["name"], tools)]
                for tc in bad_empty:
                    tc["_raw_args"] = tc.get("arguments")
                    tc["_bad_args"] = True
                    tc["arguments"] = _safe_arguments(tc["name"], tools)
                    logger.warning(f"[DC:orch] empty-arguments tool call step={step_id} "
                                   f"round={_round} — hint as tool result: {tc['name']}")

                if round_tool_slots and any(tc.get("name") != "dcflow_step_done"
                                            for tc in round_tool_slots):
                    done_streak = 0

                async def _preflight_step_done(args_obj: dict) -> Optional[str]:
                    nonlocal done_streak, last_done_at
                    nonlocal gate_report_nudges, done_struct_prompts
                    nonlocal done_completion_rejects
                    from .. import rest_api
                    if is_gate:
                        _ap = args_obj.get("approval")
                        _ap_err = (rest_api._validate_approval(_ap)
                                   if isinstance(_ap, dict) else "")
                        if not _ap_err and _gate_report_present(
                                msgs, round_text,
                                {"input": args_obj, "_is_error": False}):
                            return None
                        if gate_report_nudges >= 2:
                            logger.warning(f"[DC:orch] gate step {step_id} done without report "
                                           f"after {gate_report_nudges} nudges — keeping gate wait")
                            return None
                        gate_report_nudges += 1
                        logger.info(f"[DC:orch] gate report nudge for step={step_id} "
                                    f"at round {_round} — preflight reject "
                                    f"({gate_report_nudges}/2)")
                        if isinstance(_ap, dict):
                            return (
                                "【系统提示】你的 dcflow_step_done 被系统拒收，审批请求未提交："
                                + _ap_err
                                + " 请修正后重新调用 dcflow_step_done 提交 approval"
                                "（doc_path 用 dcflow_list_dir 确认真实存在的方案文档路径，"
                                "写完整相对路径；无方案文档的 gate 传空字符串）。"
                                "不要在对话里输出方案正文——系统直接读文档展示。")
                        return (
                            "【系统提示】你请求完成 gate 审批步骤，但 dcflow_step_done 未携带"
                            "合法 approval 参数。请在 approval 中提交：options（决策选项数组，"
                            "每项含 option/cost/risk/pros；多方案逐方案一项 + 末尾「拒绝并调整」）"
                            "+ doc_path（方案文档相对路径，系统读取原文展示给人类审批）"
                            "+ recommendation/questions。不要在对话里输出方案正文——系统直接"
                            "读文档展示；若人类已在对话中拍板，则输出「✅ 已确认人类决策："
                            "<决策内容>」后再调用 dcflow_step_done。")
                    if not give_up_check:
                        return None
                    _now_mono = time.monotonic()
                    _done_in_window = (last_done_at is not None
                                       and _now_mono - last_done_at
                                       <= self.STEP_DONE_AGAIN_WINDOW_S)
                    last_done_at = _now_mono
                    if done_streak < 1 and not _done_in_window:
                        done_streak += 1
                        logger.info(f"[DC:orch] done-check nudge for step={step_id} "
                                    f"at round {_round}")
                        return (
                            "请先核对：步骤目标"
                            "（任务描述/已批方案）是否已全部达成？验证命令是否都实际"
                            "跑过且通过？结果有无可疑之处？是不是还存在尚未尝试的路径或未跑"
                            "的验证？真的全部都做完了吗？")
                    _comp = args_obj.get("completion")
                    if not isinstance(_comp, dict):
                        if done_struct_prompts >= 2:
                            logger.warning(f"[DC:orch] step {step_id} done without structured "
                                           f"completion after {done_struct_prompts} prompts — pass")
                            return None
                        done_struct_prompts += 1
                        _sent = ("、".join(str(k) for k in args_obj)
                                 if args_obj else "（无）")
                        logger.info(f"[DC:orch] done-structured prompt for step={step_id} "
                                    f"at round {_round}")
                        return (
                            "【系统提示】你的 dcflow_step_done 缺少必填参数 completion"
                            f"（本次只传了：{_sent}）。完成声明必须结构化，正确写法："
                            'dcflow_step_done({"summary": "...", "completion": '
                            '{"done": ["已完成项→落地位置"], '
                            '"verified": ["验证命令→实际结果"]}})。'
                            "四态含义：done/verified 至少一项；unfinished 只写"
                            "本步骤职责范围内未完成的事项（不属于本步骤的后续工作写进"
                            "summary 的建议，不要进 unfinished）；受阻走 blocked"
                            "（tried 失败证据 + variants_left 为空）。")
                    _cerr = rest_api._validate_completion(_comp)
                    if _cerr and done_completion_rejects < 3:
                        done_completion_rejects += 1
                        logger.info(f"[DC:orch] completion rejected for step={step_id} "
                                    f"at round {_round} — preflight reject "
                                    f"({done_completion_rejects}/3)")
                        return f"[Error] {_cerr}"
                    if _cerr:
                        logger.warning(f"[DC:orch] step {step_id} completion still rejected "
                                       f"after {done_completion_rejects} — passing through")
                    return None

                if graceful.is_draining():
                    logger.info(f"[DC:orch] graceful drain during tool execution, step={step_id}")
                    raise _StepGracefulDrain(step_id)

                empty_streak = 0

                logger.info(f"[DC:tool] executing {len(round_tool_slots)} tool(s): "
                            f"{', '.join(tc['name'] for tc in round_tool_slots)}")
                seq_te = self._publish(task_id, "toolExecuting", {
                    "stepId": step_id,
                    "callIds": [tc["id"] for tc in round_tool_slots],
                    "entityIds": [tc["id"] for tc in round_tool_slots],
                    "toolNames": [tc["name"] for tc in round_tool_slots]})
                if round_tool_slots:
                    self._update_step_live(task_id, step_id, seq=seq_te, streaming=False)
                    for tc in round_tool_slots:
                        self._upsert_step_live_tool(
                            task_id, step_id, tc["id"], status="executing")
                if round_text.strip():
                    rounds.append({"text": round_text, "toolCalls": [], "roundUid": round_uid})
                    full_content = ""
                    saved_text_len = 0

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
                    _retry = False
                    _raw_err: Optional[bool] = None
                    _disp_args = tc.get("_raw_args", tc["arguments"])
                    try:
                        input_obj = json.loads(tc["arguments"] or "{}")
                    except (ValueError, TypeError):
                        input_obj = {}
                    if not isinstance(input_obj, dict):
                        input_obj = {}
                    seq_ts = self._publish(task_id, "toolCallStart", {
                        "stepId": step_id, "callId": tc["id"], "entityId": tc["id"],
                        "toolName": tc["name"],
                        "input": _disp_args})
                    self._upsert_step_live_tool(
                        task_id, step_id, tc["id"], name=tc["name"],
                        input_=_disp_args or "", seq=seq_ts)
                    pending_chunks.append({
                        "chunk_type": "tool_call_start", "content": tc["name"], "call_id": tc["id"]})
                    if tc["name"] == "dcflow_ask_user":
                        try:
                            _ask_args = json.loads(tc["arguments"] or "{}")
                        except (json.JSONDecodeError, TypeError):
                            _ask_args = {}
                        _ask_q = str(_ask_args.get("question") or "").strip()
                        _ask_opts = [str(x).strip() for x in (_ask_args.get("options") or [])
                                     if str(x).strip()][:6]
                        await self._storage.save_artifact(
                            task_id, step_id, "question",
                            json.dumps({"question": _ask_q, "options": _ask_opts,
                                        "call_id": tc["id"]}, ensure_ascii=False), "json")
                        self._publish(task_id, "askUser", {
                            "stepId": step_id, "callId": tc["id"],
                            "question": _ask_q, "options": _ask_opts or None,
                        })
                        _ask_evt = asyncio.Event()
                        self._ask_user_answers[task_id] = _ask_evt
                        self._clear_step_live(task_id, step_id)
                        try:
                            await _ask_evt.wait()
                        finally:
                            self._ask_user_answers.pop(task_id, None)
                        output_text = f"用户回答：{self._ask_user_results.pop(task_id, '')}"
                        await self._storage.save_artifact(task_id, step_id, "question", "", "json")
                    elif tc.get("_bad_args"):
                        output_text = _missing_args_feedback(
                            tc["name"], tools, tc.get("_raw_args"))
                    elif tc["name"] == "dcflow_step_done":
                        _syn = await _preflight_step_done(input_obj)
                        if _syn is not None:
                            output_text = _syn
                            _raw_err = output_text.startswith("[Error]")
                            _retry = True
                        else:
                            output_text = await self._invoke_tool(
                                task_id, step_id, tc["name"], tc["arguments"], tc["id"])
                            _raw_err = output_text.startswith("[Error]")
                            if _raw_err:
                                if is_gate and gate_report_nudges < 2:
                                    gate_report_nudges += 1
                                    _retry = True
                                    output_text = (
                                        "【系统提示】你的 dcflow_step_done 被系统拒收，"
                                        "审批请求未提交：" + output_text[:800]
                                        + " 请修正后重新调用 dcflow_step_done 提交 approval"
                                        "（doc_path 用 dcflow_list_dir 确认真实存在的方案文档"
                                        "路径，写完整相对路径；无方案文档的 gate 传空字符串）。"
                                        "不要在对话里输出方案正文——系统直接读文档展示。")
                                elif (not is_gate and not give_up_check
                                        and done_reject_nudges < 2):
                                    _retry = True
                                    done_reject_nudges += 1
                                    output_text = (
                                        "【系统提示】你的 dcflow_step_done 被系统拒收，"
                                        "步骤并未完成：" + output_text[:800]
                                        + " 请按上述要求补齐参数后重新调用 dcflow_step_done"
                                        "（参数不能用对话正文替代）。")
                                elif (give_up_check
                                        and isinstance(input_obj.get("completion"), dict)
                                        and done_completion_rejects < 3):
                                    _retry = True
                                    done_completion_rejects += 1
                    else:
                        output_text = await self._invoke_tool(task_id, step_id, tc["name"], tc["arguments"], tc["id"])
                    if review_kind and tc["name"] == "dcflow_adjust_flow":
                        if "action=no_change" in output_text:
                            no_change_cnt += 1
                        else:
                            no_change_cnt = 0
                    _is_err = (_raw_err if _raw_err is not None
                               else output_text.startswith("[Error]"))
                    tc_rec = {"name": tc["name"], "input": input_obj, "output": output_text,
                              "call_id": tc["id"], "_is_error": _is_err,
                              "_retry": _retry}
                    recorded_tool_calls.append(tc_rec)
                    round_tcs.append(tc_rec)
                    logger.info(f"[DC:tool] {tc['name']} OK: {output_text[:80]!r}")
                    seq_res = self._publish(task_id, "toolCallResult", {
                        "stepId": step_id, "callId": tc["id"], "entityId": tc["id"],
                        "toolName": tc["name"],
                        "output": output_text[:5000]})
                    self._update_step_live(task_id, step_id, seq=seq_res, streaming=True)
                    self._upsert_step_live_tool(
                        task_id, step_id, tc["id"], output=output_text[:5000],
                        status="done")
                    pending_chunks.append({
                        "chunk_type": "tool_call_result", "content": output_text, "call_id": tc["id"]})
                    msgs.append({"role": "tool", "content": output_text, "tool_call_id": tc["id"]})

                rounds.append({"text": "", "toolCalls": round_tcs, "roundUid": round_uid,
                               "reasoning": round_reasoning})

                if self._rewind_restarts.get(task_id) == step_id:
                    pending_chunks.clear()
                else:
                    await self._append_round_messages(task_id, step_id, rounds)
                    await self._save_chunks(task_id, step_id, pending_chunks)
                    pending_chunks.clear()
                    await self._reorder_mid_round_user_messages(task_id, step_id)
                    self._round_in_flight[f"{task_id}:{step_id}"] = False
                    self._clear_step_live_round(task_id, step_id)

                    await self._capture_key_findings(task_id, step_id,
                                                     round_text + "\n" + round_reasoning)

                if no_change_cnt >= (1 if step_id.startswith("monitor-") else 2):
                    logger.info(f"[DC:orch] review step {step_id} converged "
                                f"({no_change_cnt}× no_change) — ending rounds")
                    done_via_tool = True
                    break

                _done_bad_args = any(tc.get("_bad_args")
                                     for tc in round_tool_slots
                                     if tc.get("name") == "dcflow_step_done")
                if (any(tc.get("name") == "dcflow_step_done" for tc in round_tool_slots)
                        and not _done_bad_args):
                    _done_call = next((tc for tc in round_tcs
                                       if tc.get("name") == "dcflow_step_done"), None)
                    if _done_call is not None and _done_call.get("_retry"):
                        logger.info(f"[DC:orch] step_done rejected for step={step_id} "
                                    f"at round {_round} — another round")
                        continue
                    if _done_call is not None and _done_call.get("_is_error"):
                        logger.warning(f"[DC:orch] step {step_id} step_done still rejected "
                                       f"after nudges — passing through")
                    done_via_tool = True
                    logger.info(f"[DC:orch] dcflow_step_done called for step={step_id} — "
                                f"completing step")
                    break

                compressed = await self._check_and_compress(task_id, step_id, msgs, round_usage["prompt"])
                if compressed is None:
                    _m2 = _count_tokens(msgs)
                    await self._graceful_finish_step(
                        task_id, step_id, msgs,
                        evidence=f"本地判定压无可压（轮末）：本轮 usage prompt="
                                 f"{round_usage['prompt']} 估算={_m2}，已压到 "
                                 f"{self.KEEP_RECENT_TOOL_ROUNDS} 轮底线仍 >"
                                 f" {self.COMPRESS_TOKENS}")
                    raise _StepGracefulFinish(step_id)
                if compressed is not msgs:
                    msgs[:] = compressed
                    logger.info(f"[DC:ai] compressed to {len(msgs)} messages after round {_round}")
                continue

            if round_text.strip() or round_reasoning.strip():
                rounds.append({"text": round_text, "toolCalls": [], "roundUid": round_uid,
                               "reasoning": round_reasoning})
                if self._rewind_restarts.get(task_id) != step_id:
                    await self._append_round_messages(task_id, step_id, rounds)
                    await self._save_chunks(task_id, step_id, pending_chunks)
                    pending_chunks.clear()
                    await self._reorder_mid_round_user_messages(task_id, step_id)
                    self._round_in_flight[f"{task_id}:{step_id}"] = False
                saved_text_len = len(full_content)
            if round_text.strip() or round_reasoning.strip():
                await self._capture_key_findings(task_id, step_id,
                                                 round_text + "\n" + round_reasoning)
            if not round_text.strip():
                empty_streak += 1
                if empty_ok:
                    break
                _spoke = first_chunk_t is not None
                _comp = round_usage["completion"]
                if empty_streak <= self.EMPTY_NUDGE_MAX:
                    _nudge = (_EMPTY_NUDGE
                              if not (_spoke or _comp or round_reasoning.strip())
                              else _SILENT_NUDGE.format(
                                  completion=_comp,
                                  reasoning_chars=len(round_reasoning)))
                    msgs.append({"role": "user", "content": _nudge})
                    await self._announce_user_message(task_id, step_id, _nudge,
                                                      round_num=len(rounds))
                    logger.warning(f"[DC:orch] empty response for step={step_id} — "
                                   f"auto retry {empty_streak}/{self.EMPTY_NUDGE_MAX} "
                                   f"spoke={_spoke} completion={_comp} "
                                   f"reasoning_chars={len(round_reasoning)} "
                                   f"text_chars={len(round_text)}")
                    continue
                break
            empty_streak = 0
            if text_finish:
                break
            continue

        if self._rewind_restarts.get(task_id) == step_id:
            pending_chunks.clear()
        else:
            tail = full_content[saved_text_len:]
            if tail.strip():
                rounds.append({"text": tail, "toolCalls": [], "roundUid": round_uid,
                               "reasoning": round_reasoning})
                await self._append_message(task_id, step_id,
                                           {"role": "assistant", "content": tail,
                                            "round_num": len(rounds) - 1,
                                            "reasoning": round_reasoning},
                                           entity_id=round_uid)
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

    async def _inject_interventions(self, task_id: str, step_id: str, msgs: list[dict],
                                    no_prefix: bool = False) -> None:
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
            delivered: list[dict] = []
            for iv in iv_list:
                itype = iv.get("type", "") if isinstance(iv, dict) else ""
                msg = iv.get("message", "") if isinstance(iv, dict) else ""
                if itype in ("pre_tool_injection", "force_inject") and msg:
                    msgs.append({"role": "user",
                                 "content": msg if no_prefix else "[用户介入]: " + msg})
                    if not iv.get("persisted"):
                        await self._announce_user_message(task_id, step_id, msg)
                    item = self._take_queued_user_confirm(
                        task_id, step_id, iv.get("client_msg_id") if isinstance(iv, dict) else None,
                        msg)
                    if item:
                        delivered.append(item)
                    logger.info(f"[DC:orch] injected intervention [{itype}]: {msg[:50]}")
                else:
                    remaining.append(iv)

            await self._storage.save_artifact(task_id, step_id, "intervention",
                                              json.dumps(remaining, ensure_ascii=False), "json")
            if delivered:
                self._publish(task_id, "messagesConfirmed",
                              {"stepId": step_id, "items": delivered})
        except Exception:
            pass

    async def _read_interventions(self, task_id: str, step_id: str) -> list[dict]:
        user_msgs: list[dict] = []
        delivered: list[dict] = []
        try:
            raw = await self._storage.get_artifact(task_id, step_id, "intervention")
            if raw and raw.get("content"):
                data = json.loads(raw["content"])
                iv_list = data if isinstance(data, list) else [data]
                for iv in iv_list:
                    msg = iv.get("message") if isinstance(iv, dict) else None
                    if msg:
                        user_msgs.append({"role": "user", "content": msg})
                        if not iv.get("persisted"):
                            await self._announce_user_message(task_id, step_id, msg)
                        item = self._take_queued_user_confirm(
                            task_id, step_id, iv.get("client_msg_id"), msg)
                        if item:
                            delivered.append(item)
                if user_msgs:
                    await self._storage.save_artifact(task_id, step_id, "intervention", "[]", "json")
        except Exception:
            pass
        if delivered:
            self._publish(task_id, "messagesConfirmed",
                          {"stepId": step_id, "items": delivered})
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
                entry["reasoning_content"] = str(m.get("reasoning") or "")
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
                if not entry["content"] and not entry.get("tool_calls") \
                        and entry["reasoning_content"]:
                    entry["content"] = " "
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
        if status in (502, 503, 504) or re.search(
                r"\b(?:502|503|504)\b|bad.?gateway|gateway.?timeout|service.?unavailable", msg):
            return {"code": "server_error", "message": getattr(err, "message", None) or str(err),
                    "retryable": True}
        if re.search(r"请求失败|响应中断|连接|readerror|httpx|超时|reset|broken pipe|network|重复循环", msg):
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
                total += len(str(m.get("reasoning_content") or ""))
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
                    str(out[start].get("reasoning_content") or "")) + len(
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
        elif name == "dcflow_run_code":
            d = str(args.get("description") or "").strip()
            if not d:
                _c = str(args.get("code") or "").strip()
                d = _c.splitlines()[0] if _c else ""
            s = f"执行代码：{d}" if d else "执行代码"
        elif name == "dcflow_sim":
            act = str(args.get("action") or "")
            d = str(args.get("description") or "").strip()
            tgt = str(args.get("until_addr") or args.get("addr") or "").strip()
            tail = d or tgt
            s = f"模拟器 {act}：{tail}" if tail else f"模拟器 {act}"
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

    def _take_compress_request(self, task_id: str, step_id: str) -> bool:
        key = f"{task_id}:{step_id}"
        if key in self._compress_requests:
            self._compress_requests.discard(key)
            return True
        return False

    async def manual_compress_check(self, task_id: str, step_id: str) -> dict:
        key = f"{task_id}:{step_id}"
        if self._running.get(task_id) and await self._step_is_active(task_id, step_id):
            self._compress_requests.add(key)
            logger.info(f"[DC:orch] manual compress queued step={step_id}")
            return {"status": "queued"}
        self._compress_requests.discard(key)
        existing_messages = await self._storage.get_step_messages(task_id, step_id)
        if not existing_messages:
            return {"status": "skipped", "reason": "no_messages"}
        prep = await self.get_step_prep(task_id, step_id)
        before_points = await self._get_compress_points(task_id, step_id)
        msgs: list[dict] = [
            {"role": "system", "content": prep.get("system_prompt", "") or ""},
            {"role": "user", "content": prep.get("step_context", "") or ""},
        ] + self._sanitize_tool_pairs(
            self._build_lm_messages(existing_messages, before_points))
        last_tokens = await self._get_last_prompt_tokens(task_id, step_id)
        tokens_before = last_tokens if last_tokens is not None else _count_tokens(msgs)
        compressed = await self._check_and_compress(task_id, step_id, msgs, last_tokens)
        if compressed is None:
            logger.info(f"[DC:orch] manual compress step={step_id} exhausted")
            return {"status": "exhausted", "tokens_before": tokens_before}
        if compressed is msgs:
            logger.info(f"[DC:orch] manual compress step={step_id} skipped (within budget)")
            return {"status": "skipped", "reason": "within_budget",
                    "tokens_before": tokens_before}
        after_points = await self._get_compress_points(task_id, step_id)
        logger.info(f"[DC:orch] manual compress step={step_id} ok "
                    f"points {len(before_points)} -> {len(after_points)}")
        return {"status": "ok", "rounds": max(0, len(after_points) - len(before_points)),
                "tokens_before": tokens_before, "tokens_after": _count_tokens(compressed)}

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
                                    reason: str = "context_limit",
                                    evidence: str = "") -> None:
        finish_label = ("[任务已完成，步骤收尾]" if reason == "task_ended"
                        else "[上下文超限，步骤已收尾]")
        if evidence:
            finish_label = f"{finish_label}（依据：{evidence}）"
        summary = ""
        degraded = False
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
                    summary = ("（收尾总结未生成，以下为该步骤最后一条进展文本）"
                               + m["content"].strip()[:500])
                    degraded = True
                    break
        if summary:
            if not degraded:
                try:
                    raw = await self._storage.get_artifact(task_id, "_flow",
                                                           _KEY_FINDING_ARTIFACT)
                    existing: list[str] = []
                    if raw and raw.get("content"):
                        existing = [ln for ln in str(raw["content"]).splitlines() if ln.strip()]
                    merged = existing + [f"[步骤 {step_id} 进度总结] {summary}"]
                    merged = merged[-_KEY_FINDING_MAX_LINES:]
                    await self._storage.save_artifact(task_id, "_flow", _KEY_FINDING_ARTIFACT,
                                                      "\n".join(merged), "text")
                except Exception:
                    pass
            try:
                await self._append_message(task_id, step_id,
                                           {"role": "assistant",
                                            "content": f"{finish_label} {summary}",
                                            "round_num": -1})
            except Exception:
                pass
        await self._sm.advance_step(task_id, step_id, "completed")
        try:
            if not step_id.startswith("monitor-"):
                await self._storage.export_step_conversation(
                    task_id, step_id, get_step_temp_dir(task_id, step_id))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] export step conversation failed "
                           f"task={task_id} step={step_id}: {e}")
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
                        out.append({k: v for k, v in m.items() if k != "tool_calls"})
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

    def _get_plan_tools(self) -> list[dict]:
        return list(_PLAN_TOOLS)

    def _get_single_chat_tools(self) -> list[dict]:
        return list(_SINGLE_CHAT_TOOLS)

    def _get_monitor_tools(self) -> list[dict]:
        return list(_MONITOR_TOOLS)

    def _get_gate_tools(self) -> list[dict]:
        return list(_GATE_TOOLS)

    def _get_reverse_tools(self) -> list[dict]:
        return list(_REVERSE_TOOLS)

    def _get_researcher_tools(self) -> list[dict]:
        return list(_RESEARCHER_TOOLS)

    def _get_requirement_tools(self) -> list[dict]:
        return list(_REQUIREMENT_TOOLS)

    def _get_architect_tools(self) -> list[dict]:
        return list(_ARCHITECT_TOOLS)

    def _get_code_review_tools(self) -> list[dict]:
        return list(_CODE_REVIEW_TOOLS)

    def _get_plan_review_tools(self) -> list[dict]:
        return list(_PLAN_REVIEW_TOOLS)

    def _make_llm_client(self, tier: str):
        from ..config import get_llm_config
        from .llm_client import LlmClient
        cfg = get_llm_config()
        if tier == "light":
            cfg2_model = cfg.get("light_model2") or ""
            cfg2_url = cfg.get("light_base_url2") or ""
            cfg2_key = cfg.get("light_api_key2") or ""
            dual = bool(cfg2_model and cfg2_url and cfg2_key)
            group = 1
            forced = None
            if dual:
                forced = self._light_route_avoid
                if forced in (1, 2):
                    group = 2 if forced == 1 else 1
                    self._light_route_avoid = None
                else:
                    group = 1
            if group == 2:
                base_url, api_key, model = cfg2_url, cfg2_key, cfg2_model
            else:
                base_url, api_key, model = (cfg.get("light_base_url") or "",
                                            cfg.get("light_api_key") or "",
                                            cfg.get("light_model") or "")
            logger.info(f"[DC:route] light group={group} model={model} "
                        f"forced={forced if forced in (1, 2) else '-'}")
        else:
            base_url, api_key, model = (cfg.get("power_base_url") or "",
                                        cfg.get("power_api_key") or "",
                                        cfg.get("power_model") or "")
        if not base_url or not api_key:
            raise LlmError(0, f"LLM not configured, 请检查设置页 "
                              f"{'Light' if tier == 'light' else 'Power'} 组 Base URL / API Key")
        client = LlmClient({"base_url": base_url, "api_key": api_key, "model": model or "mock-model"})
        if tier == "light":
            client._route_group = group
        return client

    async def _invoke_tool(self, task_id: str, step_id: str, name: str,
                           arguments_str: str, call_id: str) -> str:
        from ..step_context import get_task_root, get_task_workspace
        if name.startswith("mcp__"):
            _parts = name.split("__", 2)
            if len(_parts) != 3 or not _parts[1] or not _parts[2]:
                return f"[Error] 非法的 MCP 工具名: {name}"
            try:
                _mcp_args = json.loads(arguments_str or "{}")
                if not isinstance(_mcp_args, dict):
                    _mcp_args = {}
            except (ValueError, TypeError):
                _mcp_args = {}
            try:
                return await mcp_manager.call(_parts[1], _parts[2], _mcp_args)
            except McpError as e:
                return f"[Error] MCP 工具调用失败: {e}"
            except Exception as e:  # noqa: BLE001
                return f"[Error] MCP 工具调用异常: {str(e)[:300]}"
        if name in _REVERSE_ONLY_TOOLS:
            _step_type = await self._step_type_of(task_id, step_id)
            if _step_type != "reverse":
                return f"[Error] 工具 {name} 仅逆向专家步骤（type=reverse）可用"
        if name in _ASK_USER_ONLY:
            _ask_type = await self._step_type_of(task_id, step_id)
            if _ask_type not in _ASK_USER_TYPES:
                _ask_single_ok = False
                try:
                    _ask_task = await self._storage.get_task(task_id)
                    _ask_single_ok = bool(_ask_task
                                          and _ask_task.get("mode") == "single"
                                          and step_id == "chat")
                except Exception:  # noqa: BLE001
                    _ask_single_ok = False
                if not _ask_single_ok:
                    logger.warning(f"[DC:orch] ask_user 越权拒绝 step={step_id} "
                                   f"type={_ask_type}")
                    return ("[Error] dcflow_ask_user 仅需求分析师（type=requirement）"
                            "与计划制定者（type=plan）可用，本步骤类型无权向用户提问")
        try:
            args = json.loads(arguments_str or "{}")
            if not isinstance(args, dict):
                args = {}
        except (ValueError, TypeError):
            args = {}
        if name in ("dcflow_write_file", "dcflow_edit_file") \
                and await get_task_workspace(self._storage, task_id) == PROJECT_ROOT:
            fp = str(args.get("file_path") or "")
            if fp and not os.path.isabs(fp) \
                    and not fp.replace("\\", "/").startswith(".dc_tmp"):
                return ("[Error] 拒绝裸相对路径 file_path（解析基准是项目根，裸相对路径"
                        "会落到项目根而非步骤目录，后续步骤读不到）：请传完整路径 —— 推荐 "
                        f".dc_tmp/{task_id}/{step_id}/artifacts/<文件名>（产物目录）；"
                        "或绝对路径。产物统一放步骤产物目录 artifacts/ 下。")
        if name in _FILE_WRITE_TOOLS:
            _wtype = await self._step_type_of(task_id, step_id)
            if _wtype == "researcher":
                if name == "dcflow_edit_file":
                    return "[Error] 调研员不支持 dcflow_edit_file：报告请用 dcflow_write_file 一次写完整，落点限自己的步骤产物目录"
                fp = str(args.get("file_path") or "")
                _base = os.path.normcase(os.path.abspath(
                    get_step_temp_dir(task_id, step_id)))
                _cand = os.path.normcase(os.path.abspath(
                    fp if os.path.isabs(fp) else os.path.join(PROJECT_ROOT, fp)))
                if not _cand.startswith(_base + os.sep):
                    return ("[Error] 调研员只能写自己的步骤产物目录："
                            f".dc_tmp/{task_id}/{step_id}/artifacts/<文件名>（推荐 "
                            "research-report.md）；不得修改项目/任务代码或其他目录")
        if name in _FILE_WRITE_TOOLS:
            _pre_err = await self._doc_write_precheck(task_id, step_id, name, args)
            if _pre_err:
                logger.info(f"[DC:orch] doc write precheck blocked step={step_id} "
                            f"file={os.path.basename(str(args.get('file_path') or ''))}")
                return _pre_err
        action, block_msg = await self._intercept_step_report(task_id, step_id, name, args)
        if action == "block":
            logger.info(f"[DC:orch] flow report write blocked step={step_id}: {block_msg[:60]}")
            return block_msg
        asset_action, asset_file = await self._intercept_project_asset(task_id, step_id, name, args)
        if name in _FILE_WRITE_TOOLS:
            _skel_err = await self._doc_skeleton_precheck(task_id, step_id, name, args)
            if _skel_err:
                logger.info(f"[DC:orch] doc skeleton blocked step={step_id} "
                            f"file={os.path.basename(str(args.get('file_path') or ''))}")
                return _skel_err
        _rc_plan: Optional[dict] = None
        if name == "dcflow_run_code":
            _rc_plan = await self._before_run_code_docs(
                task_id, step_id, str(args.get("code") or ""))
            if _rc_plan.get("block"):
                logger.info(f"[DC:orch] run_code doc blocked step={step_id}")
                return _rc_plan["block"]
        if name == "dcflow_sim":
            from ..simulator.tool import run_sim_tool
            lock = self._sim_locks.setdefault(task_id, asyncio.Lock())
            try:
                await asyncio.wait_for(lock.acquire(), _SIM_LOCK_WAIT)
            except asyncio.TimeoutError:
                logger.warning(f"[DC:orch] sim lock busy task={task_id} "
                               f"(>{_SIM_LOCK_WAIT}s) step={step_id}")
                return ("[Error] 模拟器会话被占用（等待 "
                        f"{_SIM_LOCK_WAIT}s 未获得会话锁）——通常是上一次 "
                        "action=script 的代码仍在后台运行（纯 Python 死循环无法"
                        "终止）。处置：用 action=cleanup 丢弃该会话后重新 load；"
                        "或稍后重试（并行步骤可能正持有锁）。")
            try:
                return await asyncio.to_thread(run_sim_tool, task_id,
                                               self._sim_sessions, args)
            finally:
                lock.release()
        if name in _CTF_TOOL_FUNCS:
            from .. import ctf_tool
            func_name, params = _CTF_TOOL_FUNCS[name]
            fn_args = [str(args.get(k) or "") for k in params]
            return await asyncio.to_thread(getattr(ctf_tool, func_name), *fn_args)
        if self._tool_invoke is not None:
            result = await self._tool_invoke({"name": name, "args": args,
                                              "task_id": task_id, "step_id": step_id})
        else:
            from .. import rest_api
            result = await rest_api.invoke_tool({"name": name, "args": args,
                                                 "task_id": task_id, "step_id": step_id})
        if action == "mirror":
            await self._mirror_step_report(task_id, step_id)
        if asset_action == "mirror":
            await self._mirror_project_asset(task_id, step_id, asset_file)
        if _rc_plan is not None:
            _rc_note = await self._after_run_code_docs(task_id, step_id, _rc_plan)
            if _rc_note:
                _prev = (str(result.get("result", "")) if isinstance(result, dict)
                         else str(result))
                result = {"result": _rc_note + (f"\n\n{_prev}" if _prev else "")}
        text = (str(result.get("result", "(无输出)")) if isinstance(result, dict)
                else str(result))
        return self._file_write_nudge(task_id, step_id, name, args, text)

    def _file_write_nudge(self, task_id: str, step_id: str, name: str,
                          args: dict, result_text: str) -> str:
        if name not in _FILE_WRITE_TOOLS:
            return result_text
        if result_text.startswith("[Error]") or "[Security]" in result_text[:40]:
            return result_text
        fp = str((args or {}).get("file_path") or "")
        if not fp:
            return result_text
        base = os.path.basename(fp.replace("\\", "/"))
        if base in _NUDGE_EXEMPT_FILES:
            return result_text
        full = fp if os.path.isabs(fp) else os.path.join(PROJECT_ROOT, fp)
        key = f"{task_id}:{step_id}:" + os.path.normcase(os.path.normpath(full))
        n = self._file_write_counts.get(key, 0) + 1
        self._file_write_counts[key] = n
        if n < _FILE_WRITE_NUDGE_FROM:
            return result_text
        logger.info(f"[DC:orch] repeated file write nudge step={step_id} "
                    f"file={base} n={n}")
        return (result_text
                + f"\n\n[提示] 你已第 {n} 次写入/修改同一文件（{base}）。"
                + "同一件事请一次调用做完：把剩余全部改动合并成一次 dcflow_run_code"
                + "（一段代码里读全文 → 批量替换 → 写回 → print 校验），"
                + "或用一次 dcflow_write_file 写完整内容——不要分开多次修改同一个文件。")


    async def _intercept_project_asset(self, task_id: str, step_id: str,
                                      name: str, args: dict) -> Tuple[str, str]:
        fp = str(args.get("file_path") or "")
        filename = os.path.basename(fp.replace("\\", "/"))
        if filename not in _PROJECT_ASSET_FILES:
            return "", ""
        if name == "dcflow_read_file":
            await self._pull_project_asset(task_id, step_id, filename)
            return "", ""
        if name in ("dcflow_write_file", "dcflow_edit_file"):
            return "mirror", filename
        return "", ""

    async def _load_project_asset(self, task_id: str, asset_type: str) -> Optional[dict]:
        return await _load_project_asset_shared(self._storage, task_id, asset_type)

    async def _pull_project_asset(self, task_id: str, step_id: str, filename: str) -> None:
        try:
            asset_type = _PROJECT_ASSET_FILES[filename]
            fp = _project_asset_local_path(task_id, asset_type, filename)
            asset = await self._load_project_asset(task_id, asset_type)
            if not asset:
                return
            content = asset.get("content") or ""
            local = _read_asset_file(fp)
            if local and os.path.isfile(fp) and os.path.getmtime(fp) > _asset_ts(asset):
                if local != content:
                    await self._mirror_project_asset(task_id, step_id, filename)
                return
            if local != content or not os.path.isfile(fp):
                _write_asset_file(fp, content)
                logger.info(f"[DC:orch] project asset pulled before read step={step_id} "
                            f"file={filename} ({len(content)} chars)")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] project asset pull failed step={step_id} "
                           f"file={filename}: {e}")

    async def _mirror_project_asset(self, task_id: str, step_id: str, filename: str,
                                   source_path: str = "") -> None:
        try:
            asset_type = _PROJECT_ASSET_FILES.get(filename)
            if not asset_type:
                return
            canon = _project_asset_local_path(task_id, asset_type, filename)
            fp = source_path or canon
            content = _read_asset_file(fp)
            if not content:
                return
            if os.path.normpath(fp) != os.path.normpath(canon):
                _write_asset_file(canon, content)
            key = await get_project_key(self._storage, task_id)
            await self._storage.save_project_asset(
                key, asset_type, content, "text",
                project_path=await get_project_path(self._storage, task_id),
                source_task_id=task_id, source_step_id=step_id)
            asset = await self._storage.get_project_asset(key, asset_type)
            await self._broadcast_project_asset(key, asset_type, content,
                                                _asset_ts(asset), except_task=task_id)
            logger.info(f"[DC:orch] project asset mirrored step={step_id} file={filename} "
                        f"({len(content)} chars)")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] project asset mirror failed step={step_id} "
                           f"file={filename}: {e}")

    async def _broadcast_project_asset(self, project_key: str, asset_type: str,
                                      content: str, asset_ts: float,
                                      except_task: str = "") -> None:
        try:
            name = _PROJECT_ASSET_CANON[asset_type]
            synced: list[str] = []
            for tid in await self._storage.list_task_ids_by_project(project_key):
                if tid == except_task:
                    continue
                tdir = os.path.join(PROJECT_ROOT, ".dc_tmp", tid)
                if not os.path.isdir(tdir):
                    continue
                fp = os.path.join(tdir, name)
                if _read_asset_file(fp) == content:
                    pass
                elif os.path.isfile(fp) and os.path.getmtime(fp) > asset_ts:
                    pass
                else:
                    _write_asset_file(fp, content)
                synced.append(tid)
            if synced:
                logger.info(f"[DC:orch] project asset broadcast type={asset_type} "
                            f"to {len(synced)} flow(s)")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] project asset broadcast failed type={asset_type}: {e}")

    async def _restore_flow_report_file(self, task_id: str, report_path: str = "") -> None:
        path = report_path or os.path.join(PROJECT_ROOT, ".dc_tmp", task_id,
                                          _FLOW_REPORT_FILENAME)
        try:
            if os.path.isfile(path):
                return
            raw = await self._storage.get_artifact(task_id, "_flow", _STEP_REPORT_ARTIFACT)
            if raw and raw.get("content"):
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(raw["content"])
                logger.info(f"[DC:orch] flow report restored from DB ({len(raw['content'])} "
                            f"chars) → {path}")
        except OSError as e:
            logger.warning(f"[DC:orch] flow report restore failed task={task_id}: {e}")

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
            await self._restore_flow_report_file(task_id, report_path)
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

    async def _mirror_step_report(self, task_id: str, step_id: str,
                                 source_path: str = "") -> None:
        try:
            report_path = source_path or os.path.join(PROJECT_ROOT, ".dc_tmp", task_id,
                                                      _FLOW_REPORT_FILENAME)
            if not source_path:
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
            cleaned = _strip_trailing_kf_block(content)
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


    async def _doc_write_precheck(self, task_id: str, step_id: str, name: str,
                                  args: dict) -> Optional[str]:
        fp = str((args or {}).get("file_path") or "")
        if not fp or name not in _FILE_WRITE_TOOLS:
            return None
        filename = _doc_canonical_name(os.path.basename(fp.replace("\\", "/")))
        if not filename:
            return None
        if name == "dcflow_write_file":
            content = str((args or {}).get("content") or "")
        else:
            from ..step_context import get_task_workspace
            root = await get_task_workspace(self._storage, task_id)
            target = _doc_abs_path(root, fp)
            if not os.path.isfile(target):
                return None
            try:
                with open(target, "r", encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                return None
            from .. import rest_api
            replaced, _note = rest_api.apply_edit_text(
                text, str((args or {}).get("old_string") or ""),
                str((args or {}).get("new_string") or ""),
                bool((args or {}).get("replace_all")),
                (args or {}).get("occurrence"))
            if replaced is None:
                return None
            content = replaced
        reason = ""
        if filename == _CODE_DIAGRAM_FILENAME:
            reason = _validate_code_diagram_content(content)
        if not reason:
            reason = _doc_length_error(filename, content)
            if reason:
                reason = "[Error] " + reason
        if not reason:
            return None
        return f"{reason}（写入前拦截：本次 {name} 未执行，文件未改动）"

    async def _step_type_of(self, task_id: str, step_id: str) -> str:
        try:
            task = await self._storage.get_task(task_id, include_hidden=True)
            step = self._find_step(task, step_id)
            if step:
                return step.get("type") or "executor"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] step type lookup failed step={step_id}: {e}")
        return "executor"

    async def _current_project_context(self, task_id: str, fp: str) -> str:
        try:
            from ..step_context import get_task_workspace
            root = await get_task_workspace(self._storage, task_id)
            local = _read_asset_file(_doc_abs_path(root, fp))
            if local:
                return local
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] project context local read failed: {e}")
        try:
            asset = await self._load_project_asset(
                task_id, _PROJECT_ASSET_FILES[_PROJECT_CONTEXT_FILENAME])
            if asset:
                return str(asset.get("content") or "")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] project context asset read failed: {e}")
        return ""

    async def _doc_skeleton_precheck(self, task_id: str, step_id: str, name: str,
                                     args: dict) -> Optional[str]:
        fp = str((args or {}).get("file_path") or "")
        filename = os.path.basename(fp.replace("\\", "/"))
        template = _doc_skeleton_template(filename)
        if not template:
            return None
        step_type = await self._step_type_of(task_id, step_id)
        template_path = _skeleton_template_path(template)
        if filename == _CODE_DIAGRAM_FILENAME and step_type != "plan":
            logger.info(f"[DC:orch] code diagram write denied step={step_id} "
                        f"type={step_type}")
            return _doc_diagram_permission_error(step_type, template_path)
        if name == "dcflow_edit_file":
            logger.info(f"[DC:orch] doc edit redirected step={step_id} file={filename}")
            return _doc_edit_redirect_error(filename, template_path)
        content = str((args or {}).get("content") or "")
        key = f"{task_id}:{step_id}:{filename}"
        if self._doc_skeleton_rejects.get(key, 0) >= _DOC_SKELETON_MAX_REJECTS:
            logger.warning(f"[DC:orch] doc skeleton safety valve open step={step_id} "
                           f"file={filename}")
            self._doc_skeleton_rejects.pop(key, None)
            return None
        error = _doc_skeleton_error(filename, content, step_id)
        if error:
            self._doc_skeleton_rejects[key] = self._doc_skeleton_rejects.get(key, 0) + 1
            return (f"[拒绝写入] {error}"
                    f"（写入前拦截：本次 {name} 未执行，文件未改动）")
        if filename == _PROJECT_CONTEXT_FILENAME and step_type != "report":
            old = await self._current_project_context(task_id, fp)
            if old.strip() and _doc_section_body(old, _CONTEXT_SUMMARY_SECTION) \
                    != _doc_section_body(content, _CONTEXT_SUMMARY_SECTION):
                logger.info(f"[DC:orch] task summary write denied step={step_id} "
                            f"type={step_type}")
                return (f"{_doc_task_summary_error(step_type)}"
                        f"（写入前拦截：本次 {name} 未执行，文件未改动）")
        self._doc_skeleton_rejects.pop(key, None)
        return None

    async def _shared_doc_entries(self, task_id: str, step_id: str,
                                  code: str) -> list[dict]:
        from ..step_context import get_task_workspace
        root = await get_task_workspace(self._storage, task_id)
        names: dict[str, list[str]] = {
            _FLOW_REPORT_FILENAME: [_FLOW_REPORT_FILENAME, f"{step_id}-步骤报告.md"],
            _CODE_DIAGRAM_FILENAME: [_CODE_DIAGRAM_FILENAME],
            _PROJECT_CONTEXT_FILENAME: [_PROJECT_CONTEXT_FILENAME],
        }
        entries: list[dict] = []
        seen: set = set()
        for filename, aliases in names.items():
            if not any(a in code for a in aliases):
                continue
            write = read = False
            for a in aliases:
                w, r = _doc_write_intent(code, a)
                write, read = write or w, read or r
            if filename == _FLOW_REPORT_FILENAME:
                canon = os.path.normpath(os.path.join(PROJECT_ROOT, ".dc_tmp", task_id,
                                                       _FLOW_REPORT_FILENAME))
            else:
                canon = _project_asset_local_path(task_id, _PROJECT_ASSET_FILES[filename],
                                                  filename)
            paths = [canon]
            for p in _doc_referenced_paths(code, filename):
                ap = _doc_abs_path(root, p)
                if ap not in paths:
                    paths.append(ap)
            for ap in paths:
                if ap in seen:
                    continue
                seen.add(ap)
                entries.append({"filename": filename, "path": ap,
                                "write": write, "read": read})
        return entries

    async def _before_run_code_docs(self, task_id: str, step_id: str, code: str) -> dict:
        plan: dict = {"block": None, "docs": [], "snap": {}, "before": {}}
        if not (code or "").strip():
            return plan
        try:
            docs = await self._shared_doc_entries(task_id, step_id, code)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] run_code doc entries failed step={step_id}: {e}")
            return plan
        plan["docs"] = docs
        if not docs:
            return plan
        for it in docs:
            if it["filename"] == _FLOW_REPORT_FILENAME:
                await self._restore_flow_report_file(task_id, it["path"])
            elif not os.path.isfile(it["path"]):
                await self._pull_project_asset(task_id, step_id, it["filename"])
        for it in docs:
            if it["filename"] != _FLOW_REPORT_FILENAME:
                continue
            if it["write"] and not it["read"]:
                action, msg = await self._intercept_step_report(
                    task_id, step_id, "dcflow_write_file", {"file_path": it["path"]})
                if action == "block":
                    plan["block"] = msg
                    return plan
        if any(it["filename"] == _CODE_DIAGRAM_FILENAME and it["write"] for it in docs):
            step_type = await self._step_type_of(task_id, step_id)
            if step_type != "plan":
                plan["block"] = (_doc_diagram_permission_error(
                    step_type, _skeleton_template_path(_DIAGRAM_SKELETON))
                    + "（写入前拦截：本段代码未执行、文件零改动）")
                return plan
        for it in docs:
            if not it["write"]:
                continue
            limit = _DOC_CHAR_LIMITS.get(it["filename"])
            if not limit:
                continue
            cur = 0
            if os.path.isfile(it["path"]):
                try:
                    with open(it["path"], "r", encoding="utf-8") as f:
                        cur_text = f.read()
                except (OSError, UnicodeDecodeError):
                    cur_text = ""
                cur = len(_strip_trailing_kf_block(cur_text)
                          if it["filename"] == _FLOW_REPORT_FILENAME else cur_text)
            if cur + len(code) > limit:
                plan["block"] = (
                    f"[Error] {it['filename']} 现有 {cur} 字符 + 本次代码 {len(code)} 字符"
                    f"已超上限 {limit} 字符：run_code 不能继续改它（判据为「代码长 +"
                    f"文件现有总长」，写入前拦截、本段代码未执行、文件零改动）。"
                    f"若要压缩（蒸馏），请改用 dcflow_write_file 一次性写入"
                    f"≤ {limit} 字符的蒸馏版（该工具按覆盖语义判定，只看新内容长度）。")
                return plan
        for it in docs:
            if it["filename"] == _FLOW_REPORT_FILENAME and it["read"]:
                try:
                    await self._save_step_report_read_round(
                        task_id, step_id, await self._get_step_report_round(task_id, step_id))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[DC:orch] run_code read round save failed step={step_id}: {e}")
        for it in docs:
            try:
                st = os.stat(it["path"])
                plan["snap"][it["path"]] = (st.st_mtime_ns, st.st_size)
            except OSError:
                plan["snap"][it["path"]] = None
            if it["write"]:
                try:
                    with open(it["path"], "r", encoding="utf-8") as f:
                        plan["before"][it["path"]] = f.read()
                except (OSError, UnicodeDecodeError):
                    plan["before"][it["path"]] = ""
        return plan

    async def _after_run_code_docs(self, task_id: str, step_id: str, plan: dict) -> str:
        msgs: list[str] = []
        for it in plan.get("docs") or []:
            if not it.get("write"):
                continue
            path, filename = it["path"], it["filename"]
            try:
                if not os.path.isfile(path):
                    continue
                st = os.stat(path)
                if plan.get("snap", {}).get(path) == (st.st_mtime_ns, st.st_size):
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            reason = _doc_length_error(filename, content)
            if not reason and filename == _CODE_DIAGRAM_FILENAME:
                reason = _validate_code_diagram_content(content)
            fix_tip = "请修正后重写。"
            if not reason:
                reason = _doc_skeleton_error(filename, content)
                if reason:
                    fix_tip = (f"请改用 dcflow_write_file 一次性写入符合骨架的整篇（骨架模板："
                               f"{_skeleton_template_path(_doc_skeleton_template(filename))}）。")
            if not reason and filename == _PROJECT_CONTEXT_FILENAME:
                step_type = await self._step_type_of(task_id, step_id)
                before = str((plan.get("before") or {}).get(path) or "")
                if step_type != "report" and before.strip() \
                        and _doc_section_body(before, _CONTEXT_SUMMARY_SECTION) \
                        != _doc_section_body(content, _CONTEXT_SUMMARY_SECTION):
                    reason = _doc_task_summary_error(step_type).removeprefix("[拒绝写入] ")
                    fix_tip = ("请改用 dcflow_write_file 保持「任务总结」章节原样、只更新你负责的"
                               "章节（该章节只有 final-reporter 可改写）。")
            if reason:
                detail = reason[len("[Error] "):] if reason.startswith("[Error] ") else reason
                logger.info(f"[DC:orch] run_code doc rejected step={step_id} file={filename}")
                msgs.append(f"[Error] {filename} 写入未通过校验：{detail}"
                            f"本轮未同步 DB/项目级资产库（磁盘内容保持写入结果，不做回滚），"
                            f"{fix_tip}")
                continue
            try:
                if filename == _FLOW_REPORT_FILENAME:
                    await self._mirror_step_report(task_id, step_id, source_path=path)
                else:
                    await self._mirror_project_asset(task_id, step_id, filename,
                                                     source_path=path)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DC:orch] run_code doc mirror failed step={step_id} "
                               f"file={filename}: {e}")
        return "\n\n".join(msgs)


    async def _append_message(self, task_id: str, step_id: str, message: dict,
                              entity_id: Optional[str] = None,
                              publish: bool = True) -> int:
        eid = entity_id or message.get("entity_id")
        seq = await self._storage.append_message(task_id, step_id, message, entity_id=eid)
        if publish:
            self._publish(task_id, "messagesConfirmed", {
                "stepId": step_id, "items": [self._msg_item(seq, eid, message)]})
        return seq

    @staticmethod
    def _msg_item(seq: int, entity_id: Optional[str], message: dict) -> dict:
        return {"seq": seq, "entityId": entity_id,
                "role": message.get("role", ""),
                "content": message.get("content", "") or "",
                "toolName": message.get("toolName") or message.get("tool_name"),
                "input": message.get("input"),
                "output": message.get("output"),
                "roundNum": message.get("round_num", 0)}

    async def _announce_user_message(self, task_id: str, step_id: str, content: str,
                                     *, mid_round: bool = False,
                                     client_msg_id: Optional[str] = None,
                                     queued: bool = False,
                                     round_num: Optional[int] = None) -> int:
        client_msg_id = client_msg_id or f"um-{uuid4().hex}"
        msg_row = {"role": "user", "content": content, "round_num": -1}
        seq = await self._append_message(task_id, step_id, msg_row,
                                         entity_id=client_msg_id,
                                         publish=not queued)
        if queued:
            self._queued_user_confirms.setdefault(
                f"{task_id}:{step_id}", {})[client_msg_id] = \
                self._msg_item(seq, client_msg_id, msg_row)
        if mid_round:
            self._mid_round_user_seqs.setdefault(f"{task_id}:{step_id}", []).append(seq)
        self._publish(task_id, "userMessage",
                      {"stepId": step_id, "message": content, "msgSeq": seq,
                       "entityId": client_msg_id, "midRound": mid_round,
                       "roundNum": round_num})
        return seq

    def _take_queued_user_confirm(self, task_id: str, step_id: str,
                                  entity_id: Optional[str],
                                  content: str) -> Optional[dict]:
        bucket = self._queued_user_confirms.get(f"{task_id}:{step_id}")
        if not bucket:
            return None
        if entity_id and entity_id in bucket:
            return bucket.pop(entity_id)
        for eid, item in list(bucket.items()):
            if item.get("content") == content:
                del bucket[eid]
                return item
        return None

    def is_round_in_flight(self, task_id: str, step_id: str) -> bool:
        return bool(self._round_in_flight.get(f"{task_id}:{step_id}"))

    async def _reorder_mid_round_user_messages(self, task_id: str, step_id: str) -> None:
        seqs = self._mid_round_user_seqs.pop(f"{task_id}:{step_id}", None)
        if not seqs:
            return
        try:
            new_seqs = await self._storage.move_messages_to_end(task_id, step_id, seqs)
            logger.info(f"[DC:orch] mid-round user messages reordered step={step_id} "
                        f"old={seqs} new={new_seqs}")
            bucket = self._queued_user_confirms.get(f"{task_id}:{step_id}")
            if bucket and new_seqs:
                by_old = {old: new for old, new in zip(seqs, new_seqs)}
                for item in bucket.values():
                    old = item.get("seq")
                    if old in by_old:
                        item["seq"] = by_old[old]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] mid-round user message reorder failed "
                           f"step={step_id}: {e}")

    def _account_run_delta(self, task_id: str, step_id: str,
                           since: Optional[float] = None) -> int:
        key = f"{task_id}:{step_id}"
        now = time.monotonic()
        prev = self._step_run_accounted.get(key)
        if prev is None:
            prev = since if since is not None else now
        self._step_run_accounted[key] = now
        return max(0, int((now - prev) * 1000))

    async def _update_step_stats(self, task_id: str, step_id: str, requests: int = 0,
                                 ttft_ms: Optional[int] = None, output_ms: int = 0,
                                 run_ms: Optional[int] = None) -> None:
        await self._storage.update_step_stats(
            task_id, step_id, requests=requests, ttft_ms=ttft_ms,
            output_ms=output_ms, run_ms=run_ms)
        try:
            stats = await self._storage.get_step_stats(task_id, step_id)
            if stats:
                self._publish(task_id, "stepStatsUpdated", {
                    "taskId": task_id, "stepId": step_id, "stats": stats})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DC:orch] step stats publish failed step={step_id}: {e}")

    async def _add_step_tokens(self, task_id: str, step_id: str,
                               prompt: int, cached: int, completion: int,
                               record_last: bool = True,
                               context_tokens: Optional[int] = None) -> None:
        try:
            await self._storage.add_step_tokens(
                task_id, step_id, prompt, cached, completion,
                context_tokens=context_tokens if context_tokens is not None else prompt)
            self._publish(task_id, "stepTokensUpdated", {
                "stepId": step_id, "prompt": prompt, "cached": cached,
                "completion": completion,
                "contextTokens": context_tokens if context_tokens is not None else prompt})
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
        items: list[dict] = []
        for i in range(saved, len(rounds)):
            r = rounds[i]
            tool_calls = r.get("toolCalls", [])
            round_uid = r.get("roundUid")
            if (r.get("text") or "").strip() or tool_calls or (r.get("reasoning") or "").strip():
                entry: dict = {"role": "assistant", "content": r.get("text", "") or "",
                               "round_num": i}
                if r.get("reasoning"):
                    entry["reasoning"] = r["reasoning"]
                if tool_calls:
                    entry["tool_calls"] = json.dumps([
                        {"id": tc.get("call_id") or f"call_{i}_{j}", "type": "function",
                         "function": {"name": tc.get("name", ""),
                                       "arguments": json.dumps(tc.get("input") or {},
                                                                ensure_ascii=False)}}
                        for j, tc in enumerate(tool_calls)
                    ], ensure_ascii=False)
                _is_carrier = bool(tool_calls) and not (r.get("text") or "").strip()
                _entity_id = None if _is_carrier else round_uid
                seq_a = await self._storage.append_message(
                    task_id, step_id, entry, entity_id=_entity_id)
                items.append(self._msg_item(seq_a, _entity_id, entry))
            for tc in tool_calls:
                entry_t = {
                    "role": "tool", "content": tc.get("output", "") or "",
                    "tool_call_id": tc.get("call_id"),
                    "toolName": tc.get("name", ""), "input": tc.get("input"),
                    "output": tc.get("output", "") or "", "round_num": i,
                }
                seq_t = await self._storage.append_message(
                    task_id, step_id, entry_t, entity_id=tc.get("call_id"))
                items.append(self._msg_item(seq_t, tc.get("call_id"), entry_t))
            saved = i + 1
        self._last_saved_round[key] = saved
        if items:
            self._publish(task_id, "messagesConfirmed", {"stepId": step_id, "items": items})

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
                cleaned = _strip_trailing_kf_block(existing_text)
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


    def _publish(self, task_id: str, command: str, payload: dict) -> int:
        return self.sse_hub.publish(task_id, command, payload)


    def _ensure_step_live(self, task_id: str, step_id: str) -> dict:
        key = (task_id, step_id)
        live = self._step_live.get(key)
        if live is None:
            live = {"seq": 0, "streaming": False, "roundUid": None,
                    "thinking": "", "text": "", "tools": []}
            self._step_live[key] = live
        return live

    def _update_step_live(self, task_id: str, step_id: str, seq: Optional[int] = None,
                          **fields) -> None:
        live = self._ensure_step_live(task_id, step_id)
        if seq is not None and seq > live["seq"]:
            live["seq"] = seq
        for k, v in fields.items():
            if k in ("thinking", "text") and isinstance(v, str):
                live[k] = live[k] + v
            elif k == "roundUid" and v is None:
                continue
            else:
                live[k] = v

    def _upsert_step_live_tool(self, task_id: str, step_id: str, call_id: str,
                               name: str = "", input_: Optional[str] = None,
                               output: Optional[str] = None,
                               status: Optional[str] = None,
                               seq: Optional[int] = None) -> None:
        live = self._ensure_step_live(task_id, step_id)
        if seq is not None and seq > live["seq"]:
            live["seq"] = seq
        slot = next((t for t in live["tools"] if t["callId"] == call_id), None)
        if slot is None:
            slot = {"callId": call_id, "name": name, "input": input_ or "",
                    "output": None, "status": "streaming"}
            live["tools"].append(slot)
        if name:
            slot["name"] = name
        if input_ is not None:
            slot["input"] = input_
        if output is not None:
            slot["output"] = output
        if status:
            slot["status"] = status

    def _append_step_live_tool_param(self, task_id: str, step_id: str,
                                     call_id: str, delta: str,
                                     seq: Optional[int] = None) -> None:
        live = self._step_live.get((task_id, step_id))
        if live:
            slot = next((t for t in live["tools"] if t["callId"] == call_id), None)
            if slot:
                slot["input"] = (slot["input"] or "") + delta
        if seq is not None:
            self._update_step_live(task_id, step_id, seq=seq)

    def _clear_step_live_round(self, task_id: str, step_id: str) -> None:
        live = self._step_live.get((task_id, step_id))
        if live is not None:
            live["text"] = ""
            live["thinking"] = ""
            live["tools"] = []
            live["roundUid"] = None

    def _clear_step_live(self, task_id: str, step_id: str) -> None:
        self._step_live.pop((task_id, step_id), None)

    def _abort_round_live(self, task_id: str, step_id: str, round_uid: str = "") -> None:
        live = self._step_live.get((task_id, step_id))
        call_ids = [t.get("callId") for t in (live or {}).get("tools", [])
                    if t.get("callId")]
        self._clear_step_live_round(task_id, step_id)
        self._publish(task_id, "roundAborted", {
            "stepId": step_id, "roundUid": round_uid or None,
            "toolCallIds": call_ids})

    def _clear_task_live(self, task_id: str) -> None:
        for key in [k for k in self._step_live if k[0] == task_id]:
            del self._step_live[key]

    def get_step_live(self, task_id: str, step_id: str) -> Optional[dict]:
        return self._step_live.get((task_id, step_id))

    async def _publish_state_update(self, task_id: str, step_id: Optional[str] = None,
                                    status: Optional[str] = None) -> None:
        if step_id:
            self._publish(task_id, "stepStatus", {
                "taskId": task_id, "stepId": step_id, "status": status})
        await self._publish_task_updated(task_id)

    async def _publish_task_updated(self, task_id: str) -> None:
        try:
            task = await self._storage.get_task(task_id)
        except Exception:  # noqa: BLE001
            return
        if not task:
            return
        steps = [{"stepId": s.get("step_id"), "status": s.get("status"),
                  "sortOrder": s.get("sort_order"), "title": s.get("title")}
                 for s in (task.get("steps") or [])]
        self._publish(task_id, "taskUpdated", {
            "taskId": task_id, "status": task.get("status"),
            "pauseLevel": task.get("pause_level"),
            "updatedAt": task.get("updated_at"), "title": task.get("title"),
            "autoApprove": bool(task.get("auto_approve")),
            "steps": steps})

    async def stream_events(self, task_id: str, last_seq: int = 0,
                            last_msg_seq: int = -1, step_id: str = ""):
        if step_id and last_msg_seq >= 0:
            catchup_limit = self.CATCHUP_REPLAY_LIMIT
            try:
                rows = await self._storage.get_step_messages(
                    task_id, step_id, after_seq=last_msg_seq,
                    limit=catchup_limit + 1)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DC:orch] catchup replay failed task={task_id} "
                               f"step={step_id}: {e}")
                rows = []
            if len(rows) > catchup_limit:
                logger.warning(f"[DC:orch] catchup replay truncated task={task_id} "
                               f"step={step_id}: >{catchup_limit} messages pending, "
                               f"frontend backfills via reconnect fetch")
                rows = rows[:catchup_limit]
            if rows:
                self._publish(task_id, "messagesConfirmed", {
                    "stepId": step_id,
                    "items": [self._msg_item(r["seq"], r.get("entity_id"), r)
                              for r in rows]})

        agen = self.sse_hub.subscribe(task_id, last_seq=last_seq).__aiter__()
        nxt: Optional[asyncio.Task] = asyncio.ensure_future(agen.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {nxt}, timeout=self.HEARTBEAT_INTERVAL_S)
                if not done:
                    yield {"command": "heartbeat", "taskId": task_id,
                           "seq": self.sse_hub.last_seq(task_id)}
                    continue
                try:
                    event = nxt.result()
                except StopAsyncIteration:
                    return
                yield event
                nxt = asyncio.ensure_future(agen.__anext__())
        finally:
            if not nxt.done():
                nxt.cancel()
                try:
                    await nxt
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
            await agen.aclose()

    def _emit_chunk(self, task_id: str, step_id: str, chunk: str,
                    entity_id: Optional[str] = None) -> None:
        payload: dict = {"stepId": step_id, "chunk": chunk}
        if entity_id:
            payload["entityId"] = entity_id
        seq = self._publish(task_id, "streamChunk", payload)
        self._update_step_live(task_id, step_id, seq=seq, text=chunk,
                               roundUid=entity_id)

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
