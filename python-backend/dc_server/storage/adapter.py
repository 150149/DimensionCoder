

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class StorageAdapter(ABC):
    """持久化抽象接口"""

    # ── Epic ──

    @abstractmethod
    async def create_epic(self, epic: dict) -> str:
        """创建 Epic，返回 epic_id"""
        ...

    @abstractmethod
    async def get_epic(self, epic_id: str) -> Optional[dict]:
        """获取 Epic"""
        ...

    @abstractmethod
    async def list_epics(self) -> list[dict]:
        """列出所有 Epic"""
        ...

    # ── Task ──

    @abstractmethod
    async def create_task(self, task: dict) -> str: ...

    @abstractmethod
    async def delete_task(self, task_id: str) -> None:
        """删除 Task 及其关联数据"""
        ...

    @abstractmethod
    async def get_task(self, task_id: str) -> Optional[dict]:
        """获取 Task（含 steps）"""
        ...

    @abstractmethod
    async def update_task(self, task_id: str, updates: dict) -> None:
        """
        更新 Task 字段。

        updates 可包含: title, description, status, pause_level, assignee, steps
        如果 updates 包含 steps，则替换整个步骤列表（Monitor 编排用）
        """
        ...

    @abstractmethod
    async def list_tasks(
        self,
        epic_id: Optional[str] = None,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> list[dict]:
        """列出 Task（含 steps）"""
        ...

    @abstractmethod
    async def update_step_status(self, task_id: str, step_id: str, status: str) -> None:
        """更新单个步骤的状态"""
        ...

    @abstractmethod
    async def add_steps(self, task_id: str, steps: list[dict],
                        after_step_id: Optional[str] = None) -> None:
        """追加步骤到 Task（after_step_id：插入到指定步骤之后，默认追加末尾）"""
        ...

    @abstractmethod
    async def remove_steps(self, task_id: str, step_ids: list[str]) -> None:
        """删除指定步骤（只能删除 pending/stopped 步骤）"""
        ...

    @abstractmethod
    async def reorder_steps(self, task_id: str, new_order: list[str]) -> None:
        """重排步骤顺序"""
        ...

    # ── Artifacts ──

    @abstractmethod
    async def save_artifact(
        self, task_id: str, step_id: str, artifact_type: str, content: str, content_format: str = "json"
    ) -> None:
        """
        保存步骤产物。

        artifact_type: result | process | conversation | intervention
        """
        ...

    @abstractmethod
    async def get_artifact(self, task_id: str, step_id: str, artifact_type: str) -> Optional[dict]:
        """获取步骤产物"""
        ...

    @abstractmethod
    async def list_artifacts(self, task_id: str, step_id: Optional[str] = None) -> list[dict]:
        """列出 Task/步骤的产物"""
        ...

    @abstractmethod
    async def delete_artifacts(self, task_id: str, step_id: Optional[str] = None,
                               artifact_type: Optional[str] = None) -> None:
        """删除产物（重置流程用）：step_id 缺省删全任务；artifact_type 可再过滤"""
        ...

    # ── Events ──

    @abstractmethod
    async def append_event(self, task_id: str, event: dict) -> None:
        """追加事件"""
        ...

    @abstractmethod
    async def get_events(self, task_id: str, step_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        """获取 Task 的事件列表"""
        ...

    # ── Conversation ──

    @abstractmethod
    async def save_conversation(self, task_id: str, step_id: str, messages: list[dict]) -> None:
        """保存完整对话记录"""
        ...

    @abstractmethod
    async def get_conversation(self, task_id: str, step_id: str) -> Optional[list[dict]]:
        """获取对话记录"""
        ...

    # ── Step Messages（追加式） ──

    @abstractmethod
    async def append_message(self, task_id: str, step_id: str, message: dict) -> int:
        """追加一条消息到 step_messages，返回 seq"""
        ...

    @abstractmethod
    async def get_step_messages(self, task_id: str, step_id: str, after_seq: int = -1,
                                limit: Optional[int] = None,
                                before_seq: int = -1) -> list[dict]:
        """查询步骤消息（分页）：after_seq 增量（-1 全量）；limit=最近 N 条
        （配合 before_seq 可往前翻页：seq < before_seq 的最近 N 条，-1 不限制）"""
        ...

    @abstractmethod
    async def count_step_messages(self, task_id: str, step_id: str) -> int:
        """步骤消息总数（getTask 瘦身：总览页只需消息量，全量由 getStep 分页拉取）"""
        ...

    # ── Stream Chunks ──

    @abstractmethod
    async def save_chunk(self, task_id: str, step_id: str, chunk: dict) -> int:
        """保存一个流式 chunk，返回 seq"""
        ...

    @abstractmethod
    async def save_chunks(self, task_id: str, step_id: str, chunks: list[dict]) -> None:
        """批量保存流式 chunk（单事务）：流式逐条 commit 拖慢 UI（实测 1.79ms/条 vs
        单事务 0.01ms/条）；stream_chunks 仅审计用途无前端消费方，批量无实时性损失"""
        ...

    @abstractmethod
    async def get_chunks(self, task_id: str, step_id: str, after_seq: int = -1) -> list[dict]:
        """增量查询流式 chunk"""
        ...

    @abstractmethod
    async def clear_step_messages(self, task_id: str, step_id: str) -> None:
        """清除某步骤的消息和 chunk"""
        ...

    # ── 临时文件导出 ──

    @abstractmethod
    async def export_for_ai(self, task_id: str, step_id: str, target_dir: str) -> dict:
        """
        从 DB 导出该步骤 AI 所需的所有数据到临时目录。

        返回:
        {
            "task_context_path": str,     # Task 基本信息 + 当前步骤定义
            "artifacts_dir": str,          # 产物目录
            "conversations_dir": str,      # 对话记录目录
            "process_paths": list[str],    # 上一步骤的 process.md 路径列表
        }
        """
        ...

    # ── 高级查询 ──

    @abstractmethod
    async def query_tasks(
        self, filters: dict, page: int = 1, page_size: int = 20
    ) -> tuple[list[dict], int]:
        """
        分页查询 Task。

        filters: {status, type, epic_id, keyword}
        返回: (task_list, total_count)
        """
        ...

    # ── 生命周期 ──

    @abstractmethod
    async def close(self) -> None:
        """关闭连接"""
        ...
