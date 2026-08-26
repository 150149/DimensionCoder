
import os
import sys
from functools import lru_cache

_PROMPTS_DIR = os.path.dirname(__file__)

def rules_dir() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "prompts", "rules")
    return os.path.join(_PROMPTS_DIR, "rules")

@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    path = os.path.join(_PROMPTS_DIR, f"{name}.md")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()
