
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any
import uuid


# ═══════════════════════════════════════════════════════════════════
# 枚举类型
# ═══════════════════════════════════════════════════════════════════


class TaskStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class PauseLevel(str, Enum):
    STEP = "step"   # 步骤内暂停（⏹打断步骤）
    FLOW = "flow"   # 流程级暂停（🛑强制介入）


class StepStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    STOPPED = "stopped"


class HumanAttention(str, Enum):
    NONE = "none"
    NOTIFY = "notify"
    REVIEW = "review"
    GATE = "gate"


class ModelTier(str, Enum):
    LIGHT = "light"
    POWER = "power"


class ArtifactType(str, Enum):
    RESULT = "result"
    PROCESS = "process"
    CONVERSATION = "conversation"
    INTERVENTION = "intervention"


class EventType(str, Enum):
    STEP_COMPLETE = "step_complete"
    GATE_DECISION = "gate_decision"
    TYPE_SWITCH = "type_switch"
    ORCHESTRATION = "orchestration"
    REQUIREMENT_CHANGE = "requirement_change"
    COMMENT = "comment"


class EventActor(str, Enum):
    AI = "ai"
    HUMAN = "human"
    SYSTEM = "system"


# ═══════════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════════


@dataclass
class Epic:
    """Epic（一组关联任务）"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    description_md: str = ""
    status: str = "active"
    assignees: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StepDefinition:
    """
    步骤定义（从 TASK_TYPES 硬编码模板复制而来）
    
    Task 创建时，从 TASK_TYPES[type] 复制步骤列表，
    每个步骤成为一个 StepDefinition 存入 task_steps 表。
    """
    step_id: str                           # 如 "step-1", "cr-r1", "cr-r2"
    title: str                             # 如 "需求分析与范围确认"
    required: bool = True                   # 必做?
    parallel_with: list[str] = field(default_factory=list)  # 可并行的步骤 step_id
    human_attention: HumanAttention = HumanAttention.NONE   # none|notify|review|gate
    model_tier: ModelTier = ModelTier.LIGHT                  # light|power
    process_template: str = ""             # 过程记录模板（Markdown）
    process_read_rules: list[str] = field(default_factory=list)  # 可读取的产物步骤 step_id

    def to_dict(self) -> dict:
        d = asdict(self)
        d["human_attention"] = self.human_attention.value
        d["model_tier"] = self.model_tier.value
        d["required"] = 1 if self.required else 0
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "StepDefinition":
        return cls(
            step_id=data["step_id"],
            title=data["title"],
            required=bool(data.get("required", True)),
            parallel_with=data.get("parallel_with", []),
            human_attention=HumanAttention(data.get("human_attention", "none")),
            model_tier=ModelTier(data.get("model_tier", "light")),
            process_template=data.get("process_template", ""),
            process_read_rules=data.get("process_read_rules", []),
        )


@dataclass
class TaskStep:
    """
    步骤运行时状态（对应 task_steps 表）
    
    StepDefinition + 运行时状态 = TaskStep
    """
    task_id: str = ""
    step_id: str = ""
    title: str = ""
    status: StepStatus = StepStatus.PENDING
    required: bool = True
    parallel_with: list[str] = field(default_factory=list)
    human_attention: HumanAttention = HumanAttention.NONE
    model_tier: ModelTier = ModelTier.LIGHT
    process_template: str = ""
    process_read_rules: list[str] = field(default_factory=list)
    sort_order: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["human_attention"] = self.human_attention.value
        d["model_tier"] = self.model_tier.value
        d["required"] = 1 if self.required else 0
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "TaskStep":
        return cls(
            task_id=data.get("task_id", ""),
            step_id=data["step_id"],
            title=data["title"],
            status=StepStatus(data.get("status", "pending")),
            required=bool(data.get("required", 1)),
            parallel_with=data.get("parallel_with", []),
            human_attention=HumanAttention(data.get("human_attention", "none")),
            model_tier=ModelTier(data.get("model_tier", "light")),
            process_template=data.get("process_template", ""),
            process_read_rules=data.get("process_read_rules", []),
            sort_order=data.get("sort_order", 0),
        )

    @classmethod
    def from_step_definition(cls, definition: StepDefinition, task_id: str, sort_order: int) -> "TaskStep":
        """从 StepDefinition 创建运行时 TaskStep"""
        return cls(
            task_id=task_id,
            step_id=definition.step_id,
            title=definition.title,
            required=definition.required,
            parallel_with=definition.parallel_with,
            human_attention=definition.human_attention,
            model_tier=definition.model_tier,
            process_template=definition.process_template,
            process_read_rules=definition.process_read_rules,
            sort_order=sort_order,
        )


@dataclass
class Task:
    """任务（核心实体）"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    epic_id: Optional[str] = None
    type: str = ""                          # dev-full-flow | small-change | etc.
    title: str = ""
    description: str = ""
    status: TaskStatus = TaskStatus.ACTIVE
    pause_level: Optional[PauseLevel] = None
    assignee: str = ""
    steps: list[TaskStep] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["pause_level"] = self.pause_level.value if self.pause_level else None
        d["steps"] = [s.to_dict() for s in self.steps]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        task = cls(
            id=data.get("id", ""),
            epic_id=data.get("epic_id"),
            type=data.get("type", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            status=TaskStatus(data.get("status", "active")),
            pause_level=PauseLevel(data["pause_level"]) if data.get("pause_level") else None,
            assignee=data.get("assignee", ""),
            steps=[],
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )
        if "steps" in data and data["steps"]:
            task.steps = [TaskStep.from_dict(s) for s in data["steps"]]
        return task


@dataclass
class Artifact:
    """步骤产物"""
    task_id: str = ""
    step_id: str = ""
    artifact_type: ArtifactType = ArtifactType.RESULT
    content: str = ""
    content_format: str = "json"   # json | markdown
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["artifact_type"] = self.artifact_type.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Artifact":
        return cls(
            task_id=data.get("task_id", ""),
            step_id=data.get("step_id", ""),
            artifact_type=ArtifactType(data.get("artifact_type", "result")),
            content=data.get("content", ""),
            content_format=data.get("content_format", "json"),
            created_at=data.get("created_at", ""),
        )


@dataclass
class Event:
    """事件记录（append-only）"""
    task_id: str = ""
    event_type: EventType = EventType.STEP_COMPLETE
    step_id: Optional[str] = None
    actor: EventActor = EventActor.AI
    content: dict = field(default_factory=dict)   # JSON 内容（what_happened + why_decided + ignored_conditions）
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        d["actor"] = self.actor.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(
            task_id=data.get("task_id", ""),
            event_type=EventType(data.get("event_type", "step_complete")),
            step_id=data.get("step_id"),
            actor=EventActor(data.get("actor", "ai")),
            content=data.get("content", {}),
            timestamp=data.get("timestamp", ""),
        )
