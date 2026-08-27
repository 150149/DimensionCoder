

from __future__ import annotations
from typing import Optional


# 硬编码任务类型定义（已清空——模板见 prompts/flow-templates.md）
TASK_TYPES: dict[str, dict] = {}


# 预设类型清单（已退役——Monitor 统一编排）
PRESET_TYPES: list[str] = []


def get_task_type(task_type: str) -> Optional[dict]:
    """获取任务类型定义（已退役：恒返回 None）。"""
    return TASK_TYPES.get(task_type)


def get_task_type_names() -> list[dict]:
    """获取所有任务类型的名称和描述（已退役：恒返回空）。"""
    return []


def get_steps(task_type: str) -> list:
    """获取任务类型的步骤列表（已退役：恒返回空——流程由 Monitor 生成）。"""
    tt = TASK_TYPES.get(task_type)
    if tt is None:
        return []
    return list(tt["steps"])


def get_step(task_type: str, step_id: str) -> Optional[dict]:
    """获取指定步骤定义（已退役：恒返回 None）。"""
    steps = get_steps(task_type)
    for s in steps:
        if s.step_id == step_id:
            return s
    return None


def validate_task_type(task_type: str) -> bool:
    """验证任务类型是否合法（已退役：仅 "custom" 保留）。"""
    return task_type in TASK_TYPES or task_type == "custom"


def get_all_types() -> list[str]:
    """获取所有任务类型 key（已退役：恒返回空）。"""
    return list(TASK_TYPES.keys())
