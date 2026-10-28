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
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from starlette.responses import Response

from . import config
from .config import DB_PATH, PROJECT_ROOT, CORS_ORIGINS
from .storage import create_storage, StorageAdapter
from .state_machine.state_machine import StateMachine
from .monitor_context import MonitorContext
from .step_context import (StepContext, get_step_temp_dir, get_task_workspace,
                           strip_key_findings_block,
                           STEP_TYPE_MODEL_TIER as _STEP_TYPE_MODEL_TIER,
                           compute_task_score, validate_score_params,
                           score_context_section,
                           CODE_DIAGRAM_ASSET, get_project_key, get_project_path,
                           load_project_asset)
from .prompts import load_prompt, rules_dir, templates_dir
from .prompts.registry import is_hidden_step, is_virtual_step, prompt_for_step, load_role_prompt
from .tool_security import safe_resolve
from .graceful import graceful
from .brain.orchestrator import (Orchestrator, OrchestratorBusyError, _StepGracefulDrain,
                                 _FLOW_REPORT_FILENAME, _FLOW_REPORT_ANCHOR_CHARS)
from .brain.llm_client import LlmClient, LlmError
from .brain.mcp_client import mcp_manager, McpError
from .auth import AuthMiddleware, _auth_manager

logger = logging.getLogger(__name__)


rest_app = FastAPI(title="DimensionCoding API", version="0.2.0")

rest_app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
rest_app.add_middleware(AuthMiddleware)


_storage: Optional[StorageAdapter] = None
_state_machine: Optional[StateMachine] = None
_monitor_context: Optional[MonitorContext] = None
_step_context: Optional[StepContext] = None
_orchestrator: Optional[Orchestrator] = None
_sse_hub: Optional[Any] = None


def _get_storage() -> StorageAdapter:
    global _storage
    if _storage is None:
        _storage = create_storage({"db_path": DB_PATH})
    return _storage


def _get_state_machine() -> StateMachine:
    global _state_machine
    if _state_machine is None:
        _state_machine = StateMachine(
            storage=_get_storage(),
            notify=lambda tid, sid=None, st=None:
                _get_orchestrator()._publish_state_update(tid, sid, st))
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
    global _sse_hub
    if _sse_hub is None:
        from .brain.sse_hub import SseHub
        _sse_hub = SseHub()
    return _sse_hub


def _get_orchestrator() -> Orchestrator:
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




def _state_error(e: Exception) -> HTTPException:
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




@rest_app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.2.0"}


@rest_app.get("/api/prompt/{name}")
async def get_prompt(name: str):
    try:
        content = load_prompt(name)
    except FileNotFoundError:
        raise HTTPException(404, f"Prompt not found: {name}")
    return {"name": name, "content": content}




@rest_app.post("/api/task")
async def create_task(payload: dict):
    task_type = payload.get("task_type", "custom")
    title = payload.get("title", "")
    description = payload.get("description", "")
    mode = payload.get("mode") or "flow"
    if mode not in ("flow", "single"):
        mode = "flow"
    if not title.strip() and description.strip():
        title = description.strip()[:20]
    epic_id = payload.get("epic_id")
    assignee = payload.get("assignee", "")
    payload.get("auto_start", True)
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
        mode=mode,
    )
    try:
        os.makedirs(workspace_dir or os.path.join(PROJECT_ROOT, task_id),
                    exist_ok=True)
    except OSError:
        logger.exception(f"create_task: 创建任务根失败 task_id={task_id}")

    if mode == "single":
        await _get_storage().ensure_step(task_id, "chat", "对话", "executor")
        if description.strip():
            try:
                await sm.handle_step_intervention(task_id, "chat", "send", description)
                await _get_orchestrator().start_task(task_id)
            except Exception:
                logger.exception(f"create_task: single 首句启动失败 task_id={task_id}")
    else:
        try:
            orch = _get_orchestrator()
            await orch._ensure_initial_orchestration(task_id)
            await orch.start_task(task_id)
        except Exception:
            pass

    try:
        asyncio.create_task(_auto_generate_title(task_id, description))
    except Exception:
        pass

    return {
        "task_id": task_id,
        "task_type": task_type,
        "title": title,
        "steps": [],
        "mode": mode,
    }


async def _auto_generate_title(task_id: str, description: str) -> None:
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
            return
        await _get_storage().update_task(task_id, {"title": title})
        try:
            await _get_orchestrator()._publish_task_updated(task_id)
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"[DC:REST] auto title generated for {task_id}: {title}")
    except Exception:  # noqa: BLE001
        pass


_LIGHT_STEP_FIELDS = ("description", "process_template", "process_read_rules")


def _strip_step_heavy_fields(steps: list) -> list:
    for s in steps:
        for k in _LIGHT_STEP_FIELDS:
            s.pop(k, None)
    return steps


@rest_app.get("/api/tasks")
async def get_project_overview():
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
        "available_task_types": [],
    }


