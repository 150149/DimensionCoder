

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import TYPE_CHECKING

from .storage.adapter import StorageAdapter
from .prompts import load_prompt, rules_dir
from .prompts.registry import get_step_prompt
from .config import PROJECT_ROOT

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# 步骤描述映射（AI 需要知道每个步骤具体做什么）
# ═══════════════════════════════════════════════════════════════════

_STEP_DESCRIPTIONS = {
    "理解需求": "阅读任务描述，调用 dcflow_read_doc 查阅相关业务文档，明确需求范围。输出需求理解摘要：用户期望什么、涉及哪些模块、有哪些模糊点需要后续步骤澄清。",
    "阅读业务文档": "调用 dcflow_read_doc 阅读知识库中与任务相关的业务文档。梳理文档中与当前任务相关的业务规则、约束条件、已知问题。",
    "阅读代码": "调用 dcflow_search_code 和 dcflow_read_file 搜索并阅读项目中与任务相关的代码。理解当前代码结构、关键类和函数。",
    "梳理现有逻辑": "基于前序步骤的分析，完整梳理当前系统的业务逻辑。画出业务流程（触发条件→处理逻辑→状态变化→代码位置），标注与用户预期的差异点。",
    "对比分析": "将前序步骤梳理的现有逻辑与用户需求做逐条对比。列出差异点、根因、修复复杂度。输出对比表格。",
    "提出修改方案": "基于对比分析，提出 2-3 个修改方案。对比各方案优缺点、影响范围、风险，选择推荐方案并说明理由。**此步骤为 Gate，需人工审批。**",
    "测试设计": "为推荐方案设计测试用例。覆盖正向、边界、异常场景。输出测试用例表格。",
    "编写测试代码": "根据测试设计，编写实际可运行的测试代码。确保测试覆盖关键路径。",
    "代码修改": "根据审批通过的方案，修改代码。调用 dcflow_write_file/dcflow_edit_file 实现变更。修改完成后调用 dcflow_search_code 检查引用完整性。",
    "运行测试": "调用 dcflow_run_cmd 运行测试，确保所有测试通过。如有失败，修复后重新运行。",
    "文档更新": "根据代码变更更新相关文档。调用 dcflow_read_doc 检查现有文档，然后用 dcflow_write_file 更新或新增文档。",
    "Code Review": "审查代码变更。检查逻辑正确性、代码风格、边界处理、安全性。输出 CR 意见。",
    "代码修改(第二轮)": "根据 CR 意见修改代码。逐条处理审查意见，修复后重新检查。",
    "最终验证": "最终验证所有测试通过、文档完整、无遗漏。确认可以合并。",
}


def _describe_step(title: str, task_type: str) -> str:
    """根据步骤标题生成任务描述"""
    for key, desc in _STEP_DESCRIPTIONS.items():
        if key in title:
            return desc
    # 通用回退
    return f"根据任务描述和前面步骤的产出，完成「{title}」这一步骤的工作。用 dcflow_read_file 读取前序对话了解上下文，然后执行相应操作，完成后调用 dcflow_step_done。"


# ═══════════════════════════════════════════════════════════════════
# 模块级临时目录缓存（避免每次 poll 创建新目录）
# ═══════════════════════════════════════════════════════════════════

_TEMP_DIR_CACHE: dict[str, str] = {}


# ═══════════════════════════════════════════════════════════════════
# .dc_tmp 双粒度导出目录（T2.6，A1 方案① + P1-7）
#
# 从系统临时目录（PROJECT_ROOT 外）迁到 PROJECT_ROOT/.dc_tmp/<task_id>/<step_id>/：
# - 与 safe_resolve 兼容（tool_security 放行 .dc_tmp 相对路径）；
# - task+step 双粒度（cache_key = f"{task_id}:{step_id}"），并行/同任务多步骤互不覆盖；
# - 步骤目录保留到任务结束（2026-08-22：不再步骤完成即删——后续步骤需读前序步骤
#   AI 产出的文件，DB 实证 a2a0d5df），由 server 启动兜底清理回收（重启时清已完成
#   任务目录、保留进行中任务目录）；任务级目录同样保留到重启。
# ═══════════════════════════════════════════════════════════════════


def get_step_temp_dir(task_id: str, step_id: str) -> str:
    """步骤临时导出目录 = PROJECT_ROOT/.dc_tmp/<task_id>/<step_id>/（双粒度）。"""
    return os.path.join(PROJECT_ROOT, ".dc_tmp", task_id, step_id)


