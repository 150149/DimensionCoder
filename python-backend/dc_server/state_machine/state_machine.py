
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
    """parallel_with 归一化（P2-11 修订，硬性）。

    接受 null / str（逗号分隔）/ list[str]，一律归一化为 list[str] 或 null；
    其他类型（如 dict）抛 ValueError。
    """
    if value is None:
        return None
    if isinstance(value, str):
        parts = [s.strip() for s in value.split(",") if s.strip()]
        return parts or None
    if isinstance(value, list):
        return [str(s) for s in value]
    raise ValueError(f"invalid parallel_with: {value!r} (must be null, str or list[str])")


class StateMachine:
    """
    状态机引擎。

    职责：
    - Task 创建/生命周期管理
    - 步骤状态转移 (pending → active → completed/skipped/stopped)
    - 步骤内介入 (💬排队注入 / ⛔强制插入 / ⏹打断步骤)
    - 流程级介入 (🛑强制介入 / 💬排队调整)
    - Gate 审批处理
    - 获取下一个可执行步骤
    """

    def __init__(
        self,
        storage: StorageAdapter,
    ):
        self.storage = storage

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _new_id(self) -> str:
        return str(uuid.uuid4())

    # ═══════════════════════════════════════════════════════════════
    # Task 创建
    # ═══════════════════════════════════════════════════════════════

    async def create_task(
        self,
        task_type: str,
        title: str,
        description: str = "",
        epic_id: Optional[str] = None,
        assignee: str = "",
        workspace_dir: Optional[str] = None,
    ) -> str:
        """
        创建 Task（空步骤，流程由 Monitor 初始编排生成）。

        预设模板已文本化退役（prompts/flow-templates.md）：新任务一律创建为
        空步骤任务，Monitor 在创建后自动编排（add_steps）生成完整流程。
        task_type 仅作展示字段保留（旧版从 TASK_TYPES 复制模板步骤的逻辑已移除）。
        workspace_dir（2026-08-26 用户需求）：自定义工作目录（空 = 自动分配
        workspace/<tid>/）；非空则 AI 相对路径基准为该目录。
        """
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
            "workspace_dir": workspace_dir,
            "created_at": now,
            "steps": [],
        }

        await self.storage.create_task(task_data)

        # 写入创建事件
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
        """创建 custom 类型任务并写入动态步骤。steps 每项必须含:
        step_id/title/required/parallel_with/human_attention/model_tier/sort_order，
        human_attention ∈ {none,notify,review,gate}，model_tier ∈ {light,power}，
        required ∈ {0,1}；type 可选（executor|gate|plan|code_review，缺省 executor）。
        字段非法抛 ValueError。"""
        if not isinstance(steps, list) or not steps:
            raise ValueError("steps required for custom task")

        validated: list[dict] = []
        for i, raw in enumerate(steps):
            if not isinstance(raw, dict):
                raise ValueError(f"step {i} must be a dict")

            # 必含字段（sort_order 除外——规定：按列表顺序自增，忽略传入冲突）
            for field in ("step_id", "title", "required", "parallel_with",
                          "human_attention", "model_tier"):
                if field not in raw:
                    raise ValueError(f"step {i} missing field: {field}")

            # 枚举复用（禁止手写字符串集合）：值不在枚举内即抛 ValueError
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

            # parallel_with 归一化（P2-11）：null / str（逗号分隔）/ list[str]
            # → list[str] 或 null；其他类型抛 ValueError
            parallel_with = _normalize_parallel_with(raw["parallel_with"])

            # gate 禁并行（问题 5）：gate 步骤 parallel_with 必须为 null
            if ha == HumanAttention.GATE and parallel_with is not None:
                raise ValueError(
                    f"gate step {raw['step_id']} cannot have parallel_with"
                )

            # process 字段（P2-10）：custom 无预设模板，必须置 null
            # type（可选）：executor|gate|plan|code_review，缺省 executor；_ 前缀为系统保留
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
                # sort_order 按列表顺序自增，忽略传入值（P2-10 规定）
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
            "steps": [],  # 步骤经 add_steps 逐条写入（sort_order 按序自增）
        }

        await self.storage.create_task(task_data)
        await self.storage.add_steps(task_id, validated)

        # 写入创建事件
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

    # ═══════════════════════════════════════════════════════════════
    # 步骤状态转移
    # ═══════════════════════════════════════════════════════════════

    async def advance_step(self, task_id: str, step_id: str, new_status: str,
                           tolerant: bool = False) -> None:
        """
        推进步骤状态。

        合法转移:
        - pending → active (开始执行)
        - active → completed (执行完成)
        - active → stopped (用户打断)
        - stopped → active (恢复执行)
        - pending → skipped (Monitor 决定跳过)

        tolerant=True（2026-08-21 竞态宽容）：外部操作（暂停/停止/恢复）可能已在
        调用前修改了步骤状态，目标语义已被满足（如想置 stopped 但已是 pending/
        stopped）→ 幂等返回不抛错（19:53 崩溃实证：pending→stopped 非法转移崩溃）。
        """
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        # 2026-08-20：_load_steps 过滤虚拟步骤（_monitor:* 实例行不可见），
        # 状态读取需兜底查虚拟表——Monitor 多实例复用本状态机的关键前提
        current_status = await self._get_step_status(task_id, step_id)
        if current_status is None:
            raise ValueError(f"Step not found: {step_id} in task {task_id}")

        # C2 幂等修订（WP2-2 T2.3 步骤 7）：同状态转移（如 stopped→stopped）
        # 幂等成功不抛错，直接返回（不写事件）。
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
                # 竞态宽容：目标为中止类（stopped）且当前已是 pending/stopped——
                # 外部已把步骤移出 active（暂停/停止链），语义已达成，幂等返回
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

        # 写入事件
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
        """开始执行步骤 (pending → active)"""
        await self.advance_step(task_id, step_id, StepStatus.ACTIVE.value)

    async def complete_step(self, task_id: str, step_id: str) -> None:
        """
        完成步骤 (active → completed)。

        *注意*: Monitor 编排和调度器继续由 TypeScript 端 Orchestrator 的
        `_executionLoop` 和 `_monitorOrchestrate` 负责，Python 端不再触发。
        """
        await self.advance_step(task_id, step_id, StepStatus.COMPLETED.value)


    async def skip_step(self, task_id: str, step_id: str, reason: str = "") -> None:
        """跳过步骤 (pending → skipped)，Monitor 决定"""
        await self.advance_step(task_id, step_id, StepStatus.SKIPPED.value)
        logger.info(f"Step {step_id} skipped: {reason}")

    async def reset_step_for_continuation(self, task_id: str, step_id: str,
                                          reason: str = "续做重置") -> None:
        """终态豁免入口（2026-08-21 规范化）：任意状态 → pending（续做/rebuild/
        介入审查复用行回收/驳回重跑）。状态机 completed/skipped 无出边（R5），
        续做语义由本方法统一承担——写 step_resumed 事件，替换散落的裸更新。
        active/stopped 也可重置（竞态宽容：外部已介入时无冲突）。"""
        current_status = await self._get_step_status(task_id, step_id)
        if current_status is None:
            raise ValueError(f"Step not found: {step_id} in task {task_id}")
        if current_status == StepStatus.PENDING.value:
            return  # 已是目标，幂等
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

    # ═══════════════════════════════════════════════════════════════
    # 步骤内介入 (⏹打断 / 恢复)
    # ═══════════════════════════════════════════════════════════════

    async def stop_step(self, task_id: str, step_id: str) -> None:
        """
        用户 ⏹ 打断步骤。

        - 步骤标记 stopped
        - Task 状态变为 paused (pause_level=step)
        """
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        # 2026-08-20：虚拟步骤（_monitor:*）实例行被 _load_steps 过滤，
        # 状态读取兜底查虚拟表（Monitor 页 stop 的前置校验）
        if await self._get_step_status(task_id, step_id) != StepStatus.ACTIVE.value:
            raise ValueError(f"Step {step_id} is not active, cannot stop")

        # 步骤 → stopped
        await self.storage.update_step_status(task_id, step_id, StepStatus.STOPPED.value)

        # Task → paused (step level)
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
        """
        用户发送消息恢复被打断的步骤。

        - 步骤从 stopped → pending（让 TS Orchestrator 的 get_next_step 重新捡起）
        - Task 从 paused → active, pause_level 清除
        - message 作为介入消息注入（写入 intervention 产物）

        *注意*: 不再调用 scheduler.resume_execution() — 实际执行由
        TypeScript 端 Orchestrator 的 _executionLoop 驱动。
        """
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        # A3 语义修订（WP2-2 T2.3 步骤 6 + B1 步骤 9，审查五第 2 条）：
        # - task 前置放宽为 active 或 paused 均可恢复（旧代码要求必须 paused，
        #   llmError 重试链路因此断裂——审查 A3）；
        # - step 前置放宽为 {"stopped", "active"}（llmError 中断时步骤保持
        #   active，重试须能恢复——B1 场景②）。
        if task["status"] not in (TaskStatus.PAUSED.value, TaskStatus.ACTIVE.value):
            raise ValueError(f"Task {task_id} is not paused or active, cannot resume")

        # 2026-08-20：虚拟步骤（_monitor:*）实例行被 _load_steps 过滤，
        # 状态读取兜底查虚拟表（Monitor 页 resume 的前置校验）
        if await self._get_step_status(task_id, step_id) not in (
                StepStatus.STOPPED.value, StepStatus.ACTIVE.value):
            raise ValueError(f"Step {step_id} is not stopped or active, cannot resume")

        # 步骤 → pending（让 TS Orchestrator 重新捡起）
        await self.storage.update_step_status(task_id, step_id, StepStatus.PENDING.value)

        # Task → active
        await self.storage.update_task(task_id, {
            "status": TaskStatus.ACTIVE.value,
            "pause_level": None,
        })

        # 如果有恢复消息，写入 intervention 产物
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

    # ═══════════════════════════════════════════════════════════════
    # 流程级介入 (🛑强制介入 / 💬排队调整)
    # ═══════════════════════════════════════════════════════════════

    async def flow_intervene(self, task_id: str, reason: str) -> None:
        """
        🛑 流程级强制介入。

        1. 当前 active 步骤标记 stopped
        2. Task → paused (pause_level=flow)
        3. 调用 Monitor Agent（全部对话记录 + reason）
        4. Monitor 调整步骤后 → Task 恢复 active → 调度器继续
        """
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        # 找到当前 active 步骤并打断
        active_step = self._find_active_step(task)
        if active_step:
            await self.storage.update_step_status(
                task_id, active_step["step_id"], StepStatus.STOPPED.value
            )

        # Task → paused (flow level)
        await self.storage.update_task(task_id, {
            "status": TaskStatus.PAUSED.value,
            "pause_level": PauseLevel.FLOW.value,
        })

        # 写入 intervention 事件
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

        # 写入 intervention 产物（供 Monitor 读取）
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

        # *注意*: TypeScript 端 Orchestrator 负责在 flowIntervene 后主动触发 Monitor
        # （见 extension.ts flowIntervene handler + Orchestrator.triggerMonitor）
        # Python 端不再调用 monitor_agent.handle_flow_intervention()
        #
        # *注意2*: 不在这里调用 _resume_after_intervention() —
        # TS 端 triggerMonitor 完成 Monitor 编排后，会自行重启 orchestrator，
        # orchestrator 的 _executionLoop 重新捡起 pending 步骤。

    async def flow_pending_intervention(self, task_id: str, reason: str) -> None:
        """
        💬 流程级排队调整。

        不打断当前流程，将 reason 写入 intervention 产物。
        等流程所有步骤自然完成后，Monitor 读取并调整。
        """
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        # 写入 intervention 产物（排队类型）
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
        """
        Monitor 完成介入调整后，恢复 Task 状态。

        *注意*: 不再调用 scheduler.resume_execution() — TS 端 Orchestrator
        会在 triggerMonitor 完成后自行重启 _executionLoop。
        """
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

    # ═══════════════════════════════════════════════════════════════
    # Gate 审批
    # ═══════════════════════════════════════════════════════════════

    async def handle_gate(
        self, task_id: str, step_id: str, decision: str, reason: str = ""
    ) -> None:
        """
        处理 Gate 审批结果。

        decision: "approved" | "rejected"
        - approved: 步骤完成，继续下一步
        - rejected: 步骤标记 stopped，等待重新执行或人工处理
        """
        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        step = self._find_step(task, step_id)
        if not step:
            raise ValueError(f"Step not found: {step_id}")

        if step.get("human_attention") != HumanAttention.GATE.value:
            raise ValueError(f"Step {step_id} is not a Gate step")

        # 写入 Gate 决策事件
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
            # Gate 通过 → 步骤完成（TypeScript Orchestrator 会在 approveGate handler 中重启）
            await self.complete_step(task_id, step_id)
        elif decision == "rejected":
            # Gate 拒绝 → 步骤停止
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
        """
        Gate 拒绝后重置步骤为 pending（对话续接模式）。
        与 handle_gate rejected 不同：这里保持 completed 对话记录，
        将步骤重置为 pending 并注入拒绝原因，下次执行时 AI 可见。
        """
        import json

        task = await self.storage.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        step = self._find_step(task, step_id)
        if not step:
            raise ValueError(f"Step not found: {step_id}")

        # 步骤 completed → pending（保留对话记录，2026-08-21 规范化：走豁免入口）
        await self.reset_step_for_continuation(task_id, step_id, "gate 驳回续做")

        # 写入拒绝原因为 intervention artifact
        await self.storage.save_artifact(
            task_id=task_id,
            step_id=step_id,
            artifact_type="intervention",
            content=json.dumps({"type": "gate_rejection", "message": reason}),
            content_format="json",
        )

        # Task 暂停（等 Orchestrator 重启）
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
        """
        暂停任务（端点 30，H11 修复）。

        Task → paused，pause_level="gate"（字符串约定——M2 注明：
        不扩展 PauseLevel 枚举，旧 data_models.py 只有 step/flow）。
        实现与 reject_gate 的 task 更新段相同。
        """
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

    # ═══════════════════════════════════════════════════════════════
    # 步骤内介入 (💬排队注入 / ⛔强制插入)
    # ═══════════════════════════════════════════════════════════════

    async def handle_step_intervention(
        self, task_id: str, step_id: str, intervention_type: str, message: str
    ) -> None:
        """
        处理步骤内介入。

        intervention_type:
        - "send" (💬排队注入): 写入 intervention 产物，下次介入窗口注入
        - "force_inject" (⛔强制插入): 写入 intervention 产物，立即中断 LLM
        - "stop" (⏹打断步骤): 调用 stop_step
        """
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

        # 追加到 intervention 产物（使用 append 模式）
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
        """
        已完成步骤续做重置（用户向 completed 步骤发消息触发）。

        - 当前步骤：completed → pending（保留对话消息，续做上下文），
          删过期 summary、清 intervention（旧介入防重复注入，随后 send 追加新消息）、
          删 last_prompt_tokens（旧上下文 token 记录）
        - 后续真实步骤（sort_order 更大）：清 step_messages/chunks，删全部产物
          （summary/monitor_conversation/intervention/last_prompt_tokens），状态 → pending
        - 审查/收尾步骤（2026-08-21 去绑定普通化）：monitor 类型（monitor-N /
          monitor-intervene-N / monitor-init）是步骤完成后动态创建的非占位产物——
          后续（sort_order 更大）的 monitor 行直接删除（旧内容已清空无保留意义，
          步骤重跑完成时自然重建）；preserve_steps 指定行保留（rebuild 续跑当前
          实例，状态 → pending）；review/report（收尾特判保留）仍清消息/产物/置
          pending
        - task → active（清 pause_level）；写 step_resumed + step_status_change 事件
        - 保留：key_findings（_flow，跨步骤知识）、_flow 级 intervention、events 审计
        """
        task = await self.storage.get_task(task_id, include_hidden=True)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        steps = task.get("steps", [])
        cur = next((s for s in steps if s.get("step_id") == step_id), None)
        if not cur:
            raise ValueError(f"Step not found: {step_id}")
        cur_order = cur.get("sort_order", 0)

        # 当前步骤：状态 → pending（续做），清过期产物
        await self.storage.update_step_status(task_id, step_id, StepStatus.PENDING.value)
        await self.storage.delete_artifacts(task_id, step_id, "summary")
        await self.storage.delete_artifacts(task_id, step_id, "last_prompt_tokens")
        await self.storage.save_artifact(task_id, step_id, "intervention", "[]", "json")

        # 后续真实步骤：消息/产物/状态全重置（跳过审查/收尾步骤——它们由下方
        # 虚拟清理段统一处理，preserve_steps 保留当前 monitor 消息）
        for s in steps:
            sid = s.get("step_id", "")
            if sid == step_id or is_hidden_step(s):
                continue
            if s.get("sort_order", 0) <= cur_order:
                continue
            await self.storage.clear_step_messages(task_id, sid)
            await self.storage.delete_artifacts(task_id, sid)
            await self.storage.update_step_status(task_id, sid, StepStatus.PENDING.value)

        # 审查/收尾步骤（2026-08-21 去绑定普通化）：monitor 类型（monitor-N /
        # monitor-intervene-N / monitor-init）是动态创建的非占位产物——后续
        # （sort_order 大于锚点）的 monitor 行直接删除（行+消息+产物），步骤重跑
        # 完成时 _insert_monitor_step 自然重建；preserve_steps 指定行保留（rebuild
        # 续跑当前实例，仅状态 → pending）。review/report（收尾特判保留）仍走
        # 清消息/产物/置 pending。
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
                # 保留消息/产物（当前 monitor 实例），仅状态 → pending
                await self.storage.update_step_status(task_id, sid, StepStatus.PENDING.value)
                continue
            if sid in to_delete:
                continue
            if s.get("sort_order", 0) <= cur_order:
                # 锚点之前的审查/收尾步骤：已完成历史，保持原状态与消息
                continue
            await self.storage.clear_step_messages(task_id, sid)
            await self.storage.delete_artifacts(task_id, sid)
            await self.storage.update_step_status(task_id, sid, StepStatus.PENDING.value)

        # task → active（清 pause_level）
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

    # ═══════════════════════════════════════════════════════════════
    # 查询辅助
    # ═══════════════════════════════════════════════════════════════

    async def get_next_steps(self, task_id: str) -> list[dict]:
        """
        获取下一个应执行的步骤列表。

        规则:
        1. 找到所有 pending 步骤中 sort_order 最小的
        2. 如果该步骤有 parallel_with，返回所有并行步骤
        3. 如果有 active 步骤，返回空（等待当前步骤完成）
        """
        task = await self.storage.get_task(task_id, include_hidden=True)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        steps = task.get("steps", [])

        # 有步骤 active → 不返回新步骤（串行推进语义）。实体化（2026-08-21）：
        # monitor/review/report 参与执行循环，active 同样阻塞（不再有虚拟排除）
        active_steps = [s for s in steps
                        if s["status"] == StepStatus.ACTIVE.value]
        if active_steps:
            return []

        # 找到所有 pending 步骤（含 monitor/review/report——实体化后参与执行循环）
        pending = [s for s in steps
                   if s["status"] == StepStatus.PENDING.value]
        if not pending:
            return []  # 没有 pending 步骤

        # 2026-08-21（评审确认）：RV 失败阻塞——review 处于 stopped 时 report 不得
        # 拾取（审查未完成不得产出报告；等用户恢复 RV 重跑后再收尾）。
        # monitor-intervene 仍优先（介入可处理 RV 失败场景，见下）
        review = next((s for s in steps if s.get("step_id") == "review"), None)
        if review and review.get("status") == "stopped":
            pending = [s for s in pending if s.get("step_id") != "report"]
            if not pending:
                return []

        # 2026-08-21（DB 实证 e726f3e6 19:41）：介入审查优先——pending 的
        # monitor-intervene-*（用户介入消息待消费）必须最先执行，即使其 sort_order
        # 在 gate 步骤之后（恢复全部逐个 resume / 历史数据可能未移动排序，19:41
        # 实证：恢复后 gate step-17(80) 先于 monitor-intervene(90) 被拾取，用户
        # 消息被只读 gate 吞掉再次空转，流程"不动"）
        # 2026-08-21 去绑定普通化：monitor-intervene-N 多实例，前缀匹配
        mi = next((s for s in pending
                   if str(s.get("step_id", "")).startswith("monitor-intervene")), None)
        if mi:
            return [mi]

        # 2026-08-22 扩展（DB 实证 e726f3e6 05:04）：有未消费介入消息的 monitor 步骤
        # （monitor-N / monitor-init）也优先——gate 暂停期间用户给 monitor 发消息
        # = 恢复后先消费；monitor 带介入时不得被 sort_order 更小的 gate 抢占
        # （否则 gate 反复执行→提交决策包→任务又 paused→死循环）
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

        # 2026-08-23（用户反馈 99248a9f）：非 gate 的 stopped 步骤阻塞流程——跳过它
        # 跑后续 pending 会越过中断点（用户点「继续」后直接跑待执行）；必须显式恢复
        # （前端「恢复执行/恢复全部/继续」会 resume 后重启）。例外（不阻塞）：
        # 1) 并行组内成员（组内独立推进语义，A73 既有设计）；
        # 2) 介入打断（存在 monitor-intervene = 用户已介入处理，immediate 流程
        #    既有序继续；monitor-intervene 本身优先执行，其 stopped 不被这里挡）。
        parallel_ids = set()
        for s in steps:
            pw = s.get("parallel_with")
            # DB 读回为 JSON 字符串（存储序列化）；兼容旧逗号分隔与直写脏 JSON
            # （A73 测试直写 '[\\"step-1\\"]' 反斜杠转义引号——json.loads 严格失败）
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

        # 找 sort_order 最小的 pending 步骤
        pending.sort(key=lambda s: s.get("sort_order", 0))
        next_step = pending[0]

        # 检查是否有并行步骤
        result = [next_step]
        parallel_ids = next_step.get("parallel_with", [])
        if parallel_ids:
            for s in pending:
                if s["step_id"] in parallel_ids:
                    result.append(s)

        return result

    async def is_task_complete(self, task_id: str) -> bool:
        """判断 Task 是否所有步骤都已完成（completed/skipped）"""
        task = await self.storage.get_task(task_id)
        if not task:
            return False

        steps = task.get("steps", [])
        return all(
            s["status"] in (StepStatus.COMPLETED.value, StepStatus.SKIPPED.value)
            for s in steps
        )

    async def complete_task(self, task_id: str) -> None:
        """标记 Task 为完成"""
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

        # 2026-08-22：任务终态不再清理 .dc_tmp 目录（此前 2026-08-21 即刻清理）——
        # 用户确认任务结束后保留目录到重启（启动兜底清理会清已完成任务目录、
        # 保留进行中任务目录），便于回看各步骤 AI 产出文件

        logger.info(f"Task {task_id} completed")

    async def abandon_task(self, task_id: str, reason: str = "") -> None:
        """废弃 Task"""
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

        # 2026-08-22：终态保留（与 complete_task 同策略，目录保留到重启）

    # ═══════════════════════════════════════════════════════════════
    # 内部辅助
    # ═══════════════════════════════════════════════════════════════

    async def _get_step_status(self, task_id: str, step_id: str) -> Optional[str]:
        """读取步骤状态（2026-08-21 实体化：审查/收尾步骤也是真实行，全量查询
        直接命中）。返回 None 表示步骤不存在。"""
        task = await self.storage.get_task(task_id, include_hidden=True)
        if not task:
            return None
        step = self._find_step(task, step_id)
        if step:
            return step["status"]
        return None

    def _find_step(self, task: dict, step_id: str) -> Optional[dict]:
        """在 Task 中查找步骤"""
        for s in task.get("steps", []):
            if s["step_id"] == step_id:
                return s
        return None

    def _find_active_step(self, task: dict) -> Optional[dict]:
        """找到当前 active 的步骤"""
        for s in task.get("steps", []):
            if s["status"] == StepStatus.ACTIVE.value:
                return s
        return None
