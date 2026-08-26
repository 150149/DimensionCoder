
import os
from typing import Any, Optional

_angr = None

DECOMPILER_TIMEOUT = 120

def _get_angr() -> Any:
    global _angr
    if _angr is None:
        import angr as _a
        _angr = _a
    return _angr

def _detect_format(file_path: str) -> str:
    with open(file_path, 'rb') as f:
        magic = f.read(4)
    if magic[:2] == b'MZ':
        return 'pe'
    elif magic[:4] == b'\x7fELF':
        return 'elf'
    else:
        return 'raw'

def _parse_address(addr_str: str) -> int:
    addr_str = addr_str.strip()
    if addr_str.lower().startswith('0x'):
        return int(addr_str, 16)
    else:
        return int(addr_str)

def _check_function_has_loop(func: Any) -> bool:
    try:
        for src, dst in func.transition_graph.edges():
            if hasattr(src, 'addr') and hasattr(dst, 'addr'):
                if dst.addr <= src.addr:
                    return True
        return False
    except (AttributeError, TypeError):
        return False

def _find_function_containing(cfg: Any, target_addr: int) -> Any:
    func = cfg.kb.functions.get(target_addr)
    if func is not None:
        return func

    for func in cfg.kb.functions.values():
        if func.size > 0 and func.addr <= target_addr < func.addr + func.size:
            return func
        try:
            if target_addr in func.block_addrs_set:
                return func
        except (AttributeError, TypeError):
            pass

    return None

def _validate_input(args: tuple) -> Optional[str]:
    if len(args) < 2:
        return (f"[ERROR] get_decompiled_code: expected 2 arguments "
                f"(file_path, address), got {len(args)}")
    file_path = args[0]
    if not os.path.isfile(file_path):
        return f"[ERROR] get_decompiled_code: file not found: {file_path}"
    if os.path.getsize(file_path) == 0:
        return f"[ERROR] get_decompiled_code: file is empty: {file_path}"
    return None

def _load_project(file_path: str) -> Any:
    angr = _get_angr()
    fmt = _detect_format(file_path)
    if fmt in ('pe', 'elf'):
        return angr.Project(file_path, auto_load_libs=False)
    else:
        return angr.Project(
            file_path,
            auto_load_libs=False,
            main_opts={'backend': 'blob',
                       'arch': 'x86_64',
                       'base_addr': 0x0}
        )

def _run_decompiler(proj: Any, target_func: Any) -> Any:
    import concurrent.futures
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(proj.analyses.Decompiler, target_func)
    try:
        return future.result(timeout=DECOMPILER_TIMEOUT)
    except concurrent.futures.TimeoutExpired:
        return None
    finally:
        executor.shutdown(wait=False)

def _format_vm_hint(cfg: Any, target_addr: int) -> str:
    return (
        f"[ERROR] get_decompiled_code: no function found at address "
        f"{hex(target_addr)}.\n"
        f"// [VM Hint] The address may be inside a VM handler or "
        f"unrecognized code region.\n"
        f"// Try using search_bytes to examine bytes at this address, "
        f"or use extract_constants\n"
        f"// to scan for cryptographic constants in the binary.\n"
        f"//\n"
        f"// Known functions in this binary (first 20):\n"
        + "\n".join(
            f"//   {hex(f.addr)} — {f.name} ({len(f.block_addrs_set)} blocks)"
            for f in list(cfg.kb.functions.values())[:20]
        )
    )

def _format_imported_func(target_func: Any) -> str:
    return (
        f"// ===== {target_func.name} @ {hex(target_func.addr)} =====\n"
        f"// This is an imported/external function "
        f"(SimProcedure/PLT/syscall).\n"
        f"// No decompilation available for imported functions."
    )

def _format_header(target_func: Any, file_path: str, target_addr: int,
                   block_count: int) -> list:
    return [
        f"// ===== {target_func.name} @ {hex(target_func.addr)} =====",
        f"// File: {file_path}",
        f"// Target address: {hex(target_addr)}",
        f"// Function info: {block_count} blocks, "
        f"starts at {hex(target_func.addr)}",
        "",
    ]

