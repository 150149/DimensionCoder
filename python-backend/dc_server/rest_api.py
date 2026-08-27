
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse  # SWP3-C：/sse（sse-starlette，requirements v3.0）

from . import config  # SWP3-C：config.json 读写/get_llm_config（GET/PUT /api/config 数据源）
from .config import DB_PATH, PROJECT_ROOT, CORS_ORIGINS
from .storage import create_storage, StorageAdapter
from .state_machine.state_machine import StateMachine
from .monitor_context import MonitorContext
from .step_context import StepContext, get_task_workspace
from .prompts import load_prompt, rules_dir
from .prompts.registry import is_hidden_step, is_virtual_step, prompt_for_step
from .tool_security import safe_resolve
from .graceful import graceful
from .brain.orchestrator import (Orchestrator, OrchestratorBusyError, _StepGracefulDrain,
                                 _FLOW_REPORT_FILENAME, _FLOW_REPORT_ANCHOR_CHARS)  # SWP3-B1 跨包追加点
from .brain.llm_client import LlmClient, LlmError  # SWP3-C：/api/config/test-llm

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# FastAPI 实例 + CORS（收紧：仅允许 config.CORS_ORIGINS）
# ═══════════════════════════════════════════════════════════════════

rest_app = FastAPI(title="DimensionCoding API", version="0.2.0")

rest_app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════════
# 全局依赖（延迟初始化）
# ═══════════════════════════════════════════════════════════════════

_storage: Optional[StorageAdapter] = None
_state_machine: Optional[StateMachine] = None
_monitor_context: Optional[MonitorContext] = None
_step_context: Optional[StepContext] = None
_orchestrator: Optional[Orchestrator] = None   # SWP3-B1：执行引擎单例（running 归属 orchestrator，C4）
_sse_hub: Optional[Any] = None                 # SWP3-B1：SSE 事件中心（SWP3-C 挂 /sse 时复用）


def _get_storage() -> StorageAdapter:
    global _storage
    if _storage is None:
        _storage = create_storage({"db_path": DB_PATH})
    return _storage


_memory_storage_instance: Optional[Any] = None   # 2026-08-25：记忆模块懒单例


def _get_memory_storage():
    """Lazy singleton for MemoryStorage. Returns None if memory disabled.
    （Hindsight 记忆模块 B-2 契约：enabled=False 时全部跳过）"""
    global _memory_storage_instance
    from .config import get_memory_config
    mem_cfg = get_memory_config()
    if not mem_cfg.get("enabled"):
        return None
    if _memory_storage_instance is None:
        from .memory import get_memory_storage
        _memory_storage_instance = get_memory_storage(mem_cfg)
    return _memory_storage_instance


def _get_state_machine() -> StateMachine:
    global _state_machine
    if _state_machine is None:
        _state_machine = StateMachine(storage=_get_storage())
    return _state_machine


def _get_monitor_context() -> MonitorContext:
    global _monitor_context
    if _monitor_context is None:
        _monitor_context = MonitorContext(storage=_get_storage())
    return _monitor_context


def _get_step_context() -> StepContext:
    global _step_context
    if _step_context is None:
        _step_context = StepContext(storage=_get_storage())
    return _step_context


def _get_sse_hub():
    """SSE 事件中心单例（SWP3-B1：执行引擎发布事件；SWP3-C 挂 /sse 时复用同一实例）。"""
    global _sse_hub
    if _sse_hub is None:
        from .brain.sse_hub import SseHub
        _sse_hub = SseHub()
    return _sse_hub


def _get_orchestrator() -> Orchestrator:
    """执行引擎单例（SWP3-B1 跨包追加点）。

    测试隔离：conftest 替换 _storage/_state_machine 后（引用变化）自动重建。
    """
    global _orchestrator
    if (_orchestrator is None
            or _orchestrator._storage is not _storage
            or _orchestrator._sm is not _state_machine):
        _orchestrator = Orchestrator(
            storage=_get_storage(),
            state_machine=_get_state_machine(),
            sse_hub=_get_sse_hub(),
        )
    return _orchestrator


# ═══════════════════════════════════════════════════════════════════
# 错误文案约定（第 8 轮 J5 修订）
#
# 状态敏感端点（advance/approve/reject/resume/delete/pause/start）校验失败
# 或并发冲突时，detail/error 必须用用户可读中文文案，禁止直接透出英文状态机
# 消息（invalid transition 等仅可追加在括号内作调试信息）。
# ═══════════════════════════════════════════════════════════════════


def _state_error(e: Exception) -> HTTPException:
    """把状态机 ValueError 转成用户可读的中文 HTTPException（J5）。"""
    msg = str(e)
    low = msg.lower()
    if "task not found" in low:
        return HTTPException(404, detail=f"任务不存在 ({msg})")
    if "step not found" in low:
        return HTTPException(404, detail=f"步骤不存在 ({msg})")
    return HTTPException(
        400,
        detail=f"当前状态不允许此操作，请刷新查看最新状态 ({msg})",
    )


# ═══════════════════════════════════════════════════════════════════
# 健康检查 + 提示词
# ═══════════════════════════════════════════════════════════════════


@rest_app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.2.0"}


@rest_app.get("/api/prompt/{name}")
async def get_prompt(name: str):
    """获取指定提示词文件内容（供前端/编排侧获取分类器等 prompt）。"""
    try:
        content = load_prompt(name)
    except FileNotFoundError:
        raise HTTPException(404, f"Prompt not found: {name}")
    return {"name": name, "content": content}


# ═══════════════════════════════════════════════════════════════════
# Task CRUD
# ═══════════════════════════════════════════════════════════════════


@rest_app.post("/api/task")
async def create_task(payload: dict):
    """创建 Task。

    响应 {task_id, task_type, title, steps}。
    预设模板/分类器已退役（Monitor 统一编排）：新任务一律创建为空步骤任务，
    创建后后台自动触发 Monitor 初始编排生成完整流程（SSE _monitor 事件广播，
    前端总览实时可见）。auto_start 为废弃参数（L4 修订）：读取但忽略。
    """
    task_type = payload.get("task_type", "custom")
    title = payload.get("title", "")
    description = payload.get("description", "")
    # 分类器退役后无标题生成：未传 title 时用描述前 20 字兜底（原 planner 标题规则）
    if not title.strip() and description.strip():
        title = description.strip()[:20]
    epic_id = payload.get("epic_id")
    assignee = payload.get("assignee", "")
    payload.get("auto_start", True)  # 废弃参数，读取但忽略（L4）
    # 2026-08-26（用户需求：创建流程可选工作目录）：workspace_dir 空 = 自动分配
    # workspace/<tid>/；非空 = 自定义工作区（AI 相对路径基准）。相对路径基于
    # PROJECT_ROOT 解析（与 config.projectRoot 语义一致）；目录由创建时幂等创建
    workspace_dir = payload.get("workspace_dir")
    if isinstance(workspace_dir, str):
        workspace_dir = workspace_dir.strip() or None
    if workspace_dir:
        workspace_dir = os.path.abspath(
            workspace_dir if os.path.isabs(workspace_dir)
            else os.path.join(PROJECT_ROOT, workspace_dir))

    sm = _get_state_machine()

    task_id = await sm.create_task(
        task_type=task_type,
        title=title,
        description=description,
        epic_id=epic_id,
        assignee=assignee,
        workspace_dir=workspace_dir,
    )
    # 2026-08-24（用户需求：任务独立 uuid 文件夹）：创建任务根 workspace/<task_id>/
    # ——工具相对路径基准（代码等持久化产物落这里，任务互不干扰）；幂等。
    # 2026-08-26：自定义工作目录任务建自定义目录（不建 workspace/<tid>/ 任务根）
    try:
        os.makedirs(workspace_dir or os.path.join(PROJECT_ROOT, task_id),
                    exist_ok=True)
    except OSError:
        logger.exception(f"create_task: 创建任务根失败 task_id={task_id}")

    # 实体化（2026-08-21）：初始编排 = 插入 monitor-init 步骤 + 启动执行循环
    # （执行循环先执行 monitor-init 编排 → 完成后自动插 review+report → 真实步骤；
    # start_task 幂等 + _ensure_initial_orchestration 防重入兜底）
    try:
        orch = _get_orchestrator()
        await orch._ensure_initial_orchestration(task_id)
        await orch.start_task(task_id)
    except Exception:
        pass  # 初始编排失败不影响创建（任务保持空步骤，可后续重试）

    # 自动生成流程标题（2026-08-20 恢复用户需求）：后台轻量模型根据描述生成
    # 简洁中文标题并更新 DB——未生成完成前保持前 20 字兜底（前端 1s 轮询
    # 自动刷新显示）；失败静默（不影响创建）
    try:
        asyncio.create_task(_auto_generate_title(task_id, description))
    except Exception:
        pass  # 标题生成触发失败不影响创建

    return {
        "task_id": task_id,
        "task_type": task_type,
        "title": title,
        "steps": [],
    }


async def _auto_generate_title(task_id: str, description: str) -> None:
    """后台自动生成任务标题（轻量模型，≤12 字简洁中文）。

    失败/生成异常静默返回——保持创建时的前 20 字兜底标题；
    成功则 storage.update_task 更新 title（前端轮询自动显示）。
    """
    desc = (description or "").strip()
    if not desc:
        return
    try:
        orch = _get_orchestrator()
        client = orch._make_llm_client("light")
        prompt = (
            "根据下面的任务描述生成一个简洁的中文任务标题。\n"
            "规则：不超过 12 个字；不要标点、引号、句号；不要任何解释或前缀；\n"
            "直接输出标题本身；描述里出现文件路径/目录时，提炼其目的而非照抄路径。\n\n"
            f"任务描述：{desc[:200]}"
        )
        text = ""
        async for ev in client.stream_chat(
            [{"role": "system", "content": "你是任务标题生成器，只输出标题本身，不输出任何其他内容。"},
             {"role": "user", "content": prompt}],
            tools=None,
            signal=None,
        ):
            if ev["type"] == "text":
                text += ev["text"]
        title = text.strip().strip('\"\'“”‘’。，,、：:；;\n\t')
        if not title or len(title) > 20:
            return  # 生成异常/超长 → 保持兜底标题
        await _get_storage().update_task(task_id, {"title": title})
        logger.info(f"[DC:REST] auto title generated for {task_id}: {title}")
    except Exception:  # noqa: BLE001
        pass  # 标题生成失败不影响任务


# 轮询瘦身（2026-08-27 用户反馈：每秒 200KB 传输）：listTasks/getTask 的
# steps 剔除长文本字段（前端零引用；详情页全字段走 getStep）
_LIGHT_STEP_FIELDS = ("description", "process_template", "process_read_rules")


def _strip_step_heavy_fields(steps: list) -> list:
    """从步骤字典剔除长文本字段（轮询响应瘦身，保留展示/统计字段）。"""
    for s in steps:
        for k in _LIGHT_STEP_FIELDS:
            s.pop(k, None)
    return steps


@rest_app.get("/api/tasks")
async def get_project_overview():
    """获取项目总览。

    available_task_types 含 7 预设 + "custom"（M6 修订：分类器可选输出）。
    2026-08-27：steps 剔除长文本字段（Sidebar 1s 轮询，全量曾 265KB/次）。
    """
    storage = _get_storage()
    epics = await storage.list_epics()
    tasks = await storage.list_tasks()
    for t in tasks:
        if t.get("steps"):
            _strip_step_heavy_fields(t["steps"])

    status_count = {}
    for t in tasks:
        s = t.get("status", "unknown")
        status_count[s] = status_count.get(s, 0) + 1

    return {
        "epics": epics,
        "tasks": tasks,
        "task_count": len(tasks),
        "status_distribution": status_count,
        # 预设类型已退役（Monitor 统一编排）：保留空数组兼容旧前端
        "available_task_types": [],
    }


