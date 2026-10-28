from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .storage.adapter import StorageAdapter
from .prompts import load_prompt, rules_dir, templates_dir
from .prompts.registry import get_step_prompt
from .config import PROJECT_ROOT

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

STEP_TYPE_MODEL_TIER = {
    "executor": "light", "monitor": "light", "gate": "light",
    "code_review": "light", "report": "light", "researcher": "light",
    "requirement": "light",
    "plan": "power", "review": "power", "reverse": "power",
    "architect": "power", "plan_review": "power",
}


ARCHITECT_SCORE_THRESHOLD = 7
_SCORE_VERSION = "v1-2026-09-04"

_SCORE_FIELDS = {
    "functionPoints": ("number", "功能点数量（数字 ≥0）"),
    "fileCount": ("number", "涉及文件数量（数字 ≥0）"),
    "isMultiModule": ("bool", "是否多模块（true/false）"),
    "isArchitectural": ("bool", "是否架构改造（true/false）"),
    "clarity": ("int", "需求明确度 1-10（1-3 非常明确/4-6 部分明确/7-10 模糊探索）"),
    "difficulty": ("int", "技术难度 1-10（1-3 简单/4-6 中等/7-10 困难）"),
}


def validate_score_params(params) -> str | None:
    if not isinstance(params, dict):
        return ("score 必须是对象（6 个字段：functionPoints/fileCount/"
                "isMultiModule/isArchitectural/clarity/difficulty）")
    for field, (kind, desc) in _SCORE_FIELDS.items():
        if field not in params:
            return f"score 缺少字段 {field}（{desc}）"
        v = params[field]
        if kind == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                return f"score.{field} 非法（期望{desc}，实际 {v!r}）"
        elif kind == "bool":
            if not isinstance(v, bool):
                return f"score.{field} 非法（期望{desc}，实际 {v!r}）"
        else:
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 10:
                return f"score.{field} 非法（期望{desc}，实际 {v!r}）"
    return None


def compute_task_score(params: dict, delta: float = 0.0) -> dict:
    objective = ((params["functionPoints"] * 0.8
                  + params["fileCount"] * 0.5
                  + (3 if params["isMultiModule"] else 0)
                  + (4 if params["isArchitectural"] else 0)) / 4.0)
    subjective = params["clarity"] * 0.4 + params["difficulty"] * 0.6
    final_raw = objective * 0.6 + subjective * 0.4 + delta
    tier = max(1, min(10, int(math.ceil(final_raw))))
    return {
        "objective": round(objective, 3), "subjective": round(subjective, 3),
        "final": round(final_raw, 3), "tier": tier, "delta": delta,
        "need_architect": tier >= ARCHITECT_SCORE_THRESHOLD,
        "version": _SCORE_VERSION,
        **{k: params[k] for k in _SCORE_FIELDS},
    }


async def score_context_section(storage, task_id: str) -> str:
    try:
        raw = await storage.get_artifact(task_id, "_flow", "task_score")
    except Exception:  # noqa: BLE001
        return ""
    if not raw or not raw.get("content"):
        return ""
    try:
        sc = json.loads(raw["content"])
    except (ValueError, TypeError):
        return ""
    arch_note = ("需要（架构分析步骤已插入，方案设计须严格依据其架构文档）"
                 if sc.get("need_architect") else "无需")
    return ("## 任务评分（系统按需求分析结果算分，结果只读）\n"
            f"- 最终分/档位：{sc.get('tier')}/10（客观 {sc.get('objective')}、"
            f"主观 {sc.get('subjective')}）\n"
            f"- 是否需要架构分析：{arch_note}\n"
            "（评分由需求分析师产出，其他角色无需也不得评分）\n")


