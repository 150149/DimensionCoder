

import os
import sys
from functools import lru_cache

_PROMPTS_DIR = os.path.dirname(__file__)


def rules_dir() -> str:
    """规则库目录（与提示词同族，供 step_context 注入路径与 rest_api 只读白名单共用，
    避免硬编码路径——源码形态与打包形态目录位置不同）：
    - 源码运行：prompts/rules（与 __file__ 同目录）；
    - PyInstaller 打包：sys._MEIPASS/prompts/rules（打包时须
      --add-data "prompts/rules;prompts/rules" 保持相对结构）。
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "prompts", "rules")
    return os.path.join(_PROMPTS_DIR, "rules")


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """
    从 prompts/ 目录加载指定名称的提示词文件。

    Args:
        name: 提示词名称（不含 .md 后缀），如 "executor", "monitor"

    Returns:
        提示词文本内容

    Raises:
        FileNotFoundError: 文件不存在时抛出
    """
    path = os.path.join(_PROMPTS_DIR, f"{name}.md")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()
