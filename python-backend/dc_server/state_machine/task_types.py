
from __future__ import annotations
from typing import Optional

TASK_TYPES: dict[str, dict] = {}

PRESET_TYPES: list[str] = []

def get_task_type(task_type: str) -> Optional[dict]:
    return TASK_TYPES.get(task_type)

def get_task_type_names() -> list[dict]:
    return []

def get_steps(task_type: str) -> list:
    tt = TASK_TYPES.get(task_type)
    if tt is None:
        return []
    return list(tt["steps"])

def get_step(task_type: str, step_id: str) -> Optional[dict]:
    steps = get_steps(task_type)
    for s in steps:
        if s.step_id == step_id:
            return s
    return None

def validate_task_type(task_type: str) -> bool:
    return task_type in TASK_TYPES or task_type == "custom"

def get_all_types() -> list[str]:
    return list(TASK_TYPES.keys())