_STEP_DESCRIPTIONS = {
    "理解需求": "阅读任务描述，调用 dcflow_read_doc 查阅相关业务文档，明确需求范围。输出需求理解摘要：用户期望什么、涉及哪些模块、有哪些模糊点需要后续步骤澄清。",
    "阅读业务文档": "调用 dcflow_read_doc 阅读知识库中与任务相关的业务文档。梳理文档中与当前任务相关的业务规则、约束条件、已知问题。",
    "阅读代码": "调用 dcflow_search_code 和 dcflow_read_file 搜索并阅读项目中与任务相关的代码。理解当前代码结构、关键类和函数。",
    "梳理现有逻辑": "基于前序步骤的分析，完整梳理当前系统的业务逻辑。画出业务流程（触发条件→处理逻辑→状态变化→代码位置），标注与用户预期的差异点。",
    "对比分析": "将前序步骤梳理的现有逻辑与用户需求做逐条对比。列出差异点、根因、修复复杂度。输出对比表格。",
    "提出修改方案": "基于对比分析，提出 2-3 个修改方案。对比各方案优缺点、影响范围、风险，选择推荐方案并说明理由。**此步骤为 Gate，需人工审批。**",
    "测试设计": "为推荐方案设计测试用例。覆盖正向、边界、异常场景。输出测试用例表格。",
    "编写测试代码": "根据测试设计，编写实际可运行的测试代码。确保测试覆盖关键路径。",
    "代码修改": "根据审批通过的方案，修改代码。调用 dcflow_write_file/dcflow_edit_file 实现变更（同一文件的多处改动合并到一次 dcflow_run_code 里批量替换，不要分成多次调用改同一文件）。修改完成后调用 dcflow_search_code 检查引用完整性。",
    "运行测试": "调用 dcflow_run_code 运行测试（一段 Python 代码里跑 pytest/unittest 并只 print 失败摘要），确保所有测试通过。如有失败，修复后重新运行。",
    "文档更新": "根据代码变更更新相关文档。调用 dcflow_read_doc 检查现有文档，然后用 dcflow_write_file 一次写完整文档（不要分多次补写同一文件）。",
    "Code Review": "审查代码变更。检查逻辑正确性、代码风格、边界处理、安全性。输出 CR 意见。",
    "代码修改(第二轮)": "根据 CR 意见修改代码。逐条处理审查意见（同一文件的多条意见合并到一次 dcflow_run_code 里改完，不要一条意见一轮），修复后重新检查。",
    "最终验证": "最终验证所有测试通过、文档完整、无遗漏。确认可以合并。",
}


def _describe_step(title: str, task_type: str) -> str:
    for key, desc in _STEP_DESCRIPTIONS.items():
        if key in title:
            return desc
    return f"根据任务描述和前面步骤的产出，完成「{title}」这一步骤的工作。用 dcflow_read_file 读取前序对话了解上下文，然后执行相应操作，完成后调用 dcflow_step_done。"




def get_step_temp_dir(task_id: str, step_id: str) -> str:
    return os.path.join(PROJECT_ROOT, ".dc_tmp", task_id, step_id)


def get_task_root(task_id: str) -> str:
    p = os.path.join(PROJECT_ROOT, task_id)
    return p if os.path.isdir(p) else PROJECT_ROOT


async def get_task_workspace(storage, task_id: str) -> str:
    try:
        task = await storage.get_task(task_id)
        wd = (task or {}).get("workspace_dir") or ""
        if wd:
            return resolve_workspace_dir(wd)
    except Exception:
        pass
    return get_task_root(task_id)




def normalize_project_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(
        os.path.abspath(os.path.expanduser(str(path or "")))))


def resolve_workspace_dir(wd: str) -> str:
    wd = str(wd or "")
    return os.path.abspath(wd if os.path.isabs(wd) else os.path.join(PROJECT_ROOT, wd))


async def _resolve_project_paths(storage, task_id: str) -> tuple[str, str]:
    wd = ""
    try:
        task = await storage.get_task(task_id)
        wd = str((task or {}).get("workspace_dir") or "").strip()
    except Exception:  # noqa: BLE001 —— 项目键解析失败不得阻断执行
        wd = ""
    base = resolve_workspace_dir(wd) if wd else os.path.join(PROJECT_ROOT, task_id)
    return base, normalize_project_path(base)


async def get_project_key(storage, task_id: str) -> str:
    return (await _resolve_project_paths(storage, task_id))[1]


async def get_project_path(storage, task_id: str) -> str:
    return (await _resolve_project_paths(storage, task_id))[0]



CODE_DIAGRAM_FILENAME = "代码流程图.md"
CODE_DIAGRAM_ASSET = "code_diagram"
PROJECT_CONTEXT_FILENAME = "项目背景与约定.md"
PROJECT_CONTEXT_ASSET = "project_context"

_MIG_LEGACY_FILENAME = "项目流程图.md"
_MIG_LEGACY_ARTIFACT = "project_diagram"

PROJECT_ASSET_FILES = {
    CODE_DIAGRAM_FILENAME: CODE_DIAGRAM_ASSET,
    PROJECT_CONTEXT_FILENAME: PROJECT_CONTEXT_ASSET,
}
PROJECT_ASSET_CANON = {
    CODE_DIAGRAM_ASSET: CODE_DIAGRAM_FILENAME,
    PROJECT_CONTEXT_ASSET: PROJECT_CONTEXT_FILENAME,
}
PROJECT_ASSET_TYPES = (CODE_DIAGRAM_ASSET, PROJECT_CONTEXT_ASSET)

