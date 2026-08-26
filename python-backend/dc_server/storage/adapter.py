
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

class StorageAdapter(ABC):

    @abstractmethod
    async def create_epic(self, epic: dict) -> str:
        ...

    @abstractmethod
    async def get_epic(self, epic_id: str) -> Optional[dict]:
        ...

    @abstractmethod
    async def list_epics(self) -> list[dict]:
        ...

    @abstractmethod
    async def create_task(self, task: dict) -> str: ...

    @abstractmethod
    async def delete_task(self, task_id: str) -> None:
        ...

    @abstractmethod
    async def get_task(self, task_id: str) -> Optional[dict]:
        ...

    @abstractmethod
    async def update_task(self, task_id: str, updates: dict) -> None:
        ...

    @abstractmethod
    async def list_tasks(
        self,
        epic_id: Optional[str] = None,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> list[dict]:
        ...

    @abstractmethod
    async def update_step_status(self, task_id: str, step_id: str, status: str) -> None:
        ...

    @abstractmethod
    async def add_steps(self, task_id: str, steps: list[dict],
                        after_step_id: Optional[str] = None) -> None:
        ...

    @abstractmethod
    async def remove_steps(self, task_id: str, step_ids: list[str]) -> None:
        ...

    @abstractmethod
    async def reorder_steps(self, task_id: str, new_order: list[str]) -> None:
        ...

    @abstractmethod
    async def save_artifact(
        self, task_id: str, step_id: str, artifact_type: str, content: str, content_format: str = "json"
    ) -> None:
        ...

    @abstractmethod
    async def get_artifact(self, task_id: str, step_id: str, artifact_type: str) -> Optional[dict]:
        ...

    @abstractmethod
    async def list_artifacts(self, task_id: str, step_id: Optional[str] = None) -> list[dict]:
        ...

    @abstractmethod
    async def delete_artifacts(self, task_id: str, step_id: Optional[str] = None,
                               artifact_type: Optional[str] = None) -> None:
        ...

    @abstractmethod
    async def append_event(self, task_id: str, event: dict) -> None:
        ...

    @abstractmethod
    async def get_events(self, task_id: str, step_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        ...

    @abstractmethod
    async def save_conversation(self, task_id: str, step_id: str, messages: list[dict]) -> None:
        ...

    @abstractmethod
    async def get_conversation(self, task_id: str, step_id: str) -> Optional[list[dict]]:
        ...

    @abstractmethod
    async def append_message(self, task_id: str, step_id: str, message: dict) -> int:
        ...

    @abstractmethod
    async def get_step_messages(self, task_id: str, step_id: str, after_seq: int = -1,
                                limit: Optional[int] = None,
                                before_seq: int = -1) -> list[dict]:
        ...

    @abstractmethod
    async def count_step_messages(self, task_id: str, step_id: str) -> int:
        ...

    @abstractmethod
    async def save_chunk(self, task_id: str, step_id: str, chunk: dict) -> int:
        ...

    @abstractmethod
    async def save_chunks(self, task_id: str, step_id: str, chunks: list[dict]) -> None:
        ...

    @abstractmethod
    async def get_chunks(self, task_id: str, step_id: str, after_seq: int = -1) -> list[dict]:
        ...

    @abstractmethod
    async def clear_step_messages(self, task_id: str, step_id: str) -> None:
        ...

    @abstractmethod
    async def export_for_ai(self, task_id: str, step_id: str, target_dir: str) -> dict:
        ...

    @abstractmethod
    async def query_tasks(
        self, filters: dict, page: int = 1, page_size: int = 20
    ) -> tuple[list[dict], int]:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...