def _format_empty_codegen(target_func: Any) -> list:
    return [
        "// [Empty Codegen] "
        "Decompilation produced no output for this function.",
        "// This typically occurs when:",
        "//   1. The function is a single basic block "
        "(thunk/stub) — too small for angr codegen",
        "//   2. The function is an import wrapper "
        "or tail-call stub",
        "//   3. The function contains only a return instruction",
        "//",
        "// Suggested next steps:",
        f"//   - Use search_bytes to examine the raw bytes "
        f"at {hex(target_func.addr)}",
        "//   - Use extract_constants to scan the binary "
        "for crypto constants",
        "//   - If this is a thunk, follow the jump target address "
        "and decompile that function instead",
    ]

def _format_vm_dispatcher_hint(block_count: int) -> list:
    return [
        "",
        f"// [VM Hint] This function has {block_count} blocks "
        f"and contains loops.",
        "// It may be a VM dispatcher loop. Consider examining "
        "individual VM handlers",
        "// by using search_bytes to locate handler entry points "
        "in the binary.",
    ]

def _format_decompiled_output(
    target_func: Any, file_path: str, target_addr: int,
    d: Any, block_count: int, has_loop: bool
) -> str:
    lines = _format_header(target_func, file_path, target_addr, block_count)

    if d.codegen and d.codegen.text:
        lines.append(d.codegen.text)
        if block_count > 50 and has_loop:
            lines.extend(_format_vm_dispatcher_hint(block_count))
    else:
        lines.extend(_format_empty_codegen(target_func))

    return "\n".join(lines)

def _parse_target_address(addr_str: str) -> tuple:
    try:
        return _parse_address(addr_str), None
    except ValueError:
        return None, (f"[ERROR] get_decompiled_code: invalid address "
                      f"'{addr_str}', expected hex (0x...) or decimal")

def _format_decompile_error(target_func: Any, file_path: str,
                            target_addr: int, block_count: int, e: Any) -> str:
    lines = _format_header(target_func, file_path, target_addr, block_count)
    lines.append(f"// Decompilation error: {e}")
    lines.append(f"// Function info: {block_count} blocks")
    return "\n".join(lines)

def _format_decompile_timeout(target_func: Any, file_path: str,
                              target_addr: int, block_count: int) -> str:
    lines = _format_header(target_func, file_path, target_addr, block_count)
    lines.append(f"// [TIMEOUT] Decompilation exceeded {DECOMPILER_TIMEOUT}s "
                 f"time limit.")
    lines.append(f"// Function info: {block_count} blocks")
    return "\n".join(lines)

def get_decompiled_code(*args: str) -> str:
    try:
        err = _validate_input(args)
        if err:
            return err
        file_path, addr_str = args[0], args[1]

        target_addr, err = _parse_target_address(addr_str)
        if err:
            return err

        proj = _load_project(file_path)
        cfg = proj.analyses.CFGFast(normalize=True)

        target_func = _find_function_containing(cfg, target_addr)
        if target_func is None:
            return _format_vm_hint(cfg, target_addr)

        if (target_func.is_simprocedure or target_func.is_plt
                or target_func.is_syscall):
            return _format_imported_func(target_func)

        block_count = len(target_func.block_addrs_set)
        has_loop = _check_function_has_loop(target_func)
        try:
            d = _run_decompiler(proj, target_func)
        except Exception as e:
            return _format_decompile_error(
                target_func, file_path, target_addr, block_count, e)
        if d is None:
            return _format_decompile_timeout(
                target_func, file_path, target_addr, block_count)

        return _format_decompiled_output(
            target_func, file_path, target_addr, d, block_count, has_loop)

    except Exception as e:
        return f"[ERROR] get_decompiled_code: {type(e).__name__}: {e}"