_DECISION_PKG_RE = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```")


async def _get_decision_pkg(storage, task_id: str, step_id: str) -> Optional[dict]:
    try:
        raw = await storage.get_artifact(task_id, step_id, "decision")
    except Exception:  # noqa: BLE001
        return None
    if not raw or not raw.get("content"):
        return None
    try:
        obj = json.loads(raw["content"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("options"), list):
        return None
    return obj


async def _get_pending_question(storage, task_id: str, step_id: str) -> Optional[dict]:
    try:
        raw = await storage.get_artifact(task_id, step_id, "question")
    except Exception:  # noqa: BLE001
        return None
    if not raw or not raw.get("content"):
        return None
    try:
        obj = json.loads(raw["content"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("question"), str):
        return None
    if not obj["question"].strip():
        return None
    return obj


async def _step_has_decision_pkg(storage, task_id: str, step_id: str) -> bool:
    if await _get_decision_pkg(storage, task_id, step_id):
        return True
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

    step_message_counts: dict[str, int] = {}
    steps = task.get("steps", [])
    for s in steps:
        sid = s.get("step_id", "")
        cnt = await storage.count_step_messages(task_id, sid)
        if cnt:
            step_message_counts[sid] = cnt
        if s.get("human_attention") == "gate":
            s["has_decision_pkg"] = await _step_has_decision_pkg(storage, task_id, sid)

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
    orch = _get_orchestrator()
    if orch.is_running(task_id):
        await orch.abort(task_id)
        await orch.wait_stopped(task_id)
    storage = _get_storage()
    await storage.delete_task(task_id)
    return {"status": "deleted", "task_id": task_id}


@rest_app.post("/api/task/{task_id}/switch-to-flow")
async def switch_to_flow(task_id: str):
    orch = _get_orchestrator()
    storage = _get_storage()

    task = await storage.get_task(task_id, include_hidden=True)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task.get("mode") != "single":
        raise _state_error(ValueError("仅单步骤对话任务可切换为多步骤流程"))

    if orch.is_running(task_id):
        await orch.abort(task_id)
        await orch.wait_stopped(task_id)

    msgs = await storage.get_step_messages(task_id, "chat")
    parts: list[str] = [
        f"以下是本任务此前的单步骤对话完整记录（用户与 AI 的一来一往，"
        f"共 {len(msgs)} 条）。请基于对话内容理解用户目标，编排后续流程。"
        "此前产生的文件产物在任务工作目录与 .dc_tmp/<tid>/chat/artifacts/ 下，"
        "路径仍有效，可直接用 dcflow_read_file 读取。",
        "",
    ]
    for m in msgs or []:
        role = m.get("role", "")
        content = str(m.get("content") or "").strip()
        if role == "user":
            if content:
                parts.append(f"[用户]\n{content}")
        elif role == "assistant":
            if m.get("tool_calls"):
                name = m.get("tool_name") or ""
                desc = ""
                try:
                    _tc = json.loads(m.get("tool_calls") or "[]")
                    if _tc:
                        _fn = (_tc[0].get("function") or {})
                        name = _fn.get("name") or name
                        _args = json.loads(_fn.get("arguments") or "{}")
                        desc = str(_args.get("description") or _args.get("file_path")
                                   or _args.get("pattern") or "")[:200]
                except (ValueError, TypeError):
                    pass
                out = str(m.get("tool_output") or "")[:500]
                parts.append(f"[AI 工具] {name} {desc}\n输出: {out}")
            elif content:
                parts.append(f"[AI]\n{content}")
    try:
        raw_iv = await storage.get_artifact(task_id, "chat", "intervention")
        if raw_iv and raw_iv.get("content"):
            ivs = json.loads(raw_iv["content"])
            iv_list = ivs if isinstance(ivs, list) else [ivs]
            for iv in iv_list:
                if isinstance(iv, dict) and iv.get("message"):
                    parts.append(f"[用户（发送中未送达）]\n{iv['message']}")
    except (ValueError, TypeError):
        pass
    transcript = "\n\n".join(p for p in parts if p.strip())
    await storage.save_artifact(task_id, "_flow", "chat_history",
                                transcript, "text")

    await storage.update_task(task_id, {"mode": "flow"})

    await storage.remove_steps(task_id, ["chat"])
    conn = await storage._get_conn()
    conn.execute("DELETE FROM stream_chunks WHERE task_id = ? AND step_id = 'chat'",
                 (task_id,))
    conn.commit()
    orch._prep_cache.pop(f"{task_id}:chat", None)

    await orch._ensure_initial_orchestration(task_id)
    await orch.start_task(task_id)

    try:
        await orch._publish_task_updated(task_id)
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"[DC:REST] task {task_id} switched single → flow (in-place, same tid)")
    return {"status": "ok", "task_id": task_id}


@rest_app.post("/api/task/{task_id}/pause")
async def pause_task(task_id: str, payload: dict = None):
    if payload:
        payload.get("pause_level", "gate")
    sm = _get_state_machine()
    orch = _get_orchestrator()
    storage = _get_storage()
    try:
        await sm.pause_task(task_id)
        await orch.abort(task_id)
        task = await storage.get_task(task_id, include_hidden=True)
        if task:
            for s in task.get("steps", []):
                if s.get("status") == "active":
                    await sm.advance_step(task_id, s["step_id"], "pending",
                                          tolerant=True)
    except ValueError as e:
        raise _state_error(e)
    return {"status": "ok", "task_id": task_id}




@rest_app.post("/api/task/{task_id}/start")
async def start_task(task_id: str):
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


@rest_app.post("/api/task/{task_id}/auto-approve")
async def set_auto_approve(task_id: str, payload: dict):
    storage = _get_storage()
    task = await storage.get_task(task_id)
    if not task:
        raise HTTPException(404, detail="任务不存在")
    enabled = 1 if payload.get("enabled") else 0
    await storage.update_task(task_id, {"auto_approve": enabled})
    try:
        await _get_orchestrator()._publish_task_updated(task_id)
    except Exception:  # noqa: BLE001
        pass
    return {"status": "ok", "task_id": task_id, "auto_approve": bool(enabled)}


@rest_app.get("/api/task/{task_id}/code-diagram")
async def get_code_diagram(task_id: str):
    storage = _get_storage()
    asset = await load_project_asset(storage, task_id, CODE_DIAGRAM_ASSET) or {}
    return {
        "task_id": task_id,
        "content": asset.get("content") or "",
        "project_key": await get_project_key(storage, task_id),
        "project_path": asset.get("project_path") or await get_project_path(storage, task_id),
        "updated_at": asset.get("updated_at") or "",
        "source_task_id": asset.get("source_task_id") or "",
    }


def _validate_completion(completion: Any) -> str:
    if not isinstance(completion, dict):
        return ("completion 必须是对象，正确写法："
                'completion = {"done": ["已完成项→落地位置"], '
                '"verified": ["验证命令→实际结果"]}（done/verified 至少一项）')

    def _items(v: Any) -> list:
        return v if isinstance(v, list) else []

    done = _items(completion.get("done"))
    verified = _items(completion.get("verified"))
    unfinished = _items(completion.get("unfinished"))
    blocked = completion.get("blocked") if isinstance(completion.get("blocked"), dict) else {}
    if not done and not verified and not unfinished and not blocked:
        return ("完成声明必须结构化：done/verified 至少一项，"
                "或显式声明 unfinished/blocked。正确写法："
                'completion = {"done": ["已完成项→落地位置"], '
                '"verified": ["验证命令→实际结果摘要"]}')
    if unfinished:
        preview = "、".join(str(x) for x in unfinished[:3])
        return (f"存在未完成项（{preview}），不得以半途状态结束——"
                "unfinished 只写**本步骤职责范围内**、尚未完成的事项，这些必须继续完成；"
                "不属于本步骤的后续工作/其他角色事项（由后续步骤、编排者或人工承接）"
                "请从 unfinished 移除并写进 summary 的建议；本步已尝试但未达成、已登记为"
                "受限/风险的结论写进 done（含登记位置与替代判据），不要留在 unfinished；"
                "确属受阻走 blocked 申报（须附 tried 失败证据且 variants_left 为空）")
    vl = _items(blocked.get("variants_left"))
    if vl:
        preview = "、".join(str(x) for x in vl[:3])
        return (f"存在未尝试变体（{preview}），不得以受阻为由结束——"
                "继续尝试变体，或写明放弃每个变体的理由"
                "（conclusion_level=route 需全部变体证据）")
    return ""


def _validate_approval(approval: Any) -> str:
    if not isinstance(approval, dict):
        return ("gate 步骤 step_done 必须携带 approval 对象（options 决策选项数组 + "
                "doc_path 方案文档路径（绝对或相对均可）；无方案文档的 gate 传空字符串）")
    options = approval.get("options")
    if not isinstance(options, list) or not options:
        return "approval.options 必须是非空数组（每项含 option/cost/risk/pros）"
    for i, o in enumerate(options):
        if not isinstance(o, dict) or not str(o.get("option") or "").strip():
            return f"approval.options[{i}] 必须是含非空 option 文本的对象"
    if not isinstance(approval.get("doc_path"), str):
        return ("approval.doc_path 必须是字符串（方案文档路径，绝对或相对均可；"
                "无方案文档的 gate 传空字符串）")
    return ""


async def _resolve_gate_doc(task_id: str, doc_path: str) -> str:
    p = str(doc_path or "").strip()
    if not p:
        return ""
    try:
        root = (await get_task_workspace(_get_storage(), task_id)
                if task_id else PROJECT_ROOT)
    except Exception:  # noqa: BLE001
        root = PROJECT_ROOT
    try:
        return tool_resolve(root, p)
    except ValueError:
        pass
    if os.path.isabs(p):
        return os.path.normpath(p)
    for base in (root, PROJECT_ROOT):
        cand = os.path.normpath(os.path.join(base, p))
        if os.path.isfile(cand):
            return cand
    return os.path.normpath(os.path.join(root, p))


async def _check_gate_doc_exists(task_id: str, doc_path: str) -> str:
    full = await _resolve_gate_doc(task_id, doc_path)
    if not full or not os.path.isfile(full):
        return (f"方案文档不存在：{doc_path}（用 dcflow_list_dir 确认实际路径后重填；"
                "无方案文档的 gate 传空字符串）")
    return ""


def _read_text_capped(path: str, max_chars: int) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(max_chars + 1)


@rest_app.post("/api/admin/graceful-restart")
async def graceful_restart(payload: dict):
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
    await asyncio.sleep(0.5)
    try:
        await graceful.wait_idle()
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
            await asyncio.sleep(1.0)
        os._exit(0)
    except Exception:
        logger.exception("[DC:graceful] graceful exec failed")


@rest_app.get("/api/step/{step_id}")
async def get_step_detail(step_id: str, task_id: str, limit: int = 200, before_seq: int = -1):
    storage = _get_storage()
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
    for _m in messages:
        _m.pop("reasoning", None)
    truncated = bool(messages and messages[0]["seq"] > 0)
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
        "decision": await _get_decision_pkg(storage, task_id, step_id),
        "pendingQuestion": await _get_pending_question(storage, task_id, step_id),
    }




@rest_app.get("/api/task/{task_id}/next-step")
async def get_next_step(task_id: str):
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
    task_id = payload["task_id"]
    step_id = payload["step_id"]

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
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    payload.get("conversation")

    storage = _get_storage()
    sm = _get_state_machine()

    task = await storage.get_task(task_id)
    step = next((s for s in task.get("steps", []) if s["step_id"] == step_id), None) if task else None
    if step and step.get("status") != "completed":
        await sm.advance_step(task_id, step_id, "completed")
        if step.get("type") not in ("monitor", "review", "report") \
                and not step_id.startswith("monitor-"):
            try:
                await storage.export_step_conversation(
                    task_id, step_id, get_step_temp_dir(task_id, step_id))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DC:REST] export step conversation failed "
                               f"task={task_id} step={step_id}: {e}")


    return {"status": "step_completed", "task_id": task_id, "step_id": step_id}


@rest_app.post("/api/step/save-conversation")
async def save_step_conversation(payload: dict):
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    conversation = payload.get("conversation")

    if conversation:
        await _get_storage().save_conversation(task_id, step_id, conversation)

    return {"status": "ok", "task_id": task_id, "step_id": step_id}


@rest_app.post("/api/step/message/append")
async def append_step_message(payload: dict):
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    message = payload.get("message", {})

    seq = await _get_storage().append_message(task_id, step_id, message)
    return {"status": "ok", "task_id": task_id, "step_id": step_id, "seq": seq}


@rest_app.get("/api/step/{step_id}/messages")
async def get_step_messages(step_id: str, task_id: str, after_seq: int = -1,
                            limit: int = -1, before_seq: int = -1):
    storage = _get_storage()
    if limit > 0 and after_seq >= 0:
        messages = await storage.get_step_messages(
            task_id, step_id, after_seq, limit=limit)
        max_seq = messages[-1]["seq"] if messages else after_seq
        total = None
        truncated = None
    elif limit > 0:
        latest = await storage.get_step_messages(task_id, step_id, limit=1)
        max_seq = latest[0]["seq"] if latest else -1
        messages = await storage.get_step_messages(
            task_id, step_id, limit=limit, before_seq=before_seq)
        total = await storage.count_step_messages(task_id, step_id)
        truncated = bool(messages and messages[0]["seq"] > 0)
    else:
        messages = await storage.get_step_messages(task_id, step_id, after_seq)
        max_seq = messages[-1]["seq"] if messages else (after_seq if after_seq >= 0 else -1)
        total = None
        truncated = None
    for _m in messages:
        _m.pop("reasoning", None)
    return {
        "task_id": task_id,
        "step_id": step_id,
        "messages": messages,
        "max_seq": max_seq,
        "after_seq": after_seq,
        "total": total,
        "truncated": truncated,
    }


@rest_app.post("/api/step/chunk/save")
async def save_stream_chunk(payload: dict):
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    chunk = payload.get("chunk", {})

    seq = await _get_storage().save_chunk(task_id, step_id, chunk)
    return {"status": "ok", "seq": seq}


@rest_app.get("/api/step/{step_id}/chunks")
async def get_stream_chunks(step_id: str, task_id: str, after_seq: int = -1):
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
    task_id = payload["task_id"]
    step_id = payload["step_id"]

    await _get_storage().clear_step_messages(task_id, step_id)
    return {"status": "ok"}


@rest_app.get("/api/artifact/{task_id}/{step_id}/intervention")
async def get_intervention_artifact(task_id: str, step_id: str):
    artifact = await _get_storage().get_artifact(task_id, step_id, "intervention")
    if artifact:
        return {"content": artifact["content"]}
    return {}


@rest_app.post("/api/step/messages/clear-intervention")
async def clear_intervention(payload: dict):
    task_id = payload["task_id"]
    step_id = payload["step_id"]

    try:
        await _get_storage().save_artifact(task_id, step_id, "intervention", "[]", "json")
    except Exception:
        pass
    return {"status": "ok"}


@rest_app.post("/api/artifact/save")
async def save_artifact(payload: dict):
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    artifact_type = payload["artifact_type"]
    content = payload["content"]
    content_format = payload.get("content_format", "json")

    await _get_storage().save_artifact(task_id, step_id, artifact_type, content, content_format)
    return {"status": "ok"}




_KILL_VERB_RES = (
    re.compile(r"\btaskkill\b", re.IGNORECASE),
    re.compile(r"\bkill\b", re.IGNORECASE),
    re.compile(r"\bstop-process\b", re.IGNORECASE),
    re.compile(r"\bremove-process\b", re.IGNORECASE),
)


def _is_kill_self_cmd(command: str, self_pid: int) -> bool:
    kill_verb = (any(r.search(command) for r in _KILL_VERB_RES)
                 or (re.search(r"\bwmic\b", command, re.IGNORECASE)
                     and re.search(r"\b(?:delete|terminate)\b", command, re.IGNORECASE)))
    if not kill_verb:
        return False
    pid_str = str(self_pid)
    if pid_str in command:
        return True
    if re.search(r"[/-]IM\s+python", command, re.IGNORECASE):
        return True
    if re.search(r"(?:-Name|name)\s*[=:]?\s*['\"]?python", command, re.IGNORECASE):
        return True
    if re.search(r"wmic\b", command, re.IGNORECASE) and re.search(r"python", command, re.IGNORECASE):
        return True
    return False


def _kill_self_block_message(self_pid: int) -> str:
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



_RC_TARGET_LIT_RE = re.compile(r"""(['"])([^'"\n]{0,300}?\.(?:log|txt))\1""",
                               re.IGNORECASE)
_RC_WRITE_RE = re.compile(
    r"open\s*\((?:[^()]|\([^()]*\))*['\"][waxb+]{1,3}['\"]|\.write\s*\(|"
    r"write_text\s*\(|write_bytes\s*\(|writelines\s*\(|os\.replace\s*\(|"
    r"os\.rename\s*\(|shutil\.(?:copyfile|copy|copy2|move)\s*\(|"
    r"logging\.FileHandler\s*\(|basicConfig\s*\([^)]*filename\s*=")
_RC_REDIRECT_RE = re.compile(
    r"""(?<![-<=])>>?(?![=&])\s*['"]?(?P<t>[^\s'"|&;]{1,300})""")
_RC_TEE_RE = re.compile(r"\|\s*tee\s+(?:-a\s+)?['\"]?(?P<t>[^\s'\"|&;]{1,300})")
_RC_LOG_TXT_END_RE = re.compile(r"\.(?:log|txt)$", re.IGNORECASE)
_RC_ASSIGN_LIT_RE = re.compile(
    r"""^\s*([A-Za-z_]\w*)\s*=\s*([rRbBuUfF]*)['"]([^'"\n]{1,300})['"]\s*(?:#.*)?$""")
_RC_ASSIGN_JOIN_RE = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*=\s*os\.path\.join\s*\(([^)]*)\)\s*(?:#.*)?$")
_PLAIN_STR_RE = re.compile(r"""^[rRbBuUfF]*['"]([^'"\n]{0,300})['"]$""")
_RC_ID_WRITE_RES = (
    re.compile(r"open\s*\(\s*([A-Za-z_]\w*)\s*,[^)]*?['\"][waxb+]{1,3}['\"]"),
    re.compile(r"([A-Za-z_]\w*)\.write_(?:text|bytes)\s*\("),
    re.compile(r"(?:os\.(?:replace|rename)|shutil\.(?:copyfile|copy|copy2|move))"
               r"\s*\([^()]*,\s*([A-Za-z_]\w*)\s*[,)]"),
    re.compile(r"logging\.FileHandler\s*\(\s*([A-Za-z_]\w*)"),
    re.compile(r"basicConfig\s*\([^()]*filename\s*=\s*([A-Za-z_]\w*)"),
)


def _rc_subst_fstring(body: str, consts: dict) -> Optional[str]:
    def _rep(m: "re.Match") -> str:
        v = consts.get(m.group(1))
        return v if v is not None else m.group(0)

    out = re.sub(r"\{(\w+)(?::[^{}]*)?\}", _rep, body)
    if "{" in out or "}" in out:
        return None
    return out


def _rc_split_args(text: str) -> list:
    args: list = []
    cur: list = []
    depth, q = 0, ""
    for ch in text:
        if q:
            cur.append(ch)
            if ch == q:
                q = ""
        elif ch in "'\"":
            q = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        args.append("".join(cur).strip())
    return [a for a in args if a]


def _rc_resolve_arg(arg: str, consts: dict) -> Optional[str]:
    m = _PLAIN_STR_RE.match(arg)
    if m:
        body = m.group(1)
        return _rc_subst_fstring(body, consts) if "{" in body else body
    if re.fullmatch(r"[A-Za-z_]\w*", arg):
        return consts.get(arg)
    return None


def _rc_paren_span(line: str, open_idx: int) -> Optional[tuple]:
    depth, q = 0, ""
    for k in range(open_idx, len(line)):
        ch = line[k]
        if q:
            if ch == q:
                q = ""
            continue
        if ch in "'\"":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return line[open_idx + 1:k], k
    return None


def _rc_enclosing_join(line: str, lit_start: int) -> Optional[int]:
    pos = line.rfind("os.path.join", 0, lit_start)
    if pos < 0:
        return None
    open_idx = line.find("(", pos + len("os.path.join"))
    if open_idx < 0 or open_idx >= lit_start:
        return None
    span = _rc_paren_span(line, open_idx)
    if span is None or span[1] < lit_start:
        return None
    return open_idx


def _rc_const_map(code: str) -> dict:
    consts: dict = {}
    lines = code.splitlines()
    for _ in range(2):
        for ln in lines:
            m = _RC_ASSIGN_LIT_RE.match(ln)
            if m:
                body = m.group(3)
                if "{" in body:
                    body = _rc_subst_fstring(body, consts)
                    if body is None:
                        continue
                consts[m.group(1)] = body
                continue
            m = _RC_ASSIGN_JOIN_RE.match(ln)
            if m:
                got: list = []
                for a in _rc_split_args(m.group(2)):
                    v = _rc_resolve_arg(a, consts)
                    if v is None:
                        break
                    got.append(v)
                if got:
                    consts[m.group(1)] = os.path.join(*got)
    return consts


def _rc_lit_sites(lines: list, i: int, consts: dict) -> list:
    out: list = []
    ln = lines[i]
    for m in _RC_TARGET_LIT_RE.finditer(ln):
        jpos = _rc_enclosing_join(ln, m.start())
        if jpos is not None:
            span = _rc_paren_span(ln, jpos)
            if span is None:
                continue
            got: list = []
            for a in _rc_split_args(span[0]):
                v = _rc_resolve_arg(a, consts)
                if v is None:
                    break
                got.append(v)
            if got:
                out.append((m.group(0)[:120], os.path.join(*got)))
            continue
        body = m.group(2)
        if "{" in body:
            body = _rc_subst_fstring(body, consts)
            if body is None:
                continue
        out.append((m.group(0)[:120], body))
    return out


def _rc_ident_sites(code: str, consts: dict) -> list:
    out: list = []
    for ln in code.splitlines():
        for pat in _RC_ID_WRITE_RES:
            for m in pat.finditer(ln):
                v = consts.get(m.group(1))
                if v and _RC_LOG_TXT_END_RE.search(v):
                    out.append((m.group(0)[:120], v))
    return out


def _rc_redirect_sites(code: str, consts: dict) -> list:
    out: list = []
    for ln in code.splitlines():
        for pat in (_RC_REDIRECT_RE, _RC_TEE_RE):
            for m in pat.finditer(ln):
                t = m.group("t").strip().strip("'\"")
                if not t:
                    continue
                if "{" in t:
                    t = _rc_subst_fstring(t, consts)
                    if t is None:
                        continue
                if _RC_LOG_TXT_END_RE.search(t):
                    out.append((m.group(0)[:120], t))
    return out


def _rc_line_has_marker(lines: list, i: int) -> bool:
    for k in (i - 1, i, i + 1):
        if 0 <= k < len(lines) and _RC_WRITE_RE.search(lines[k]):
            return True
    return False


def _rc_log_txt_write_sites(code: str, consts: dict) -> list:
    sites: list = []
    lines = code.splitlines()
    for i, ln in enumerate(lines):
        if ln and _rc_line_has_marker(lines, i):
            sites.extend(_rc_lit_sites(lines, i, consts))
    sites.extend(_rc_ident_sites(code, consts))
    sites.extend(_rc_redirect_sites(code, consts))
    return sites


def _rc_logtxt_block_message(snippet: str, step_dir: str) -> str:
    if not step_dir:
        return (f"[Blocked] run_code 未执行：本次调用没有 task_id/step_id 归属，"
                f"不存在「自己的步骤文件夹」——任何 .log/.txt 写入一律拒绝"
                f"（命中：{snippet}）。请在带任务/步骤归属的执行链里调用。")
    return (f"[Blocked] run_code 未执行：代码会写本次步骤目录之外的 .log/.txt"
            f"（命中：{snippet}）。\n"
            f"规则：run_code 生成的 .log/.txt 只能写在本次步骤目录内 —— "
            f"{step_dir}{os.sep}（例如 {step_dir}{os.sep}artifacts{os.sep}build.log）；"
            f"项目树 / 任务工作目录 / 其他步骤目录 / 任务目录根 一律拒绝。\n"
            f"请把写入路径（含 shell 重定向 > / >> 的目标）改成上述目录下的"
            f"绝对路径后重发。")


def _run_code_logtxt_violation(code: str, owner: tuple, root: str) -> str:
    consts = _rc_const_map(code)
    sites = _rc_log_txt_write_sites(code, consts)
    if not sites:
        return ""
    tid, sid = owner
    step_dir = (os.path.normpath(os.path.join(PROJECT_ROOT, ".dc_tmp",
                                              str(tid), str(sid)))
                if (tid and sid) else "")
    if not step_dir:
        return _rc_logtxt_block_message(sites[0][0], "")
    step_key = os.path.normcase(step_dir)
    for snippet, prefix in sites:
        p = os.path.normcase(os.path.normpath(os.path.join(root, prefix)))
        if p == step_key or p.startswith(step_key + os.sep):
            continue
        return _rc_logtxt_block_message(snippet, step_dir)
    return ""


def _proc_alive(pid: int) -> bool:
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, timeout=10)
        return re.search(rf"\b{pid}\b", r.stdout.decode(errors="replace")) is not None
    except Exception:
        return False


_TASK_CMD_PIDS: dict[str, set[int]] = {}


async def kill_task_cmds(task_id: str) -> None:
    pids = _TASK_CMD_PIDS.pop(task_id, set())
    for pid in pids:
        try:
            await asyncio.to_thread(_kill_cmd_tree, pid)
        except Exception:  # noqa: BLE001
            pass


def _kill_cmd_tree(pid: int) -> None:
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
    for child in reversed(descendants):
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(child)],
                           capture_output=True, timeout=10)
        except Exception:
            pass


_MOJIBAKE_CHARS = set(
    "鏂囦椤洰鍌瓨鐩偍樺锟"
)


def _looks_like_gbk_mojibake(text: str) -> bool:
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
    p = rel_or_abs.replace("\\", "/")
    if (not os.path.isabs(rel_or_abs) and p.startswith(".dc_tmp/")) \
            or (os.path.isabs(rel_or_abs) and "/.dc_tmp/" in p):
        return safe_resolve(PROJECT_ROOT, rel_or_abs)
    return safe_resolve(root, rel_or_abs)


def _list_docs(root: str) -> list:
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


def _feed_stdin(stream, data: bytes) -> None:
    try:
        stream.write(data)
        stream.flush()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


_SEARCH_SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".venv",
                     "__pycache__", ".pytest_cache", ".dc_tmp", "bin", "obj", "out"}
_SEARCH_MAX_FILES = 20000
_SEARCH_TIME_LIMIT = 60.0


def _impl_list_dir(root: str, args: dict) -> dict:
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
    fp = args["file_path"]
    _abs_fp = os.path.realpath(fp if os.path.isabs(fp) else os.path.join(root, fp))
    _norm_abs = os.path.normcase(_abs_fp)
    if any(_norm_abs.startswith(os.path.normcase(os.path.realpath(_d) + os.sep))
           for _d in (rules_dir(), templates_dir())):
        full = _abs_fp
    else:
        try:
            full = tool_resolve(root, fp)
        except ValueError:
            return {"result": "[Security] path outside project root"}
    if not os.path.isfile(full):
        return {"result": f"(文件不存在: {fp})"}
    with open(full, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif raw.startswith(b"\x00\x00\xfe\xff") or raw.startswith(b"\xff\xfe\x00\x00"):
        text = raw.decode("utf-32")
    else:
        if b"\x00" in raw:
            return {"result": (f"(二进制文件: {fp}。请改用 dcflow_run_code "
                                f"处理（一段 Python 读头部/hexdump/提取），不要用 read_file 读二进制)")}
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gb18030")
            except UnicodeDecodeError:
                return {"result": (f"(二进制文件: {fp}。请改用 dcflow_run_code "
                                    f"处理（一段 Python 读头部/hexdump/提取），不要用 read_file 读二进制)")}
        else:
            if _looks_like_gbk_mojibake(text):
                try:
                    text = raw.decode("gb18030")
                except UnicodeDecodeError:
                    text = raw.decode("gb18030", errors="replace")
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
        return {"result": f"读取文本超过限制，当前{len(content)}字符，限制30000字符，请减少行数来读取。"}
    return {"result": f"[L{start_line}-L{end_line}] {content}"}


def _impl_write_file(root: str, args: dict) -> dict:
    fp = args["file_path"]
    try:
        full = tool_resolve(root, fp)
    except ValueError:
        return {"result": "[Security] path outside project root"}
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(args["content"])
    return {"result": f"✓ 已写入 {fp} ({len(args['content'].encode('utf-8'))} bytes)"}


def apply_edit_text(text: str, old: str, new: str,
                    replace_all: bool, occurrence) -> tuple:
    if not old:
        return None, "[Error] old_string 不能为空（edit 需要精确匹配旧文本）"
    count = text.count(old)
    if count == 0:
        return None, f"(未找到匹配文本: {old[:80]}...)"
    if new == old:
        return None, "(new_string 与 old_string 相同，未做任何修改)"
    if replace_all:
        return text.replace(old, new), ""
    occ = occurrence
    if occ is None:
        occ = 1
        hint = (f"（该文本共 {count} 处，已替换第 1 处；"
                f"如需其他位置请传 occurrence）" if count > 1 else "")
    else:
        try:
            occ = int(occ)
        except (TypeError, ValueError):
            return None, f"[Error] occurrence 必须是正整数: {occurrence!r}"
        if occ < 1 or occ > count:
            return None, f"(第 {occ} 处匹配不存在：共 {count} 处，occurrence 范围 1-{count})"
        hint = ""
    parts = text.split(old)
    return old.join(parts[:occ]) + new + old.join(parts[occ:]), hint


def _impl_edit_file(root: str, args: dict) -> dict:
    fp = args["file_path"]
    try:
        full = tool_resolve(root, fp)
    except ValueError:
        return {"result": "[Security] path outside project root"}
    if not os.path.isfile(full):
        return {"result": f"(文件不存在: {fp})"}
    old = args.get("old_string") or ""
    new = args.get("new_string") or ""
    with open(full, "r", encoding="utf-8") as f:
        text = f.read()
    replaced, note = apply_edit_text(text, old, new,
                                     bool(args.get("replace_all")), args.get("occurrence"))
    if replaced is None:
        return {"result": note}
    with open(full, "w", encoding="utf-8") as f:
        f.write(replaced)
    with open(full, "r", encoding="utf-8") as f:
        if new not in f.read():
            return {"result": "[Error] 写入后回读未发现新内容，请重试"}
    if args.get("replace_all"):
        return {"result": f"✓ 已替换 {text.count(old)} 处 ({fp})"}
    occ = args.get("occurrence")
    return {"result": f"✓ 已替换第 {1 if occ is None else int(occ)} 处 ({fp}){note}"}


def _impl_read_doc(root: str, args: dict) -> dict:
    fn = args.get("filename") or ""
    if fn == "list":
        avail = _list_docs(root)
        return {"result": "\n".join(avail)
                or "(未配置任何文档目录: .github/docs、.github/instructions、docs 均不存在)"}
    if not fn:
        return {"result": "[Error] filename 必填（可用 filename=\"list\" 查看全部文档）"}
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
    pat = args["pattern"]
    try:
        re.compile(pat)
    except re.error as e:
        pos = getattr(e, "pos", None)
        loc = ""
        if pos is not None:
            loc = (f"\n非法位置: ...{pat[max(0, pos - 20):pos + 20]}...\n"
                   + " " * min(20, 40) + "^")
        return {"result": f"[Error] 正则无效: {pat[:80]}{loc}"}
    try:
        d = tool_resolve(root, args.get("path_filter", "."))
    except ValueError:
        return {"result": "[Security] path outside project root"}
    matches = []
    scanned = 0
    truncated = False
    timed_out = False
    start = time.monotonic()
    for dp, dirs, fns in os.walk(d):
        dirs[:] = [x for x in dirs if x not in _SEARCH_SKIP_DIRS]
        for fn in fns:
            scanned += 1
            if scanned > _SEARCH_MAX_FILES:
                truncated = True
                break
            if time.monotonic() - start > _SEARCH_TIME_LIMIT:
                timed_out = True
                break
            fp = os.path.join(dp, fn)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for li, line in enumerate(f, 1):
                        if li % 4096 == 0 and time.monotonic() - start > _SEARCH_TIME_LIMIT:
                            timed_out = True
                            break
                        if re.search(pat, line):
                            matches.append(f"{fp}:{li}: {line.rstrip()[:200]}")
                            if len(matches) >= 30:
                                break
            except Exception:
                pass
            if timed_out or len(matches) >= 30 or truncated:
                break
        if timed_out or len(matches) >= 30 or truncated:
            break
    if timed_out:
        hint = (f"检索时间过长（超过 {int(_SEARCH_TIME_LIMIT)} 秒），"
                f"请调整参数重试（缩小 path_filter 范围或更具体的 pattern）。")
        if matches:
            return {"result": hint + "已捕获的结果：\n" + "\n".join(matches)}
        return {"result": hint + "尚未捕获到结果。"}
    result = "\n".join(matches) or "未找到匹配"
    if truncated:
        result += f"\n(扫描超过 {_SEARCH_MAX_FILES} 文件已截断)"
    return {"result": result}


def _resolve_owner(payload: dict, args: dict) -> tuple[str, str]:
    return (str(payload.get("task_id") or args.get("task_id") or ""),
            str(payload.get("step_id") or args.get("step_id") or ""))


@rest_app.post("/api/tool/invoke")
async def invoke_tool(payload: dict):
    name = payload["name"]
    args = payload.get("args", {})
    storage = _get_storage()
    sm = _get_state_machine()
    task_id = payload.get("task_id")
    if task_id:
        root = await get_task_workspace(_get_storage(), task_id)
    else:
        root = PROJECT_ROOT

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

        elif name == "dcflow_run_code":
            if graceful.is_draining():
                raise _StepGracefulDrain("run_code")
            timeout = int(args.get("timeout_seconds", 60))
            code = str(args.get("code", ""))
            if not code.strip():
                return {"result": "[Error] code 不能为空（传完整 Python 代码，顶层直接执行）"}
            if _is_kill_self_cmd(code, os.getpid()):
                return {"result": _kill_self_block_message(os.getpid())}
            _logtxt_block = _run_code_logtxt_violation(
                code, _resolve_owner(payload, args), root)
            if _logtxt_block:
                return {"result": _logtxt_block}
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"]
                                        if env.get("PYTHONPATH") else "")
            p = subprocess.Popen([sys.executable, "-X", "utf8", "-"],
                                 cwd=root, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _code_bytes = (code if code.endswith("\n") else code + "\n").encode("utf-8")
            t0 = threading.Thread(target=_feed_stdin, args=(p.stdin, _code_bytes),
                                  daemon=True)
            t0.start()
            graceful.add_cmd()
            _TASK_CMD_PIDS.setdefault(task_id, set()).add(p.pid)
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
                await asyncio.to_thread(_kill_cmd_tree, p.pid)
                try:
                    await asyncio.to_thread(p.wait, 5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    try:
                        await asyncio.to_thread(p.wait, 5)
                    except subprocess.TimeoutExpired:
                        return {"result": _timeout_result(timeout, out_buf, err_buf, clean=False)}
                return {"result": _timeout_result(timeout, out_buf, err_buf, clean=True)}
            finally:
                graceful.done_cmd()
                _TASK_CMD_PIDS.get(task_id, set()).discard(p.pid)
            t1.join(2)
            t2.join(2)
            t0.join(2)
            out = _decode_bytes_auto(b"".join(out_buf))
            err = _decode_bytes_auto(b"".join(err_buf))
            prefix = "" if p.returncode == 0 else f"[exit {p.returncode}] "
            body = out[:10000] or "(无输出)"
            if err.strip():
                body += "\n[stderr] " + (err[-2000:] if len(err) > 2000 else err)
            return {"result": prefix + body}

        elif name == "dcflow_ask_user":
            tid, sid = _resolve_owner(payload, args)
            if not tid or not sid:
                return {"result": "[Error] 无法确定任务/步骤归属——请重试，系统会自动挂到当前步骤"}
            question = str(args.get("question") or "").strip()
            if not question:
                return {"result": "[Error] question 不能为空（写清要确认什么与为什么需要确认）"}
            _ask_caller = "executor"
            _at = None
            try:
                _at = await storage.get_task(tid, include_hidden=True)
                _as_ = next((s for s in (_at or {}).get("steps", [])
                             if s.get("step_id") == sid), None)
                if _as_ is not None:
                    _ask_caller = _as_.get("type") or "executor"
            except Exception:  # noqa: BLE001
                pass
            _ask_single_ok = bool(_at and _at.get("mode") == "single" and sid == "chat")
            if _ask_caller not in ("requirement", "plan") and not _ask_single_ok:
                logger.warning(f"[DC:REST] ask_user 越权拒绝 step={sid} type={_ask_caller}")
                return {"result": "[Error] dcflow_ask_user 仅需求分析师（type=requirement）"
                                  "与计划制定者（type=plan）可用"}
            options = [str(x).strip() for x in (args.get("options") or [])
                       if str(x).strip()][:6]
            ask_payload = {"question": question, "options": options}
            await storage.save_artifact(tid, sid, "question",
                                        json.dumps(ask_payload, ensure_ascii=False), "json")
            ask_msg = (f"需要向你确认一个需求问题：\n\n{question}\n\n```json\n"
                       + json.dumps({"ask_user": ask_payload}, ensure_ascii=False, indent=2)
                       + "\n```")
            await storage.append_message(tid, sid, {"role": "assistant",
                                                    "content": ask_msg, "round_num": -1})
            logger.info(f"[DC:REST] ask_user tid={tid} step={sid} q={question[:50]}")
            return {"result": "✓ 问题已提交用户，步骤将暂停等待回答——"
                              "不要继续推进，也不要重复提问"}

        elif name == "dcflow_step_done":
            tid, target = _resolve_owner(payload, args)
            caller = target
            if target.startswith("monitor-"):
                try:
                    raw = await storage.get_artifact(tid, "_flow", "monitor_anchors")
                    if raw and raw.get("content"):
                        anchors = json.loads(raw["content"])
                        if isinstance(anchors, dict) and anchors.get(target):
                            target = str(anchors[target])
                except Exception:  # noqa: BLE001
                    pass
            _trow = None
            if tid:
                try:
                    _trow = await storage.get_task(tid, include_hidden=True)
                except Exception:  # noqa: BLE001
                    _trow = None
            if not _trow:
                return {"result": ("[Error] 无法确定任务归属（task_id 无效）——"
                                   "请重试，系统会自动挂到当前任务")}
            _caller_type = "executor"
            _s = next((s for s in (_trow or {}).get("steps", [])
                       if s.get("step_id") == target), None)
            _s_caller = _s if caller == target else next(
                (s for s in (_trow or {}).get("steps", [])
                 if s.get("step_id") == caller), None)
            if _s_caller is not None:
                _caller_type = _s_caller.get("type") or "executor"
            if _caller_type == "code_review":
                _exec_keys = ("execution_business_logic", "execution_architecture_security",
                              "execution_code_style", "execution_compile_check")
                _res_keys = ("residual_business_logic", "residual_architecture_security")
                _missing = [k for k in _exec_keys + _res_keys
                            if not isinstance(args.get(k), list)]
                if _missing:
                    return {"result": (
                        "[Error] 代码审查 step_done 必须携带问题分类数组"
                        f"（缺失: {', '.join(_missing)}；无问题传空数组 []）")}
                _exec_findings = {k: [str(x) for x in args[k]]
                                  for k in _exec_keys if args[k]}
                if _exec_findings:
                    return {"result": await _get_orchestrator().rewind_code_review(
                        task_id=tid, step_id=target,
                        reason=str(args.get("summary") or "审查未通过"),
                        summary=str(args.get("summary") or ""),
                        exec_findings=_exec_findings)}
                _residual = {k: [str(x) for x in args[k]] for k in _res_keys if args[k]}
                if _residual:
                    await storage.save_artifact(tid, target, "review_residual",
                                                json.dumps(_residual, ensure_ascii=False),
                                                "json")
            elif _caller_type == "plan_review":
                _pr_keys = ("plan_issues", "plan_suggestions")
                _pr_missing = [k for k in _pr_keys if not isinstance(args.get(k), list)]
                if _pr_missing:
                    return {"result": (
                        "[Error] 方案审查 step_done 必须携带 plan_issues/plan_suggestions"
                        f" 数组（缺失: {', '.join(_pr_missing)}；无内容传空数组 []）")}
                _pr_issues = [str(x) for x in args["plan_issues"]]
                if _pr_issues:
                    return {"result": await _get_orchestrator().rewind_plan_review(
                        task_id=tid, step_id=target,
                        reason=str(args.get("summary") or "方案审查未通过"),
                        summary=str(args.get("summary") or ""),
                        issues=_pr_issues,
                        suggestions=[str(x) for x in args["plan_suggestions"]])}
            completion = args.get("completion")
            if completion is not None and _caller_type == "executor":
                err = _validate_completion(completion)
                if err:
                    return {"result": f"[Error] {err}"}
            score = args.get("score")
            if _caller_type == "requirement":
                if score is None:
                    return {"result": (
                        "[Error] 需求分析师 step_done 必须携带 score 参数"
                        "（functionPoints/fileCount/isMultiModule/isArchitectural/"
                        "clarity/difficulty 六个字段，只填数字与布尔值，不写解释）")}
                _score_err = validate_score_params(score)
                if _score_err:
                    return {"result": f"[Error] {_score_err}"}
            if ((_s_caller or {}).get("human_attention") == "gate"
                    and _caller_type not in ("plan", "plan_review")):
                approval = args.get("approval")
                _ap_err = _validate_approval(approval)
                if not _ap_err and str(approval.get("doc_path") or "").strip():
                    _ap_err = await _check_gate_doc_exists(
                        tid, str(approval["doc_path"]).strip())
                if _ap_err:
                    return {"result": f"[Error] {_ap_err}"}
                pkg = {
                    "options": [
                        {"option": str(o.get("option") or "").strip(),
                         "cost": str(o.get("cost") or "").strip(),
                         "risk": str(o.get("risk") or "").strip(),
                         "pros": str(o.get("pros") or "").strip()}
                        for o in approval["options"] if isinstance(o, dict)
                    ],
                    "doc_path": str(approval.get("doc_path") or "").strip(),
                    "recommendation": str(approval.get("recommendation") or "").strip(),
                    "questions": [str(q).strip()
                                  for q in (approval.get("questions") or [])
                                  if str(q).strip()],
                }
                await storage.save_artifact(tid, target, "decision",
                                            json.dumps(pkg, ensure_ascii=False), "json")
                _doc_note = (f"方案文档 {pkg['doc_path']}"
                             if pkg["doc_path"] else "无方案文档")
                _note = (f"已提交审批请求：{_doc_note}，"
                         f"{len(pkg['options'])} 个决策选项\n\n```json\n"
                         + json.dumps(pkg, ensure_ascii=False, indent=2) + "\n```")
                try:
                    await _get_orchestrator()._append_message(
                        tid, target,
                        {"role": "assistant", "content": _note, "round_num": -1},
                        entity_id=f"gate-{target}")
                except Exception as _me:  # noqa: BLE001
                    logger.warning(f"[DC:REST] gate approval note failed "
                                   f"step={target}: {_me}")
                try:
                    _get_orchestrator()._publish(tid, "gateApprovalReady", {
                        "taskId": tid, "stepId": target, "approval": pkg})
                except Exception as _pe:  # noqa: BLE001
                    logger.warning(f"[DC:REST] gateApprovalReady publish failed "
                                   f"step={target}: {_pe}")
            if args.get("summary"):
                await storage.save_artifact(tid, target,
                                            "summary", args["summary"], "text")
            if completion is not None:
                await storage.save_artifact(tid, target, "completion",
                                            json.dumps(completion, ensure_ascii=False),
                                            "json")
            if _caller_type == "requirement":
                _prev = await storage.get_artifact(tid, "_flow", "task_score")
                _delta = 0.0
                if _prev and _prev.get("content"):
                    try:
                        _delta = float(json.loads(_prev["content"]).get("delta") or 0.0)
                    except (ValueError, TypeError):
                        _delta = 0.0
                sc = compute_task_score(score, _delta)
                await storage.save_artifact(tid, target, "score",
                                            json.dumps(sc, ensure_ascii=False), "json")
                await storage.save_artifact(tid, "_flow", "task_score",
                                            json.dumps(sc, ensure_ascii=False), "json")
                if sc["need_architect"]:
                    _t_all = await storage.get_task(tid, include_hidden=True)
                    _has_arch = any(s.get("type") == "architect"
                                    and s.get("status") in ("pending", "active", "stopped")
                                    for s in (_t_all or {}).get("steps", []))
                    if not _has_arch:
                        _existing = {s.get("step_id")
                                     for s in (_t_all or {}).get("steps", [])}
                        _arch_id = "arch-1"
                        _n = 1
                        while _arch_id in _existing:
                            _n += 1
                            _arch_id = f"arch-{_n}"
                        await storage.add_steps(tid, [{
                            "step_id": _arch_id, "title": "架构分析",
                            "type": "architect", "human_attention": "none",
                            "model_tier": "power", "required": 1,
                            "parallel_with": None,
                            "description": (
                                "依据需求分析师的用户旅程文档产出最优架构方案："
                                "候选架构枚举→逐方案评估（扩展性/一致性/改动面/风险/"
                                "与现有架构契合度）→选定最优并给理由；产出 架构方案.md"
                                "（目标架构图 Mermaid、模块职责与边界、数据流与接口契约、"
                                "对 planner 的约束清单 AR-n）")}],
                            after_step_id=target)
                        score_note = f"；已插入架构分析步骤 {_arch_id}（方案设计前）"
                    else:
                        score_note = "；架构分析步骤已存在（不重复插入）"
                else:
                    score_note = "；无需架构分析"
                logger.info(f"[DC:REST] task score tid={tid} "
                            f"tier={sc['tier']} need_architect={sc['need_architect']}")
                return {"result": (f"✓ 步骤 {target} 确认完成"
                                   f"（任务评分 {sc['tier']}/10）{score_note}")}
            if score is not None:
                logger.warning(f"[DC:REST] score ignored for non-requirement step "
                               f"type={_caller_type} step={target}")
            return {"result": f"✓ 步骤 {target} 确认完成"}

        elif name == "dcflow_list_steps":
            tid = payload.get("task_id") or args.get("task_id")
            if not tid:
                return {"result": "[Error] 无法确定任务归属——请重试，系统会自动挂到当前任务"}
            task = await storage.get_task(tid)
            if not task:
                return {"result": "Task not found"}
            steps = [{"step_id": s.get("step_id"), "title": s.get("title"),
                      "status": s.get("status"), "type": s.get("type", "executor"),
                      "human_attention": s.get("human_attention", "none"),
                      "required": s.get("required", 1),
                      "sort_order": s.get("sort_order", 0),
                      "parallel_with": s.get("parallel_with")}
                     for s in task.get("steps", []) if not is_hidden_step(s)]
            return {"result": json.dumps(steps, ensure_ascii=False, indent=2)}

        elif name == "dcflow_adjust_flow":
            def _norm_ids(val):
                if isinstance(val, str):
                    s = val.strip()
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
            tid = payload.get("task_id") or args.get("task_id")
            if not tid:
                return {"result": "[Error] 无法确定任务归属——请重试，系统会自动挂到当前任务"}
            ids = _norm_ids(args.get("step_ids", ""))
            new_steps, steps_error = _norm_steps(args.get("steps_json"))
            new_order = _norm_order(args.get("order_json", ""))
            reasoning = args.get("reasoning", "")

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
                if new_steps is None:
                    return {"result": (
                        "[Error] steps_json 解析失败: " + (steps_error or "未知错误") +
                        "。请用 JSON 对象数组提供步骤，如 [{\"step_id\": \"step-1\", "
                        "\"title\": \"...\", \"description\": \"...\"}]"
                        "（禁止 JSON 字符串形式，内层转义不可靠）")}
                if not new_steps:
                    return {"result": "[Error] add_steps 未携带任何步骤：steps_json 必须是非空数组"}
                top_after = args.get("after_step_id") or None
                for s in new_steps:
                    if isinstance(s, dict):
                        s["model_tier"] = _STEP_TYPE_MODEL_TIER.get(
                            s.get("type") or "executor", "light")
                insert_parts: list[str] = []
                prev_inserted = None
                for s in new_steps:
                    if not isinstance(s, dict):
                        continue
                    anchor_id = s.pop("after_step_id", None) or prev_inserted or top_after
                    if not anchor_id:
                        task_now = await storage.get_task(tid, include_hidden=True)
                        if task_now:
                            for s2 in sorted(task_now.get("steps", []),
                                             key=lambda x: x.get("sort_order", 0)):
                                if s2.get("step_id") in ("review", "report"):
                                    break
                                if not is_hidden_step(s2):
                                    anchor_id = s2.get("step_id")
                    if anchor_id and is_hidden_step({"step_id": anchor_id}):
                        return {"result": ("[Error] after_step_id 不能是审查/收尾"
                                           "步骤（monitor/review/report）")}
                    await storage.add_steps(tid, [s], anchor_id)
                    if s.get("step_id"):
                        prev_inserted = str(s["step_id"])
                    insert_parts.append(f"{s.get('step_id')} → "
                                        f"{anchor_id + ' 之后' if anchor_id else '末尾真实步骤之后'}")
                await storage.append_event(tid, {
                    "event_type": "orchestration", "actor": "ai",
                    "content": {"action": "add_steps", "steps": new_steps,
                                 "reasoning": reasoning, "after_step_id": top_after},
                })
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
        raise
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return {"result": f"[Error] {e}"}




