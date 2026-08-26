
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
    for key, desc in _STEP_DESCRIPTIONS.items():
        if key in title:
            return desc
    return f"根据任务描述和前面步骤的产出，完成「{title}」这一步骤的工作。用 dcflow_read_file 读取前序对话了解上下文，然后执行相应操作，完成后调用 dcflow_step_done。"

_TEMP_DIR_CACHE: dict[str, str] = {}

def get_step_temp_dir(task_id: str, step_id: str) -> str:
    return os.path.join(PROJECT_ROOT, ".dc_tmp", task_id, step_id)

def get_task_root(task_id: str) -> str:
    p = os.path.join(PROJECT_ROOT, task_id)
    return p if os.path.isdir(p) else PROJECT_ROOT

def list_prior_step_outputs(task_id: str, steps: list, current_step_id: str) -> list[str]:
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
                if fn.startswith("_flow-") or fn.endswith(("-summary.json",
                                                           "-intervention.json",
                                                           "-monitor_conversation.json")):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext in _PRIOR_OUTPUT_SKIP_EXT \
                        or os.path.getsize(full) > _PRIOR_OUTPUT_MAX_BYTES:
                    continue
                if fn.endswith(".py") and "/artifacts/" not in rel:
                    continue
                files.append(rel)
        if files:
            lines.append(f"- {s['step_id']}「{s.get('title', '')}」: " + "、".join(files))
    return lines

_PRIOR_OUTPUT_MAX_BYTES = 512 * 1024
_PRIOR_OUTPUT_SKIP_EXT = {".log", ".tmp", ".swp", ".pyc"}

class StepContext:

    def __init__(self, storage: StorageAdapter):
        self.storage = storage

    async def prepare_step(self, task_id: str, step_id: str) -> dict:
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        step = next((s for s in task.get("steps", []) if s["step_id"] == step_id), None)
        if not step:
            raise ValueError(f"Step not found: {step_id}")

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

        step_desc = step.get("description") or _describe_step(step.get("title", ""), task.get("type", ""))

        system_prompt = get_step_prompt(step)

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
        if get_task_root(task_id) != PROJECT_ROOT:
            step_context += (
                f"## 任务工作目录\n"
                f"本任务独立工作区（持久化产物写这里，其他任务互不可见）:\n"
                f"  {os.path.join(PROJECT_ROOT, task_id)}\n"
                f"相对路径（如 src/x.py、docs/plan.md）基于此目录解析；\n"
                f".dc_tmp 临时文件用绝对路径引用（dcflow_read_file / run_cmd）\n\n"
            )
        if prev_summaries:
            step_context += (
                f"## 前序步骤产出摘要\n"
                f"{chr(10).join(prev_summaries)}\n\n"
            )
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

        if step.get("type") == "code_review":
            step_context += (
                f"## 规则库\n"
                f"代码审查规范按文件类型存放，路径: {rules_dir()}\n"
                f"（用 dcflow_read_file 读取对应规则文件；找不到时用 dcflow_list_dir "
                f"在项目内搜索 rules 目录）\n\n"
            )

        try:
            from .config import get_memory_config
            mem_cfg = get_memory_config()
            if mem_cfg.get("enabled"):
                from .rest_api import _get_memory_storage
                ms = _get_memory_storage()
                if ms is not None:
                    bank_id = ms.get_or_create_bank_for_project(PROJECT_ROOT)
                    from .memory import get_recaller
                    recaller = get_recaller(ms, mem_cfg)
                    recall_result = await recaller.recall(
                        bank_id=bank_id,
                        query=f"{step.get('title', '')} {task.get('description') or ''}",
                        max_tokens=mem_cfg.get("recall_max_tokens", 4096),
                        budget="low",
                    )
                    mem_text = ms.format_recall_for_prompt(recall_result)
                    if mem_text:
                        step_context += f"\n\n## 项目记忆（来自历史任务）\n{mem_text}\n"
        except Exception:
            logger.warning("[DC:stepctx] memory recall skipped (disabled or error)", exc_info=True)

        system_msg = system_prompt + "\n\n" + step_context

        return {
            "system_prompt": system_prompt,
            "step_context": step_context,
            "system_message": system_msg,
            "temp_dir": tmp_dir,
            "model_tier": step.get("model_tier", "light"),
            "step_title": step.get("title", ""),
            "step_id": step_id,
        }
