
from .adapter import StorageAdapter
from .sqlite_adapter import SQLiteAdapter

def create_storage(config: dict = None) -> StorageAdapter:
    if config is None:
        config = {}

    backend = config.get("storage_backend", "sqlite")

    if backend == "sqlite":
        db_path = config.get("db_path", "./dimensioncoding.db")
        return SQLiteAdapter(db_path)

    elif backend == "mysql":
        raise NotImplementedError("MySQL adapter not yet implemented")

    else:
        raise ValueError(f"Unknown storage backend: {backend}")