_DECISION_PKG_RE = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```")


async def _step_has_decision_pkg(storage, task_id: str, step_id: str) -> bool:
    """J5：步骤最近消息中是否已有 AI 输出的决策请求包（含 options 的 JSON 块）。
    只取最近 10 条消息找最后一条非空 assistant 文本判定，代价极低（getTask 轮询安全）。"""
    try:
        msgs = await storage.get_step_messages(task_id, step_id, limit=10)
    except Exception:
        return False
    for m in reversed(msgs):
        if m.get("role") == "assistant" and str(m.get("content", "") or "").strip():
            content = str(m["content"])
            match = _DECISION_PKG_RE.search(content)
            if not match:
                return False
            try:
                obj = json.loads(match.group(1))
            except (json.JSONDecodeError, TypeError):
                return False
            return isinstance(obj, dict) and isinstance(obj.get("options"), list)
    return False


@rest_app.get("/api/task/{task_id}")
async def get_task_detail(task_id: str):
    """获取 Task 详情（含步骤状态 + 产物摘要 + Monitor 对话 + 最近事件）。

    task.steps 结构（问题 17）：[{step_id,title,status,required,parallel_with,
    human_attention,model_tier,sort_order}]——执行循环与前端 FlowOverview /
    ProgressRail 的唯一步骤来源。
    """
    storage = _get_storage()
    task = await storage.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    artifacts = await storage.list_artifacts(task_id)
    artifact_summary = {}
    monitor_conversations = {}
    for a in artifacts:
        key = f"{a['step_id']}/{a['artifact_type']}"
        content = a.get("content", "")
        if a.get("artifact_type") == "monitor_conversation":
            sid = a.get("step_id", "monitor-init")
            try:
                msgs = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                msgs = []
            monitor_conversations[sid] = {
                "message_count": len(msgs),
            }
        else:
            artifact_summary[key] = {
                "format": a.get("content_format", "json"),
                "preview": content if a.get("artifact_type") == "conversation" else content[:200],
            }

    # step_messages 只返回每步消息计数（瘦身：总览页无消费方，全量消息曾使响应
    # 达数十 MB——getTask 每秒轮询被拖到 3.5s；全量对话由 getStep 端点分页拉取）
    step_message_counts: dict[str, int] = {}
    steps = task.get("steps", [])
    for s in steps:
        sid = s.get("step_id", "")
        cnt = await storage.count_step_messages(task_id, sid)
        if cnt:
            step_message_counts[sid] = cnt
        # J5：gate 步骤附加 has_decision_pkg（AI 是否已输出决策请求包）——
        # 总览页据此区分交互：选项类（去决策）vs 审批类（通过/拒绝）
        if s.get("human_attention") == "gate":
            s["has_decision_pkg"] = await _step_has_decision_pkg(storage, task_id, sid)

    # 2026-08-27：轮询瘦身——剔除步骤长文本字段（详情页全字段走 getStep）
    _strip_step_heavy_fields(steps)

    events = await storage.get_events(task_id, limit=10)

    return {
        "task": task,
        "artifacts": artifact_summary,
        "monitor_conversations": monitor_conversations,
        "step_messages": step_message_counts,
        "recent_events": events,
    }


@rest_app.delete("/api/task/{task_id}")
async def delete_task(task_id: str):
    """删除 Task（含关联数据）。

    SWP3-B1 接线（V-13）：若任务正在执行，orchestrator 先 abort + 等 running 退出
    （wait_stopped），再删 DB——执行循环与级联删除不并发。
    """
    orch = _get_orchestrator()
    if orch.is_running(task_id):
        await orch.abort(task_id)
        await orch.wait_stopped(task_id)
    storage = _get_storage()
    await storage.delete_task(task_id)
    return {"status": "deleted", "task_id": task_id}


@rest_app.post("/api/task/{task_id}/pause")
async def pause_task(task_id: str, payload: dict = None):
    """暂停任务（端点 30，H11 修复的唯一新增端点）。

    请求体可选 pause_level（默认 "gate"——M2 注明：不扩展 PauseLevel 枚举，
    "gate" 为字符串约定），读取但忽略，行为固定 gate 级暂停。
    响应 {status:"ok", task_id}。

    2026-08-20：任务级暂停 = 停止执行（用户直觉语义——原实现只置
    paused(gate) 不打断，当前步骤继续跑，总览页暂停"点了没反应"）。
    追加 abort 打断执行循环/当前 LLM 轮；active 真实步骤重置 pending
    （resume 后重跑；gate 步骤执行中被打断 → 重新生成决策包）。
    虚拟步骤（_monitor:*）不动——由 Monitor 编排管理，不占执行循环。
    """
    if payload:
        payload.get("pause_level", "gate")  # 可选参数，读取但忽略（固定 gate 语义）
    sm = _get_state_machine()
    orch = _get_orchestrator()
    storage = _get_storage()
    try:
        await sm.pause_task(task_id)
        await orch.abort(task_id)
        # 打断后 active 真实步骤（执行中/LLM 轮）→ pending，resume 续跑重跑。
        # 2026-08-21 修复：include_hidden=True——monitor/review/report 是实体行
        # （此前 get_task 不含 hidden，monitor 步骤暂停后保持 active，Monitor 页
        # 恢复按钮不显示）
        task = await storage.get_task(task_id, include_hidden=True)
        if task:
            for s in task.get("steps", []):
                if s.get("status") == "active":
                    await sm.advance_step(task_id, s["step_id"], "pending")
    except ValueError as e:
        raise _state_error(e)
    return {"status": "ok", "task_id": task_id}


# ═══════════════════════════════════════════════════════════════════
# 端点 31/32（SWP3-B1 跨包追加点，A1/J1）
# ═══════════════════════════════════════════════════════════════════


@rest_app.post("/api/task/{task_id}/start")
async def start_task(task_id: str):
    """启动 Task 执行（端点 31，第 7 轮 A1——V2「立即启动」通道）。

    语义：校验 task 存在且 status ∈ {active, paused}（第 8 轮 J3）→ 调
    brain.orchestrator.start_task（幂等，已 running 则直接返回 ok）→ 触发执行循环。
    响应 {status:"ok", task_id}。
    """
    storage = _get_storage()
    task = await storage.get_task(task_id)
    if not task:
        raise HTTPException(404, detail="任务不存在")
    if task.get("status") not in ("active", "paused"):
        raise HTTPException(
            400,
            detail=f"当前状态不允许此操作，请刷新查看最新状态 (task status={task.get('status')})",
        )
    orch = _get_orchestrator()
    await orch.start_task(task_id)
    return {"status": "ok", "task_id": task_id}


@rest_app.post("/api/task/{task_id}/best-effort")
async def set_best_effort(task_id: str, payload: dict):
    """切换任务尽力模式（Best-effort Mode，2026-08-16 用户需求）：开启后
    gate 审批自动走用户决策路径放行 + 步骤内防放弃提醒 + 收尾复核。
    payload: {enabled: bool}。响应 {status:"ok", best_effort}。"""
    storage = _get_storage()
    task = await storage.get_task(task_id)
    if not task:
        raise HTTPException(404, detail="任务不存在")
    enabled = 1 if payload.get("enabled") else 0
    await storage.update_task(task_id, {"best_effort": enabled})
    return {"status": "ok", "task_id": task_id, "best_effort": bool(enabled)}


@rest_app.post("/api/admin/graceful-restart")
async def graceful_restart(payload: dict):
    """优雅重启/关闭（用户定义的安全点——只挑「执行命令前、可重试但未开始」场景）。

    action: "restart"（默认，新进程接管后旧进程退出）| "shutdown"（排空后纯关闭）
           | ""（取消排空，恢复执行）
    语义：置 draining 标志 → run_cmd 不再启动新命令；执行循环将 active 步骤置回
    pending（可重试）后退出；正在执行的命令不打断（计数等待其自然完成）；
    命令计数归零后执行动作（后台任务 _graceful_exec）。
    """
    action = str(payload.get("action", "restart"))
    if action not in ("restart", "shutdown", ""):
        raise HTTPException(400, detail=f"action 仅支持 restart/shutdown/空串（取消），收到: {action!r}")
    graceful.request(action)
    if action:
        asyncio.create_task(_graceful_exec(action))
    msg = {
        "restart": "优雅重启已请求：等待当前命令完成后自动重启",
        "shutdown": "优雅关闭已请求：等待当前命令完成后关闭",
        "": "优雅重启已取消，任务恢复执行",
    }[action]
    return {"status": "ok", "action": action, "message": msg}


async def _graceful_exec(action: str) -> None:
    """排空完成后执行动作（后台任务）：restart → Popen 新进程 + 退出旧进程。"""
    await asyncio.sleep(0.5)  # 确保响应已写出
    try:
        await graceful.wait_idle()
        # 等待执行循环退出（draining 检查点：active → pending 后退出）
        for _ in range(120):
            if not _get_orchestrator()._running:
                break
            await asyncio.sleep(0.5)
        logger.info(f"[DC:graceful] drain complete — executing {action}")
        if action == "restart":
            backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            subprocess.Popen(
                [sys.executable, "-m", "dc_server.server"],
                cwd=backend_dir,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            await asyncio.sleep(1.0)  # 新进程启动窗口（绑定冲突按 server.py 重试兜底）
        # WAL 模式：已提交事务由 SQLite 崩溃恢复保证，os._exit 安全（不跑 cleanup）
        os._exit(0)
    except Exception:
        logger.exception("[DC:graceful] graceful exec failed")


@rest_app.get("/api/step/{step_id}")
async def get_step_detail(step_id: str, task_id: str, limit: int = 200, before_seq: int = -1):
    """StepDetail 数据源聚合端点（端点 32，第 8 轮 J1）。

    响应逐字段固化：{stepId, taskId, prep, conversation, messages, max_seq, total,
    truncated, step}
    prep = 端点 8 prepare 结果（幂等，入 _prepCache）；messages = 最近 limit 条
    （默认 200，分页加载——全量曾达 21MB 拖慢页面打开）；before_seq 往前翻页
    （seq < before_seq 的最近 limit 条）；max_seq = 最新 seq；total = 消息总数；
    truncated = 是否还有更早消息；step = task_steps 行。
    """
    storage = _get_storage()
    # 2026-08-22：include_hidden——monitor-init/monitor-N/review/report 为隐藏
    # 步骤，不含则查不到 → 404 → MonitorDetail 拉不到 prep（注入消息不展示）
    task = await storage.get_task(task_id, include_hidden=True)
    if not task:
        raise HTTPException(404, detail="任务不存在")
    step = next((s for s in task.get("steps", []) if s["step_id"] == step_id), None)
    if not step:
        raise HTTPException(404, detail="步骤不存在")

    orch = _get_orchestrator()
    prep = await orch.get_step_prep(task_id, step_id)
    total = await storage.count_step_messages(task_id, step_id)
    latest = await storage.get_step_messages(task_id, step_id, limit=1)
    max_seq = latest[0]["seq"] if latest else -1
    messages = await storage.get_step_messages(task_id, step_id, limit=limit, before_seq=before_seq)
    # truncated：是否还有比窗口首条更早的消息（seq 从 0 连续分配，首条 > 0 即前有历史）
    truncated = bool(messages and messages[0]["seq"] > 0)
    # 2026-08-27（详情页首屏进行中状态）：live 快照 = 思考累积/执行中工具/streaming
    # （进行中事件只存在于 SSE 流、不落库，快照保证首屏可渲染）
    return {
        "stepId": step_id,
        "taskId": task_id,
        "prep": prep,
        "conversation": messages,
        "messages": messages,
        "max_seq": max_seq,
        "total": total,
        "truncated": truncated,
        "step": step,
        "live": orch.get_step_live(task_id, step_id),
    }


# ═══════════════════════════════════════════════════════════════════
# 调度 & 步骤
# ═══════════════════════════════════════════════════════════════════


@rest_app.get("/api/task/{task_id}/next-step")
async def get_next_step(task_id: str):
    """获取下一个待执行的步骤。"""
    sm = _get_state_machine()
    steps = await sm.get_next_steps(task_id)
    if steps:
        s = steps[0]
        return {
            "step_id": s.get("step_id", ""),
            "title": s.get("title", ""),
            "model_tier": s.get("model_tier", "light"),
        }
    return {}


@rest_app.post("/api/step/prepare")
async def prepare_step(payload: dict):
    """
    准备步骤执行 prompt（前端调用 LLM 前使用）。

    返回（B5 字段补齐）:
        {system_message, system_prompt, step_context, temp_dir,
         model_tier, step_title, step_id}
    system_prompt = 纯规则提示词（executor.md 等，按步骤角色）；
    step_context = 任务背景 + 前序产物；system_message = 两者拼装后完整值。
    """
    task_id = payload["task_id"]
    step_id = payload["step_id"]

    # 审查/收尾步骤（实体化 2026-08-21）：type=monitor/review/report 返回
    # monitor_prep（提示词按类型/命名选 + 动态摘要）——原元步骤最小响应删除
    storage = _get_storage()
    task = await storage.get_task(task_id, include_hidden=True)
    step_row = next((s for s in task.get("steps", []) if s.get("step_id") == step_id), None) if task else None
    if step_row and (step_row.get("type") in ("monitor", "review", "report")
                     or step_id.startswith("monitor-") or step_id in ("review", "report")):
        orch = _get_orchestrator()
        prep = await orch._monitor_prep(task_id, step_row)
        return {
            "system_message": f"{prep['system_prompt']}\n\n{prep['step_context']}",
            "system_prompt": prep["system_prompt"],
            "step_context": prep["step_context"],
            "temp_dir": "",
            "model_tier": prep["model_tier"],
            "step_title": prep["step_title"],
            "step_id": step_id,
        }

    agent = _get_step_context()
    return await agent.prepare_step(task_id, step_id)


@rest_app.post("/api/step/submit")
async def submit_step_result(payload: dict):
    """提交步骤执行结果（幂等：已完成的不重复推进）。

    conversation 参数已废弃（旧代码读取但忽略）——step_messages 是唯一消息源。
    """
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    payload.get("conversation")  # 废弃参数，读取但忽略

    storage = _get_storage()
    sm = _get_state_machine()

    task = await storage.get_task(task_id)
    step = next((s for s in task.get("steps", []) if s["step_id"] == step_id), None) if task else None
    if step and step.get("status") != "completed":
        await sm.advance_step(task_id, step_id, "completed")

    # 2026-08-25（Hindsight 记忆模块 B-5）：步骤完成 → retain（异步，不阻塞提交；
    # 记忆库所有 LLM 调用统一 light tier）
    try:
        from .config import get_memory_config
        mem_cfg = get_memory_config()
        if mem_cfg.get("enabled"):
            ms = _get_memory_storage()
            if ms is not None and task:
                from .memory import get_retainer
                bank_id = ms.get_or_create_bank_for_project(PROJECT_ROOT)
                conv = await storage.get_conversation(task_id, step_id)
                artifacts = await storage.list_artifacts(task_id, step_id)
                retain_text = ms.build_retain_text(conv, artifacts, task, step_id)
                retainer = get_retainer(ms, _get_orchestrator()._make_llm_client("light"), mem_cfg)
                asyncio.create_task(retainer.retain(
                    bank_id=bank_id,
                    text=retain_text,
                    tags=[task.get("type", "general"), f"step:{step_id}"],
                    source_ref={"task_id": task_id, "step_id": step_id},
                    event_date=task.get("created_at"),
                ))
    except Exception:
        logger.warning(f"[DC:mem] retain skipped for {step_id} (disabled or error)", exc_info=True)

    # 2026-08-22：不再删除步骤临时目录（此前 T2.6 C2 步骤完成即删）——后续步骤
    # 需要读前序步骤 AI 产出的文件（DB 实证 a2a0d5df：step-3 读 step-2 产出失败）。
    # 步骤目录保留到任务结束，由 server 启动兜底清理回收（已完成任务目录重启即清）

    return {"status": "step_completed", "task_id": task_id, "step_id": step_id}


@rest_app.post("/api/step/save-conversation")
async def save_step_conversation(payload: dict):
    """全量保存对话记录（兼容旧协议；sqlite 实现下 save_conversation 为 no-op，
    step_messages 是唯一消息源）。"""
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    conversation = payload.get("conversation")

    if conversation:
        await _get_storage().save_conversation(task_id, step_id, conversation)

    return {"status": "ok", "task_id": task_id, "step_id": step_id}


@rest_app.post("/api/step/message/append")
async def append_step_message(payload: dict):
    """
    追加一条消息到 step_messages（步骤执行中实时调用）。

    message: {role, content, toolName?, input?, output?, round_num?,
              tool_call_id?, tool_calls?}
    tool_call_id: tool 消息的关联 id；tool_calls: assistant 消息的 OpenAI
    tool_calls JSON 数组文本（B1 方案②）。
    """
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    message = payload.get("message", {})

    seq = await _get_storage().append_message(task_id, step_id, message)
    return {"status": "ok", "task_id": task_id, "step_id": step_id, "seq": seq}


@rest_app.get("/api/step/{step_id}/messages")
async def get_step_messages(step_id: str, task_id: str, after_seq: int = -1):
    """
    增量查询步骤消息。
    GET /api/step/{step_id}/messages?task_id=X&after_seq=N
    """
    storage = _get_storage()
    messages = await storage.get_step_messages(task_id, step_id, after_seq)
    max_seq = messages[-1]["seq"] if messages else (after_seq if after_seq >= 0 else -1)
    return {
        "task_id": task_id,
        "step_id": step_id,
        "messages": messages,
        "max_seq": max_seq,
        "after_seq": after_seq,
    }


@rest_app.post("/api/step/chunk/save")
async def save_stream_chunk(payload: dict):
    """
    保存一个流式 chunk 到 stream_chunks（实时落盘）。
    chunk: {chunk_type: "text"|"tool_call_start"|"tool_call_result", content, call_id?}
    """
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    chunk = payload.get("chunk", {})

    seq = await _get_storage().save_chunk(task_id, step_id, chunk)
    return {"status": "ok", "seq": seq}


@rest_app.get("/api/step/{step_id}/chunks")
async def get_stream_chunks(step_id: str, task_id: str, after_seq: int = -1):
    """增量查询流式 chunk。GET /api/step/{step_id}/chunks?task_id=X&after_seq=N"""
    storage = _get_storage()
    chunks = await storage.get_chunks(task_id, step_id, after_seq)
    max_seq = chunks[-1]["seq"] if chunks else (after_seq if after_seq >= 0 else -1)
    return {
        "task_id": task_id,
        "step_id": step_id,
        "chunks": chunks,
        "max_seq": max_seq,
        "after_seq": after_seq,
    }


@rest_app.post("/api/step/messages/clear")
async def clear_step_messages(payload: dict):
    """清除步骤的消息和 chunk（重新执行前调用）。"""
    task_id = payload["task_id"]
    step_id = payload["step_id"]

    await _get_storage().clear_step_messages(task_id, step_id)
    return {"status": "ok"}


@rest_app.get("/api/artifact/{task_id}/{step_id}/intervention")
async def get_intervention_artifact(task_id: str, step_id: str):
    """读取步骤的 intervention 产物（恢复执行时前端读取并注入对话）。"""
    artifact = await _get_storage().get_artifact(task_id, step_id, "intervention")
    if artifact:
        return {"content": artifact["content"]}
    return {}


@rest_app.post("/api/step/messages/clear-intervention")
async def clear_intervention(payload: dict):
    """清除步骤的 intervention 产物（恢复消息已注入对话后调用）。"""
    task_id = payload["task_id"]
    step_id = payload["step_id"]

    try:
        await _get_storage().save_artifact(task_id, step_id, "intervention", "[]", "json")
    except Exception:
        pass
    return {"status": "ok"}


@rest_app.post("/api/artifact/save")
async def save_artifact(payload: dict):
    """保存 artifact（通用端点，由编排侧写回剩余的介入消息等）。"""
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    artifact_type = payload["artifact_type"]
    content = payload["content"]
    content_format = payload.get("content_format", "json")

    await _get_storage().save_artifact(task_id, step_id, artifact_type, content, content_format)
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
# 通用 Tool 执行端点（10 个工具，args 字段逐字固化，见 WP2-3 §3 工具表）
# ═══════════════════════════════════════════════════════════════════


_KILL_VERB_RES = (
    re.compile(r"\btaskkill\b", re.IGNORECASE),
    re.compile(r"\bkill\b", re.IGNORECASE),
    re.compile(r"\bstop-process\b", re.IGNORECASE),
    re.compile(r"\bremove-process\b", re.IGNORECASE),
)


def _is_kill_self_cmd(command: str, self_pid: int) -> bool:
    """检测命令是否试图结束 dc_server 自身进程（V-12 防误杀）。

    两类目标都拦截：
    1) 命令中出现服务自身 PID 且属杀进程类操作（taskkill /PID、Stop-Process -Id、
       wmic ... delete、kill 等）；
    2) 按 python 镜像名/通配符的清理（taskkill /IM python.exe、Stop-Process
       -Name python*、wmic name='python.exe' delete）——服务本身就是 python.exe
       进程（D:\\Python\\python.exe -m dc_server.server），按镜像名清理必杀服务。
       修订（V-16）：AI 曾先查 PID 再杀（1 已覆盖），后又改用镜像名绕过（2）。
    """
    kill_verb = (any(r.search(command) for r in _KILL_VERB_RES)
                 or (re.search(r"\bwmic\b", command, re.IGNORECASE)
                     and re.search(r"\b(?:delete|terminate)\b", command, re.IGNORECASE)))
    if not kill_verb:
        return False
    pid_str = str(self_pid)
    if pid_str in command:
        return True
    # 按 python 镜像名/通配符（taskkill /IM python.exe、Stop-Process -Name python*）
    if re.search(r"[/-]IM\s+python", command, re.IGNORECASE):
        return True
    if re.search(r"(?:-Name|name)\s*[=:]?\s*['\"]?python", command, re.IGNORECASE):
        return True
    # wmic 按 python 进程名删除
    if re.search(r"wmic\b", command, re.IGNORECASE) and re.search(r"python", command, re.IGNORECASE):
        return True
    return False


def _kill_self_block_message(self_pid: int) -> str:
    """拦截提示（V-16 增强：附事实核查 + 清理指导，让 AI 信服非误拦截、不再绕过）。"""
    try:
        from . import config as _cfg
        port = _cfg.PORT
    except Exception:
        port = 8501
    return (
        f"[Blocked] 命令会终止 dc_server 服务进程（自身），已拦截。事实核查（非误拦截）：\n"
        f"- 服务进程：python.exe（PID {self_pid}）\n"
        f"- 启动命令行：{sys.executable} -m dc_server.server（HTTP 监听 0.0.0.0:{port}）\n"
        f"- 服务本身就是 python.exe 进程：任何按『python 镜像名/通配符』的进程清理"
        f"（如 taskkill /IM python.exe、Stop-Process -Name python*、wmic name='python.exe' "
        f"delete、或脚本内 psutil/os.kill 按名称匹配）都会杀死服务本身 → 全部任务中断，"
        f"正在执行的命令与步骤全部丢失。\n"
        f"正确做法：如需清理残留进程，必须显式排除本服务——用 taskkill /F /PID 只杀目标 PID"
        f"（不要使用 /IM python），或在脚本中先按命令行过滤（排除含 dc_server.server 的进程）"
        f"再杀。"
    )


def _proc_alive(pid: int) -> bool:
    """探测进程是否存活（Windows）。os.kill(pid, 0) 的 sig=0 会被当作 CTRL_C_EVENT
    向进程组广播 Ctrl+C、中断同组所有进程（含调用者）——必须用 tasklist 查询。"""
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, timeout=10)
        return re.search(rf"\b{pid}\b", r.stdout.decode(errors="replace")) is not None
    except Exception:
        return False


# 2026-08-25（用户需求）：任务正在运行的命令进程注册表——run_cmd Popen 后
# 登记 {task_id: pids}，abort（立即中断/暂停）时 kill_task_cmds 整树清理
# （run_cmd 不响应 cancel_event，不杀则命令继续跑到 60s 超时）
_TASK_CMD_PIDS: dict[str, set[int]] = {}


async def kill_task_cmds(task_id: str) -> None:
    """杀该任务正在运行的命令进程树（与超时同路径 _kill_cmd_tree，
    taskkill /T /F 连孙进程），杀后清空注册表。"""
    pids = _TASK_CMD_PIDS.pop(task_id, set())
    for pid in pids:
        try:
            await asyncio.to_thread(_kill_cmd_tree, pid)
        except Exception:  # noqa: BLE001
            pass


def _kill_cmd_tree(pid: int) -> None:
    """终止命令进程树（Windows）。subprocess.run(timeout=) 超时只终止 shell 直接
    子进程（cmd.exe），命令派生的孙进程（python.exe 等）不会终止、继续后台运行——
    ① 根进程存活时 taskkill /T 整树（最多重试 3 次、每次间隔后验证存活）；
    ② 根已退出/未杀净时，WMI 全量快照一次（PID→PPID 映射），BFS 收集全部后代
    含孙进程，自底向上逐个 taskkill /T（子进程存活可递归覆盖其整树）。"""
    for _ in range(3):
        if not _proc_alive(pid):
            break
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        time.sleep(0.3)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "ForEach-Object { \"$($_.ProcessId),$($_.ParentProcessId)\" }"],
            capture_output=True, timeout=15)
        pairs: dict[int, int] = {}
        for ln in out.stdout.decode(errors="replace").splitlines():
            a, _, b = ln.strip().partition(",")
            if a.isdigit() and b.isdigit():
                pairs[int(a)] = int(b)
    except Exception:
        pairs = {}
    descendants: list[int] = []
    frontier = [pid]
    while frontier:
        nxt = [c for c, pp in pairs.items()
               if pp in frontier and c not in descendants and c != pid]
        if not nxt:
            break
        descendants.extend(nxt)
        frontier = nxt
    for child in reversed(descendants):  # 先杀最深的孙进程
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(child)],
                           capture_output=True, timeout=10)
        except Exception:
            pass


# ── 文本解码兜底（Windows 中文环境）──
# 双编码策略：UTF-8 严格优先，失败回退 GB18030（GBK 超集，覆盖四字节扩展字符）。
# 另处理「伪合法乱码」：GBK 字节流恰好构成合法 UTF-8 时 utf-8 解码“成功”但内容
# 错误（如 dir 输出“文件存储盘”→鏂囦欢瀛樺偍鐩�、路径“项目储存”→椤圭洰鍌ㄥ瓨）。
# 这种连 utf8.Valid 检测都发现不了，只能靠 mojibake 特征字评分。
_MOJIBAKE_CHARS = set(
    "鏂囦椤洰鍌瓨鐩偍樺锟"  # 取自 DB 实证样本的罕见字（正常中文几乎不出现）
)


def _looks_like_gbk_mojibake(text: str) -> bool:
    """utf-8 解码“成功”后判断是否实为 GBK 误解码产物：统计罕见 mojibake 特征字，
    命中 ≥2 个不同字 → 判定伪合法乱码（正常中文文本几乎不含这些字，误判率极低）。"""
    if not text:
        return False
    seen = 0
    for ch in set(text):
        if ch in _MOJIBAKE_CHARS:
            seen += 1
            if seen >= 2:
                return True
    return False


def _decode_bytes_auto(raw: bytes) -> str:
    """文本字节自动解码：UTF-8（含 BOM）严格优先 → 失败回退 GB18030(replace)；
    UTF-8 解码成功但命中 mojibake 特征 → 用 GB18030 重解码（消除伪合法乱码）。"""
    if not raw:
        return ""
    try:
        s = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")
    if _looks_like_gbk_mojibake(s):
        try:
            return raw.decode("gb18030")
        except UnicodeDecodeError:
            return raw.decode("gb18030", errors="replace")
    return s


_DOC_DIRS = [os.path.join(".github", "docs"),
             os.path.join(".github", "instructions"),
             "docs"]


def tool_resolve(root: str, rel_or_abs: str) -> str:
    """工具路径解析（2026-08-24 任务隔离）：.dc_tmp 前缀（相对）或绝对路径含
    /.dc_tmp/ 的路径按 workspace 根解析（.dc_tmp 在 workspace 根，任务根外）；
    其余按 root（任务根/workspace 根）。无 task_id 时 root 即 PROJECT_ROOT，行为不变。"""
    p = rel_or_abs.replace("\\", "/")
    if (not os.path.isabs(rel_or_abs) and p.startswith(".dc_tmp/")) \
            or (os.path.isabs(rel_or_abs) and "/.dc_tmp/" in p):
        return safe_resolve(PROJECT_ROOT, rel_or_abs)
    return safe_resolve(root, rel_or_abs)


def _list_docs(root: str) -> list:
    """read_doc 可用文档清单（P9）：三个文档目录下全部文件名（含相对路径）。"""
    avail = []
    for rel in _DOC_DIRS:
        try:
            d = tool_resolve(root, rel)
        except ValueError:
            continue
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if os.path.isfile(os.path.join(d, f)):
                    avail.append(f"{rel}/{f}")
    return avail


def _timeout_result(timeout: int, out_buf: list, err_buf: list, clean: bool) -> str:
    """run_cmd 超时结果（P4）：已捕获输出 stdout+stderr 尾部 4000 字符。"""
    raw = b"".join(out_buf) + b"".join(err_buf)
    if raw:
        try:
            s = raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            s = raw.decode("gb18030", errors="replace")
        tail = s[-4000:]
    else:
        tail = "(无已捕获输出)"
    note = "" if clean else " (进程未清理干净)"
    return f"[Error] timeout after {timeout}s{note}\n已捕获输出尾部:\n{tail}"


# 2026-08-27（用户反馈：工具执行卡死 + FireFox 无法连接 127.0.0.1:8501）：文件类
# 工具为纯同步 IO——直接在 async invoke_tool 内执行会阻塞 asyncio 事件循环
# （search_code 全目录递归最严重，实测 workspace 1.8 万文件 + node_modules/.git 等
# 巨型目录，阻塞数秒~数十秒，期间所有 HTTP/SSE 请求排队）→ 全部提取为同步实现，
# 经 asyncio.to_thread 线程池执行。常量放模块级：测试可 monkeypatch 验证扫描上限。
_SEARCH_SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".venv",
                     "__pycache__", ".pytest_cache", ".dc_tmp", "bin", "obj", "out"}
_SEARCH_MAX_FILES = 20000


def _impl_list_dir(root: str, args: dict) -> dict:
    """dcflow_list_dir 同步实现（线程池执行，防事件循环阻塞）。"""
    # T2.2: 路径解析走 safe_resolve（越界返回 [Security]）
    try:
        full = tool_resolve(root, args.get("dir_path", "."))
    except ValueError:
        return {"result": "[Security] path outside project root"}
    if os.path.isdir(full):
        entries = os.listdir(full)
        lines = [f"{'[DIR] ' if os.path.isdir(os.path.join(full, e)) else '[FILE] '}{e}" for e in sorted(entries)]
        return {"result": "\n".join(lines) or "(空目录)"}
    return {"result": f"(目录不存在: {args.get('dir_path', '.')})"}


def _impl_read_file(root: str, args: dict) -> dict:
    """dcflow_read_file 同步实现（线程池执行，防大文件读取阻塞事件循环）。"""
    fp = args["file_path"]
    # 规则库只读白名单：prompts/rules 位于 PROJECT_ROOT 外（safe_resolve 会
    # 拦截），但审查/执行步骤需按需读取规则文件（code-reviewer Round 1 必读）
    # ——仅 read 放行规则库目录，write/edit 仍走 safe_resolve 拦截（防篡改提示词）
    _rules_dir = rules_dir()
    _abs_fp = os.path.realpath(fp if os.path.isabs(fp) else os.path.join(root, fp))
    if os.path.normcase(_abs_fp).startswith(
            os.path.normcase(os.path.realpath(_rules_dir) + os.sep)):
        full = _abs_fp
    else:
        # T2.2: 一律 safe_resolve——相对 root 解析，绝对路径也必须在 root 内（V3）
        try:
            full = tool_resolve(root, fp)
        except ValueError:
            return {"result": "[Security] path outside project root"}
    if not os.path.isfile(full):
        return {"result": f"(文件不存在: {fp})"}
    with open(full, "rb") as f:
        raw = f.read()
    # 100% 二进制判定（raw 字节层，非启发式，DB 审计实证）：1) BOM 特判
    # UTF-16/32 文本；2) NUL 字节存在 → 必是二进制（单字节文本编码
    # ASCII/UTF-8/GBK 不含 NUL）；3) utf-8/gb18030 strict 均解码失败 →
    # 必是二进制（任何文本必可 strict 解码）。两者皆不成立 = 可无损解码的
    # 文本。替代原 _decode_bytes_auto（errors='replace' 永远成功，检测不出
    # 二进制——AI 反复读 .pyc/.exe dump 空转的根因）。
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif raw.startswith(b"\x00\x00\xfe\xff") or raw.startswith(b"\xff\xfe\x00\x00"):
        text = raw.decode("utf-32")
    else:
        if b"\x00" in raw:
            return {"result": (f"(二进制文件: {fp}。请改用 dcflow_run_cmd + python "
                                f"处理（读头部/hexdump/提取），不要用 read_file 读二进制)")}
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gb18030")
            except UnicodeDecodeError:
                return {"result": (f"(二进制文件: {fp}。请改用 dcflow_run_cmd + python "
                                    f"处理（读头部/hexdump/提取），不要用 read_file 读二进制)")}
        else:
            # 伪合法 mojibake：GBK 字节恰好构成合法 UTF-8 → 特征检测后 gb18030 重解
            if _looks_like_gbk_mojibake(text):
                # P2（2026-08-20）：特征误判/边缘字节 → gb18030 严格解码
                # 抛裸 codec 错误（DB 实证 step-4 现场 500）→ 回退 replace
                try:
                    text = raw.decode("gb18030")
                except UnicodeDecodeError:
                    text = raw.decode("gb18030", errors="replace")
    # 对齐旧 open(..., newline=None) 的 universal newlines 语义：Windows 下
    # 文本写文件会 \n→\r\n 落盘，读回需归一化回 \n，否则行尾/行数断言漂移
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    total = len(lines)
    try:
        start_line = int(args.get("start_line") or 1)
        end_line = int(args.get("end_line") or total)
    except (TypeError, ValueError):
        return {"result": f"(行号无效: start_line={args.get('start_line')!r}, end_line={args.get('end_line')!r})"}
    start_line = max(1, start_line)
    end_line = min(total, end_line)
    if start_line > end_line or start_line > total:
        return {"result": f"(行范围无效: 文件共 {total} 行, 请求 L{start_line}-L{end_line})"}
    content = "".join(lines[start_line - 1:end_line])
    if len(content) > 30000:
        # 超长提示：不返回内容，引导 AI 缩小行数范围重试（单次上限 30000 字符）
        return {"result": f"读取文本超过限制，当前{len(content)}字符，限制30000字符，请减少行数来读取。"}
    return {"result": f"[L{start_line}-L{end_line}] {content}"}


def _impl_write_file(root: str, args: dict) -> dict:
    """dcflow_write_file 同步实现（线程池执行）。"""
    fp = args["file_path"]
    # T2.2 步骤 4: write/edit 绝对路径分支不再允许，一律 safe_resolve
    try:
        full = tool_resolve(root, fp)
    except ValueError:
        return {"result": "[Security] path outside project root"}
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(args["content"])
    return {"result": f"✓ 已写入 {fp} ({len(args['content'].encode('utf-8'))} bytes)"}


def _impl_edit_file(root: str, args: dict) -> dict:
    """dcflow_edit_file 同步实现（线程池执行）。"""
    fp = args["file_path"]
    # T2.2 步骤 4: 绝对路径分支不再允许，一律 safe_resolve
    try:
        full = tool_resolve(root, fp)
    except ValueError:
        return {"result": "[Security] path outside project root"}
    if not os.path.isfile(full):
        return {"result": f"(文件不存在: {fp})"}
    old = args.get("old_string") or ""
    new = args.get("new_string") or ""
    if not old:
        return {"result": "[Error] old_string 不能为空（edit 需要精确匹配旧文本）"}
    with open(full, "r", encoding="utf-8") as f:
        text = f.read()
    # 可靠性修复（DB 审计实证）：旧实现 replace_all=False 时 count 硬编码 1，
    # old 未匹配也写回原文件并报「已替换 1 处」——AI 误以为已修改（假成功）。
    # 现改为写前真实计数：未匹配 / 空 old / new==old 一律不写文件并明确报错。
    count = text.count(old)
    if count == 0:
        return {"result": f"(未找到匹配文本: {old[:80]}...)"}
    if new == old:
        return {"result": "(new_string 与 old_string 相同，未做任何修改)"}
    replace_all = bool(args.get("replace_all"))
    occ = args.get("occurrence")
    if replace_all:
        replaced, done = text.replace(old, new), count
    else:
        if occ is None:
            occ = 1
            hint = (f"（该文本共 {count} 处，已替换第 1 处；"
                    f"如需其他位置请传 occurrence）" if count > 1 else "")
        else:
            try:
                occ = int(occ)
            except (TypeError, ValueError):
                return {"result": f"[Error] occurrence 必须是正整数: {occ!r}"}
            if occ < 1 or occ > count:
                return {"result": f"(第 {occ} 处匹配不存在：共 {count} 处，occurrence 范围 1-{count})"}
            hint = ""
        # 定位第 occ 处：split 法，只替换目标处
        parts = text.split(old)
        replaced = old.join(parts[:occ]) + new + old.join(parts[occ:])
        done = 1
    with open(full, "w", encoding="utf-8") as f:
        f.write(replaced)
    # 写后回读验证（防写盘异常静默失败）
    with open(full, "r", encoding="utf-8") as f:
        if new not in f.read():
            return {"result": "[Error] 写入后回读未发现新内容，请重试"}
    if replace_all:
        return {"result": f"✓ 已替换 {done} 处 ({fp})"}
    return {"result": f"✓ 已替换第 {occ} 处 ({fp}){hint}"}


def _impl_read_doc(root: str, args: dict) -> dict:
    """dcflow_read_doc 同步实现（线程池执行）。"""
    fn = args.get("filename") or ""
    # P9（2026-08-20）：list 模式返回全部可用文档；目录不存在明示"未配置"
    if fn == "list":
        avail = _list_docs(root)
        return {"result": "\n".join(avail)
                or "(未配置任何文档目录: .github/docs、.github/instructions、docs 均不存在)"}
    if not fn:
        return {"result": "[Error] filename 必填（可用 filename=\"list\" 查看全部文档）"}
    # T2.2: 三个目录也走 safe_resolve（filename 含 ../ 时越界拒绝）
    for rel in [os.path.join(".github", "docs"),
                os.path.join(".github", "instructions"),
                "docs"]:
        try:
            fp = tool_resolve(root, os.path.join(rel, fn))
        except ValueError:
            return {"result": "[Security] path outside project root"}
        if os.path.isfile(fp):
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                return {"result": f.read()[:30000]}
    avail = _list_docs(root)
    hint = ("；可用文档:\n" + "\n".join(avail)) if avail else "（未配置任何文档目录）"
    return {"result": f"(文档不存在: {fn}){hint}"}


def _impl_search_code(root: str, args: dict) -> dict:
    """dcflow_search_code 同步实现（线程池执行 + 扫描防御）。
    2026-08-27：跳过巨型依赖/产物目录 + 扫描文件数上限——防全项目递归
    （默认 path_filter="."）扫入海量文件拖垮服务。"""
    pat = args["pattern"]
    # 非法正则（re.error）捕获返回 [Error] 正则无效 + e.pos 定位（P11）
    try:
        re.compile(pat)
    except re.error as e:
        pos = getattr(e, "pos", None)
        loc = ""
        if pos is not None:
            loc = (f"\n非法位置: ...{pat[max(0, pos - 20):pos + 20]}...\n"
                   + " " * min(20, 40) + "^")
        return {"result": f"[Error] 正则无效: {pat[:80]}{loc}"}
    # T2.2: 搜索根路径走 safe_resolve（path_filter 越界拒绝）
    try:
        d = tool_resolve(root, args.get("path_filter", "."))
    except ValueError:
        return {"result": "[Security] path outside project root"}
    matches = []
    scanned = 0
    truncated = False
    for dp, dirs, fns in os.walk(d):
        dirs[:] = [x for x in dirs if x not in _SEARCH_SKIP_DIRS]
        for fn in fns:
            scanned += 1
            if scanned > _SEARCH_MAX_FILES:
                truncated = True
                break
            fp = os.path.join(dp, fn)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for li, line in enumerate(f, 1):
                        if re.search(pat, line):
                            matches.append(f"{fp}:{li}: {line.rstrip()[:200]}")
                            if len(matches) >= 30:
                                break
            except Exception:
                pass
            if len(matches) >= 30 or truncated:
                break
        if len(matches) >= 30 or truncated:
            break
    result = "\n".join(matches) or "未找到匹配"
    if truncated:
        result += f"\n(扫描超过 {_SEARCH_MAX_FILES} 文件已截断)"
    return {"result": result}


@rest_app.post("/api/tool/invoke")
async def invoke_tool(payload: dict):
    """执行 AI 发起的 tool call。返回 {"result": str}（工具错误也包在 result 内）。"""
    name = payload["name"]
    args = payload.get("args", {})
    storage = _get_storage()
    sm = _get_state_machine()
    # 2026-08-24（任务隔离）：工具路径基准——带 task_id 且任务根存在 → 任务根
    # （workspace/<task_id>/，相对路径落任务根）；否则 workspace 根（兼容旧调用/旧任务）
    # 2026-08-26：自定义工作目录任务（workspace_dir）→ 基准 = 自定义目录
    task_id = payload.get("task_id")
    if task_id:
        root = await get_task_workspace(_get_storage(), task_id)
    else:
        root = PROJECT_ROOT  # 配置化：PROJECT_ROOT（T2.1 步骤 3）

    try:
        if name == "dcflow_list_dir":
            return await asyncio.to_thread(_impl_list_dir, root, args)

        elif name == "dcflow_read_file":
            return await asyncio.to_thread(_impl_read_file, root, args)

        elif name == "dcflow_write_file":
            return await asyncio.to_thread(_impl_write_file, root, args)

        elif name == "dcflow_edit_file":
            return await asyncio.to_thread(_impl_edit_file, root, args)

        elif name == "dcflow_read_doc":
            return await asyncio.to_thread(_impl_read_doc, root, args)

        elif name == "dcflow_search_code":
            return await asyncio.to_thread(_impl_search_code, root, args)

        elif name == "dcflow_run_cmd":
            # 优雅重启检查点（用户定义的安全点）：draining 期间不启动新命令、
            # 不给 AI 返回任何文案——直接中断（抛 _StepGracefulDrain，由执行
            # 引擎捕获：步骤置回 pending，重启后自动恢复）；正在执行的命令
            # 不打断（计数等待其自然完成）
            if graceful.is_draining():
                raise _StepGracefulDrain("run_cmd")
            # T2.2 步骤 3: 不做命令白名单/黑名单（用户拍板），维持同步实现语义；
            # V-06 修订: 调用侧异步化——经 asyncio.to_thread 执行，防单事件循环阻塞
            # 60s；TimeoutExpired 捕获返回 [Error] timeout after Ns。
            # V-07 修订: 同步模式超时后按进程树清理——Windows 下 run 超时只杀
            # shell 直接子进程，命令派生的孙进程不会终止（_kill_cmd_tree 兜底）。
            # V-11 修订: 取消 background 后台模式（用户要求）——一律同步执行，
            # 长任务用 timeout_seconds 控制，超时整树清理。
            # 编码：text=True 会用系统默认编码（Windows 中文环境 = GBK）解码子进程
            # 输出，UTF-8 输出触发 UnicodeDecodeError → 线程异常、stdout=None。
            # 改为 bytes 捕获 + 双编码兜底（UTF-8 优先，失败回退 GBK），不崩溃。
            timeout = int(args.get("timeout_seconds", 60))
            command = str(args.get("command", ""))
            # V-12：防误杀自身——命令试图结束 dc_server 自身进程时拦截并提示 AI
            # （taskkill/kill/Stop-Process/wmic delete 且目标 PID 为当前进程）
            if _is_kill_self_cmd(command, os.getpid()):
                return {"result": _kill_self_block_message(os.getpid())}
            # V-07 修订: subprocess.run 超时抛的 TimeoutExpired 不带 pid（Python 3.9
            # 无 pid 属性），无法定位进程树——改 Popen + wait(timeout=)，超时后拿
            # p.pid 走 _kill_cmd_tree 整树清理。注意不能用 communicate(timeout=)：
            # 其超时依赖 reader 线程 join，在 asyncio.to_thread 的 executor 线程
            # 里不生效（实测等满命令完成才返回），wait(timeout=) 是纯 WinAPI
            # 等待，线程无关；进程退出后再 communicate 收输出（无超时参数）。
            p = subprocess.Popen(command, shell=True, cwd=root,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            graceful.add_cmd()  # 优雅重启计数：命令执行期间 +1（排空等待它自然完成）
            # 2026-08-25（用户需求）：登记运行命令 pid——abort（立即中断/暂停）时
            # kill_task_cmds 整树清理（taskkill /T 连孙进程，与超时同路径）
            _TASK_CMD_PIDS.setdefault(task_id, set()).add(p.pid)
            # V-13: 边读边等（DB 审计实证）——wait 等待期间不消费管道时，子进程
            # 大输出（>Windows 匿名管道缓冲 ~64KB）会阻塞在 write 上永不退出 →
            # 必超时（kctf 任务中 python 脚本打印反汇编/解压大结果反复 60s 超时、
            # 884 轮空转的根因）。pump 线程持续读管道；超时杀树后管道 EOF，
            # pump 自然退出；正常路径进程退出后管道 EOF，join 兑底。
            out_buf, err_buf = [], []

            def _pump(stream, buf):
                try:
                    while True:
                        chunk = stream.read(65536)
                        if not chunk:
                            break
                        buf.append(chunk)
                except Exception:
                    pass

            t1 = threading.Thread(target=_pump, args=(p.stdout, out_buf), daemon=True)
            t2 = threading.Thread(target=_pump, args=(p.stderr, err_buf), daemon=True)
            t1.start()
            t2.start()
            try:
                await asyncio.to_thread(p.wait, timeout)
            except subprocess.TimeoutExpired:
                # V-08 修订: _kill_cmd_tree 走 to_thread——taskkill/WMI 最长可达数十秒，
                # 同步调用会阻塞整个事件循环（其他请求/SSE 全卡）；且进程树清理失败
                # 时残留进程持有管道写端，等管道 EOF 会永久阻塞（挂起根因：工具调用
                # 永不返回、步骤卡死、只能重启）——进程未退出则放弃收集输出直接返回。
                # P4（2026-08-20）：超时返回已捕获输出尾部（此前直接丢弃——DB 实证
                # 40 条 timeout 输出恒 25 字符，AI 无法判断执行进展/卡在哪一步）
                await asyncio.to_thread(_kill_cmd_tree, p.pid)
                try:
                    await asyncio.to_thread(p.wait, 5)
                except subprocess.TimeoutExpired:
                    p.kill()  # 进程树清理失败的最后手段（只杀 shell 直接子进程）
                    try:
                        await asyncio.to_thread(p.wait, 5)
                    except subprocess.TimeoutExpired:
                        return {"result": _timeout_result(timeout, out_buf, err_buf, clean=False)}
                return {"result": _timeout_result(timeout, out_buf, err_buf, clean=True)}
            finally:
                graceful.done_cmd()
                # 2026-08-25：命令结束（正常/超时/被杀）统一注销——杀后 wait 返回也走这里
                _TASK_CMD_PIDS.get(task_id, set()).discard(p.pid)
            t1.join(2)  # 进程已退出 → 管道 EOF → pump 线程已结束；join 兑底
            t2.join(2)
            out = b"".join(out_buf)
            err = b"".join(err_buf)

            def _dec(b):
                # 双编码兜底（V-11）+ 伪合法 mojibake 重解码：GBK 字节恰好构成
                # 合法 UTF-8 时 utf-8 解码成功但内容乱码 → 特征检测后 gb18030 重解
                return _decode_bytes_auto(b)

            out = _dec(out) + ("\n" + _dec(err) if err else "")
            # 退出码标注（审计实证）：命令失败（非 0 退出）时输出加 [exit N] 前缀，
            # 避免 AI 把报错输出误判为执行成功
            prefix = "" if p.returncode == 0 else f"[exit {p.returncode}] "
            return {"result": prefix + (out[:10000] or "(无输出)")}

        elif name == "dcflow_step_done":
            # summary 存 artifact（artifact_type="summary"、content_format="text"，M9 修订）
            if args.get("summary"):
                await storage.save_artifact(args["task_id"], args["step_id"], "summary", args["summary"], "text")
            return {"result": f"✓ 步骤 {args.get('step_id', '')} 确认完成"}

        elif name == "dcflow_list_steps":
            tid = args.get("task_id")
            if not tid:
                return {"result": "[Error] 缺少 task_id 参数（dcflow_list_steps 必须携带 task_id）"}
            task = await storage.get_task(tid)
            if not task:
                return {"result": "Task not found"}
            # 审查/收尾步骤工具级不可见（实体化：type=monitor/review/report）——
            # 系统自动执行，不向 AI 暴露；2026-08-22 字段扩展：补 type/
            # human_attention/required/model_tier/sort_order/parallel_with（与
            # GET /api/task 契约一致）——否则 Monitor 无法区分 gate/plan/
            # code_review 类型与审批属性（用户实证：看不到步骤种类无法决策）
            steps = [{"step_id": s.get("step_id"), "title": s.get("title"),
                      "status": s.get("status"), "type": s.get("type", "executor"),
                      "human_attention": s.get("human_attention", "none"),
                      "required": s.get("required", 1),
                      "model_tier": s.get("model_tier", "light"),
                      "sort_order": s.get("sort_order", 0),
                      "parallel_with": s.get("parallel_with")}
                     for s in task.get("steps", []) if not is_hidden_step(s)]
            return {"result": json.dumps(steps, ensure_ascii=False, indent=2)}

        elif name == "dcflow_adjust_flow":
            # ── 参数归一化辅助函数（step_ids/steps_json/order_json 兼容 str/list）──
            def _norm_ids(val):
                if isinstance(val, str):
                    s = val.strip()
                    # 2026-08-24（DB 实证 99248a9f）：AI 工具参数传 JSON 数组字符串
                    # （"[\"step-4\", \"step-5\"]"）→ 先 json 解析，失败再按逗号分割
                    if s.startswith("["):
                        try:
                            p = json.loads(s)
                            if isinstance(p, list):
                                return [str(x).strip() for x in p if str(x).strip()]
                        except Exception:
                            pass
                    return [x.strip() for x in s.split(",") if x.strip()]
                if isinstance(val, (list, tuple)):
                    return [str(s) for s in val if str(s).strip()]
                return []

            def _norm_steps(val) -> tuple[list, str | None]:
                """归一化 steps_json → (steps, error)。返回 (None, 错误信息) 表示解析失败：
                不再静默吞错——假成功（空步骤仍返回 done）会让 AI 空转且步骤永久丢失
                （DB 实证 2026-08-17：kctf5 初始编排 18 次 add_steps 全部解析失败）。"""
                if val is None or (isinstance(val, str) and not val.strip()):
                    return [], None
                if isinstance(val, str):
                    try:
                        p = json.loads(val)
                    except Exception as e:
                        return None, f"JSON 解析失败: {e}"
                    if isinstance(p, list):
                        return [dict(s) for s in p if isinstance(s, dict)], None
                    if isinstance(p, dict):
                        return [dict(p)], None
                    return None, "解析结果必须是对象数组"
                if isinstance(val, list):
                    return [dict(s) for s in val if isinstance(s, dict)], None
                if isinstance(val, dict):
                    return [dict(val)], None
                return None, f"类型不支持: {type(val).__name__}"

            def _norm_order(val):
                if isinstance(val, str):
                    if not val.strip():
                        return []
                    try:
                        p = json.loads(val)
                        return [str(s) for s in p] if isinstance(p, list) else [str(p)]
                    except Exception:
                        return []
                if isinstance(val, (list, tuple)):
                    return [str(s) for s in val]
                return []

            action = args.get("action", "no_change")
            tid = args.get("task_id")
            if not tid:
                return {"result": "[Error] 缺少 task_id 参数（dcflow_adjust_flow 必须携带 task_id）"}
            ids = _norm_ids(args.get("step_ids", ""))
            new_steps, steps_error = _norm_steps(args.get("steps_json"))
            new_order = _norm_order(args.get("order_json", ""))
            reasoning = args.get("reasoning", "")

            # ── 审查/收尾步骤工具级防护（实体化 2026-08-21：type=monitor/review/report
            # 或 id 匹配 monitor-*/review/report——系统保留，不可观测/操作/创建）──
            # skip/remove/reorder 的 step_ids 或 add_steps 的 step_id 命中 → 拒绝
            hidden_hits = [s for s in ids + new_order if is_hidden_step({"step_id": s})]
            hidden_creates = [s.get("step_id") for s in (new_steps or [])
                              if isinstance(s, dict) and is_hidden_step(s)]
            if hidden_hits or hidden_creates:
                return {"result": (
                    "[Blocked] 审查/收尾步骤（monitor/review/report）由系统自动插入执行，"
                    "不可通过编排工具操作或创建。")}


            if action in ("no_change", ""):
                logger.info(f"[Monitor:REST] no_change for task {tid}: {reasoning[:100]}")
                await storage.append_event(tid, {
                    "event_type": "orchestration", "actor": "ai",
                    "content": {"action": "no_change", "reasoning": reasoning},
                })
            elif action == "skip_steps":
                logger.info(f"[Monitor:REST] skip_steps for task {tid}: ids={ids}")
                # 2026-08-24（DB 实证 99248a9f）：防假成功——解析后的 id 必须真实存在，
                # 否则静默 done 会让 AI 误以为已跳过，步骤随后照常被拾取执行
                # （此前 JSON 字符串 step_ids 被拆成带引号假 id，UPDATE 0 行）
                task = await storage.get_task(tid)
                exist_ids = {s["step_id"] for s in (task or {}).get("steps", [])}
                missing = [sid for sid in ids if sid not in exist_ids]
                if missing:
                    return {"result": (
                        "[Error] 步骤不存在，无法跳过: " + ", ".join(missing) +
                        "。请用 dcflow_list_steps 查看当前有效 step_id（JSON 数组字符串"
                        "与逗号分隔均支持）")}
                for sid in ids:
                    await storage.update_step_status(tid, sid, "skipped")
                await storage.append_event(tid, {
                    "event_type": "orchestration", "actor": "ai",
                    "content": {"action": "skip_steps", "step_ids": ids, "reasoning": reasoning},
                })
            elif action == "add_steps":
                logger.info(f"[Monitor:REST] add_steps for task {tid}: count={len(new_steps) if new_steps else 0}")
                # 防假成功（DB 实证 2026-08-17）：steps_json 解析失败/为空必须报错返回，
                # 让 AI 收到错误后修正重试；静默返回 done 会导致步骤永久丢失 + AI 空转
                if new_steps is None:
                    return {"result": (
                        "[Error] steps_json 解析失败: " + (steps_error or "未知错误") +
                        "。请用 JSON 对象数组提供步骤，如 [{\"step_id\": \"step-1\", "
                        "\"title\": \"...\", \"description\": \"...\", \"model_tier\": \"power\"}]"
                        "（禁止 JSON 字符串形式，内层转义不可靠）")}
                if not new_steps:
                    return {"result": "[Error] add_steps 未携带任何步骤：steps_json 必须是非空数组"}
                # 2026-08-20：after_step_id 支持——新步骤插入到指定步骤之后
                # （rebuild/续做场景，Monitor 上下文已引导）；审查/收尾步骤不可作锚点
                # 2026-08-24（用户反馈：AI 调用后无法控制插入位置，需二次 reorder）：
                # steps_json 元素可内嵌 after_step_id（per-step 锚点，优先）——
                # 逐步骤单独插入不同位置；无内嵌锚点的按顶层/默认锚点整批插入；
                # 返回插入位置反馈（AI 即时确认，减少 list_steps/reorder 二次调用）
                top_after = args.get("after_step_id") or None
                anchored_steps = [s for s in new_steps
                                  if isinstance(s, dict) and s.get("after_step_id")]
                plain_steps = [s for s in new_steps
                               if not (isinstance(s, dict) and s.get("after_step_id"))]
                insert_parts: list[str] = []
                if anchored_steps:
                    for s in anchored_steps:
                        anchor_id = s.pop("after_step_id")
                        if is_hidden_step({"step_id": anchor_id}):
                            return {"result": ("[Error] after_step_id 不能是审查/收尾"
                                                "步骤（monitor/review/report）")}
                        await storage.add_steps(tid, [s], anchor_id)
                        insert_parts.append(f"{s.get('step_id')} → {anchor_id} 之后")
                if plain_steps:
                    if top_after and is_hidden_step({"step_id": top_after}):
                        return {"result": ("[Error] after_step_id 不能是审查/收尾"
                                            "步骤（monitor/review/report）")}
                    # 实体化（2026-08-21）：默认插入位置在 review/report 之前——
                    # 末尾真实步骤之后（AI 追加步骤不会落到 report 之后）
                    if not top_after:
                        task_now = await storage.get_task(tid, include_hidden=True)
                        if task_now:
                            anchor = None
                            for s in sorted(task_now.get("steps", []),
                                            key=lambda x: x.get("sort_order", 0)):
                                if s.get("step_id") in ("review", "report"):
                                    break
                                if not is_hidden_step(s):
                                    anchor = s.get("step_id")
                            if anchor:
                                top_after = anchor
                    await storage.add_steps(tid, plain_steps, top_after)
                    plain_ids = ",".join(str(s.get("step_id")) for s in plain_steps)
                    insert_parts.append(f"{plain_ids} → "
                                        f"{top_after + ' 之后' if top_after else '末尾真实步骤之后'}")
                await storage.append_event(tid, {
                    "event_type": "orchestration", "actor": "ai",
                    "content": {"action": "add_steps", "steps": new_steps,
                                 "reasoning": reasoning, "after_step_id": top_after},
                })
                # 2026-08-24：返回插入位置反馈（AI 即时确认，减少二次 list/reorder）
                return {"result": ("action=add_steps done。插入位置："
                                    + "；".join(insert_parts)
                                    if insert_parts else "action=add_steps done")}
            elif action == "remove_steps":
                logger.info(f"[Monitor:REST] remove_steps for task {tid}: ids={ids}")
                if ids:
                    await storage.remove_steps(tid, ids)
                await storage.append_event(tid, {
                    "event_type": "orchestration", "actor": "ai",
                    "content": {"action": "remove_steps", "step_ids": ids, "reasoning": reasoning},
                })
            elif action == "reorder_steps":
                logger.info(f"[Monitor:REST] reorder_steps for task {tid}: order={new_order}")
                if new_order:
                    await storage.reorder_steps(tid, new_order)
                await storage.append_event(tid, {
                    "event_type": "orchestration", "actor": "ai",
                    "content": {"action": "reorder_steps", "order": new_order, "reasoning": reasoning},
                })
            elif action == "mark_complete":
                # 2026-08-21（评审确认）：mark_complete 只能由最终审查（review
                # 执行中）调用——任务级收尾是 final-reviewer 的职责；MIV/MS/MI
                # 不得直接结束任务（防 Monitor 误收尾触发补尾兜底重跑）
                task_cur = await storage.get_task(tid, include_hidden=True)
                review = next((s for s in (task_cur or {}).get("steps", [])
                               if s.get("step_id") == "review"), None)
                if not (review and review.get("status") == "active"):
                    return {"result": "[Error] mark_complete 只能由最终审查（review）执行"}
                logger.info(f"[Monitor:REST] mark_complete for task {tid}")
                await sm.complete_task(tid)
            return {"result": f"action={action} done"}

        else:
            return {"result": f"(工具 {name} 暂不支持)"}

    except _StepGracefulDrain:
        # 优雅重启排空：放行给执行引擎（步骤置 pending 后退出循环），
        # 不包装成错误文案返回给 AI（用户要求：直接停住，不拒绝文案）
        raise
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return {"result": f"[Error] {e}"}


# ═══════════════════════════════════════════════════════════════════
# 步骤状态推进（含 Gate 审批）
# ═══════════════════════════════════════════════════════════════════


@rest_app.post("/api/step/advance")
async def advance_step(payload: dict):
    """推进步骤状态。含 Gate 审批（传入 decision 参数时走 gate 分支）。

    decision 仅支持 "approved" | "rejected"（P0-4 修订：无 changes_requested，
    传其他值归 400，不再 500）。校验失败 → 400/404 中文文案（J5）。
    """
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    sm = _get_state_machine()

    decision = payload.get("decision")
    if decision is not None:
        if decision not in ("approved", "rejected"):
            raise HTTPException(
                400,
                detail=f"当前状态不允许此操作，请刷新查看最新状态 (invalid gate decision: {decision})",
            )
        reason = payload.get("reason", "")
        # Gate 审批接线：running 检查（V-03② 409）→
        # approved：完成步骤 + resume + start_task 继续后续步骤（旧语义保持）；
        # rejected：步骤回 pending + 任务暂停，不自动重跑（旧语义，SWP2-B 断言为准）——
        # 人工 resume + start 后重跑，拒绝原因经 intervention artifact 注入
        orch = _get_orchestrator()
        try:
            if decision == "rejected":
                await orch.reject_gate_and_run(task_id, step_id, reason)
            else:
                # 2026-08-20：选项类 gate 的决策内容（reason）追加进 summary——
                # 后续步骤的 step_context 含前序 summary，选中的方向才能传递下去
                # （DB 实证 e726f3e6 step-3：用户选 C 后无推进，决策丢失）
                if reason.strip():
                    try:
                        prev = await _get_storage().get_artifact(task_id, step_id, "summary")
                        merged = reason.strip()
                        if prev and prev.get("content"):
                            merged = str(prev["content"]).rstrip() + "\n\n【用户决策】" + reason.strip()
                        else:
                            merged = "【用户决策】" + reason.strip()
                        await _get_storage().save_artifact(task_id, step_id, "summary",
                                                           merged, "text")
                    except Exception:  # noqa: BLE001
                        pass
                await orch.approve_gate_and_run(task_id, step_id, reason)
        except OrchestratorBusyError:
            raise HTTPException(409, detail="该步骤正在审查中，请稍后再审批")
        return {"status": "ok", "step_id": step_id, "decision": decision}

    new_status = payload["new_status"]
    try:
        await sm.advance_step(task_id, step_id, new_status)
    except ValueError as e:
        raise _state_error(e)
    return {"status": "ok", "step_id": step_id, "new_status": new_status}


@rest_app.post("/api/step/compress")
async def compress_step(payload: dict):
    """
    压缩步骤的对话历史（手动触发）。保留最近 6 条，早期消息合并为一条 system 摘要。

    消息 ≤6：{status:"skipped", reason:"too_few_messages", count}；
    否则 {status:"ok", original_count, compressed_count}。

    seq 语义（问题 21 + 第 9 轮 B2/V9）：合并摘要的 seq = 被合并消息的最小 seq，
    其余保留消息 seq 不变——after_seq 增量拉取与前端去重不受影响
    （旧代码 clear+重新 append 导致 seq 全从 0 重排，作废）。
    实现：append_message 已支持显式 seq；早期消息删除经 storage 直连 SQL
    （契约允许"compress 直接 SQL 更新"）；非 SQLite 适配器 fallback 到
    clear + 显式 seq 重写（仍满足 seq 语义）。
    """
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    storage = _get_storage()
    messages = await storage.get_step_messages(task_id, step_id)
    if len(messages) <= 6:
        return {"status": "skipped", "reason": "too_few_messages", "count": len(messages)}

    early = messages[:-6]
    recent = messages[-6:]
    summary_seq = early[0]["seq"]  # 合并摘要 seq = 被合并消息的最小 seq
    summary_lines = [f"[{m['role']}]: {str(m.get('content', ''))[:100]}..." for m in early]
    summary_content = "[早期对话已压缩]\n" + "\n".join(summary_lines)

    conn_factory = getattr(storage, "_get_conn", None)
    if conn_factory is not None:
        # 直接 SQL 更新（契约明示允许）：删除早期消息，写入摘要（占最小 seq）
        conn = await conn_factory()
        conn.execute(
            "DELETE FROM step_messages WHERE task_id = ? AND step_id = ? AND seq < ?",
            (task_id, step_id, recent[0]["seq"]),
        )
        conn.execute(
            """INSERT INTO step_messages (task_id, step_id, seq, role, content, round_num, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, step_id, summary_seq, "system", summary_content, 0,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    else:
        # 非 SQLite 适配器：clear + 显式 seq 重写（其余消息 seq 不变）
        await storage.clear_step_messages(task_id, step_id)
        await storage.append_message(
            task_id, step_id,
            {"role": "system", "content": summary_content, "round_num": 0},
            seq=summary_seq,
        )
        for m in recent:
            await storage.append_message(task_id, step_id, m, seq=m["seq"])

    return {"status": "ok", "original_count": len(messages), "compressed_count": len(early)}


@rest_app.post("/api/step/resume")
async def resume_step(payload: dict):
    """恢复被打断的步骤。
    after_intervention=True 时仅恢复 Task 状态（介入后专用）。
    """
    task_id = payload["task_id"]
    after_intervention = payload.get("after_intervention", False)

    sm = _get_state_machine()
    if after_intervention:
        await sm._resume_after_intervention(task_id)
        return {"status": "ok", "task_id": task_id}
    else:
        step_id = payload["step_id"]
        message = payload.get("message", "")
        try:
            await sm.resume_step(task_id, step_id, message)
        except ValueError as e:
            raise _state_error(e)
        # 2026-08-21 修复：resume 后自动重启执行循环——原语义仅恢复状态不 start
        # （DB 实证 8d53c09f step-9：LLM 错误 stopped → 用户点恢复 → pending 无循环
        # 卡死 3 分多钟；前端恢复按钮虽带 startTask，总览页等入口只有 resume）。
        # start_task 幂等：已 running 直接返回；僵尸 running（cancelled）走有界等待。
        await _get_orchestrator().start_task(task_id)
        return {"status": "resumed", "step_id": step_id}


# ═══════════════════════════════════════════════════════════════════
# 介入控制
# ═══════════════════════════════════════════════════════════════════


@rest_app.post("/api/intervene/step")
async def intervene_step(payload: dict):
    """步骤内介入。intervention_type: send | force_inject | stop

    SWP3-B1 接线（WP3 §2.2）：stop → 状态机 stop_step + orchestrator abort；
    force_inject → 写 artifact + abort（中断当前 LLM 流，下一轮注入后继续）；
    send → 仅排队写入（不打断）。
    """
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    intervention_type = payload["intervention_type"]
    message = payload["message"]

    sm = _get_state_machine()
    orch = _get_orchestrator()
    try:
        if intervention_type == "stop":
            await sm.stop_step(task_id, step_id)
            await orch.abort(task_id)
        elif intervention_type == "force_inject":
            await sm.handle_step_intervention(task_id, step_id, intervention_type, message)
            await orch.abort(task_id, kind="force_inject")
            # 2026-08-21 实体化：统一循环重启（原 _resume_gate_pending 收敛）——
            # 任务 paused(gate)+gate active 时由执行循环 paused 分支消费介入消息；
            # start_task 幂等（running 直接返回）
            await orch.start_task(task_id)
        else:
            # send：步骤已完成 → 续做重置（清后续步骤消息/产物/状态 + 当前步骤回
            # 进行中 + 注入消息 + 自动继续执行）；非 completed 保持原排队注入。
            # 2026-08-21 去绑定普通化：审查/收尾步骤（monitor-N / monitor-intervene-N
            # / review / report）是独立步骤——send 到已完成/跳过/停止的 monitor 实例
            # = 实例续跑（保留原对话上下文，reset_step_for_continuation 不清消息/产物）；
            # 不走 reset_flow_from_step（那是真实步骤续做语义，会清空后续步骤内容）
            task = await _get_storage().get_task(task_id, include_hidden=True)
            step = None
            if task:
                step = next((s for s in task.get("steps", []) if s.get("step_id") == step_id), None)
            if step and is_hidden_step(step):
                # 审查/收尾步骤：终态/停止 → 续接重置（保留对话）+ 注入消息 + 重启
                cur_status = step.get("status")
                if cur_status in ("completed", "skipped", "stopped"):
                    if orch.is_running(task_id):
                        await orch.abort(task_id)
                        await orch.wait_stopped(task_id)
                    await sm.reset_step_for_continuation(task_id, step_id, "用户消息续做")
                await sm.handle_step_intervention(task_id, step_id, "send", message)
                await orch.start_task(task_id)
            elif step and step.get("status") == "completed":
                if orch.is_running(task_id):
                    await orch.abort(task_id)
                    await orch.wait_stopped(task_id)
                await sm.reset_flow_from_step(task_id, step_id)
                await sm.handle_step_intervention(task_id, step_id, "send", message)
                await orch.start_task(task_id)
            elif step and step.get("status") == "skipped":
                # 2026-08-21（矩阵 A31 行）：send 到 skipped 步骤 = 续做重置——
                # skipped→pending + 注入消息 + 重启执行（用户对跳过的步骤发消息
                # = 要重做该步骤，与 completed 续做同语义）
                if orch.is_running(task_id):
                    await orch.abort(task_id)
                    await orch.wait_stopped(task_id)
                await sm.reset_step_for_continuation(task_id, step_id, "用户消息续做")
                await sm.handle_step_intervention(task_id, step_id, "send", message)
                await orch.start_task(task_id)
            elif step and step.get("status") == "stopped":
                # 用户 stop 后发消息 = 恢复 + 注入消息 + 重启执行（发消息即继续的预期）。
                # resume_step 写入 intervention（type=resume_message，orchestrator 的
                # _read_interventions 按 message 字段注入并清空）→ 步骤回 pending +
                # 任务 active；start_task 的 zombie 守卫自动等待旧执行循环退出。
                await sm.resume_step(task_id, step_id, message)
                await orch.start_task(task_id)
            else:
                await sm.handle_step_intervention(task_id, step_id, "send", message)
                # 2026-08-21 实体化：统一循环重启（原 _resume_gate_pending 收敛）
                await orch.start_task(task_id)
    except ValueError as e:
        raise _state_error(e)
    return {"status": "ok", "intervention_type": intervention_type, "step_id": step_id}


@rest_app.post("/api/monitor/control")
async def monitor_control(payload: dict):
    """Monitor 页控制端点（2026-08-20 用户需求：Monitor 也要暂停/恢复）。

    payload: {task_id, action: "stop"|"resume", message?, step_id?}
    - stop：直接复用 sm.stop_step（monitor 实例运行时 active，前置校验通过；
      实例 stopped + 任务 paused + step_stopped 事件全复用）；step_id 缺省时
      找当前 active 的 monitor 步骤（实体化：monitor-* 真实行）
    - resume：直接复用 sm.resume_step（实例 stopped→pending + 任务 active + 事件）
      + 有 message 时 flow_pending 排队并触发 monitor-intervene（无消息仅恢复
      执行循环，不产生空 reason 介入）
    """
    task_id = payload["task_id"]
    action = payload.get("action", "")
    message = payload.get("message") or ""
    sm = _get_state_machine()
    orch = _get_orchestrator()
    storage = _get_storage()
    try:
        if action == "stop":
            step_id = payload.get("step_id") or None
            # 2026-08-21：不再自动定位——前端页面总传当前 monitor id；
            # 缺 step_id 无法确定目标，直接报错（有问题就是有问题）
            if not step_id:
                raise _state_error(ValueError("monitor/control stop requires step_id"))
            # stop_step：实例 stopped + 任务 paused + 事件；再 abort 打断正在跑的
            # monitor 轮（cancel event → _execute_step 的 LlmAborted 分支见
            # is_cancelled 保持 stopped，不覆盖状态）
            await sm.stop_step(task_id, step_id)
            await orch.abort(task_id)
        elif action == "resume":
            step_id = payload.get("step_id") or None
            # 2026-08-21：不再自动定位（此前取 stopped 列表第一个会定位到
            # monitor-init 残留串台，DB 实证 e726f3e6 13:36）——前端页面总传
            # 当前 monitor id，缺 step_id 直接报错（有问题就是有问题）
            if not step_id:
                raise _state_error(ValueError("monitor/control resume requires step_id"))
            cur_status = await sm._get_step_status(task_id, step_id)
            if cur_status == "stopped":
                await sm.resume_step(task_id, step_id)
            elif cur_status == "pending":
                # 已 pending（排队/重置残留）：无需状态转移，直接激活任务
                # （消息走 flow_pending + trigger_monitor，start_task 后由
                # 介入/该步骤消费）
                await sm._resume_after_intervention(task_id)
            else:
                raise ValueError(
                    f"Step {step_id} is not stopped or pending, cannot resume")
            if message:
                # 有消息：排队注入 + 触发介入 monitor（消费消息重分析）
                await sm.flow_pending_intervention(task_id, message)
                await orch.trigger_monitor(task_id, message)
            else:
                # 2026-08-20 修复：无消息的恢复（顶栏恢复按钮）不触发 Monitor 介入——
                # 空 reason 会让 monitor_trigger 上下文写「介入原因：（空）」，
                # AI 误以为介入并请求人类澄清（DB 实证 _intervene 对话空介入原因）
                pass
            # 恢复执行循环（有消息时等介入 monitor 完成后再恢复）
            await sm._resume_after_intervention(task_id)
            await orch.start_task(task_id)
        else:
            raise ValueError(f"Unknown action: {action}")
    except ValueError as e:
        raise _state_error(e)
    return {"status": "ok", "task_id": task_id, "action": action}


@rest_app.post("/api/intervene/flow")
async def intervene_flow(payload: dict):
    """🛑 流程级介入。mode="pending" 时仅排队调整，不打断当前流程。

    SWP3-B1 接线（WP3 §2.2，N3）：immediate → flow_intervene + abort +
    trigger_monitor（三步）+ resume + start_task。

    pending 补触发（2026-08-15，用户决策）：任务已 completed 时，排队消息不再
    等待流程自然完成（已无触发点）——补触发一次最终审查（_final_review 消费
    flow_pending → Monitor 读取消息 → add_steps 追加步骤 → 任务重新激活 →
    执行循环跑新步骤），与运行中 pending 的消费机制一致。

    rebuild（2026-08-20 用户需求：Monitor 已完成时发送 → 清理后续步骤消息和
    产出 → 继续当前 monitor 步骤）：任务 completed 时复制 intervene_step
    send-on-completed 分支（abort + reset_flow_from_step + 注入消息），再后台
    触发 _monitor_orchestrate 重跑 Monitor；收尾复制 _final_review 的 pending
    检查（排除 target_sid——reset_flow_from_step 会把 target_sid 自身置 pending，
    不排除则执行循环误重跑最后真实步骤）。

    step_id（必传，2026-08-21 起）：前端 Monitor 页传当前触发步骤（实体 id——
    真实步骤或 monitor 步骤）——步骤已完成但任务仍 active（后续步骤在跑）时
    也走清理重跑；真实步骤直接命中（须 completed），审查步骤（monitor-step-X/
    monitor-init/monitor-intervene/review/report）解析为 sort_order 小于它的最大
    completed 真实步骤（monitor-step-1 → step-1，清理其之后全部）。
    2026-08-21：不再惰性回退（有问题就是有问题）——缺 step_id / 锚点无法解析
    （虚拟步骤、无前驱的 monitor-init/review/report）→ 直接 400 报错，不再回退
    "最大 completed 真实步骤"或降级 pending 排队（此前会清错范围/静默失败，
    DB 实证 e726f3e6）。
    """
    task_id = payload["task_id"]
    reason = payload.get("reason", "")
    mode = payload.get("mode", "immediate")
    step_id = payload.get("step_id") or None

    sm = _get_state_machine()
    orch = _get_orchestrator()
    storage = _get_storage()
    try:
        if mode == "rebuild":
            # 2026-08-21：不再惰性回退（有问题就是有问题）——step_id 必传且必须
            # 能解析出清理锚点；前端总传当前 monitor 步骤 id（handleRebuildConfirm）。
            # 此前无 step_id 时回退"最大 completed 真实步骤"、锚点缺失时降级
            # pending 排队，都会清错范围/静默失败（DB 实证 e726f3e6）
            if not step_id:
                raise _state_error(ValueError("rebuild requires step_id"))
            task_hidden = await storage.get_task(task_id, include_hidden=True)
            target_sid = None
            if task_hidden:
                steps_all = task_hidden.get("steps", [])
                sid = str(step_id)
                # ① 真实步骤直接命中（须已完成）
                step = next((s for s in steps_all if s.get("step_id") == sid), None)
                if step and step.get("status") == "completed" and not is_hidden_step(step):
                    target_sid = sid
                # ② 审查步骤 id（monitor-N/monitor-intervene-N/monitor-init/
                # review/report）→ 锚点解析：取 sort_order 小于该审查步骤的最大
                # completed 真实步骤（monitor-1 → step-1，清理其之后全部）
                elif is_hidden_step({"step_id": sid, "type": ""}):
                    mrow = next((s for s in steps_all
                                 if s.get("step_id") == sid), None)
                    if mrow is not None:
                        morder = mrow.get("sort_order", 0)
                        before = [s for s in steps_all
                                  if not is_hidden_step(s)
                                  and s.get("status") == "completed"
                                  and s.get("sort_order", 0) < morder]
                        if before:
                            target_sid = (max(before,
                                              key=lambda s: s.get("sort_order", 0))
                                          .get("step_id"))
            if target_sid is None:
                raise _state_error(ValueError(
                    f"rebuild anchor not found for step_id={step_id}"))
            # 复制 intervene_step send-on-completed 分支（L1492-1498）
            # 2026-08-20：abort+等循环退出移到后台协程内（工具调用最长 60s，
            # 前台 10s 有界等待可能超时返回——旧循环还在跑时 reset 会与其竞争，
            # 造成 rebuild 后"还有执行在跑"、消息/产物错乱）
            # 无条件 abort：不仅停执行循环，也取消正在跑的介入 monitor
            # （trigger_monitor 与执行循环共享 cancel_event）
            async def _rebuild_monitor() -> None:
                await orch.abort(task_id)
                await orch.wait_stopped(task_id, timeout=180.0)
                # 2026-08-21 去绑定普通化：preserve 当前 monitor 实例（位置锚定，
                # 不依赖命名绑定）——step_id 为审查步骤时 preserve 其本身；真实
                # 步骤时 preserve 紧随其后（sort_order 更大）的最小 monitor 行，
                # reset 保留其消息/产物、置 pending，执行循环自然拾取重跑
                preserve: tuple[str, ...] = ()
                if is_hidden_step({"step_id": str(step_id), "type": ""}):
                    preserve = (str(step_id),)
                else:
                    target_row = next((s for s in steps_all
                                       if s.get("step_id") == target_sid), None)
                    target_order = target_row.get("sort_order", 0) if target_row else 0
                    next_mons = [s for s in steps_all
                                 if is_hidden_step(s)
                                 and s.get("type") == "monitor"
                                 and s.get("sort_order", 0) > target_order]
                    if next_mons:
                        preserve = (min(next_mons,
                                        key=lambda s: s.get("sort_order", 0))
                                    .get("step_id", ""),)
                await sm.reset_flow_from_step(
                    task_id, target_sid, preserve_steps=preserve)
                # 2026-08-20 修复：rebuild 只重跑当前 monitor，不重跑触发真实步骤——
                # reset 把 target 置 pending 是"步骤续做"语义；恢复 completed 防止
                # 执行循环拾取 target 重跑（弹窗文案"重新执行当前 Monitor 步骤"）
                await storage.update_step_status(task_id, target_sid, "completed")
                await sm.flow_pending_intervention(task_id, reason)
                # 2026-08-24（DB 实证 99248a9f rebuild 后 monitor 无反应）：消息
                # 同时写给 preserve 的 monitor 实例——monitor 续跑时
                # _read_interventions 消费落库为 user 消息（前端 UI 可见）；仅
                # flow_pending（注入 system 侧 user_context 不落库）会导致
                # 用户消息不显示、monitor 看起来完全没跑
                if preserve:
                    await sm.handle_step_intervention(
                        task_id, preserve[0], "send", reason)
                # 实体化：执行循环拾取 preserve 的 monitor 步骤重跑
                # （_monitor_prep 消费 flow_pending）；start_task 幂等防双循环
                await orch.start_task(task_id)

            asyncio.create_task(_rebuild_monitor())
            return {"status": "queued", "task_id": task_id,
                    "reason": reason, "mode": "rebuild"}
        if mode == "pending":
            # 2026-08-21 去绑定普通化（用户决策）：active 任务 → 直接创建介入实例
            # monitor-intervene-N（trigger_monitor send：插入当前运行步骤之前 + 消息
            # 写入实例 intervention），不再写 flow_pending——flow_pending 由任意下一个
            # monitor 步骤消费是消息串台根因（DB 实证 e726f3e6）；flow_pending 仅保留
            # completed 补触发链路（review/report 执行时 _monitor_prep 消费）
            task = await storage.get_task(task_id)
            if task and task.get("status") == "completed":
                await sm.flow_pending_intervention(task_id, reason)
                # 已完成流程：补触发收尾链（实体化 2026-08-21）：任务 completed 但 review
                # 未完成 → 激活 + 确保 review/report + 重启执行循环（实体状态防重——
                # 已有 review/report 行则不重复插入；原 _final_reviewed_tasks set 删除）
                logger.info(f"[DC:REST] completed task pending intervention -> "
                            f"backfill tail chain for {task_id}")
                # 2026-08-27（用户反馈：已完成流程总览页发消息没反应）：收尾链
                # （review/report）均已完成（tail_done）时补触发无效——review/report
                # 不重跑、flow_pending 无人消费 → 显式创建 monitor-intervene-N 介入
                # 步骤（插在 final review 之后）处理用户消息；消息同时写入实例
                # intervention（monitor 执行时 _read_interventions 消费落库为 user
                # 消息，UI 可见）——与 rebuild 分支双写模式一致
                task_h = await storage.get_task(task_id, include_hidden=True)
                hsteps = (task_h or {}).get("steps", [])
                tail_done = all(
                    next((s.get("status") for s in hsteps
                          if s.get("step_id") == sid), None) == "completed"
                    for sid in ("review", "report"))
                if tail_done:
                    instance_id = await orch._next_intervene_instance_id(task_id)
                    await storage.add_steps(task_id, [{
                        "step_id": instance_id, "title": f"介入审查 {instance_id}",
                        "type": "monitor", "human_attention": "none",
                        "model_tier": "power", "required": 1,
                        "parallel_with": None, "description": "",
                    }], after_step_id="review")
                    await sm.handle_step_intervention(
                        task_id, instance_id, "send", reason)
                    logger.info(f"[DC:REST] completed tail_done intervention -> "
                                f"create {instance_id} for {task_id}")
                else:
                    await orch._ensure_tail_steps(task_id)
                await orch.start_task(task_id)
            elif task and task.get("status") == "paused":
                # 2026-08-21（用户反馈 e726f3e6 17:07 + DB 实证 19:05）：paused(gate)
                # 等待审批时排队消息无人消费（执行循环等审批退出、gate AI 只读无法
                # 执行流程调整）→ 升级为即时介入审查：**先打断 gate（active→stopped）**
                # 再 force_inject——否则 trigger_monitor 的 gate 分支（paused+gate
                # active 无条件匹配）把消息写给 gate AI（只读无法执行删除/重建），
                # force_inject 被架空空转（19:05 实证：消息注入 step-17 会话，任务
                # 再次 paused 等审批，收尾步骤 report 被误拾取乱调 adjust_flow）
                gate = next((s for s in task.get("steps", [])
                             if s.get("human_attention") == "gate"
                             and s.get("status") == "active"), None)
                if gate:
                    logger.info(f"[DC:REST] break gate {gate['step_id']} before "
                                f"force_inject for {task_id}")
                    await sm.advance_step(task_id, gate["step_id"], "stopped")
                logger.info(f"[DC:REST] paused task pending intervention -> "
                            f"escalate to force_inject for {task_id}")
                await orch.trigger_monitor(task_id, reason, mode="force_inject")
                await sm._resume_after_intervention(task_id)
                await orch.start_task(task_id)
            else:
                # active（含未启动）：创建介入实例排队（挂当前运行步骤之前）
                await orch.trigger_monitor(task_id, reason, mode="send")
            return {"status": "queued", "task_id": task_id, "reason": reason}
        else:
            await sm.flow_intervene(task_id, reason)
            await orch.abort(task_id, kind="immediate")
            # 实体化（2026-08-21）：force_inject——打断当前步骤、monitor-intervene
            # 优先执行（flow_intervene 已把当前步骤置 stopped → cur 定位不到，仅
            # ensure 行 + 消息；随后 start 拾取 monitor-intervene 重跑）
            await orch.trigger_monitor(task_id, reason, mode="force_inject")
            await sm._resume_after_intervention(task_id)
            await orch.start_task(task_id)
            return {"status": "ok", "task_id": task_id, "reason": reason}
    except ValueError as e:
        raise _state_error(e)


# ═══════════════════════════════════════════════════════════════════
# Monitor — 内联导出（对话摘要直接嵌入 system_message，无临时文件夹）
# ═══════════════════════════════════════════════════════════════════


@rest_app.post("/api/monitor/export")
async def monitor_export(payload: dict, storage: Any = None):
    """
    内联导出 Monitor 所需数据，对话摘要直接嵌入 system_message。

    返回: {system_message, task_id, step_states:[{step_id,title,status,type,
    human_attention,required,model_tier,sort_order,parallel_with}]}
    storage（2026-08-21 实体化）：执行器内部调用时注入自身 storage（避免
    默认库/测试库不一致）；默认取全局实例。
    """
    task_id = payload["task_id"]
    step_id = payload.get("step_id", "")
    is_final = payload.get("final_review", False)

    storage = storage or _get_storage()
    monitor = MonitorContext(storage=storage)
    context = await monitor._build_monitor_context(task_id, step_id)
    if is_final:
        context["check_type"] = "final_review"

    task = context.get("task", {})
    steps = task.get("steps", [])
    completed_conv = context.get("completed_conversations", {})

    # 内联已完成步骤的对话摘要（头部锚点 5 条 + 最新 20 条，单条截断，避免 token 超限）
    # 摘要方向修复（Monitor 信息缺陷）：msgs[:20] 取最早 20 条（get_conversation 按
    # seq 升序），步骤 5000+ 条消息时最新进展完全丢失；改为头部锚点 + 最新 20 条
    conv_summary_parts = []
    for sid, msgs in completed_conv.items():
        step_title = ""
        for s in steps:
            if s.get("step_id") == sid:
                step_title = s.get("title", "")
                break
        conv_summary_parts.append(f"## {sid}: {step_title} ({len(msgs)} 条消息)")
        head = msgs[:5]  # 头部锚点（上下文开头）
        tail = msgs[-20:] if len(msgs) > 25 else []  # 最新进展（核心信息）
        picked = head + [m for m in tail if m not in head]
        for m in picked:
            role = m.get("role", "?")
            content = str(m.get("content", ""))[:300]
            if role == "tool":
                tool_name = m.get("tool_name", m.get("toolName", ""))
                conv_summary_parts.append(f"[{role}/{tool_name}]: {content[:200]}")
            else:
                conv_summary_parts.append(f"[{role}]: {content}")
        # 步骤 summary 注入（最浓缩的结论性信息；无则跳过）
        try:
            summ = await monitor.storage.get_artifact(task_id, sid, "summary")
        except Exception:
            summ = None
        if summ and summ.get("content"):
            conv_summary_parts.append(f"## {sid} 摘要")
            conv_summary_parts.append(str(summ["content"])[:800])
        conv_summary_parts.append("")

    conv_text = "\n".join(conv_summary_parts) if conv_summary_parts else "(无已完成步骤对话)"

    if is_final:
        # 实体化（2026-08-21）：review → final-reviewer、report → final-reporter
        # （step_type 由调用方显式传；缺省按 step_id 兜底）
        step_type = payload.get("step_type") or (
            "report" if step_id == "report" else "review")
        monitor_prompt = load_prompt(prompt_for_step(step_id, step_type))
    else:
        # monitor 类（monitor-init/monitor-step-X/monitor-intervene）→ orchestrator 提示词
        monitor_prompt = load_prompt(prompt_for_step(step_id, "monitor"))

    # 任务关键发现注入（与 executor system 注入同源：_flow/key_findings artifact）
    # 小节恒存在（无数据时标注），Monitor 信息核查清单第 2 项有明确结论
    kf_parts = []
    try:
        kf = await monitor.storage.get_artifact(task_id, "_flow", "key_findings")
        if kf and kf.get("content"):
            kf_parts.append(f"## 任务关键发现\n{kf['content']}")
        else:
            kf_parts.append("## 任务关键发现\n(无关键发现记录)")
    except Exception:
        kf_parts.append("## 任务关键发现\n(无关键发现记录)")

    # 动态层（user 消息）：任务信息前缀（任务内固定）+ 动态后缀（摘要/检查点/
    # 场景说明/关键发现/流程报告）。预设流程模板已拼入 system（_monitor_prep：
    # orchestrator.md + flow-templates.md 静态拼接，跨任务/跨步骤恒定 → 前缀缓存
    # 命中率最高）；可用操作在 orchestrator.md「可用操作」完整列出，user 不重复注入
    user_context = (
        f"当前任务: {task.get('title', '')} (类型: {task.get('type', '')})\n"
        f"任务 ID: {task_id}\n"
        f"- 需求: {task.get('description', '') or '(无)'}\n\n"
        f"## 已完成步骤对话摘要（内联，无需读文件）\n"
        f"{conv_text}\n\n"
    )
    # 编排检查点（2026-08-20 修复）：明确告知 Monitor 当前续做步骤与插入位置——
    # 否则 Monitor 不知道新步骤该放哪（用户反馈：新步骤塞到第一步）
    if step_id and not is_final:
        user_context += (
            f"## 编排检查点\n"
            f"当前是在步骤 [{step_id}]（刚完成/续做点）之后的编排检查。\n"
            f"- 新步骤必须插入到 [{step_id}] 之后：add_steps 时传 after_step_id=\"{step_id}\"；\n"
            f"- 已保留的待执行步骤继续执行，不要重复创建、不要把它们插到流程开头；\n"
            f"- 如需调整顺序用 reorder_steps（order_json 必须包含全部步骤 id）。\n\n"
        )
    # 场景说明（动态后缀；模板在系统提示词中，初始编排指明用 add_steps 创建流程）
    user_context += (
        "这是新任务的**初始编排**：请根据任务描述参考系统提示词中的预设流程模板，"
        "用 add_steps 一次性创建完整流程（3~12 步、≥1 个 gate、每步 description 必填）。"
        if not steps and not is_final else
        "这是一个刚完成的步骤后的常规编排检查。" if not is_final else
        "这是一次最终审查，所有步骤已完成，请判断是否需要追加步骤。"
    )
    if kf_parts:
        user_context += "\n\n" + "\n\n".join(kf_parts)

    # 流程报告提示（2026-08-16 用户需求）：流程报告（多步骤共享）是当前工作进展
    # 最重要的资产——Monitor 编排/收尾决策前先读它；内联开头锚点防忽略。
    try:
        fr = await monitor.storage.get_artifact(task_id, "_flow", "step_report")
        if fr and fr.get("content"):
            user_context += (
                f"\n\n## 流程报告（当前工作进展，最重要，先读再决策）\n"
                f"请先用 dcflow_read_file 读取完整流程报告："
                f".dc_tmp/{task_id}/{_FLOW_REPORT_FILENAME}。此处为开头锚点：\n"
                f"{fr['content'][:_FLOW_REPORT_ANCHOR_CHARS]}"
            )
    except Exception:
        pass

    # 兼容：拼装为一条 system_message（契约端点 25 B5）
    system_msg = f"{monitor_prompt}\n\n{user_context}"

    return {
        "system_message": system_msg,
        "system_prompt": monitor_prompt,   # 新增：纯规则稳定层（可缓存）
        "user_context": user_context,      # 新增：动态上下文（LLM 作 user 消息）
        "task_id": task_id,
        "step_states": [
            {"step_id": s.get("step_id", ""), "title": s.get("title", ""),
             "status": s.get("status", ""), "type": s.get("type", "executor"),
             "human_attention": s.get("human_attention", "none"),
             "required": s.get("required", 1),
             "model_tier": s.get("model_tier", "light"),
             "sort_order": s.get("sort_order", 0),
             "parallel_with": s.get("parallel_with")}
            for s in steps if not is_hidden_step(s)
        ],
    }


@rest_app.post("/api/monitor/cleanup")
async def monitor_cleanup(payload: dict):
    """清理 Monitor 临时文件夹。"""
    tmp_dir = payload.get("temp_dir", "")
    if tmp_dir and os.path.isdir(tmp_dir):
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"status": "ok"}


@rest_app.post("/api/monitor/trigger")
async def monitor_trigger(payload: dict):
    """
    流程级介入后触发 Monitor（内联模式，不再创建临时文件夹）。

    返回: {system_message, task_id, trigger_step_id, step_states}
    """
    task_id = payload["task_id"]
    trigger_step_id = payload.get("trigger_step_id", "")
    reason = payload.get("reason", "")

    monitor = _get_monitor_context()
    context = await monitor._build_monitor_context(task_id, trigger_step_id)

    task = context.get("task", {})
    steps = task.get("steps", [])
    completed_conv = context.get("completed_conversations", {})

    conv_summary_parts = []
    for sid, msgs in completed_conv.items():
        step_title = ""
        for s in steps:
            if s.get("step_id") == sid:
                step_title = s.get("title", "")
                break
        conv_summary_parts.append(f"## {sid}: {step_title} ({len(msgs)} 条消息)")
        for m in msgs[:15]:
            role = m.get("role", "?")
            content = str(m.get("content", ""))[:200]
            conv_summary_parts.append(f"[{role}]: {content}")
        conv_summary_parts.append("")

    conv_text = "\n".join(conv_summary_parts) if conv_summary_parts else "(无已完成步骤对话)"

    # 实体化（2026-08-21）：介入审查 → orchestrator 提示词（monitor-intervene 等）
    monitor_prompt = load_prompt(prompt_for_step(trigger_step_id, "monitor"))
    # 动态层（user 消息）：任务背景 + 介入原因 + 摘要 + 工具清单（Task 8：与纯规则分离）
    user_context = (
        f"当前任务: {task.get('title', '')} (类型: {task.get('type', '')})\n\n"
        f"## 介入原因\n"
        f"用户发起了流程级介入：{reason}\n\n"
        f"请根据介入原因重新评估当前流程，决定是否需要调整后续步骤。\n\n"
        f"## 已完成步骤对话摘要（内联）\n"
        f"{conv_text}\n\n"
        f"## 可用工具\n"
        f"- dcflow_list_steps: 查看当前所有步骤状态\n"
        f"- dcflow_adjust_flow: 修改流程步骤（skip/add/remove/reorder/mark_complete/no_change）；"
        f"add_steps 可用 after_step_id（顶层参数或 steps_json 元素内嵌）指定插入位置\n"
    )
    # 兼容：拼装为一条 system_message（契约端点 27 B5）
    system_msg = f"{monitor_prompt}\n\n{user_context}"

    return {
        "system_message": system_msg,
        "system_prompt": monitor_prompt,   # 新增：纯规则稳定层（可缓存）
        "user_context": user_context,      # 新增：动态上下文（LLM 作 user 消息）
        "task_id": task_id,
        "trigger_step_id": trigger_step_id,
        "step_states": [
            {"step_id": s.get("step_id", ""), "title": s.get("title", ""),
             "status": s.get("status", ""), "type": s.get("type", "executor"),
             "human_attention": s.get("human_attention", "none"),
             "required": s.get("required", 1),
             "model_tier": s.get("model_tier", "light"),
             "sort_order": s.get("sort_order", 0),
             "parallel_with": s.get("parallel_with")}
            for s in steps if not is_hidden_step(s)
        ],
    }


@rest_app.post("/api/monitor/save-conversation")
async def monitor_save_conversation(payload: dict, storage: Any = None):
    """
    保存 Monitor Agent 的对话记录。

    Monitor 对话以 artifact 形式存储，artifact_type = "monitor_conversation"，
    step_id = trigger_step_id（触发 Monitor 的步骤 step_id，或 "monitor-init" 默认）。
    storage（2026-08-21 实体化）：执行器内部调用时注入自身 storage。
    """
    task_id = payload["task_id"]
    trigger_step_id = payload.get("trigger_step_id", "monitor-init")
    conversation = payload.get("conversation", [])

    storage = storage or _get_storage()
    await storage.save_artifact(
        task_id=task_id,
        step_id=trigger_step_id,
        artifact_type="monitor_conversation",
        content=json.dumps(conversation, ensure_ascii=False),
        content_format="json",
    )
    logger.info(f"Monitor conversation saved: task={task_id}, trigger={trigger_step_id}, msgs={len(conversation)}")
    return {"status": "ok", "task_id": task_id, "trigger_step_id": trigger_step_id, "message_count": len(conversation)}


@rest_app.get("/api/task/{task_id}/monitor-conversations")
async def get_monitor_conversations(task_id: str):
    """
    获取某个 Task 的所有 Monitor 对话记录。

    返回: {task_id, monitor_conversations: {trigger_step_id: [...messages]},
           step_tokens: {实例id: {token_prompt, token_cached, token_completion}},
           monitor_steps: {实例id: status}}
    （Token 展示：_monitor:*/_review/_report 实例的 token 用量，
     MonitorDetail 据此渲染上下文占用条）
    """
    storage = _get_storage()
    artifacts = await storage.list_artifacts(task_id)
    result = {}
    for a in artifacts:
        if a.get("artifact_type") == "monitor_conversation":
            sid = a.get("step_id", "monitor-init")
            try:
                msgs = json.loads(a.get("content", "[]"))
            except (json.JSONDecodeError, TypeError):
                msgs = []
            result[sid] = msgs
    step_tokens = await storage.list_virtual_step_tokens(task_id)
    monitor_steps = await storage.list_virtual_steps(task_id)
    monitor_order = await storage.list_virtual_step_orders(task_id)
    # 2026-08-23：monitor 锚点（{instance_id: 触发步骤 id}）——monitor sort_order
    # 被后续插入步骤挤压漂移，FlowOverview 按锚点步骤归属眼睛图标
    monitor_anchors: dict[str, str] = {}
    try:
        raw_anchor = await storage.get_artifact(task_id, "_flow", "monitor_anchors")
        if raw_anchor and raw_anchor.get("content"):
            data = json.loads(raw_anchor["content"])
            monitor_anchors = data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    # 2026-08-21：审查/收尾步骤（实体行）运行统计——输出速度/首字延迟/运行时长/
    # 请求数由 orchestrator 每轮 LLM 流结束落库（_update_step_stats）；getTask
    # 不含 hidden 步骤，故随 monitor 数据一并返回（MonitorDetail TokenMetrics 数据源）
    step_stats = {}
    task_hidden = await storage.get_task(task_id, include_hidden=True)
    for s in (task_hidden or {}).get("steps", []):
        if s.get("type") in ("monitor", "review", "report"):
            step_stats[s.get("step_id", "")] = {
                "run_duration_ms": s.get("run_duration_ms") or 0,
                "output_duration_ms": s.get("output_duration_ms") or 0,
                "ttft_total_ms": s.get("ttft_total_ms") or 0,
                "ttft_samples": s.get("ttft_samples") or 0,
                "requests": s.get("requests") or 0,
            }
    return {"task_id": task_id, "monitor_conversations": result,
            "step_tokens": step_tokens, "monitor_steps": monitor_steps,
            "monitor_order": monitor_order, "step_stats": step_stats,
            "monitor_anchors": monitor_anchors}


# ═══════════════════════════════════════════════════════════════════
# SSE 端点（SWP3-C，WP3 §2.1）
#
# GET /sse?taskId=X&lastSeq=N → EventSourceResponse（sse-starlette）
# - 只发 data: 行（B2），无 event: 行；ping 禁用（ping=0），不产生额外行
# - 补发/溢出/实时推送全部由 brain/sse_hub 的 SseHub.subscribe 处理
# ═══════════════════════════════════════════════════════════════════


@rest_app.get("/sse")
async def sse_stream(request: Request, taskId: str, lastSeq: int = 0):
    """SSE 事件流（WP3 §2.1）：订阅任务事件，携带 lastSeq 增量补发。

    2026-08-27：经 orch.stream_events 订阅——补发跳过 live 快照已覆盖的
    同步骤事件（详情页首屏快照初始化后防重复渲染）。
    """
    orch = _get_orchestrator()

    async def event_gen():
        async for event in orch.stream_events(taskId, last_seq=lastSeq):
            # 断连感知：uvicorn 对已断开的 TCP 连接 write 失败不抛异常（仅 asyncio
            # 打 warning：socket.send() raised exception）→ EventSourceResponse 会
            # 持续向死连接推送刷屏。每事件轮询 is_disconnected，断开即退出，触发
            # subscribe 的 finally 注销订阅（队列不再被 publish 喂数据）
            if await request.is_disconnected():
                return
            # 裸 JSON 字符串：EventSourceResponse 会自动加 "data: " 前缀并补
            # "\n\n"，若此处再 yield format_event 的成品会变成 "data: data: ..."
            yield json.dumps(event, ensure_ascii=False)

    return EventSourceResponse(event_gen(), ping=None)


# ═══════════════════════════════════════════════════════════════════
# 文件系统端点（SWP3-C，WP3 §2.2）
#
# - GET  /api/fs/tree?path=&recursive=  → {path, entries:[{name,type,size?}], truncated?}
#   （P1-8 realpath 校验 = tool_security.safe_resolve 同规则；FSTREE_HIDDEN 过滤
#     node_modules/.git/dist/.dc_tmp；recursive=true 全树节点上限 2000，超限截断
#     置 truncated:true）
# - GET  /api/fs/file?path=             → {path, content, mtime, size}
#   （mtime = 文件修改时间戳，前端 baseMtime 来源；2MB 上限 → 413，UTF-8）
# - PUT  /api/fs/file {path, content, baseMtime?}
#   → {status:"ok", path, size}；baseMtime 乐观锁（V-11）：不匹配 → 409
#     {ok:false, error:"file changed elsewhere"}
# ═══════════════════════════════════════════════════════════════════

FSTREE_HIDDEN = {"node_modules", ".git", "dist", ".dc_tmp"}  # WP4-4：后端过滤，前端不渲染

FS_FILE_MAX_BYTES = 2 * 1024 * 1024  # 2MB 上限（/api/fs/file 读取，WP3 §2.2）
FS_TREE_MAX_NODES = 2000             # recursive=true 全树节点上限


def _fs_resolve(path: str) -> str:
    """fs 端点统一路径解析：safe_resolve 同规则（P1-8 realpath 校验），越界 400。"""
    try:
        return safe_resolve(PROJECT_ROOT, path)
    except ValueError:
        raise HTTPException(400, detail="path outside project root")


@rest_app.get("/api/fs/browse")
async def fs_browse(path: str = ""):
    """目录选择浏览（2026-08-26 创建流程自定义工作目录用）：只读列出 path 的
    下一级目录/文件；path 空 → Windows 盘符列表。任意绝对路径可浏览（本地单
    用户工具，与 projectRoot 配置同信任级别）；目录不存在 → 404。
    现有 fs/tree 被 safe_resolve(PROJECT_ROOT) 限制在 workspace 内，自定义
    目录在其外（如 E:\code\ProjectNoChinese\...），故独立端点。"""
    if not path:
        drives = []
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if os.path.exists(f"{c}:\\"):
                drives.append({"name": f"{c}:", "type": "dir"})
        return {"path": "", "entries": drives}
    full = os.path.realpath(path)
    if not os.path.isdir(full):
        raise HTTPException(404, detail="目录不存在")
    entries = []
    for name in sorted(os.listdir(full),
                       key=lambda n: (not os.path.isdir(os.path.join(full, n)), n)):
        p = os.path.join(full, name)
        entries.append({"name": name,
                        "type": "dir" if os.path.isdir(p) else "file"})
    return {"path": full, "entries": entries}


@rest_app.get("/api/fs/tree")
async def fs_tree(path: str = "", recursive: bool = False):
    """文件树（单层懒加载 / recursive 全树，WP3 §2.2）。"""
    full = _fs_resolve(path)
    if not os.path.isdir(full):
        raise HTTPException(404, detail="目录不存在")
    entries = []
    truncated = False
    if recursive:
        # 全树：相对 root 的 posix 路径作 name（前端文件树/编辑器 mtime 检查按路径匹配）；
        # 节点上限 2000，超限截断置 truncated:true
        for dp, dirs, files in os.walk(full):
            dirs[:] = sorted(d for d in dirs if d not in FSTREE_HIDDEN)
            for name in sorted(dirs + files):
                if len(entries) >= FS_TREE_MAX_NODES:
                    truncated = True
                    break
                p = os.path.join(dp, name)
                rel = os.path.relpath(p, full).replace("\\", "/")
                if os.path.isdir(p):
                    entries.append({"name": rel, "type": "dir"})
                else:
                    entries.append({"name": rel, "type": "file", "size": os.path.getsize(p)})
            if truncated:
                break
    else:
        # 单层：name = 文件名；FSTREE_HIDDEN 过滤；目录在前、名字升序
        for name in sorted(os.listdir(full), key=lambda n: (not os.path.isdir(os.path.join(full, n)), n)):
            if name in FSTREE_HIDDEN:
                continue
            p = os.path.join(full, name)
            if os.path.isdir(p):
                entries.append({"name": name, "type": "dir"})
            else:
                entries.append({"name": name, "type": "file", "size": os.path.getsize(p)})
    return {"path": path or ".", "entries": entries, "truncated": truncated}


@rest_app.get("/api/fs/file")
async def fs_file(path: str = ""):
    """读取文件：{path, content, mtime, size}；>2MB → 413（WP4-4 大文件提示）。"""
    full = _fs_resolve(path)
    if not os.path.isfile(full):
        raise HTTPException(404, detail="文件不存在")
    size = os.path.getsize(full)
    if size > FS_FILE_MAX_BYTES:
        raise HTTPException(
            413,
            detail=f"文件超过 2MB 上限（{size} bytes），请在本地编辑",
        )
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return {"path": path, "content": content, "mtime": os.path.getmtime(full), "size": size}


@rest_app.put("/api/fs/file")
async def fs_write(payload: dict):
    """写文件：baseMtime 乐观锁（V-11）——不匹配 → 409 {ok:false, error:"file changed elsewhere"}。"""
    path = payload.get("path", "")
    content = payload.get("content", "")
    base_mtime = payload.get("baseMtime")
    full = _fs_resolve(path)
    if os.path.exists(full) and base_mtime is not None:
        try:
            current_mtime = os.path.getmtime(full)
        except OSError:
            current_mtime = None
        if current_mtime is not None and float(base_mtime) != current_mtime:
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "file changed elsewhere"},
            )
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return {"status": "ok", "path": path, "size": len(content.encode("utf-8"))}


# ═══════════════════════════════════════════════════════════════════
# 配置端点（SWP3-C，WP3 §2.2）
#
# - GET /api/config → {baseUrl, lightModel, powerModel, projectRoot, port, host, hasApiKey}
#   （apiKey 不回传，仅 hasApiKey 布尔——§3.1 旧规则；port/host 数据来源 C12：
#    env > config.json > 默认 8501/0.0.0.0，config.py 已收敛）
# - PUT /api/config {baseUrl?, apiKey?, lightModel?, powerModel?, projectRoot?}
#   → {status:"ok"}；apiKey 空串保留旧值；非法 projectRoot（非字符串/空/目录不存在）→ 400
# - POST /api/config/test-llm → {ok:boolean, error?, model?}
#   （V-L1：依次测试 lightModel 与 powerModel，任一失败返回 {ok:false, error, model}；
#    V-L7：每次调用实时读 config.json）
# ═══════════════════════════════════════════════════════════════════


def _validate_project_root(value) -> str:
    """校验并规范化 projectRoot：非字符串/空 → 400；相对路径按 config.json 所在目录
    解析（与 config._resolve_project_root 同规则）；目录不存在 → 400。"""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, detail="projectRoot 不能为空")
    expanded = os.path.expanduser(value.strip())
    if os.path.isabs(expanded):
        candidate = os.path.abspath(expanded)
    else:
        candidate = os.path.abspath(
            os.path.join(os.path.dirname(config.CONFIG_PATH), expanded)
        )
    if not os.path.isdir(candidate):
        raise HTTPException(400, detail=f"projectRoot 目录不存在: {value}")
    return candidate


@rest_app.get("/api/config")
async def get_config():
    """读取 LLM/工作区配置（apiKey 掩码：仅返回 hasApiKey/hasLightApiKey/hasPowerApiKey，
    不回传明文）。2026-08-23：light/power 独立端点与 Key + 六项价格。"""
    llm = config.get_llm_config()
    return {
        "baseUrl": llm["base_url"],
        "lightModel": llm["light_model"],
        "powerModel": llm["power_model"],
        "lightBaseUrl": llm["light_base_url"],
        "powerBaseUrl": llm["power_base_url"],
        "hasLightApiKey": bool(llm["light_api_key"]),
        "hasPowerApiKey": bool(llm["power_api_key"]),
        "lightInputPrice": llm["light_input_price"],
        "lightCachedPrice": llm["light_cached_price"],
        "lightOutputPrice": llm["light_output_price"],
        "powerInputPrice": llm["power_input_price"],
        "powerCachedPrice": llm["power_cached_price"],
        "powerOutputPrice": llm["power_output_price"],
        "projectRoot": config.PROJECT_ROOT,
        "port": config.PORT,
        "host": config.HOST,
        "hasApiKey": bool(llm["api_key"]),
        "contextWindow": config.get_context_window(),  # Token 展示：上下文窗口总容量
        "channelType": llm.get("channel_type", ""),  # 2026-08-19：newapi_channel_conn 等通道类型（llmChannel 配置）
        "hasChannel": bool(llm.get("channel_type")),  # llmChannel 已配置（key 不掩码回传，同 apiKey 策略）
    }


@rest_app.put("/api/config")
async def put_config(payload: dict):
    """保存配置（WP3 §2.2）：apiKey/lightApiKey/powerApiKey 空串保留旧值（write_config 内处理）。"""
    allowed = {"baseUrl", "apiKey", "lightModel", "powerModel", "projectRoot", "contextWindow",
               "llmChannel", "lightBaseUrl", "lightApiKey", "powerBaseUrl", "powerApiKey",
               "lightInputPrice", "lightCachedPrice", "lightOutputPrice",
               "powerInputPrice", "powerCachedPrice", "powerOutputPrice"}
    unknown = set(payload) - allowed
    if unknown:
        raise HTTPException(400, detail=f"未知配置字段: {sorted(unknown)}")
    if payload.get("projectRoot") is not None:
        _validate_project_root(payload["projectRoot"])
    if payload.get("contextWindow") is not None:
        v = payload["contextWindow"]
        if not isinstance(v, int) or isinstance(v, bool) or v < 1000:
            raise HTTPException(400, detail="contextWindow 必须是 ≥1000 的整数")
    # 2026-08-23：六项价格校验（非负数字；bool 拒绝）
    for pk in ("lightInputPrice", "lightCachedPrice", "lightOutputPrice",
               "powerInputPrice", "powerCachedPrice", "powerOutputPrice"):
        if payload.get(pk) is not None:
            v = payload[pk]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                raise HTTPException(400, detail=f"{pk} 必须是非负数字")
    # llmChannel：接受 dict 或 JSON 字符串（New API 通道导出）；空串/None 清除
    lc = payload.get("llmChannel")
    if lc is not None and not isinstance(lc, (str, dict)):
        raise HTTPException(400, detail="llmChannel 必须是 JSON 字符串或对象")
    if isinstance(lc, str) and lc.strip():
        try:
            json.loads(lc)
        except json.JSONDecodeError:
            raise HTTPException(400, detail="llmChannel 不是合法 JSON")
    update = {k: v for k, v in payload.items() if v is not None}
    config.write_config(update)
    return {"status": "ok"}


@rest_app.post("/api/config/test-llm")
async def test_llm():
    """测试 LLM 连接（V-L1/V-L7）：实时读配置，依次测试 light 组与 power 组
    （2026-08-23 拆分：各自端点/Key），任一失败返回 {ok:false, error, model}。"""
    cfg = config.get_llm_config()
    groups = [
        ("light", cfg.get("light_base_url"), cfg.get("light_api_key"), cfg.get("light_model")),
        ("power", cfg.get("power_base_url"), cfg.get("power_api_key"), cfg.get("power_model")),
    ]
    for name, base_url, api_key, model in groups:
        if not model:
            return {"ok": False, "error": f"{name} 组模型未配置，请检查设置页", "model": model}
        if not base_url or not api_key:
            return {"ok": False, "error": f"{name} 组未配置 Base URL / API Key，请检查设置页", "model": model}
        try:
            client = LlmClient({"base_url": base_url, "api_key": api_key, "model": model})
            async for _ in client.stream_chat(
                [{"role": "user", "content": "ping"}], None, None
            ):
                pass
        except LlmError as e:
            return {"ok": False, "error": e.message, "model": model}
        except Exception as e:  # 本地网络/连接类异常也归失败（不 500）
            return {"ok": False, "error": str(e)[:200], "model": model}
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# 生命周期（SWP3-C，WP3 T3.7）
#
# - _recover_stale_tasks：启动恢复（V1/N16 容错）——任务级 status=active 且步骤卡在
#   active（上次进程崩溃/被杀残留）→ 置 stopped；paused（gate 审查等待等）与
#   completed/abandoned 任务不动；不自动重启执行循环（Web 版语义：人工 resume + start）。
# - _graceful_shutdown：优雅退出（B3）——中止全部 running 任务（orchestrator abort）
#   + 步骤 active→stopped + 任务 pause_task；server.py 的 SIGINT handler 调用。
# ═══════════════════════════════════════════════════════════════════


async def _recover_stale_tasks() -> int:
    """启动恢复：active 步骤残留 → pending（自动继续执行，避免重启后正在运行的
    步骤被置 stopped 而被 get_next_steps 跳过——重启场景应自动续跑，与执行循环
    的 stuck 兜底语义一致）。返回恢复的步骤数。"""
    storage = _get_storage()
    sm = _get_state_machine()
    tasks = await storage.list_tasks()
    recovered = 0
    for t in tasks:
        if t.get("status") != "active":
            continue  # paused 不动（gate 审查等待/用户暂停）；completed/abandoned 不动
        # 2026-08-21：include_hidden=True——monitor/review/report 是实体行，
        # 残留 active 同样阻塞执行循环（与 _park_active_steps 同语义）；
        # list_tasks 默认过滤隐藏行，必须换 get_task 全量读取
        full = await storage.get_task(t["id"], include_hidden=True) or {}
        for s in full.get("steps", []):
            if s.get("status") == "active":
                try:
                    await sm.advance_step(t["id"], s["step_id"], "pending")
                    recovered += 1
                except ValueError:
                    continue  # 状态机拒绝（理论上不可达：active→pending 合法）不中断扫描
    if recovered:
        logger.info(f"[DC:recover] {recovered} stale active step(s) → pending (auto-resume)")
    return recovered


async def _auto_resume_tasks() -> int:
    """启动后自动恢复未完成任务执行（V-13，优雅重启/崩溃重启闭环）。

    判定：任务 active 且已有执行历史（存在非 pending 步骤，如 completed/stopped）
    → 自动 start（执行循环拾取 pending 步骤续跑）。
    不自动启动：新建未启动任务（全 pending——保持 B4 手动启动语义）、
    空步骤任务（初始编排中——用户启动时执行循环会等待编排完成）。
    返回恢复的任务数。幂等：start_task 对已 running 任务直接返回 ok。
    """
    storage = _get_storage()
    orch = _get_orchestrator()
    resumed = 0
    try:
        tasks = await storage.list_tasks()
    except Exception:
        logger.exception("[DC:recover] list_tasks failed for auto-resume")
        return 0
    for t in tasks:
        if t.get("status") != "active":
            continue
        steps = t.get("steps", [])
        if not steps:
            continue  # 初始编排中：保持等待用户启动
        if any(s.get("status") != "pending" for s in steps):
            try:
                await orch.start_task(t["id"])
                resumed += 1
                logger.info(f"[DC:recover] task {t['id']} auto-resumed")
            except Exception:
                logger.exception(f"[DC:recover] auto-resume failed for task {t['id']}")
    return resumed


async def _graceful_shutdown() -> None:
    """优雅退出：中止全部 running 任务 + 置 stopped（B3/T3.7）。

    遍历 orchestrator._running（running 归属 orchestrator 维护，C4），逐个
    abort（置取消标志，等执行循环退出）→ 步骤 active→stopped（含并行组全部
    active 步骤）→ 任务 pause_task。SIGINT 时由 server.py 调用；测试直接调用。
    """
    orch = _get_orchestrator()
    storage = _get_storage()
    sm = _get_state_machine()
    running_ids = [tid for tid, running in list(orch._running.items()) if running]
    for task_id in running_ids:
        try:
            await orch.abort(task_id)
        except Exception:
            logger.exception(f"[DC:shutdown] abort task {task_id} failed")
        task = await storage.get_task(task_id)
        if not task:
            continue
        for s in task.get("steps", []):
            if s.get("status") == "active":
                try:
                    await sm.advance_step(task_id, s["step_id"], "stopped")
                except ValueError:
                    pass
        try:
            await sm.pause_task(task_id)
        except ValueError:
            pass
        logger.info(f"[DC:shutdown] task {task_id} stopped during graceful shutdown")

    # 2026-08-25（Hindsight 记忆模块 B-2）：关闭记忆存储连接
    try:
        from .memory import close_memory
        close_memory()
    except Exception:
        logger.exception("[DC:shutdown] close_memory failed")


# ─── Memory（Hindsight 记忆模块，2026-08-25 B-2）─────────────────
# 记忆库所有 LLM 调用统一 light tier（用户要求）；disabled 时全部返回
# {"enabled": False} 或 {"error": "memory disabled"}


@rest_app.get("/api/memory/stats")
async def memory_stats():
    ms = _get_memory_storage()
    if ms is None:
        return {"enabled": False}
    return ms.get_stats()


@rest_app.post("/api/memory/retain")
async def memory_retain(body: dict):
    """Manual retain for testing. body: {text, tags, source_ref}（LLM 走 light）"""
    ms = _get_memory_storage()
    if ms is None:
        return {"error": "memory disabled"}
    from .memory import get_retainer
    from .config import get_memory_config
    retainer = get_retainer(ms, _get_orchestrator()._make_llm_client("light"), get_memory_config())
    bank_id = ms.get_or_create_bank_for_project(body.get("project_root", ""))
    result = await retainer.retain(
        bank_id=bank_id,
        text=body.get("text", ""),
        tags=body.get("tags", []),
        source_ref=body.get("source_ref", {}),
    )
    return result


@rest_app.post("/api/memory/recall")
async def memory_recall(body: dict):
    """Manual recall for testing. body: {query, max_tokens, budget}"""
    ms = _get_memory_storage()
    if ms is None:
        return {"error": "memory disabled"}
    from .memory import get_recaller
    from .config import get_memory_config
    recaller = get_recaller(ms, get_memory_config())
    bank_id = ms.get_or_create_bank_for_project(body.get("project_root", ""))
    result = await recaller.recall(
        bank_id=bank_id,
        query=body.get("query", ""),
        max_tokens=body.get("max_tokens", 4096),
        budget=body.get("budget", "mid"),
    )
    return result


@rest_app.post("/api/memory/consolidate")
async def memory_consolidate(body: dict = None):
    """Trigger consolidation manually（LLM 走 light）。"""
    ms = _get_memory_storage()
    if ms is None:
        return {"error": "memory disabled"}
    from .memory import get_consolidator
    from .config import get_memory_config
    consolidator = get_consolidator(ms, _get_orchestrator()._make_llm_client("light"), get_memory_config())
    bank_id = ms.get_or_create_bank_for_project(body.get("project_root", "") if body else "")
    result = await consolidator.consolidate(bank_id)
    return result


@rest_app.get("/api/memory/observations")
async def memory_observations(project_root: str = ""):
    ms = _get_memory_storage()
    if ms is None:
        return {"error": "memory disabled"}
    bank_id = ms.get_or_create_bank_for_project(project_root)
    return {"observations": ms.list_observations(bank_id)}


@rest_app.get("/api/memory/facts")
async def memory_facts(project_root: str = "", page: int = 1, page_size: int = 50):
    ms = _get_memory_storage()
    if ms is None:
        return {"error": "memory disabled"}
    bank_id = ms.get_or_create_bank_for_project(project_root)
    facts, total = ms.list_facts(bank_id, page, page_size)
    return {"facts": facts, "total": total, "page": page, "page_size": page_size}


@rest_app.get("/api/memory/bank")
async def memory_bank(project_root: str = ""):
    ms = _get_memory_storage()
    if ms is None:
        return {"enabled": False}
    bank_id = ms.get_or_create_bank_for_project(project_root)
    conn = ms._get_conn()
    row = conn.execute("SELECT * FROM memory_banks WHERE id = ?", (bank_id,)).fetchone()
    if row:
        return dict(row)
    return {"bank_id": bank_id, "config": {}}


@rest_app.post("/api/memory/reflect")
async def memory_reflect(body: dict):
    """手动调用 reflect（Phase 5）（LLM 走 light）。"""
    ms = _get_memory_storage()
    if ms is None:
        return {"error": "memory disabled"}
    from .memory import get_reflector
    from .config import get_memory_config
    reflector = get_reflector(ms, _get_orchestrator()._make_llm_client("light"), get_recaller_inst(), get_memory_config())
    bank_id = ms.get_or_create_bank_for_project(body.get("project_root", ""))
    result = await reflector.reflect(
        bank_id=bank_id,
        query=body.get("query", ""),
        tags=body.get("tags"),
    )
    return result


def get_recaller_inst():
    """reflect 路由用的 recaller（懒建，避免重复实例）。"""
    from .memory import get_recaller
    from .config import get_memory_config
    return get_recaller(_get_memory_storage(), get_memory_config())


# ═══════════════════════════════════════════════════════════════════
# 全局异常处理（T2.3 步骤 8，C3/L2/L3 修订）
#
# ValueError / KeyError 一律返回 400 {"detail": str(e)}，不再 500：
# - 非法 decision（如 advance 传 changes_requested）
# - 缺参 KeyError（如 prepare 缺少 task_id）
# 状态敏感端点的显式 _state_error 转换优先于本兜底（行为不变）。
# ═══════════════════════════════════════════════════════════════════


@rest_app.exception_handler(ValueError)
async def _valueerror_handler(request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@rest_app.exception_handler(KeyError)
async def _keyerror_handler(request, exc: KeyError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})
