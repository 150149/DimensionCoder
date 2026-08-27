"""
memory/__init__.py — 记忆模块工厂

对外暴露 get_memory_storage() 懒单例，其他模块按需构造。
遵循 DimensionCoder 的 rest_api.py 中 _get_storage() / _get_orchestrator() 模式。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_memory_storage: Optional["MemoryStorage"] = None
_retainer = None
_recaller = None
_consolidator = None
_mental_model_manager = None
_knowledge_page_manager = None
_reflector = None
_directive_manager = None
_maintenance_loop = None


def get_memory_storage(config: Optional[dict] = None) -> Optional["MemoryStorage"]:
    """懒单例获取 MemoryStorage。enabled=False 时返回 None。

    config: 从 config.get_memory_config() 获取的配置 dict
    """
    global _memory_storage
    if _memory_storage is not None:
        return _memory_storage
    if config is None:
        return None
    if not config.get("enabled"):
        return None

    from .storage import MemoryStorage
    from ..config import BASE_DIR, DB_PATH

    db_path = config.get("db_path", "")
    if not db_path:
        # 默认：与 dc.db 同目录下的 memory.db
        db_path = DB_PATH.replace("dimensioncoding.db", "memory.db")
    elif db_path == ":memory:":
        pass  # 内存数据库

    _memory_storage = MemoryStorage(db_path)
    return _memory_storage


def get_retainer(storage, llm_client=None, config=None):
    """获取 Retainer 实例。"""
    global _retainer
    if _retainer is None:
        from .retain import Retainer
        from .embeddings import EmbeddingProvider
        from .entity_resolver import EntityResolver

        emb_provider = None
        if config:
            emb_base = config.get("embedding_base_url", "")
            emb_key = config.get("embedding_api_key", "")
            emb_model = config.get("embedding_model", "text-embedding-3-small")
            # 复用 light model 配置
            if not emb_base or not emb_key:
                from ..config import get_llm_config
                llm_cfg = get_llm_config()
                emb_base = emb_base or llm_cfg.get("light_base_url", "")
                emb_key = emb_key or llm_cfg.get("light_api_key", "")
            emb_provider = EmbeddingProvider(emb_base, emb_key, emb_model)

        _retainer = Retainer(
            storage=storage,
            llm_client=llm_client,
            embedding_provider=emb_provider,
            config=config or {},
        )
    return _retainer


def get_recaller(storage, config=None):
    """获取 Recaller 实例。"""
    global _recaller
    if _recaller is None:
        from .recall import Recaller
        from .embeddings import EmbeddingProvider

        emb_provider = None
        if config:
            emb_base = config.get("embedding_base_url", "")
            emb_key = config.get("embedding_api_key", "")
            emb_model = config.get("embedding_model", "text-embedding-3-small")
            if not emb_base or not emb_key:
                from ..config import get_llm_config
                llm_cfg = get_llm_config()
                emb_base = emb_base or llm_cfg.get("light_base_url", "")
                emb_key = emb_key or llm_cfg.get("light_api_key", "")
            emb_provider = EmbeddingProvider(emb_base, emb_key, emb_model)

        _recaller = Recaller(storage, emb_provider, config or {})
    return _recaller


def get_consolidator(storage, llm_client=None, config=None):
    """获取 Consolidator 实例。"""
    global _consolidator
    if _consolidator is None:
        from .consolidation import Consolidator
        _consolidator = Consolidator(storage, llm_client, config or {})
    return _consolidator


def get_mental_model_manager(storage, llm_client=None, config=None):
    """获取 MentalModelManager 实例。"""
    global _mental_model_manager
    if _mental_model_manager is None:
        from .mental_models import MentalModelManager
        _mental_model_manager = MentalModelManager(storage, llm_client, config or {})
    return _mental_model_manager


def get_knowledge_page_manager(storage, mental_model_manager=None, config=None):
    """获取 KnowledgePageManager 实例。"""
    global _knowledge_page_manager
    if _knowledge_page_manager is None:
        from .knowledge_pages import KnowledgePageManager
        _knowledge_page_manager = KnowledgePageManager(
            storage, mental_model_manager, config or {}
        )
    return _knowledge_page_manager


def get_reflector(storage, llm_client=None, config=None):
    """获取 Reflector 实例。"""
    global _reflector
    if _reflector is None:
        from .reflect import Reflector
        recaller = get_recaller(storage, config)
        emb_provider = recaller.embedding_provider if recaller else None
        _reflector = Reflector(storage, llm_client, recaller, emb_provider, config or {})
    return _reflector


def get_directive_manager(storage):
    """获取 DirectiveManager 实例。"""
    global _directive_manager
    if _directive_manager is None:
        from .directives import DirectiveManager
        _directive_manager = DirectiveManager(storage)
    return _directive_manager


def get_maintenance_loop(storage, config=None):
    """获取 MaintenanceLoop 实例。"""
    global _maintenance_loop
    if _maintenance_loop is None:
        from .maintenance import MaintenanceLoop
        _maintenance_loop = MaintenanceLoop(storage, config=config or {})
    return _maintenance_loop


def close_memory():
    """关闭所有记忆模块（server shutdown 时调用）。"""
    global _memory_storage, _retainer, _recaller, _consolidator
    global _mental_model_manager, _knowledge_page_manager, _reflector
    global _directive_manager, _maintenance_loop

    if _maintenance_loop:
        _maintenance_loop.stop()
        _maintenance_loop = None

    if _memory_storage:
        _memory_storage.close()
        _memory_storage = None

    _retainer = None
    _recaller = None
    _consolidator = None
    _mental_model_manager = None
    _knowledge_page_manager = None
    _reflector = None
    _directive_manager = None
