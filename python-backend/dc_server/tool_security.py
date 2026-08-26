
import os

def safe_resolve(root: str, rel_or_abs: str) -> str:
    base = os.path.realpath(root)
    p = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(base, rel_or_abs)
    full = os.path.realpath(p)
    if os.path.normcase(full) != os.path.normcase(base) and \
            not os.path.normcase(full).startswith(os.path.normcase(base + os.sep)):
        raise ValueError("path outside project root")
    return full