GATE_DOC_MAX_CHARS = 400_000


@rest_app.get("/api/gate/doc")
async def get_gate_doc(task_id: str, step_id: str):
    pkg = await _get_decision_pkg(_get_storage(), task_id, step_id)
    if not pkg:
        return {"path": "", "content": "", "truncated": False,
                "error": "该步骤暂无审批包（AI 尚未提交 approval）"}
    doc_path = str(pkg.get("doc_path") or "").strip()
    if not doc_path:
        return {"path": "", "content": "", "truncated": False,
                "error": "该审批无方案文档（需求澄清类 gate）"}
    full = await _resolve_gate_doc(task_id, doc_path)
    if not full or not os.path.isfile(full):
        return {"path": doc_path, "content": "", "truncated": False,
                "error": f"文档不存在或已被移动：{doc_path}"}
    try:
        content = await asyncio.to_thread(_read_text_capped, full, GATE_DOC_MAX_CHARS)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[DC:REST] gate doc read failed {doc_path}: {e}")
        return {"path": doc_path, "content": "", "truncated": False,
                "error": f"文档读取失败：{e}"}
    return {"path": doc_path,
            "content": content[:GATE_DOC_MAX_CHARS],
            "truncated": len(content) > GATE_DOC_MAX_CHARS,
            "error": ""}


