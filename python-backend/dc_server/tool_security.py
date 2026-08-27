
import os


def safe_resolve(root: str, rel_or_abs: str) -> str:
    """返回 PROJECT_ROOT 内的绝对路径；越界抛 ValueError。

    - 相对路径基于 root 解析；绝对路径也必须在 root 内（V3 行为变更：
      write/edit 的"绝对路径优先"分支不再允许，一律走本函数校验）
    - 符号链接经 os.path.realpath 解析后仍须位于 root 内（symlink escape 拒绝）
    - 比较时 os.path.normcase 归一化（M14：Windows 路径大小写不敏感）
    - `.dc_tmp/` 目录（T2.6）位于 PROJECT_ROOT 内，天然放行，无需例外
    """
    base = os.path.realpath(root)
    p = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(base, rel_or_abs)
    full = os.path.realpath(p)
    if os.path.normcase(full) != os.path.normcase(base) and \
            not os.path.normcase(full).startswith(os.path.normcase(base + os.sep)):
        raise ValueError("path outside project root")
    return full
