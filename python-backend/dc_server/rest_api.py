
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
from sse_starlette.sse import EventSourceResponse

from . import config
from .config import DB_PATH, PROJECT_ROOT, CORS_ORIGINS
from .storage import create_storage, StorageAdapter
from .state_machine.state_machine import StateMachine
from .monitor_context import MonitorContext
from .step_context import StepContext, get_task_root
from .prompts import load_prompt, rules_dir
from .prompts.registry import is_hidden_step, is_virtual_step, prompt_for_step
from .tool_security import safe_resolve
from .graceful import graceful
from .brain.orchestrator import (Orchestrator, OrchestratorBusyError, _StepGracefulDrain,
                                 _FLOW_REPORT_FILENAME, _FLOW_REPORT_ANCHOR_CHARS)
from .brain.llm_client import LlmClient, LlmError

logger = logging.getLogger(__name__)

rest_app = FastAPI(title="DimensionCoder API", version="0.2.0")

rest_app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

_memory_storage_instance: Optional[Any] = None

def _get_memory_storage():
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
    if not title.strip() and description.strip():
        title = description.strip()[:20]
    epic_id = payload.get("epic_id")
    assignee = payload.get("assignee", "")
    payload.get("auto_start", True)

    sm = _get_state_machine()

    task_id = await sm.create_task(
        task_type=task_type,
        title=title,
        description=description,
        epic_id=epic_id,
        assignee=assignee,
    )
    try:
        os.makedirs(os.path.join(PROJECT_ROOT, task_id), exist_ok=True)
    except OSError:
        logger.exception(f"create_task: 创建任务根失败 task_id={task_id}")

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
        logger.info(f"[DC:REST] auto title generated for {task_id}: {title}")
    except Exception:  # noqa: BLE001
        pass

@rest_app.get("/api/tasks")
async def get_project_overview():
    storage = _get_storage()
    epics = await storage.list_epics()
    tasks = await storage.list_tasks()

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

async def _step_has_decision_pkg(storage, task_id: str, step_id: str) -> bool:
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
                    await sm.advance_step(task_id, s["step_id"], "pending")
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

@rest_app.post("/api/task/{task_id}/best-effort")
async def set_best_effort(task_id: str, payload: dict):
    storage = _get_storage()
    task = await storage.get_task(task_id)
    if not task:
        raise HTTPException(404, detail="任务不存在")
    enabled = 1 if payload.get("enabled") else 0
    await storage.update_task(task_id, {"best_effort": enabled})
    return {"status": "ok", "task_id": task_id, "best_effort": bool(enabled)}

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
async def get_step_messages(step_id: str, task_id: str, after_seq: int = -1):
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

