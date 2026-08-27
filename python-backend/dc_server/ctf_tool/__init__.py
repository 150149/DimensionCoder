"""
ctf_tool — CTF 逆向工程 MCP 工具集

提供 4 个工具函数，每个工具一个入口函数，输入字符串参数，输出字符串结果。
不需要 MCP API 端点，直接作为 Python 函数调用。

工具列表:
  - get_decompiled_code(file_path, address) — angr 反编译指定地址所属函数
  - extract_constants(file_path)              — 扫描提取密码学常量并比对
  - search_bytes(file_path, pattern)          — 通配符字节搜索
  - solve_z3(constraint_script)              — 独立进程执行 Z3 约束脚本

统一接口规范:
  - 签名: def tool_name(*args: str) -> str
  - 异常时不抛出，返回 '[ERROR] <tool_name>: <message>' 格式的错误信息字符串
"""

from .get_decompiled_code import get_decompiled_code
from .extract_constants import extract_constants
from .search_bytes import search_bytes
from .solve_z3 import solve_z3

__all__ = [
    'get_decompiled_code',
    'extract_constants',
    'search_bytes',
    'solve_z3',
]