def get_task_root(task_id: str) -> str:
    """任务根 = PROJECT_ROOT/<task_id>/（uuid 子文件夹，2026-08-24 任务隔离）。
    代码等持久化产物落这里（工具相对路径基准）；不存在（无任务根任务）返回
    PROJECT_ROOT（workspace 根兜底，兼容旧行为）。"""
    p = os.path.join(PROJECT_ROOT, task_id)
    return p if os.path.isdir(p) else PROJECT_ROOT


async def get_task_workspace(storage, task_id: str) -> str:
    """任务工作区（2026-08-26 用户需求：创建流程可选工作目录）：tasks.workspace_dir
    非空 → 返回绝对路径（相对路径基于 PROJECT_ROOT 解析）；空 → 回退
    get_task_root（旧行为：任务根/workspace 根）。工具路径基准与 step_context
    注入共用，保证 AI 相对路径落点一致。"""
    try:
        task = await storage.get_task(task_id)
        wd = (task or {}).get("workspace_dir") or ""
        if wd:
            return os.path.abspath(wd if os.path.isabs(wd)
                                   else os.path.join(PROJECT_ROOT, wd))
    except Exception:
        pass
    return get_task_root(task_id)


def list_prior_step_outputs(task_id: str, steps: list, current_step_id: str) -> list[str]:
    """列出前序步骤目录下 AI 产出的文件（相对 PROJECT_ROOT，AI 用
    dcflow_read_file 按相对路径直接读）；排除系统导出副本
    （conversations/ 目录、task_context.json）。返回："- step-N「标题」: 相对路径、..."。
    2026-08-24（用户要求：清单瘦身）：日志/临时文件/大文件（>512KB）也不列——
    DB 实证 step-9 有 40+ 个 build_output*.log（0.5-2.8MB）污染 AI 上下文。
    路径格式说明（2026-08-24 任务隔离）：步骤目录始终位于 workspace 根的 .dc_tmp/
    （任务根外），清单路径保持 workspace 根相对（.dc_tmp/<任务ID>/...）——
    新任务工具基准是任务根时，.dc_tmp 前缀由 tool_resolve 特判回 workspace 根可读。"""
    lines = []
    for s in steps:
        if s.get("step_id") == current_step_id:
            continue
        p = get_step_temp_dir(task_id, s["step_id"])
        if not os.path.isdir(p):
            continue
        files = []
        for root, _, fnames in os.walk(p):
            for fn in sorted(fnames):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, PROJECT_ROOT).replace(os.sep, "/")
                if rel.startswith(f".dc_tmp/{task_id}/{s['step_id']}/conversations/") \
                        or rel.endswith("task_context.json"):
                    continue
                # 系统导出副本（DB 镜像）不列——AI 只关心步骤自己写出的文件
                if fn.startswith("_flow-") or fn.endswith(("-summary.json",
                                                           "-intervention.json",
                                                           "-monitor_conversation.json")):
                    continue
                # 2026-08-24：日志/临时文件/大文件不进清单（AI 只关心可读的文档与代码产物）
                ext = os.path.splitext(fn)[1].lower()
                if ext in _PRIOR_OUTPUT_SKIP_EXT \
                        or os.path.getsize(full) > _PRIOR_OUTPUT_MAX_BYTES:
                    continue
                # 2026-08-25（用户需求）：步骤目录根散落的一次性分析/修复脚本（.py）
                # 不进清单——只列 artifacts/ 下明确归档的 py（DB 实证 10092ff1：
                # .dc_tmp 下 84 个 py，step-13 就有 28 个 analyze/fix 脚本污染上下文）
                if fn.endswith(".py") and "/artifacts/" not in rel:
                    continue
                files.append(rel)
        if files:
            lines.append(f"- {s['step_id']}「{s.get('title', '')}」: " + "、".join(files))
    return lines


# 2026-08-24（用户要求：产物清单瘦身）：日志/临时文件/大文件不进前序清单
_PRIOR_OUTPUT_MAX_BYTES = 512 * 1024
_PRIOR_OUTPUT_SKIP_EXT = {".log", ".tmp", ".swp", ".pyc"}


# ═══════════════════════════════════════════════════════════════════
# StepContext 类（原 ExecutionAgent，精简为纯数据准备）
# ═══════════════════════════════════════════════════════════════════