@rest_app.post("/api/tool/invoke")
async def invoke_tool(payload: dict):
    name = payload["name"]
    args = payload.get("args", {})
    storage = _get_storage()
    sm = _get_state_machine()
    task_id = payload.get("task_id")
    root = get_task_root(task_id) if task_id else PROJECT_ROOT

    try:
        if name == "dcflow_list_dir":
            try:
                full = tool_resolve(root, args.get("dir_path", "."))
            except ValueError:
                return {"result": "[Security] path outside project root"}
            if os.path.isdir(full):
                entries = os.listdir(full)
                lines = [f"{'[DIR] ' if os.path.isdir(os.path.join(full, e)) else '[FILE] '}{e}" for e in sorted(entries)]
                return {"result": "\n".join(lines) or "(空目录)"}
            return {"result": f"(目录不存在: {args.get('dir_path', '.')})"}

        elif name == "dcflow_read_file":
            fp = args["file_path"]
            _rules_dir = rules_dir()
            _abs_fp = os.path.realpath(fp if os.path.isabs(fp) else os.path.join(root, fp))
            if os.path.normcase(_abs_fp).startswith(
                    os.path.normcase(os.path.realpath(_rules_dir) + os.sep)):
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

        elif name == "dcflow_write_file":
            fp = args["file_path"]
            try:
                full = tool_resolve(root, fp)
            except ValueError:
                return {"result": "[Security] path outside project root"}
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(args["content"])
            return {"result": f"✓ 已写入 {fp} ({len(args['content'].encode('utf-8'))} bytes)"}

        elif name == "dcflow_edit_file":
            fp = args["file_path"]
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
                parts = text.split(old)
                replaced = old.join(parts[:occ]) + new + old.join(parts[occ:])
                done = 1
            with open(full, "w", encoding="utf-8") as f:
                f.write(replaced)
            with open(full, "r", encoding="utf-8") as f:
                if new not in f.read():
                    return {"result": "[Error] 写入后回读未发现新内容，请重试"}
            if replace_all:
                return {"result": f"✓ 已替换 {done} 处 ({fp})"}
            return {"result": f"✓ 已替换第 {occ} 处 ({fp}){hint}"}

        elif name == "dcflow_read_doc":
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

        elif name == "dcflow_search_code":
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
            for dp, _, fns in os.walk(d):
                for fn in fns:
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
                    if len(matches) >= 30:
                        break
                if len(matches) >= 30:
                    break
            return {"result": "\n".join(matches) or "未找到匹配"}

        elif name == "dcflow_run_cmd":
            if graceful.is_draining():
                raise _StepGracefulDrain("run_cmd")
            timeout = int(args.get("timeout_seconds", 60))
            command = str(args.get("command", ""))
            if _is_kill_self_cmd(command, os.getpid()):
                return {"result": _kill_self_block_message(os.getpid())}
            p = subprocess.Popen(command, shell=True, cwd=root,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
            out = b"".join(out_buf)
            err = b"".join(err_buf)

            def _dec(b):
                return _decode_bytes_auto(b)

            out = _dec(out) + ("\n" + _dec(err) if err else "")
            prefix = "" if p.returncode == 0 else f"[exit {p.returncode}] "
            return {"result": prefix + (out[:10000] or "(无输出)")}

        elif name == "dcflow_step_done":
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
            tid = args.get("task_id")
            if not tid:
                return {"result": "[Error] 缺少 task_id 参数（dcflow_adjust_flow 必须携带 task_id）"}
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
                        "\"title\": \"...\", \"description\": \"...\", \"model_tier\": \"power\"}]"
                        "（禁止 JSON 字符串形式，内层转义不可靠）")}
                if not new_steps:
                    return {"result": "[Error] add_steps 未携带任何步骤：steps_json 必须是非空数组"}
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
    storage = _get_storage()
    messages = await storage.get_step_messages(task_id, step_id)
    if len(messages) <= 6:
        return {"status": "skipped", "reason": "too_few_messages", "count": len(messages)}

    early = messages[:-6]
    recent = messages[-6:]
    summary_seq = early[0]["seq"]
    summary_lines = [f"[{m['role']}]: {str(m.get('content', ''))[:100]}..." for m in early]
    summary_content = "[早期对话已压缩]\n" + "\n".join(summary_lines)

    conn_factory = getattr(storage, "_get_conn", None)
    if conn_factory is not None:
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