@rest_app.post("/api/step/advance")
async def advance_step(payload: dict):
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
        orch = _get_orchestrator()
        try:
            if decision == "rejected":
                await orch.reject_gate_and_run(task_id, step_id, reason)
            else:
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
        except ValueError as e:
            raise _state_error(e)
        return {"status": "ok", "step_id": step_id, "decision": decision}

    new_status = payload["new_status"]
    try:
        await sm.advance_step(task_id, step_id, new_status)
    except ValueError as e:
        raise _state_error(e)
    return {"status": "ok", "step_id": step_id, "new_status": new_status}


@rest_app.post("/api/step/compress")
async def compress_step(payload: dict):
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    return await _get_orchestrator().manual_compress_check(task_id, step_id)


@rest_app.post("/api/step/answer")
async def answer_step_question(payload: dict):
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    answer = str(payload.get("answer") or "").strip()
    if not answer:
        raise HTTPException(400, "answer 不能为空")

    orch = _get_orchestrator()
    storage = _get_storage()

    await storage.save_artifact(task_id, step_id, "question", "", "json")

    evt = orch._ask_user_answers.get(task_id)
    if evt:
        orch._ask_user_results[task_id] = answer
        evt.set()
        logger.info(f"[DC:REST] ask_user answered tid={task_id} step={step_id} "
                    f"a={answer[:50]}")
        return {"status": "ok"}

    raise HTTPException(400, "当前步骤没有待回答的问题")