def _project_context_skeleton() -> str:
    path = os.path.join(templates_dir(), "项目背景与约定骨架.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("# 项目背景与约定\n\n## 项目信息\n\n## 开发规范\n\n"
                "## 经验教训\n\n## 任务总结\n")


def project_asset_local_path(task_id: str, asset_type: str, filename: str = "") -> str:
    return os.path.join(PROJECT_ROOT, ".dc_tmp", task_id,
                        filename or PROJECT_ASSET_CANON[asset_type])


def read_asset_file(fp: str) -> str:
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def write_asset_file(fp: str, content: str) -> None:
    os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)


def asset_ts(asset: Optional[dict]) -> float:
    try:
        return datetime.fromisoformat(str((asset or {}).get("updated_at"))).timestamp()
    except Exception:  # noqa: BLE001
        return time.time()


async def load_project_asset(storage, task_id: str, asset_type: str) -> Optional[dict]:
    key = await get_project_key(storage, task_id)
    asset = await storage.get_project_asset(key, asset_type)
    if asset and (asset.get("content") or ""):
        return asset
    content = read_asset_file(project_asset_local_path(task_id, asset_type))
    if not content:
        return None
    await storage.save_project_asset(
        key, asset_type, content, "text",
        project_path=await get_project_path(storage, task_id), source_task_id=task_id)
    logger.info(f"[DC:ctx] project asset promoted to project level task={task_id} "
                f"type={asset_type} ({len(content)} chars)")
    return await storage.get_project_asset(key, asset_type)


async def sync_project_assets(storage, task_id: str) -> None:
    try:
        key = await get_project_key(storage, task_id)
        path = await get_project_path(storage, task_id)
        for asset_type in PROJECT_ASSET_TYPES:
            asset = await load_project_asset(storage, task_id, asset_type)
            if not asset and asset_type == PROJECT_CONTEXT_ASSET:
                await storage.save_project_asset(
                    key, asset_type, _project_context_skeleton(), "text",
                    project_path=path, source_task_id=task_id)
                asset = await storage.get_project_asset(key, asset_type)
            if not asset:
                continue
            content = asset.get("content") or ""
            local_fp = project_asset_local_path(task_id, asset_type)
            local = read_asset_file(local_fp)
            if local and local != content and os.path.getmtime(local_fp) > asset_ts(asset):
                await storage.save_project_asset(key, asset_type, local, "text",
                                                 project_path=path,
                                                 source_task_id=task_id)
                content = local
            if not content:
                continue
            if read_asset_file(local_fp) != content:
                write_asset_file(local_fp, content)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[DC:ctx] sync project assets failed task={task_id}: {e}")


def _iter_legacy_diagram_files(root: str) -> list[str]:
    if not root or not os.path.isdir(root):
        return []
    found = []
    for dp, _dn, fn in os.walk(root):
        if _MIG_LEGACY_FILENAME in fn:
            found.append(os.path.join(dp, _MIG_LEGACY_FILENAME))
    return found


def _migrate_legacy_diagram_file(legacy_fp: str, summary: dict) -> None:
    canon_fp = os.path.join(os.path.dirname(legacy_fp), CODE_DIAGRAM_FILENAME)
    legacy_content = read_asset_file(legacy_fp)
    if not os.path.isfile(canon_fp):
        os.replace(legacy_fp, canon_fp)
        summary["renamed"] += 1
        summary["details"].append(f"rename {legacy_fp} -> {canon_fp}")
        return
    canon_content = read_asset_file(canon_fp)
    if legacy_content == canon_content:
        os.remove(legacy_fp)
        summary["deduped"] += 1
        summary["details"].append(f"dedupe {legacy_fp} (identical to {canon_fp})")
        return
    legacy_newer = os.path.getmtime(legacy_fp) > os.path.getmtime(canon_fp)
    if legacy_newer:
        write_asset_file(canon_fp, legacy_content)
    os.remove(legacy_fp)
    summary["conflicts"] += 1
    summary["details"].append(
        f"conflict {legacy_fp} vs {canon_fp}: legacy={len(legacy_content)} chars "
        f"canon={len(canon_content)} chars winner={'legacy' if legacy_newer else 'canon'}")


