
from __future__ import annotations

import json
import uuid
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from ..models.data_models import (
    TaskStatus, PauseLevel, StepStatus, HumanAttention, ModelTier,
    StepDefinition, Task, TaskStep, Event, EventType, EventActor,
)
from ..state_machine.task_types import get_task_type, get_steps, validate_task_type
from ..storage.adapter import StorageAdapter
from ..prompts.registry import STEP_TYPES, is_hidden_step, is_virtual_step

logger = logging.getLogger(__name__)

def _normalize_parallel_with(value) -> Optional[list[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [s.strip() for s in value.split(",") if s.strip()]
        return parts or None
    if isinstance(value, list):
        return [str(s) for s in value]
    raise ValueError(f"invalid parallel_with: {value!r} (must be null, str or list[str])")

class StateMachine:

    def __init__(
        self,
        storage: StorageAdapter,
    ):
        self.storage = storage

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _new_id(self) -> str:
        return str(uuid.uuid4())

    async def create_task(
        self,
        task_type: str,
        title: str,
        description: str = "",
        epic_id: Optional[str] = None,
        assignee: str = "",
    ) -> str:
        task_id = self._new_id()
        now = self._now()

        task_data = {
            "id": task_id,
            "type": task_type or "custom",
            "title": title,
            "description": description,
            "epic_id": epic_id,
            "status": TaskStatus.ACTIVE.value,
            "pause_level": None,
            "assignee": assignee,
            "created_at": now,
            "steps": [],
        }

        await self.storage.create_task(task_data)

        await self.storage.append_event(task_id, {
            "event_type": EventType.ORCHESTRATION.value,
            "actor": EventActor.SYSTEM.value,
            "content": {
                "what_happened": f"Task '{title}' created (steps pending Monitor orchestration)",
            },
            "timestamp": now,
        })

        logger.info(f"Task created: {task_id} ({task_type or 'custom'}: {title})")
        return task_id

    async def create_custom_task(self, title: str, description: str, steps: list[dict]) -> str:
        if not isinstance(steps, list) or not steps:
            raise ValueError("steps required for custom task")

        validated: list[dict] = []
        for i, raw in enumerate(steps):
            if not isinstance(raw, dict):
                raise ValueError(f"step {i} must be a dict")

            for field in ("step_id", "title", "required", "parallel_with",
                          "human_attention", "model_tier"):
                if field not in raw:
                    raise ValueError(f"step {i} missing field: {field}")

            try:
                ha = HumanAttention(raw["human_attention"])
            except ValueError:
                raise ValueError(f"invalid human_attention: {raw['human_attention']}")
            try:
                mt = ModelTier(raw["model_tier"])
            except ValueError:
                raise ValueError(f"invalid model_tier: {raw['model_tier']}")

            required = raw["required"]
            if required not in (0, 1):
                raise ValueError(f"invalid required: {required!r} (must be 0 or 1)")

            parallel_with = _normalize_parallel_with(raw["parallel_with"])

            if ha == HumanAttention.GATE and parallel_with is not None:
                raise ValueError(
                    f"gate step {raw['step_id']} cannot have parallel_with"
                )

            step_type = raw.get("type", "executor")
            if step_type not in STEP_TYPES:
                raise ValueError(f"invalid step type: {step_type!r}")
            if str(raw["step_id"]).startswith("_"):
                raise ValueError(f"step_id {raw['step_id']!r} starts with reserved '_' prefix")
            validated.append({
                "step_id": str(raw["step_id"]),
                "title": str(raw["title"]),
                "required": 1 if required else 0,
                "parallel_with": parallel_with,
                "human_attention": ha.value,
                "model_tier": mt.value,
                "type": step_type,
                "process_template": None,
                "process_read_rules": None,
                "sort_order": i,
            })

        task_id = self._new_id()
        now = self._now()

        task_data = {
            "id": task_id,
            "type": "custom",
            "title": title,
            "description": description,
            "epic_id": None,
            "status": TaskStatus.ACTIVE.value,
            "pause_level": None,
            "assignee": "",
            "created_at": now,
            "steps": [],
        }

        await self.storage.create_task(task_data)
        await self.storage.add_steps(task_id, validated)

        await self.storage.append_event(task_id, {
            "event_type": EventType.ORCHESTRATION.value,
            "actor": EventActor.SYSTEM.value,
            "content": {
                "what_happened": f"Task '{title}' created with type 'custom'",
                "why_decided": f"Custom task with {len(validated)} dynamic steps",
                "steps_count": len(validated),
            },
            "timestamp": now,
        })

        logger.info(f"Custom task created: {task_id} ({title}, {len(validated)} steps)")
        return task_id

    async def advance_step(self, task_id: str, step_id: str, new_status: str,
                           tolerant: bool = False) -> None:
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        current_status = await self._get_step_status(task_id, step_id)
        if current_status is None:
            raise ValueError(f"Step not found: {step_id} in task {task_id}")

        if new_status == current_status:
            logger.info(f"Step {step_id} already {new_status}, idempotent no-op")
            return

        valid_transitions = {
            "pending": ["active", "skipped"],
            "active": ["completed", "stopped", "pending"],
            "stopped": ["active", "pending"],
        }

        allowed = valid_transitions.get(current_status, [])
        if new_status not in allowed:
            if tolerant:
                if new_status == "stopped" and current_status in ("pending", "stopped"):
                    logger.info(f"Step {step_id} tolerant no-op: {current_status} → "
                                f"{new_status} (external already changed state)")
                    return
                if new_status == "pending" and current_status in ("pending", "skipped"):
                    logger.info(f"Step {step_id} tolerant no-op: {current_status} → "
                                f"{new_status} (external already changed state)")
                    return
            raise ValueError(
                f"Invalid step transition: {current_status} → {new_status} "
                f"for step {step_id}. Allowed: {allowed}"
            )

        await self.storage.update_step_status(task_id, step_id, new_status)

        await self.storage.append_event(task_id, {
            "event_type": EventType.STEP_COMPLETE.value if new_status == "completed" else "step_status_change",
            "step_id": step_id,
            "actor": EventActor.SYSTEM.value,
            "content": {
                "what_happened": f"Step {step_id} status: {current_status} → {new_status}",
                "from_status": current_status,
                "to_status": new_status,
            },
            "timestamp": self._now(),
        })

        logger.info(f"Step {step_id}: {current_status} → {new_status}")

    async def start_step(self, task_id: str, step_id: str) -> None:
        await self.advance_step(task_id, step_id, StepStatus.ACTIVE.value)

    async def complete_step(self, task_id: str, step_id: str) -> None:
        await self.advance_step(task_id, step_id, StepStatus.COMPLETED.value)

    async def skip_step(self, task_id: str, step_id: str, reason: str = "") -> None:
        await self.advance_step(task_id, step_id, StepStatus.SKIPPED.value)
        logger.info(f"Step {step_id} skipped: {reason}")

    async def reset_step_for_continuation(self, task_id: str, step_id: str,
                                          reason: str = "续做重置") -> None:
        current_status = await self._get_step_status(task_id, step_id)
        if current_status is None:
            raise ValueError(f"Step not found: {step_id} in task {task_id}")
        if current_status == StepStatus.PENDING.value:
            return
        await self.storage.update_step_status(task_id, step_id, StepStatus.PENDING.value)
        await self.storage.append_event(task_id, {
            "event_type": "step_resumed",
            "step_id": step_id,
            "actor": EventActor.SYSTEM.value,
            "content": {
                "what_happened": f"Step {step_id} reset for continuation "
                                 f"({current_status} → pending)",
                "from_status": current_status,
                "to_status": "pending",
                "reason": reason,
            },
            "timestamp": self._now(),
        })
        logger.info(f"Step {step_id}: {current_status} → pending (continuation: {reason})")

    async def stop_step(self, task_id: str, step_id: str) -> None:
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        if await self._get_step_status(task_id, step_id) != StepStatus.ACTIVE.value:
            raise ValueError(f"Step {step_id} is not active, cannot stop")

        await self.storage.update_step_status(task_id, step_id, StepStatus.STOPPED.value)

        await self.storage.update_task(task_id, {
            "status": TaskStatus.PAUSED.value,
            "pause_level": PauseLevel.STEP.value,
        })

        await self.storage.append_event(task_id, {
            "event_type": "step_stopped",
            "step_id": step_id,
            "actor": EventActor.HUMAN.value,
            "content": {
                "what_happened": f"Step {step_id} stopped by user",
                "pause_level": PauseLevel.STEP.value,
            },
            "timestamp": self._now(),
        })

        logger.info(f"Step {step_id} stopped, task paused (step level)")

    async def resume_step(self, task_id: str, step_id: str, message: str = "") -> None:
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        if task["status"] not in (TaskStatus.PAUSED.value, TaskStatus.ACTIVE.value):
            raise ValueError(f"Task {task_id} is not paused or active, cannot resume")

        if await self._get_step_status(task_id, step_id) not in (
                StepStatus.STOPPED.value, StepStatus.ACTIVE.value):
            raise ValueError(f"Step {step_id} is not stopped or active, cannot resume")

        await self.storage.update_step_status(task_id, step_id, StepStatus.PENDING.value)

        await self.storage.update_task(task_id, {
            "status": TaskStatus.ACTIVE.value,
            "pause_level": None,
        })

        if message:
            intervention = {
                "type": "resume_message",
                "message": message,
                "timestamp": self._now(),
            }
            import json
            await self.storage.save_artifact(
                task_id, step_id, "intervention",
                json.dumps(intervention, ensure_ascii=False), "json"
            )

        await self.storage.append_event(task_id, {
            "event_type": "step_resumed",
            "step_id": step_id,
            "actor": EventActor.HUMAN.value,
            "content": {
                "what_happened": f"Step {step_id} resumed by user (stopped → pending)",
                "has_message": bool(message),
            },
            "timestamp": self._now(),
        })

        logger.info(f"Step {step_id} resumed (stopped → pending), task active. Orchestrator will pick it up.")

    async def flow_intervene(self, task_id: str, reason: str) -> None:
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        active_step = self._find_active_step(task)
        if active_step:
            await self.storage.update_step_status(
                task_id, active_step["step_id"], StepStatus.STOPPED.value
            )

        await self.storage.update_task(task_id, {
            "status": TaskStatus.PAUSED.value,
            "pause_level": PauseLevel.FLOW.value,
        })

        await self.storage.append_event(task_id, {
            "event_type": "flow_intervention",
            "step_id": active_step["step_id"] if active_step else None,
            "actor": EventActor.HUMAN.value,
            "content": {
                "what_happened": "Flow-level forced intervention",
                "reason": reason,
                "stopped_step": active_step["step_id"] if active_step else None,
            },
            "timestamp": self._now(),
        })

        import json
        intervention_data = {
            "type": "flow_forced",
            "reason": reason,
            "timestamp": self._now(),
        }
        await self.storage.save_artifact(
            task_id,
            active_step["step_id"] if active_step else "_flow",
            "intervention",
            json.dumps(intervention_data, ensure_ascii=False),
            "json",
        )

        logger.info(f"Flow intervention triggered for task {task_id}: {reason}")

    async def flow_pending_intervention(self, task_id: str, reason: str) -> None:
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        import json
        intervention_data = {
            "type": "flow_pending",
            "reason": reason,
            "timestamp": self._now(),
        }
        await self.storage.save_artifact(
            task_id, "_flow", "intervention",
            json.dumps(intervention_data, ensure_ascii=False),
            "json",
        )

        await self.storage.append_event(task_id, {
            "event_type": "flow_pending_intervention",
            "actor": EventActor.HUMAN.value,
            "content": {
                "what_happened": "Flow-level pending intervention queued",
                "reason": reason,
                "will_process_after": "all steps complete",
            },
            "timestamp": self._now(),
        })

        logger.info(f"Flow pending intervention queued for task {task_id}: {reason}")

    async def _resume_after_intervention(self, task_id: str) -> None:
        await self.storage.update_task(task_id, {
            "status": TaskStatus.ACTIVE.value,
            "pause_level": None,
        })

        await self.storage.append_event(task_id, {
            "event_type": "flow_intervention_complete",
            "actor": EventActor.SYSTEM.value,
            "content": {
                "what_happened": "Flow intervention completed, resuming task",
            },
            "timestamp": self._now(),
        })

    async def handle_gate(
        self, task_id: str, step_id: str, decision: str, reason: str = ""
    ) -> None:
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        step = self._find_step(task, step_id)
        if not step:
            raise ValueError(f"Step not found: {step_id}")

        if step.get("human_attention") != HumanAttention.GATE.value:
            raise ValueError(f"Step {step_id} is not a Gate step")

        await self.storage.append_event(task_id, {
            "event_type": EventType.GATE_DECISION.value,
            "step_id": step_id,
            "actor": EventActor.HUMAN.value,
            "content": {
                "what_happened": f"Gate decision: {decision}",
                "decision": decision,
                "reason": reason,
            },
            "timestamp": self._now(),
        })

        if decision == "approved":
            await self.complete_step(task_id, step_id)
        elif decision == "rejected":
            await self.storage.update_step_status(
                task_id, step_id, StepStatus.STOPPED.value
            )
            await self.storage.update_task(task_id, {
                "status": TaskStatus.PAUSED.value,
                "pause_level": PauseLevel.STEP.value,
            })
        else:
            raise ValueError(f"Invalid gate decision: {decision}")

    async def reject_gate(self, task_id: str, step_id: str, reason: str = "") -> None:
        import json

        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        step = self._find_step(task, step_id)
        if not step:
            raise ValueError(f"Step not found: {step_id}")

        await self.reset_step_for_continuation(task_id, step_id, "gate 驳回续做")

        await self.storage.save_artifact(
            task_id=task_id,
            step_id=step_id,
            artifact_type="intervention",
            content=json.dumps({"type": "gate_rejection", "message": reason}),
            content_format="json",
        )

        await self.storage.update_task(
            task_id, {"status": TaskStatus.PAUSED.value, "pause_level": PauseLevel.STEP.value}
        )

        await self.storage.append_event(task_id, {
            "event_type": EventType.GATE_DECISION.value,
            "step_id": step_id,
            "actor": EventActor.HUMAN.value,
            "content": {
                "what_happened": f"Gate rejected: {reason}",
                "decision": "rejected",
                "reason": reason,
            },
            "timestamp": self._now(),
        })

        logger.info(f"Gate rejected for {step_id}: {reason}")

    async def pause_task(self, task_id: str) -> None:
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        await self.storage.update_task(task_id, {
            "status": TaskStatus.PAUSED.value,
            "pause_level": "gate",
        })

        await self.storage.append_event(task_id, {
            "event_type": EventType.GATE_DECISION.value,
            "actor": EventActor.HUMAN.value,
            "content": {
                "what_happened": f"Task {task_id} paused by user",
                "pause_level": "gate",
            },
            "timestamp": self._now(),
        })

        logger.info(f"Task {task_id} paused (pause_level=gate)")

    async def handle_step_intervention(
        self, task_id: str, step_id: str, intervention_type: str, message: str
    ) -> None:
        if intervention_type == "stop":
            await self.stop_step(task_id, step_id)
            return

        if intervention_type not in ("send", "force_inject"):
            raise ValueError(f"Invalid intervention type: {intervention_type}")

        import json
        intervention_data = {
            "type": "pre_tool_injection" if intervention_type == "send" else "force_inject",
            "message": message,
            "timestamp": self._now(),
        }

        existing = await self.storage.get_artifact(task_id, step_id, "intervention")
        if existing:
            interventions = json.loads(existing["content"])
            if not isinstance(interventions, list):
                interventions = [interventions]
            interventions.append(intervention_data)
            content = json.dumps(interventions, ensure_ascii=False)
        else:
            content = json.dumps([intervention_data], ensure_ascii=False)

        await self.storage.save_artifact(task_id, step_id, "intervention", content, "json")

        await self.storage.append_event(task_id, {
            "event_type": "step_intervention",
            "step_id": step_id,
            "actor": EventActor.HUMAN.value,
            "content": {
                "what_happened": f"Step intervention: {intervention_type}",
                "intervention_type": intervention_type,
                "message_length": len(message),
            },
            "timestamp": self._now(),
        })

        logger.info(f"Step intervention [{intervention_type}] for {step_id}: {message[:50]}...")

    async def reset_flow_from_step(self, task_id: str, step_id: str,
                                   preserve_steps: tuple[str, ...] = ()) -> None:
        task = await self.storage.get_task(task_id, include_hidden=True)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        steps = task.get("steps", [])
        cur = next((s for s in steps if s.get("step_id") == step_id), None)
        if not cur:
            raise ValueError(f"Step not found: {step_id}")
        cur_order = cur.get("sort_order", 0)

        await self.storage.update_step_status(task_id, step_id, StepStatus.PENDING.value)
        await self.storage.delete_artifacts(task_id, step_id, "summary")
        await self.storage.delete_artifacts(task_id, step_id, "last_prompt_tokens")
        await self.storage.save_artifact(task_id, step_id, "intervention", "[]", "json")

        for s in steps:
            sid = s.get("step_id", "")
            if sid == step_id or is_hidden_step(s):
                continue
            if s.get("sort_order", 0) <= cur_order:
                continue
            await self.storage.clear_step_messages(task_id, sid)
            await self.storage.delete_artifacts(task_id, sid)
            await self.storage.update_step_status(task_id, sid, StepStatus.PENDING.value)

        hidden_rows = [s for s in steps if is_hidden_step(s)]
        monitor_rows = [s for s in hidden_rows
                        if s.get("type") == "monitor"
                        or str(s.get("step_id", "")).startswith("monitor-")]
        to_delete = [s["step_id"] for s in monitor_rows
                     if s.get("sort_order", 0) > cur_order
                     and s["step_id"] not in preserve_steps]
        if to_delete:
            await self.storage.remove_steps(task_id, to_delete)
        for s in hidden_rows:
            sid = s["step_id"]
            if sid in preserve_steps:
                await self.storage.update_step_status(task_id, sid, StepStatus.PENDING.value)
                continue
            if sid in to_delete:
                continue
            if s.get("sort_order", 0) <= cur_order:
                continue
            await self.storage.clear_step_messages(task_id, sid)
            await self.storage.delete_artifacts(task_id, sid)
            await self.storage.update_step_status(task_id, sid, StepStatus.PENDING.value)

        await self.storage.update_task(task_id, {
            "status": TaskStatus.ACTIVE.value,
            "pause_level": None,
        })

        await self.storage.append_event(task_id, {
            "event_type": "step_resumed",
            "step_id": step_id,
            "actor": EventActor.HUMAN.value,
            "content": {
                "what_happened": f"Step {step_id} reset for continuation (completed → flow)",
                "reason": "续做重置",
            },
            "timestamp": self._now(),
        })
        await self.storage.append_event(task_id, {
            "event_type": "step_status_change",
            "step_id": step_id,
            "actor": EventActor.SYSTEM.value,
            "content": {
                "what_happened": f"Step {step_id} status: completed → pending (续做重置)",
                "from_status": StepStatus.COMPLETED.value,
                "to_status": StepStatus.PENDING.value,
            },
            "timestamp": self._now(),
        })

    async def get_next_steps(self, task_id: str) -> list[dict]:
        task = await self.storage.get_task(task_id, include_hidden=True)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        steps = task.get("steps", [])

        active_steps = [s for s in steps
                        if s["status"] == StepStatus.ACTIVE.value]
        if active_steps:
            return []

        pending = [s for s in steps
                   if s["status"] == StepStatus.PENDING.value]
        if not pending:
            return []

        review = next((s for s in steps if s.get("step_id") == "review"), None)
        if review and review.get("status") == "stopped":
            pending = [s for s in pending if s.get("step_id") != "report"]
            if not pending:
                return []

        mi = next((s for s in pending
                   if str(s.get("step_id", "")).startswith("monitor-intervene")), None)
        if mi:
            return [mi]

        for s in pending:
            if not str(s.get("step_id", "")).startswith("monitor-"):
                continue
            try:
                raw = await self.storage.get_artifact(task_id, s["step_id"], "intervention")
            except Exception:  # noqa: BLE001
                continue
            if not raw or not raw.get("content"):
                continue
            try:
                ivs = json.loads(raw["content"])
            except (ValueError, TypeError):
                continue
            iv_list = ivs if isinstance(ivs, list) else [ivs]
            if any(isinstance(iv, dict)
                   and iv.get("type") in ("pre_tool_injection", "force_inject")
                   and iv.get("message") for iv in iv_list):
                return [s]

        parallel_ids = set()
        for s in steps:
            pw = s.get("parallel_with")
            if isinstance(pw, str):
                parsed = None
                for cand in [pw, pw.replace("\\", "")]:
                    try:
                        parsed = json.loads(cand)
                        break
                    except (ValueError, TypeError):
                        continue
                pw = parsed if isinstance(parsed, list) else \
                    [p.strip() for p in pw.split(",") if p.strip()]
            for p in (pw or []):
                parallel_ids.add(p)
                parallel_ids.add(s["step_id"])
        has_intervention = any(str(s.get("step_id", "")).startswith("monitor-intervene")
                               for s in steps)
        stopped_block = [s for s in steps
                         if s["status"] == StepStatus.STOPPED.value
                         and s.get("human_attention") != "gate"
                         and s["step_id"] not in parallel_ids
                         and not has_intervention]
        if stopped_block:
            return []

        pending.sort(key=lambda s: s.get("sort_order", 0))
        next_step = pending[0]

        result = [next_step]
        parallel_ids = next_step.get("parallel_with", [])
        if parallel_ids:
            for s in pending:
                if s["step_id"] in parallel_ids:
                    result.append(s)

        return result

    async def is_task_complete(self, task_id: str) -> bool:
        task = await self.storage.get_task(task_id)
        if not task:
            return False

        steps = task.get("steps", [])
        return all(
            s["status"] in (StepStatus.COMPLETED.value, StepStatus.SKIPPED.value)
            for s in steps
        )

    async def complete_task(self, task_id: str) -> None:
        await self.storage.update_task(task_id, {
            "status": TaskStatus.COMPLETED.value,
            "pause_level": None,
        })

        await self.storage.append_event(task_id, {
            "event_type": EventType.ORCHESTRATION.value,
            "actor": EventActor.SYSTEM.value,
            "content": {
                "what_happened": "Task completed - all steps finished",
            },
            "timestamp": self._now(),
        })

        logger.info(f"Task {task_id} completed")

    async def abandon_task(self, task_id: str, reason: str = "") -> None:
        await self.storage.update_task(task_id, {
            "status": TaskStatus.ABANDONED.value,
            "pause_level": None,
        })

        await self.storage.append_event(task_id, {
            "event_type": EventType.ORCHESTRATION.value,
            "actor": EventActor.HUMAN.value,
            "content": {
                "what_happened": f"Task abandoned: {reason}",
                "reason": reason,
            },
            "timestamp": self._now(),
        })

    async def _get_step_status(self, task_id: str, step_id: str) -> Optional[str]:
        task = await self.storage.get_task(task_id, include_hidden=True)
        if not task:
            return None
        step = self._find_step(task, step_id)
        if step:
            return step["status"]
        return None

    def _find_step(self, task: dict, step_id: str) -> Optional[dict]:
        for s in task.get("steps", []):
            if s["step_id"] == step_id:
                return s
        return None

    def _find_active_step(self, task: dict) -> Optional[dict]:
        for s in task.get("steps", []):
            if s["status"] == StepStatus.ACTIVE.value:
                return s
        return None