@rest_app.post("/api/step/resume")
async def resume_step(payload: dict):
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
        await _get_orchestrator().start_task(task_id)
        return {"status": "resumed", "step_id": step_id}




async def _publish_user_message(task_id: str, step_id: str, message: str,
                                client_msg_id: Optional[str] = None) -> None:
    storage = _get_storage()
    orch = _get_orchestrator()
    if not client_msg_id:
        client_msg_id = f"um-{uuid4().hex}"
    announce = getattr(orch, "_announce_user_message", None)
    if announce is not None:
        mid_round = False
        is_in_flight = getattr(orch, "is_round_in_flight", None)
        if is_in_flight is not None:
            try:
                mid_round = bool(is_in_flight(task_id, step_id))
            except Exception:  # noqa: BLE001
                mid_round = False
        await announce(task_id, step_id, message, mid_round=mid_round,
                       client_msg_id=client_msg_id, queued=True)
    else:
        await storage.append_message(task_id, step_id,
                                     {"role": "user", "content": message, "round_num": -1})
        pub = getattr(orch, "_publish", None)
        if pub:
            pub(task_id, "userMessage", {"stepId": step_id, "message": message,
                                         "entityId": client_msg_id})
    try:
        raw = await storage.get_artifact(task_id, step_id, "intervention")
        if raw and raw.get("content"):
            data = json.loads(raw["content"])
            iv_list = data if isinstance(data, list) else [data]
            for iv in reversed(iv_list):
                if isinstance(iv, dict) and iv.get("message") == message \
                        and not iv.get("persisted"):
                    iv["persisted"] = True
                    iv["client_msg_id"] = client_msg_id
                    break
            await storage.save_artifact(task_id, step_id, "intervention",
                                        json.dumps(data, ensure_ascii=False), "json")
    except Exception:
        pass


