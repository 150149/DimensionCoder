"""
DimensionCoding — Storage 模块

工厂函数：根据配置创建对应的适配器实例。
上层业务代码只依赖 StorageAdapter 接口。
"""

from .adapter import StorageAdapter
from .sqlite_adapter import SQLiteAdapter


def create_storage(config: dict = None) -> StorageAdapter:
    """
    工厂函数：根据配置创建适配器实例。

    Args:
        config: {
            "storage_backend": "sqlite" | "mysql" (未来),
            "db_path": str (默认 "./dimensioncoding.db"),
            // MySQL 配置（未来）:
            // "mysql_host": str,
            // "mysql_user": str,
            // "mysql_password": str,
            // "mysql_database": str,
        }

    Returns:
        StorageAdapter 实例
    """
    if config is None:
        config = {}

    backend = config.get("storage_backend", "sqlite")

    if backend == "sqlite":
        db_path = config.get("db_path", "./dimensioncoding.db")
        return SQLiteAdapter(db_path)

    elif backend == "mysql":
        # 未来实现
        # from .mysql_adapter import MySQLAdapter
        # return MySQLAdapter(
        #     host=config["mysql_host"],
        #     user=config["mysql_user"],
        #     password=config["mysql_password"],
        #     database=config["mysql_database"],
        # )
        raise NotImplementedError("MySQL adapter not yet implemented")

    else:
        raise ValueError(f"Unknown storage backend: {backend}")