@rest_app.post("/api/intervene/step")
async def intervene_step(payload: dict):
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
            await orch.start_task(task_id)
        else:
            task = await _get_storage().get_task(task_id, include_hidden=True)
            step = None
            if task:
                step = next((s for s in task.get("steps", []) if s.get("step_id") == step_id), None)
            if step and is_hidden_step(step):
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
                if orch.is_running(task_id):
                    await orch.abort(task_id)
                    await orch.wait_stopped(task_id)
                await sm.reset_step_for_continuation(task_id, step_id, "用户消息续做")
                await sm.handle_step_intervention(task_id, step_id, "send", message)
                await orch.start_task(task_id)
            elif step and step.get("status") == "stopped":
                await sm.resume_step(task_id, step_id, message)
                await orch.start_task(task_id)
            else:
                await sm.handle_step_intervention(task_id, step_id, "send", message)
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

    conv_summary_parts = []
    for sid, msgs in completed_conv.items():
        step_title = ""
        for s in steps:
            if s.get("step_id") == sid:
                step_title = s.get("title", "")
                break
        conv_summary_parts.append(f"## {sid}: {step_title} ({len(msgs)} 条消息)")
        head = msgs[:5]
        tail = msgs[-20:] if len(msgs) > 25 else []
        picked = head + [m for m in tail if m not in head]
        for m in picked:
            role = m.get("role", "?")
            content = str(m.get("content", ""))[:300]
            if role == "tool":
                tool_name = m.get("tool_name", m.get("toolName", ""))
                conv_summary_parts.append(f"[{role}/{tool_name}]: {content[:200]}")
            else:
                conv_summary_parts.append(f"[{role}]: {content}")
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
        step_type = payload.get("step_type") or (
            "report" if step_id == "report" else "review")
        monitor_prompt = load_prompt(prompt_for_step(step_id, step_type))
    else:
        monitor_prompt = load_prompt(prompt_for_step(step_id, "monitor"))

    kf_parts = []
    try:
        kf = await monitor.storage.get_artifact(task_id, "_flow", "key_findings")
        if kf and kf.get("content"):
            kf_parts.append(f"## 任务关键发现\n{kf['content']}")
        else:
            kf_parts.append("## 任务关键发现\n(无关键发现记录)")
    except Exception:
        kf_parts.append("## 任务关键发现\n(无关键发现记录)")

    user_context = (
        f"当前任务: {task.get('title', '')} (类型: {task.get('type', '')})\n"
        f"任务 ID: {task_id}\n"
        f"- 需求: {task.get('description', '') or '(无)'}\n\n"
        f"## 已完成步骤对话摘要（内联，无需读文件）\n"
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
    if kf_parts:
        user_context += "\n\n" + "\n\n".join(kf_parts)

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
             "model_tier": s.get("model_tier", "light"),
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

    monitor_prompt = load_prompt(prompt_for_step(trigger_step_id, "monitor"))
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
             "model_tier": s.get("model_tier", "light"),
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
async def get_monitor_conversations(task_id: str):
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
async def sse_stream(request: Request, taskId: str, lastSeq: int = 0):
    hub = _get_sse_hub()

    async def event_gen():
        async for event in hub.subscribe(taskId, last_seq=lastSeq):
            if await request.is_disconnected():
                return
            yield json.dumps(event, ensure_ascii=False)

    return EventSourceResponse(event_gen(), ping=None)

FSTREE_HIDDEN = {"node_modules", ".git", "dist", ".dc_tmp"}

FS_FILE_MAX_BYTES = 2 * 1024 * 1024
FS_TREE_MAX_NODES = 2000

def _fs_resolve(path: str) -> str:
    try:
        return safe_resolve(PROJECT_ROOT, path)
    except ValueError:
        raise HTTPException(400, detail="path outside project root")

@rest_app.get("/api/fs/tree")
async def fs_tree(path: str = "", recursive: bool = False):
    full = _fs_resolve(path)
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
    return {"path": path or ".", "entries": entries, "truncated": truncated}

@rest_app.get("/api/fs/file")
async def fs_file(path: str = ""):
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
    }

@rest_app.put("/api/config")
async def put_config(payload: dict):
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
        from .memory import close_memory
        close_memory()
    except Exception:
        logger.exception("[DC:shutdown] close_memory failed")

@rest_app.get("/api/memory/stats")
async def memory_stats():
    ms = _get_memory_storage()
    if ms is None:
        return {"enabled": False}
    return ms.get_stats()

@rest_app.post("/api/memory/retain")
async def memory_retain(body: dict):
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
    from .memory import get_recaller
    from .config import get_memory_config
    return get_recaller(_get_memory_storage(), get_memory_config())

@rest_app.exception_handler(ValueError)
async def _valueerror_handler(request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})

@rest_app.exception_handler(KeyError)
async def _keyerror_handler(request, exc: KeyError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})
