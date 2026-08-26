
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from typing import TYPE_CHECKING

from .models.data_models import StepStatus
from .state_machine.task_types import get_task_type
from .storage.adapter import StorageAdapter
from .prompts import load_prompt
from .prompts.registry import prompt_for_step

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

class MonitorContext:

    def __init__(self, storage: StorageAdapter):
        self.storage = storage

    async def get_monitor_context(self, task_id: str, step_id: str = "") -> dict:
        tmp_dir = tempfile.mkdtemp(prefix=f"dimensioncoding_monitor_{task_id}_")
        try:
            context = await self._build_monitor_context(task_id, step_id)
            export_dir = await self._export_monitor_context(context, tmp_dir, task_id)
            messages = await self._build_messages(context, export_dir=export_dir)
        finally:
            self._cleanup_tmp_dir(tmp_dir)

        task = context.get("task", {})
        pending = context.get("pending_steps", [])

        return {
            "messages": messages,
            "task_id": task_id,
            "current_step_id": step_id,
            "pending_steps": [
                {"step_id": s["step_id"], "title": s.get("title", "")}
                for s in pending
            ],
        }

    async def apply_monitor_decision(self, task_id: str, decision: dict) -> str:
        decision = self._validate_decision(decision)
        action = decision.get("action", "no_change")

        if action != "no_change":
            await self._apply_decision(task_id, decision)

        logger.info(f"Monitor decision applied: {action}")
        return action

    async def get_final_review_context(self, task_id: str) -> dict:
        tmp_dir = tempfile.mkdtemp(prefix=f"dimensioncoding_monitor_final_{task_id}_")
        try:
            context = await self._build_monitor_context(task_id)
            context["check_type"] = "final_review"
            export_dir = await self._export_monitor_context(context, tmp_dir, task_id)
            messages = await self._build_messages(context, include_final_review=True, export_dir=export_dir)
        finally:
            self._cleanup_tmp_dir(tmp_dir)

        return {
            "messages": messages,
            "task_id": task_id,
        }

    async def _build_monitor_context(self, task_id: str, current_step_id: str = "") -> dict:
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        task_type_def = get_task_type(task.get("type", ""))

        completed_conversations = {}
        steps = task.get("steps", [])

        for step in steps:
            if step["status"] == StepStatus.COMPLETED.value:
                sid = step["step_id"]
                conv = await self.storage.get_conversation(task_id, sid)
                if conv:
                    completed_conversations[sid] = conv

        if current_step_id and current_step_id not in completed_conversations:
            conv = await self.storage.get_conversation(task_id, current_step_id)
            if conv:
                completed_conversations[current_step_id] = conv

        pending_steps = [s for s in steps if s["status"] == StepStatus.PENDING.value]

        return {
            "task": task,
            "task_type_def": task_type_def,
            "completed_conversations": completed_conversations,
            "current_step_id": current_step_id,
            "pending_steps": pending_steps,
        }

    async def _export_monitor_context(self, context: dict, tmp_dir: str, task_id: str) -> str:
        conv_dir = os.path.join(tmp_dir, "conversations")
        os.makedirs(conv_dir, exist_ok=True)
        completed_conv = context.get("completed_conversations", {})
        for sid, conv_msgs in completed_conv.items():
            fpath = os.path.join(conv_dir, f"{sid}-conversation.json")
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(conv_msgs, f, ensure_ascii=False, indent=2)

        task = context.get("task", {})
        ctx_path = os.path.join(tmp_dir, "task_context.json")
        with open(ctx_path, "w", encoding="utf-8") as f:
            json.dump({
                "task_id": task.get("id", task_id),
                "type": task.get("type", ""),
                "title": task.get("title", ""),
                "status": task.get("status", ""),
                "steps": task.get("steps", []),
            }, f, ensure_ascii=False, indent=2)

        return tmp_dir

    async def _build_messages(
        self,
        context: dict,
        include_final_review: bool = False,
        export_dir: str = "",
    ) -> list[dict]:
        user_parts = []

        task = context.get("task", {})
        user_parts.append(f"# 任务信息\n- 标题: {task.get('title', '')}\n- 类型: {task.get('type', '')}\n- 状态: {task.get('status', '')}")

        steps = task.get("steps", [])
        status_lines = []
        for s in steps:
            mark = "✓" if s["status"] == "completed" else "◆" if s["status"] == "active" else "⏸" if s["status"] == "stopped" else "─"
            status_lines.append(
                f"  [{mark}] {s['step_id']}: {s.get('title', '')} ({s['status']}"
                f"{', gate' if s.get('human_attention') == 'gate' else ''}"
                f", type={s.get('type', 'executor')})")
        user_parts.append("# 当前步骤状态\n" + "\n".join(status_lines))

        if export_dir:
            conv_dir = os.path.join(export_dir, "conversations")
            if os.path.isdir(conv_dir):
                user_parts.append(f"# 已完成步骤的完整对话记录\n导出目录: {export_dir}")
                for fname in sorted(os.listdir(conv_dir)):
                    if fname.endswith(".json"):
                        fpath = os.path.join(conv_dir, fname)
                        with open(fpath, encoding="utf-8") as f:
                            try:
                                msgs = json.load(f)
                            except json.JSONDecodeError:
                                msgs = []
                        step_id = fname.replace("-conversation.json", "")
                        user_parts.append(f"\n## {step_id} 对话 ({len(msgs)} 条消息)")
                        for m in msgs:
                            role = m.get("role", "?")
                            content = str(m.get("content", ""))
                            user_parts.append(f"[{role}]: {content}")
                        user_parts.append("")

        pending = context.get("pending_steps", [])
        if pending:
            pending_lines = [f"  - {s['step_id']}: {s.get('title', '')} (required={s.get('required', True)})" for s in pending]
            user_parts.append("# 待执行步骤\n" + "\n".join(pending_lines))

        if include_final_review:
            user_parts.append(load_prompt(prompt_for_step("review", "review")))

        current_step = context.get("current_step_id", "")
        if current_step:
            user_parts.append(f"\n刚刚完成的步骤: {current_step}")

        try:
            from .config import get_memory_config
            mem_cfg = get_memory_config()
            if mem_cfg.get("enabled"):
                from .rest_api import _get_memory_storage
                ms = _get_memory_storage()
                if ms is not None:
                    from .config import PROJECT_ROOT
                    bank_id = ms.get_or_create_bank_for_project(PROJECT_ROOT)
                    from .memory import get_recaller
                    recaller = get_recaller(ms, mem_cfg)
                    recall_result = await recaller.recall(
                        bank_id=bank_id,
                        query="编排决策 " + str(context.get("current_summary", "")),
                        max_tokens=mem_cfg.get("recall_max_tokens", 2048),
                        budget="mid",
                    )
                    if recall_result.get("results"):
                        mem_text = ms.format_recall_for_prompt(recall_result)
                        user_parts.append(f"## 历史经验（来自记忆库）\n{mem_text}")
        except Exception:
            logger.warning("[DC:monitor] memory recall skipped (disabled or error)", exc_info=True)

        messages = [
            {"role": "system", "content": load_prompt(prompt_for_step("monitor-init", "monitor"))},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]

        return messages

    def _validate_decision(self, decision: dict) -> dict:
        valid_actions = {
            "no_change", "skip_steps", "add_steps", "remove_steps",
            "reorder_steps", "mark_complete",
        }

        action = decision.get("action", "no_change")
        if action not in valid_actions:
            logger.warning(f"Monitor invalid action: {action}, fallback to no_change")
            decision["action"] = "no_change"

        if "reasoning" not in decision:
            decision["reasoning"] = "(no reasoning provided)"

        return decision

    async def _apply_decision(self, task_id: str, decision: dict) -> None:
        action = decision.get("action")

        if action == "skip_steps":
            step_ids = decision.get("step_ids", [])
            for sid in step_ids:
                await self.storage.update_step_status(task_id, sid, StepStatus.SKIPPED.value)

        elif action == "add_steps":
            new_steps = decision.get("steps", [])
            if new_steps:
                await self.storage.add_steps(task_id, new_steps)

        elif action == "remove_steps":
            step_ids = decision.get("step_ids", [])
            if step_ids:
                await self.storage.remove_steps(task_id, step_ids)

        elif action == "reorder_steps":
            new_order = decision.get("order", [])
            if new_order:
                await self.storage.reorder_steps(task_id, new_order)

        await self.storage.append_event(task_id, {
            "event_type": "orchestration",
            "actor": "ai",
            "content": {
                "what_happened": f"Monitor decision: {action}",
                "action": action,
                "reasoning": decision.get("reasoning", ""),
                "details": {k: v for k, v in decision.items() if k not in ("action", "reasoning")},
            },
        })

    def _cleanup_tmp_dir(self, tmp_dir: str) -> None:
        try:
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Failed to cleanup tmp dir {tmp_dir}: {e}")