@rest_app.post("/api/intervene/step")
async def intervene_step(payload: dict):
    task_id = payload["task_id"]
    step_id = payload["step_id"]
    intervention_type = payload["intervention_type"]
    message = payload["message"]
    client_msg_id = payload.get("clientMsgId") or None

    sm = _get_state_machine()
    orch = _get_orchestrator()
    try:
        _iv_task = await _get_storage().get_task(task_id, include_hidden=True)
        _is_single = bool(_iv_task and _iv_task.get("mode") == "single")
        if intervention_type == "stop":
            if _is_single:
                await orch.abort(task_id)
            else:
                await sm.stop_step(task_id, step_id)
                await orch.abort(task_id)
        elif intervention_type == "force_inject":
            _ftask = await _get_storage().get_task(task_id, include_hidden=True)
            _fstep = next((s for s in (_ftask or {}).get("steps", [])
                           if s.get("step_id") == step_id), None) if _ftask else None
            if _fstep and is_hidden_step(_fstep) and \
                    _fstep.get("status") in ("completed", "skipped", "stopped"):
                if orch.is_running(task_id):
                    await orch.abort(task_id)
                    await orch.wait_stopped(task_id)
                if _fstep.get("status") == "stopped":
                    await sm.resume_step(task_id, step_id)
                else:
                    await sm.reset_step_for_continuation(task_id, step_id, "强制介入续做")
            await sm.handle_step_intervention(task_id, step_id, intervention_type, message)
            await _publish_user_message(task_id, step_id, message, client_msg_id)
            await orch.abort(task_id, kind="force_inject")
            await orch.start_task(task_id)
        else:
            task = await _get_storage().get_task(task_id, include_hidden=True)
            step = None
            if task:
                step = next((s for s in task.get("steps", []) if s.get("step_id") == step_id), None)
            if _is_single:
                cur_status = step.get("status") if step else None
                if cur_status in ("completed", "skipped", "stopped"):
                    if orch.is_running(task_id):
                        await orch.abort(task_id)
                        await orch.wait_stopped(task_id)
                    await sm.reset_step_for_continuation(task_id, step_id, "用户消息续接")
                await sm.handle_step_intervention(task_id, step_id, "send", message)
                await _publish_user_message(task_id, step_id, message, client_msg_id)
                await orch.start_task(task_id)
            elif step and is_hidden_step(step):
                cur_status = step.get("status")
                if cur_status in ("completed", "skipped", "stopped"):
                    if orch.is_running(task_id):
                        await orch.abort(task_id)
                        await orch.wait_stopped(task_id)
                    await sm.reset_step_for_continuation(task_id, step_id, "用户消息续做")
                await sm.handle_step_intervention(task_id, step_id, "send", message)
                await _publish_user_message(task_id, step_id, message, client_msg_id)
                await orch.start_task(task_id)
            elif step and step.get("status") == "completed":
                if orch.is_running(task_id):
                    await orch.abort(task_id)
                    await orch.wait_stopped(task_id)
                await sm.reset_flow_from_step(task_id, step_id)
                await sm.handle_step_intervention(task_id, step_id, "send", message)
                await _publish_user_message(task_id, step_id, message, client_msg_id)
                await orch.start_task(task_id)
            elif step and step.get("status") == "skipped":
                if orch.is_running(task_id):
                    await orch.abort(task_id)
                    await orch.wait_stopped(task_id)
                await sm.reset_step_for_continuation(task_id, step_id, "用户消息续做")
                await sm.handle_step_intervention(task_id, step_id, "send", message)
                await _publish_user_message(task_id, step_id, message, client_msg_id)
                await orch.start_task(task_id)
            elif step and step.get("status") == "stopped":
                if orch.is_running(task_id):
                    await orch.abort(task_id)
                    await orch.wait_stopped(task_id)
                await sm.resume_step(task_id, step_id, message)
                await _publish_user_message(task_id, step_id, message, client_msg_id)
                await orch.start_task(task_id)
            else:
                await sm.handle_step_intervention(task_id, step_id, "send", message)
                await _publish_user_message(task_id, step_id, message, client_msg_id)
                await orch.start_task(task_id)
    except ValueError as e:
        raise _state_error(e)
    return {"status": "ok", "intervention_type": intervention_type, "step_id": step_id}


@rest_app.post("/api/monitor/control")
async def monitor_control(payload: dict):
    task_id = payload["task_id"]
    action = payload.get("action", "")
    message = payload.get("message") or ""
    sm = _get_state_machine()
    orch = _get_orchestrator()
    storage = _get_storage()
    try:
        if action == "stop":
            step_id = payload.get("step_id") or None
            if not step_id:
                raise _state_error(ValueError("monitor/control stop requires step_id"))
            await sm.stop_step(task_id, step_id)
            await orch.abort(task_id)
        elif action == "resume":
            step_id = payload.get("step_id") or None
            if not step_id:
                raise _state_error(ValueError("monitor/control resume requires step_id"))
            cur_status = await sm._get_step_status(task_id, step_id)
            if cur_status == "stopped":
                await sm.resume_step(task_id, step_id)
            elif cur_status == "pending":
                await sm._resume_after_intervention(task_id)
            else:
                raise ValueError(
                    f"Step {step_id} is not stopped or pending, cannot resume")
            if message:
                await sm.flow_pending_intervention(task_id, message)
                await orch.trigger_monitor(task_id, message)
            else:
                pass
            await sm._resume_after_intervention(task_id)
            await orch.start_task(task_id)
        else:
            raise ValueError(f"Unknown action: {action}")
    except ValueError as e:
        raise _state_error(e)
    return {"status": "ok", "task_id": task_id, "action": action}