class StepContext:
    """
    步骤上下文准备器 — 为 TS 端 Orchestrator 准备执行 prompt。

    不承担 LLM 调用或执行逻辑，仅负责：
    - 从 DB 读取任务和步骤定义
    - 导出上下文数据到临时文件夹
    - 构建 system message
    """

    def __init__(self, storage: StorageAdapter):
        self.storage = storage

    async def prepare_step(self, task_id: str, step_id: str) -> dict:
        """
        准备步骤执行的 prompt 和元数据（不执行 LLM，由 Extension 调用 vscode.lm）。

        导出对话到临时文件夹 → 注入路径到 system message → AI 按需读取。

        Returns:
            {
                "system_message": str,    # 一条 system 消息（含 temp_dir 路径 + 步骤指令）
                "temp_dir": str,          # 临时文件夹路径（AI 用 dcflow_read_file 读取）
                "model_tier": "light" | "power",
                "step_title": str,
                "step_id": str,
            }
        """
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        step = next((s for s in task.get("steps", []) if s["step_id"] == step_id), None)
        if not step:
            raise ValueError(f"Step not found: {step_id}")

        # 导出 AI 数据到 .dc_tmp/<task_id>/<step_id>/（T2.6 双粒度；模块级缓存避免重复导出）
        cache_key = f"{task_id}:{step_id}"
        if cache_key in _TEMP_DIR_CACHE and os.path.isdir(_TEMP_DIR_CACHE[cache_key]):
            tmp_dir = _TEMP_DIR_CACHE[cache_key]
            export_result = {"conversations_dir": os.path.join(tmp_dir, "conversations"),
                             "artifacts_dir": os.path.join(tmp_dir, "artifacts")}
        else:
            tmp_dir = get_step_temp_dir(task_id, step_id)
            os.makedirs(tmp_dir, exist_ok=True)
            _TEMP_DIR_CACHE[cache_key] = tmp_dir
            export_result = await self.storage.export_for_ai(task_id, step_id, tmp_dir)

        # 获取步骤描述：优先步骤自带 description（Monitor 初始编排 add_steps 携带的
        # 详细指令：目标/产出/验收），缺失时按标题从预设映射推导（G2）
        step_desc = step.get("description") or _describe_step(step.get("title", ""), task.get("type", ""))

        # 构建稳定层: 纯规则（Agent 提示词由 registry 按步骤 type 选择；
        # 兼容旧数据：无 type 时 human_attention=gate → gate-reporter，否则 step-executor）
        system_prompt = get_step_prompt(step)

        # 前序步骤产出摘要（最浓缩的结论性信息，跨步骤传递）
        prev_summaries: list[str] = []
        for s in task.get("steps", []):
            if s.get("status") == "completed" and s.get("step_id") != step_id:
                try:
                    summ = await self.storage.get_artifact(task_id, s["step_id"], "summary")
                except Exception:
                    summ = None
                if summ and summ.get("content"):
                    prev_summaries.append(
                        f"- {s['step_id']}「{s.get('title', '')}」: {str(summ['content'])[:400]}")

        # 构建动态层: 步骤特定信息
        step_context = (
            f"# 步骤: {step.get('title', '')}\n"
            f"步骤 ID: {step.get('step_id', '')}\n\n"
            f"## 任务背景\n"
            f"- 标题: {task.get('title', '')}\n"
            f"- 类型: {task.get('type', '')}\n"
            f"- 描述: {task.get('description', '')}\n\n"
            f"## 你要做什么\n"
            f"{step_desc}\n\n"
        )
        # 2026-08-24（任务级工作区隔离）：注入任务工作目录——新任务相对路径基准是
        # 任务根 workspace/<task_id>/（代码等持久化产物写这里，任务互不干扰）；
        # 2026-08-26：自定义工作目录任务（workspace_dir）注入自定义目录（绝对路径）；
        # .dc_tmp 临时目录保持 workspace 根（用绝对路径引用，见上下文数据区）
        ws = await get_task_workspace(self.storage, task_id)
        if ws != PROJECT_ROOT:
            step_context += (
                f"## 任务工作目录\n"
                f"本任务工作区（相对路径基于此目录解析，持久化产物写这里）:\n"
                f"  {ws}\n"
                f"- 代码/脚本等持久化产物：用相对路径（如 src/x.py），基于此目录解析\n"
                f"- 方案/报告等步骤产物：dcflow_write_file 传以 .dc_tmp/ 开头的完整相对路径\n"
                f"  （.dc_tmp/{task_id}/{step_id}/artifacts/xxx.md）——系统特判解析到服务端\n"
                f"  工作区 {PROJECT_ROOT}/.dc_tmp/，与本任务工作区无关；目录自动创建，无需手动建\n"
                f"- dcflow_run_cmd 的工作目录是本任务工作区：引用 .dc_tmp 下文件需用绝对路径\n"
                f"  {PROJECT_ROOT}/.dc_tmp/{task_id}/{step_id}/...（或先 cd 到该目录）\n\n"
            )
        if prev_summaries:
            step_context += (
                f"## 前序步骤产出摘要\n"
                f"{chr(10).join(prev_summaries)}\n\n"
            )
        # 2026-08-25（用户需求）：前序 gate 审批决策（reason）注入后续步骤上下文——
        # DB 实证 10092ff1：step-16 审批说"加密RVA链"，reason 只写 events 表，
        # step-17 上下文完全看不到（第一轮 description 还是旧版）导致 AI 忘记
        gate_decisions: list[str] = []
        try:
            events = await self.storage.get_events(task_id)
        except Exception:
            events = []
        step_order = {s.get("step_id"): i for i, s in enumerate(task.get("steps", []))}
        cur_idx = step_order.get(step_id, len(step_order))
        done_ids = {s.get("step_id") for s in task.get("steps", [])
                    if s.get("status") == "completed" and s.get("step_id") != step_id}
        for ev in events:
            if ev.get("event_type") != "gate_decision" or ev.get("actor") != "human":
                continue
            sid = ev.get("step_id")
            # 只注入当前步骤之前的已完成 gate 步骤决策
            if sid not in done_ids or step_order.get(sid, -1) >= cur_idx:
                continue
            try:
                content = ev.get("content") or "{}"
                if isinstance(content, str):
                    content = json.loads(content)
            except Exception:
                continue
            reason = str((content or {}).get("reason") or "").strip()
            if not reason:
                continue
            decision = (content or {}).get("decision") or ""
            step_title = next((s.get("title", "") for s in task.get("steps", [])
                               if s.get("step_id") == sid), "")
            gate_decisions.append(f"- {sid}「{step_title}」（{decision}）: {reason}")
        if gate_decisions:
            step_context += (
                f"## 前序审批决策\n"
                + "\n".join(gate_decisions) + "\n\n"
            )
        # 2026-08-22：前序步骤 AI 产出文件清单（相对 PROJECT_ROOT 路径，AI 用
        # dcflow_read_file 直接读）——此前只有对话/摘要指引，AI 不知道产出文件
        # 在哪（DB 实证 a2a0d5df：step-3 读 step-2 的根因分析报告失败）
        prior_outputs = list_prior_step_outputs(task_id, task.get("steps", []), step_id)
        if prior_outputs:
            step_context += (
                "## 前序步骤产物文件（AI 产出，用 dcflow_read_file 按相对路径读取）\n"
                + "\n".join(prior_outputs) + "\n\n"
            )
        step_context += (
            f"## 上下文数据\n"
            f"前序步骤的对话记录已导出到临时文件夹: {tmp_dir}\n\n"
            f"目录结构:\n"
            f"  - {tmp_dir}/task_context.json    → 任务和步骤定义\n"
            f"  - {tmp_dir}/conversations/       → 前序步骤完整对话（{export_result.get('conversations_dir', '无')}）\n"
            f"  - {tmp_dir}/artifacts/           → 前序步骤产物\n\n"
        )

        # 规则库路径注入（code_review 步骤）：规范按文件类型存放在规则库目录，
        # AI 按需读取（不塞进 system prompt）；路径随部署形态变化（源码/exe），
        # 由 rules_dir() 运行时解析，提示词内不硬编码
        if step.get("type") == "code_review":
            step_context += (
                f"## 规则库\n"
                f"代码审查规范按文件类型存放，路径: {rules_dir()}\n"
                f"（用 dcflow_read_file 读取对应规则文件；找不到时用 dcflow_list_dir "
                f"在项目内搜索 rules 目录）\n\n"
            )

        # 向后兼容：合并为一个 system_message（契约端点 8 B5：拼装后完整值，
        # 供展示/旧调用方；LLM 调用应使用 system_prompt + step_context 拆分结构，
        # 保证 system 前缀稳定可缓存——Task 8/§4.3.1）
        system_msg = system_prompt + "\n\n" + step_context

        return {
            "system_prompt": system_prompt,          # 新增: 纯规则
            "step_context": step_context,            # 新增: 动态上下文
            "system_message": system_msg,           # 向后兼容（Task 3 改造后会逐步移除）
            "temp_dir": tmp_dir,
            "model_tier": step.get("model_tier", "light"),
            "step_title": step.get("title", ""),
            "step_id": step_id,
        }