async def migrate_legacy_code_diagram(storage, extra_roots=(), report_roots=()) -> dict:
    summary = {"db_migrated": 0, "db_skipped": 0, "db_deleted": 0, "renamed": 0,
               "deduped": 0, "conflicts": 0, "skipped_external": [], "details": []}
    details = summary["details"]

    try:
        tasks = await storage.list_tasks()
    except Exception as e:  # noqa: BLE001
        tasks = []
        details.append(f"db: list_tasks failed: {e}")
    for t in tasks:
        tid = str((t or {}).get("id") or "")
        if not tid:
            continue
        try:
            raw = await storage.get_artifact(tid, "_flow", _MIG_LEGACY_ARTIFACT)
            content = (raw or {}).get("content") or ""
            if not content:
                continue
            key = await get_project_key(storage, tid)
            existing = await storage.get_project_asset(key, CODE_DIAGRAM_ASSET)
            if existing and (existing.get("content") or ""):
                summary["db_skipped"] += 1
                details.append(
                    f"db skip task={tid[:8]} key={key} artifact={len(content)} chars "
                    f"project={len(existing.get('content') or '')} chars (keep project level)")
            else:
                await storage.save_project_asset(
                    key, CODE_DIAGRAM_ASSET, content, "text",
                    project_path=await get_project_path(storage, tid),
                    source_task_id=tid, source_step_id="_flow")
                summary["db_migrated"] += 1
                details.append(f"db migrate task={tid[:8]} key={key} ({len(content)} chars)")
            await storage.delete_artifacts(tid, "_flow", _MIG_LEGACY_ARTIFACT)
            summary["db_deleted"] += 1
        except Exception as e:  # noqa: BLE001
            details.append(f"db failed task={tid[:8]}: {e}")

    seen: set[str] = set()
    for root in [PROJECT_ROOT, *[str(r) for r in extra_roots]]:
        for fp in _iter_legacy_diagram_files(root):
            marker = os.path.normcase(os.path.abspath(fp))
            if marker in seen:
                continue
            seen.add(marker)
            try:
                _migrate_legacy_diagram_file(fp, summary)
            except OSError as e:
                details.append(f"file failed {fp}: {e}")
    for root in report_roots:
        for fp in _iter_legacy_diagram_files(str(root)):
            marker = os.path.normcase(os.path.abspath(fp))
            if marker in seen:
                continue
            seen.add(marker)
            summary["skipped_external"].append(fp)
            details.append(f"report only (outside migration scope): {fp}")
    return summary


def list_prior_step_outputs(task_id: str, steps: list, current_step_id: str) -> list[str]:
    lines = []
    for s in steps:
        if s.get("step_id") == current_step_id:
            continue
        if s.get("status") != "completed":
            continue
        p = get_step_temp_dir(task_id, s["step_id"])
        if not os.path.isdir(p):
            continue
        has_conv = os.path.isfile(os.path.join(p, "conversation.json"))
        md_files: list[str] = []
        other_count = 0
        for root, _, fnames in os.walk(p):
            for fn in sorted(fnames):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, PROJECT_ROOT).replace(os.sep, "/")
                if fn == "conversation.json" \
                        or rel.startswith(f".dc_tmp/{task_id}/{s['step_id']}/conversations/") \
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
                if ext == ".md":
                    md_files.append(rel)
                else:
                    other_count += 1
        if not md_files and not other_count and not has_conv:
            continue
        line = f"- {s['step_id']}「{s.get('title', '')}」"
        if md_files:
            line += ": " + "、".join(md_files)
        if other_count:
            line += (f"（另有 {other_count} 个非 .md 产物在该步骤 artifacts/ 目录，"
                     "用 dcflow_list_dir 查看、dcflow_read_file 按需读取）")
        if has_conv:
            line += f"（对话原文: .dc_tmp/{task_id}/{s['step_id']}/conversation.json）"
        lines.append(line)
    return lines


_PRIOR_OUTPUT_MAX_BYTES = 512 * 1024
_PRIOR_OUTPUT_SKIP_EXT = {".log", ".tmp", ".swp", ".pyc"}

_KEY_FINDINGS_BLOCK_STRIP_RE = re.compile(
    r"<!-- DC-KEY-FINDINGS-START -->\s*## 系统注入：关键发现.*?"
    r"<!-- DC-KEY-FINDINGS-END -->\s*$", re.S)