@rest_app.post("/api/intervene/flow")
async def intervene_flow(payload: dict):
    task_id = payload["task_id"]
    reason = payload.get("reason", "")
    mode = payload.get("mode", "immediate")
    step_id = payload.get("step_id") or None

    sm = _get_state_machine()
    orch = _get_orchestrator()
    storage = _get_storage()
    _if_task = await storage.get_task(task_id)
    if _if_task and _if_task.get("mode") == "single":
        raise _state_error(ValueError(
            "单步骤对话无流程编排，请先切换为多步骤流程（或直接在聊天中发消息）"))
    try:
        if mode == "rebuild":
            if not step_id:
                raise _state_error(ValueError("rebuild requires step_id"))
            task_hidden = await storage.get_task(task_id, include_hidden=True)
            target_sid = None
            if task_hidden:
                steps_all = task_hidden.get("steps", [])
                sid = str(step_id)
                step = next((s for s in steps_all if s.get("step_id") == sid), None)
                if step and step.get("status") == "completed" and not is_hidden_step(step):
                    target_sid = sid
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
            async def _rebuild_monitor() -> None:
                await orch.abort(task_id)
                await orch.wait_stopped(task_id, timeout=180.0)
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
                await storage.update_step_status(task_id, target_sid, "completed")
                await sm.flow_pending_intervention(task_id, reason)
                if preserve:
                    await sm.handle_step_intervention(
                        task_id, preserve[0], "send", reason)
                await orch.start_task(task_id)

            asyncio.create_task(_rebuild_monitor())
            return {"status": "queued", "task_id": task_id,
                    "reason": reason, "mode": "rebuild"}
        if mode == "pending":
            task = await storage.get_task(task_id)
            if task and task.get("status") == "completed":
                await sm.flow_pending_intervention(task_id, reason)
                logger.info(f"[DC:REST] completed task pending intervention -> "
                            f"backfill tail chain for {task_id}")
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
                        "model_tier": "light", "required": 1,
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
                await orch.trigger_monitor(task_id, reason, mode="send")
            return {"status": "queued", "task_id": task_id, "reason": reason}
        else:
            await sm.flow_intervene(task_id, reason)
            await orch.abort(task_id, kind="immediate")
            await orch.trigger_monitor(task_id, reason, mode="force_inject")
            await sm._resume_after_intervention(task_id)
            await orch.start_task(task_id)
            return {"status": "ok", "task_id": task_id, "reason": reason}
    except ValueError as e:
        raise _state_error(e)




async def _monitor_global_assets(storage: Any, task_id: str, focus_step_id: str,
                                 steps: list[dict]) -> str:
    parts: list[str] = []
    step_map = {s.get("step_id"): s for s in steps}

    try:
        fr = await storage.get_artifact(task_id, "_flow", "step_report")
        if fr and fr.get("content"):
            parts.append("## 流程报告（全文，全局进展主文档）")
            parts.append(strip_key_findings_block(fr["content"]))
            parts.append("")
    except Exception:  # noqa: BLE001
        pass

    parts.append("## 已完成步骤结论（summary 全文，全局视角基础）")
    for s in steps:
        if s.get("status") != "completed" or is_hidden_step(s):
            continue
        sid = s.get("step_id", "")
        try:
            summ = await storage.get_artifact(task_id, sid, "summary")
        except Exception:  # noqa: BLE001
            summ = None
        content = str((summ or {}).get("content") or "")
        if content:
            parts.append(f"### {sid}: {s.get('title', '')}")
            parts.append(content)
        else:
            parts.append(f"- {sid}: {s.get('title', '')}（无 summary）")
    parts.append("")

    if focus_step_id:
        conv = await storage.get_conversation(task_id, focus_step_id)
        if conv:
            title = (step_map.get(focus_step_id) or {}).get("title", "")
            parts.append(f"## 触发步骤 {focus_step_id}: {title} 对话详情（本轮审查焦点）")
            head = conv[:5]
            tail = conv[-20:] if len(conv) > 25 else []
            for m in head + [m for m in tail if m not in head]:
                role = m.get("role", "?")
                content = str(m.get("content", ""))[:300]
                if role == "tool":
                    tool_name = m.get("tool_name", m.get("toolName", ""))
                    parts.append(f"[{role}/{tool_name}]: {content[:200]}")
                else:
                    parts.append(f"[{role}]: {content}")
            parts.append("")

    pend = [s for s in steps
            if s.get("status") in ("pending", "stopped") and not is_hidden_step(s)]
    if pend:
        parts.append("## 待执行/中断步骤（含 description：决策前必读，判断是否仍对齐当前进展）")
        for s in pend:
            desc = str(s.get("description") or "").strip()[:600]
            parts.append(f"- {s.get('step_id', '')} [{s.get('status', '')}] {s.get('title', '')}")
            if desc:
                parts.append(f"  description: {desc}")
        parts.append("")

    try:
        events = await storage.get_events(task_id, limit=100)
    except Exception:  # noqa: BLE001
        events = []
    orch = [e for e in events if e.get("event_type") == "orchestration"][:10]
    if orch:
        parts.append("## 最近编排决策（此前 monitor 的决定与理由，避免重复/矛盾）")
        for i, e in enumerate(orch, 1):
            c = e.get("content") or {}
            if isinstance(c, str):
                try:
                    c = json.loads(c)
                except (json.JSONDecodeError, ValueError):
                    c = {"action": "?", "reasoning": c}
            reasoning = str(c.get("reasoning") or c.get("what_happened") or "")[:500]
            parts.append(f"{i}. [{c.get('action', '?')}] {reasoning}")
        parts.append("")

    try:
        _score_sec = await score_context_section(storage, task_id)
        if _score_sec:
            parts.append(_score_sec)
            parts.append("")
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(parts)


@rest_app.post("/api/monitor/export")
async def monitor_export(payload: dict, storage: Any = None):
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

    conv_text = await _monitor_global_assets(storage, task_id, step_id, steps)

    if is_final:
        step_type = payload.get("step_type") or (
            "report" if step_id == "report" else "review")
        monitor_prompt = load_role_prompt(prompt_for_step(step_id, step_type))
    else:
        monitor_prompt = load_role_prompt(prompt_for_step(step_id, "monitor"))


    user_context = (
        f"当前任务: {task.get('title', '')} (类型: {task.get('type', '')})\n"
        f"任务 ID: {task_id}\n"
        f"- 需求: {task.get('description', '') or '(无)'}\n\n"
        f"## 全局编排资产（已内联，无需挖内部文件）\n"
        f"{conv_text}\n\n"
    )
    if step_id and not is_final:
        user_context += (
            f"## 编排检查点\n"
            f"当前是在步骤 [{step_id}]（刚完成/续做点）之后的编排检查。\n"
            f"- 新步骤必须插入到 [{step_id}] 之后：add_steps 时传 after_step_id=\"{step_id}\"；\n"
            f"- 已保留的待执行步骤继续执行，不要重复创建、不要把它们插到流程开头；\n"
            f"- 如需调整顺序用 reorder_steps（order_json 必须包含全部步骤 id）。\n\n"
        )
    user_context += (
        "这是新任务的**初始编排**：请根据任务描述参考系统提示词中的预设流程模板，"
        "用 add_steps 一次性创建完整流程（3~12 步、≥1 个 gate、每步 description 必填）。"
        if not steps and not is_final else
        "这是一个刚完成的步骤后的常规编排检查。" if not is_final else
        "这是一次最终审查，所有步骤已完成，请判断是否需要追加步骤。"
    )

    system_msg = f"{monitor_prompt}\n\n{user_context}"

    return {
        "system_message": system_msg,
        "system_prompt": monitor_prompt,
        "user_context": user_context,
        "task_id": task_id,
        "step_states": [
            {"step_id": s.get("step_id", ""), "title": s.get("title", ""),
             "status": s.get("status", ""), "type": s.get("type", "executor"),
             "human_attention": s.get("human_attention", "none"),
             "required": s.get("required", 1),
             "sort_order": s.get("sort_order", 0),
             "parallel_with": s.get("parallel_with")}
            for s in steps if not is_hidden_step(s)
        ],
    }


@rest_app.post("/api/monitor/cleanup")
async def monitor_cleanup(payload: dict):
    tmp_dir = payload.get("temp_dir", "")
    if tmp_dir and os.path.isdir(tmp_dir):
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"status": "ok"}


@rest_app.post("/api/monitor/trigger")
async def monitor_trigger(payload: dict):
    task_id = payload["task_id"]
    trigger_step_id = payload.get("trigger_step_id", "")
    reason = payload.get("reason", "")

    _mt_task = await _get_storage().get_task(task_id)
    if _mt_task and _mt_task.get("mode") == "single":
        raise _state_error(ValueError(
            "单步骤对话无流程编排，请先切换为多步骤流程（或直接在聊天中发消息）"))

    monitor = _get_monitor_context()
    context = await monitor._build_monitor_context(task_id, trigger_step_id)

    task = context.get("task", {})
    steps = task.get("steps", [])

    conv_text = await _monitor_global_assets(monitor.storage, task_id, trigger_step_id, steps)

    monitor_prompt = load_role_prompt(prompt_for_step(trigger_step_id, "monitor"))
    user_context = (
        f"当前任务: {task.get('title', '')} (类型: {task.get('type', '')})\n\n"
        f"## 介入原因\n"
        f"用户发起了流程级介入：{reason}\n\n"
        f"请根据介入原因重新评估当前流程，决定是否需要调整后续步骤。\n\n"
        f"{conv_text}\n\n"
        f"## 可用工具\n"
        f"- dcflow_list_steps: 查看当前所有步骤状态\n"
        f"- dcflow_adjust_flow: 修改流程步骤（skip/add/remove/reorder/mark_complete/no_change）；"
        f"add_steps 可用 after_step_id（顶层参数或 steps_json 元素内嵌）指定插入位置\n"
    )
    system_msg = f"{monitor_prompt}\n\n{user_context}"

    return {
        "system_message": system_msg,
        "system_prompt": monitor_prompt,
        "user_context": user_context,
        "task_id": task_id,
        "trigger_step_id": trigger_step_id,
        "step_states": [
            {"step_id": s.get("step_id", ""), "title": s.get("title", ""),
             "status": s.get("status", ""), "type": s.get("type", "executor"),
             "human_attention": s.get("human_attention", "none"),
             "required": s.get("required", 1),
             "sort_order": s.get("sort_order", 0),
             "parallel_with": s.get("parallel_with")}
            for s in steps if not is_hidden_step(s)
        ],
    }


@rest_app.post("/api/monitor/save-conversation")
async def monitor_save_conversation(payload: dict, storage: Any = None):
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
async def get_monitor_conversations(task_id: str, full: bool = False):
    storage = _get_storage()
    result = {}
    if full:
        artifacts = await storage.list_artifacts(task_id)
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
    monitor_anchors: dict[str, str] = {}
    try:
        raw_anchor = await storage.get_artifact(task_id, "_flow", "monitor_anchors")
        if raw_anchor and raw_anchor.get("content"):
            data = json.loads(raw_anchor["content"])
            monitor_anchors = data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        pass
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




@rest_app.get("/sse")
async def sse_stream(request: Request, taskId: str, lastSeq: int = 0,
                     stepId: str = "", lastMsgSeq: int = -1):
    orch = _get_orchestrator()

    async def event_gen():
        async for event in orch.stream_events(taskId, last_seq=lastSeq,
                                              last_msg_seq=lastMsgSeq,
                                              step_id=stepId):
            if await request.is_disconnected():
                return
            yield json.dumps(event, ensure_ascii=False)

    return EventSourceResponse(event_gen(), ping=None)



FSTREE_HIDDEN = {"node_modules", ".git", "dist", ".dc_tmp",
                 "__pycache__", ".pytest_cache"}

FS_FILE_MAX_BYTES = 2 * 1024 * 1024
FS_TREE_MAX_NODES = 2000


async def _fs_resolve(path: str, task_id: str = "") -> str:
    root = PROJECT_ROOT
    if task_id:
        from .step_context import get_task_workspace
        root = await get_task_workspace(_get_storage(), task_id)
    try:
        return safe_resolve(root, path)
    except ValueError:
        raise HTTPException(400, detail="path outside workspace root")


@rest_app.get("/api/fs/browse")
async def fs_browse(path: str = "", task_id: str = ""):
    if not path and not task_id:
        drives = []
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if os.path.exists(f"{c}:\\"):
                drives.append({"name": f"{c}:", "type": "dir"})
        return {"path": "", "entries": drives}
    full = await _fs_resolve(path, task_id) if task_id else os.path.realpath(path)
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
async def fs_tree(path: str = "", recursive: bool = False, task_id: str = ""):
    root_abs = await _fs_resolve("", task_id)
    full = await _fs_resolve(path, task_id)
    if not os.path.isdir(full):
        raise HTTPException(404, detail="目录不存在")
    entries = []
    truncated = False
    if recursive:
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
        for name in sorted(os.listdir(full), key=lambda n: (not os.path.isdir(os.path.join(full, n)), n)):
            if name in FSTREE_HIDDEN:
                continue
            p = os.path.join(full, name)
            if os.path.isdir(p):
                entries.append({"name": name, "type": "dir"})
            else:
                entries.append({"name": name, "type": "file", "size": os.path.getsize(p)})
    return {"path": path or ".", "entries": entries, "truncated": truncated,
            "root": root_abs,
            "workspace": os.path.basename(root_abs) or root_abs}


@rest_app.get("/api/fs/file")
async def fs_file(path: str = "", task_id: str = ""):
    full = await _fs_resolve(path, task_id)
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
    path = payload.get("path", "")
    content = payload.get("content", "")
    base_mtime = payload.get("baseMtime")
    full = await _fs_resolve(path, payload.get("task_id", ""))
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




def _validate_project_root(value) -> str:
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
    llm = config.get_llm_config()
    return {
        "baseUrl": llm["base_url"],
        "lightModel": llm["light_model"],
        "powerModel": llm["power_model"],
        "lightBaseUrl": llm["light_base_url"],
        "powerBaseUrl": llm["power_base_url"],
        "hasLightApiKey": bool(llm["light_api_key"]),
        "hasPowerApiKey": bool(llm["power_api_key"]),
        "lightModel2": llm.get("light_model2", ""),
        "lightBaseUrl2": llm.get("light_base_url2", ""),
        "hasLightApiKey2": bool(llm.get("light_api_key2")),
        "lightRouteEnabled": config.light_route_enabled(llm),
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
        "contextWindow": config.get_context_window(),
        "channelType": llm.get("channel_type", ""),
        "hasChannel": bool(llm.get("channel_type")),
        "mcpServers": config.get_mcp_config(),
    }


@rest_app.put("/api/config")
async def put_config(payload: dict):
    allowed = {"baseUrl", "apiKey", "lightModel", "powerModel", "projectRoot", "contextWindow",
               "llmChannel", "lightBaseUrl", "lightApiKey", "powerBaseUrl", "powerApiKey",
               "lightModel2", "lightBaseUrl2", "lightApiKey2",
               "lightInputPrice", "lightCachedPrice", "lightOutputPrice",
               "powerInputPrice", "powerCachedPrice", "powerOutputPrice",
               "mcpServers"}
    unknown = set(payload) - allowed
    if unknown:
        raise HTTPException(400, detail=f"未知配置字段: {sorted(unknown)}")
    if payload.get("projectRoot") is not None:
        _validate_project_root(payload["projectRoot"])
    if payload.get("contextWindow") is not None:
        v = payload["contextWindow"]
        if not isinstance(v, int) or isinstance(v, bool) or v < 1000:
            raise HTTPException(400, detail="contextWindow 必须是 ≥1000 的整数")
    for pk in ("lightInputPrice", "lightCachedPrice", "lightOutputPrice",
               "powerInputPrice", "powerCachedPrice", "powerOutputPrice"):
        if payload.get(pk) is not None:
            v = payload[pk]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                raise HTTPException(400, detail=f"{pk} 必须是非负数字")
    lc = payload.get("llmChannel")
    if lc is not None and not isinstance(lc, (str, dict)):
        raise HTTPException(400, detail="llmChannel 必须是 JSON 字符串或对象")
    if isinstance(lc, str) and lc.strip():
        try:
            json.loads(lc)
        except json.JSONDecodeError:
            raise HTTPException(400, detail="llmChannel 不是合法 JSON")
    ms = payload.get("mcpServers")
    if ms is not None:
        if not isinstance(ms, list):
            raise HTTPException(400, detail="mcpServers 必须是数组")
        for item in ms:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                raise HTTPException(400, detail="mcpServers 每项必须是含 name 的对象")
    update = {k: v for k, v in payload.items() if v is not None}
    config.write_config(update)
    return {"status": "ok"}


@rest_app.post("/api/config/test-llm")
async def test_llm():
    cfg = config.get_llm_config()
    groups = [
        ("light", cfg.get("light_base_url"), cfg.get("light_api_key"), cfg.get("light_model")),
        ("power", cfg.get("power_base_url"), cfg.get("power_api_key"), cfg.get("power_model")),
    ]
    if config.light_route_enabled(cfg):
        groups.append(("light(备用)", cfg.get("light_base_url2"),
                       cfg.get("light_api_key2"), cfg.get("light_model2")))
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
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "model": model}
    return {"ok": True}


@rest_app.post("/api/config/test-mcp")
async def test_mcp(payload: dict):
    server = payload.get("server") if isinstance(payload, dict) else None
    if not isinstance(server, dict) or not str(server.get("name") or "").strip():
        raise HTTPException(400, detail="缺少 server 配置（含 name）")
    stype = str(server.get("type") or "stdio").strip().lower()
    if stype not in ("stdio", "http"):
        raise HTTPException(400, detail="type 必须是 stdio 或 http")
    if stype == "stdio" and not str(server.get("command") or "").strip():
        raise HTTPException(400, detail="stdio 类型必须填 command")
    if stype == "http" and not str(server.get("url") or "").strip():
        raise HTTPException(400, detail="http 类型必须填 url")
    args = server.get("args") or []
    if not isinstance(args, list):
        raise HTTPException(400, detail="args 必须是字符串数组")
    env = server.get("env") or {}
    if not isinstance(env, dict):
        raise HTTPException(400, detail="env 必须是对象")
    cfg = {
        "name": str(server["name"]).strip(),
        "type": stype,
        "command": str(server.get("command") or "").strip(),
        "args": [str(a) for a in args],
        "env": {str(k): str(v) for k, v in env.items()},
        "url": str(server.get("url") or "").strip(),
        "enabled": True,
    }
    try:
        tools = await mcp_manager.test(cfg)
        names = [str(t.get("name") or "") for t in tools if isinstance(t, dict) and t.get("name")]
        return {"ok": True, "tools": names}
    except McpError as e:
        return {"ok": False, "error": str(e)[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}




@rest_app.get("/api/stats/daily-tokens")
async def stats_daily_tokens(days: str = "30"):
    try:
        n = int(days)
    except (ValueError, TypeError):
        n = 30
    if n <= 0:
        n = 30
    n = max(1, min(n, 365))
    rows = await _get_storage().get_daily_token_usage(n)
    by_date = {str(r["date"]): r for r in rows if r.get("date")}
    today = datetime.now().astimezone().date()
    items = []
    for i in range(n - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        r = by_date.get(d)
        prompt = int((r or {}).get("prompt") or 0)
        cached = int((r or {}).get("cached") or 0)
        completion = int((r or {}).get("completion") or 0)
        items.append({"date": d, "prompt": prompt, "cached": cached,
                      "completion": completion, "total": prompt + completion})
    return {"days": n, "items": items}




async def _recover_stale_tasks() -> int:
    storage = _get_storage()
    sm = _get_state_machine()
    tasks = await storage.list_tasks()
    recovered = 0
    for t in tasks:
        if t.get("status") != "active":
            continue
        full = await storage.get_task(t["id"], include_hidden=True) or {}
        for s in full.get("steps", []):
            if s.get("status") == "active":
                try:
                    await sm.advance_step(t["id"], s["step_id"], "pending")
                    recovered += 1
                except ValueError:
                    continue
    if recovered:
        logger.info(f"[DC:recover] {recovered} stale active step(s) → pending (auto-resume)")
    return recovered


async def _auto_resume_tasks() -> int:
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
        if t.get("mode") == "single":
            continue
        steps = t.get("steps", [])
        if not steps:
            continue
        if any(s.get("status") != "pending" for s in steps):
            try:
                await orch.start_task(t["id"])
                resumed += 1
                logger.info(f"[DC:recover] task {t['id']} auto-resumed")
            except Exception:
                logger.exception(f"[DC:recover] auto-resume failed for task {t['id']}")
    return resumed


async def _graceful_shutdown() -> None:
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
        if task.get("mode") == "single":
            for s in task.get("steps", []):
                if s.get("status") == "active":
                    try:
                        await sm.advance_step(task_id, s["step_id"], "pending")
                    except ValueError:
                        pass
            logger.info(f"[DC:shutdown] single chat task {task_id} parked to pending")
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

    try:
        await mcp_manager.close_all()
    except Exception:  # noqa: BLE001
        logger.exception("[DC:shutdown] mcp close_all failed")




@rest_app.exception_handler(ValueError)
async def _valueerror_handler(request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@rest_app.exception_handler(KeyError)
async def _keyerror_handler(request, exc: KeyError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})




@rest_app.get("/api/auth/status")
async def auth_status(request: Request):
    cfg = config.get_auth_config()
    return {
        "enabled": bool(cfg.get("enabled")),
        "authenticated": bool(_auth_manager.is_valid(
            request.cookies.get("dc_session"))),
    }


@rest_app.post("/api/auth/challenge")
async def auth_challenge():
    return {"challenge": _auth_manager.issue_challenge()}


@rest_app.post("/api/auth/login")
async def auth_login(body: dict, response: Response):
    challenge = str(body.get("challenge") or "")
    signature = str(body.get("signature") or "")
    token = _auth_manager.verify_login(challenge, signature)
    if token is None:
        raise HTTPException(status_code=401, detail="认证失败")
    hours = int(config.get_auth_config().get("sessionHours", 8))
    secure = bool(config.get_https_config().get("enabled"))
    response.set_cookie("dc_session", token, httponly=True, samesite="lax",
                        max_age=hours * 3600, path="/", secure=secure)
    return {"status": "ok"}


@rest_app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    _auth_manager.revoke(request.cookies.get("dc_session"))
    response.delete_cookie("dc_session", path="/")
    return {"status": "ok"}