def strip_key_findings_block(report: str) -> str:
    return _KEY_FINDINGS_BLOCK_STRIP_RE.sub("", str(report or "")).rstrip()




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

        tmp_dir = get_step_temp_dir(task_id, step_id)
        await sync_project_assets(self.storage, task_id)

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
        if step.get("type") == "plan":
            step_context += (
                "## 流程报告（全局进展主文档）\n"
                f"盘点前先用 dcflow_read_file 读 .dc_tmp/{task_id}/流程报告.md"
                "（已整合各步骤进度与关键发现；全文不在此注入以免上下文膨胀，务必先读报告再盘点）\n\n"
            )
        step_context += (
            "## 项目级共享资产\n"
            "以下两份按项目路径共享（同一项目的所有流程共用一份，修改会同步到其他流程）：\n"
            f"- {CODE_DIAGRAM_FILENAME}（.dc_tmp/{task_id}/）\n"
            f"- {PROJECT_CONTEXT_FILENAME}（.dc_tmp/{task_id}/）\n"
            "骨架模板（写前先用 dcflow_read_file 读）：\n"
            f"- 流程报告 → {templates_dir()}/流程报告骨架.md\n"
            f"- 代码流程图 → {templates_dir()}/代码流程图骨架.md\n"
            f"- 项目背景与约定 → {templates_dir()}/项目背景与约定骨架.md\n\n"
        )
        if step.get("type") == "plan":
            step_context += (
                "产出 plan 之后必须同步修改代码流程图：新增/调整的路线与分支条件写入图中，"
                "保持图与方案一致；同时把新增的项目信息/开发规范补进 项目背景与约定.md。\n\n"
            )
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
                f"- dcflow_run_code 的子进程工作目录（cwd）就是本任务工作区，解释器与后端同一个；"
                f"引用 .dc_tmp 下文件需用完整路径（从盘符开始）\n"
                f"  {PROJECT_ROOT}/.dc_tmp/{task_id}/{step_id}/...（或先 cd 到该目录）\n\n"
            )
        if prev_summaries:
            step_context += (
                f"## 前序步骤产出摘要\n"
                f"{chr(10).join(prev_summaries)}\n\n"
            )
        if step.get("type") == "plan":
            plan_lines: list[str] = []
            for s in task.get("steps", []):
                if (s.get("type") != "plan" or s.get("status") != "completed"
                        or s.get("step_id") == step_id):
                    continue
                try:
                    ps = await self.storage.get_artifact(task_id, s["step_id"], "summary")
                except Exception:
                    ps = None
                ps_text = str((ps or {}).get("content") or "(无 summary)")
                plan_lines.append(
                    f"- {s['step_id']}「{s.get('title', '')}」summary:\n"
                    f"{ps_text}\n"
                    f"  方案文档目录: .dc_tmp/{task_id}/{s['step_id']}/artifacts/（read_file 读原文盘点）")
            if plan_lines:
                step_context += (
                    f"## 历史计划索引（此前全部方案步骤的结论与文档位置，盘点必读）\n"
                    f"{chr(10).join(plan_lines)}\n\n"
                )
            pend_lines: list[str] = []
            for s in task.get("steps", []):
                if s.get("status") not in ("pending", "stopped") or s.get("step_id") == step_id:
                    continue
                d = str(s.get("description") or "").strip()[:600]
                line = f"- {s.get('step_id', '')} [{s.get('status', '')}] {s.get('title', '')}"
                if d:
                    line += f"\n  description: {d}"
                pend_lines.append(line)
            if pend_lines:
                step_context += (
                    f"## 待执行/中断步骤（后续已排计划，盘点时对照是否仍适用）\n"
                    f"{chr(10).join(pend_lines)}\n\n"
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
        step_context += "## 前序步骤资料（引用式：文件在各步骤自己的目录，按需 dcflow_read_file）\n"
        step_context += (
            ("\n".join(prior_outputs) + "\n") if prior_outputs
            else "（前序步骤暂无产物文件）\n"
        )
        step_context += (
            "\n## 本步骤产物目录（你的报告等产物写这里）\n"
            f"  绝对路径: {os.path.join(tmp_dir, 'artifacts')}\n"
            f"  工具相对路径: .dc_tmp/{task_id}/{step_id}/artifacts/<文件名>\n\n"
        )

        if step.get("type") == "code_review":
            step_context += (
                f"## 规则库\n"
                f"代码审查规范按文件类型存放，路径: {rules_dir()}\n"
                f"（用 dcflow_read_file 读取对应规则文件；找不到时用 dcflow_list_dir "
                f"在项目内搜索 rules 目录）\n\n"
            )

        step_context += await score_context_section(self.storage, task_id)

        system_msg = system_prompt + "\n\n" + step_context

        return {
            "system_prompt": system_prompt,
            "step_context": step_context,
            "system_message": system_msg,
            "temp_dir": tmp_dir,
            "model_tier": STEP_TYPE_MODEL_TIER.get(step.get("type") or "executor", "light"),
            "step_title": step.get("title", ""),
            "step_id": step_id,
        }
