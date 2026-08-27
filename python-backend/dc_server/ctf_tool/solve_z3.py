"""
solve_z3 — Z3 约束求解工具

将 AI 写的 Z3 Python 脚本在独立进程中执行，设置 5 分钟超时，捕获 stdout/stderr 返回。

接口：solve_z3(constraint_script) -> str

安全限制：仅超时限制（subprocess timeout=300s），不限制内存/CPU。
脚本需自行 `from z3 import *`，工具不自动注入。

安全警告：此工具执行任意 Python 代码，仅靠超时限制保护。
请勿用于不可信的输入源。恶意脚本可读取/修改文件、发起网络连接等。
"""

import os
import sys
import subprocess
import tempfile
from typing import Optional

# 超时时间（秒）
TIMEOUT_SECONDS = 300  # 5 分钟

# Windows 进程组创建标志
_CREATE_PROCESS_GROUP = 0x00000200  # CREATE_NEW_PROCESS_GROUP


def _write_temp_script(script: str) -> str:
    """
    将脚本写入临时文件，返回文件路径。
    [R3-4] os.fdopen 失败时 os.close(fd) 防止 fd 泄漏。
    """
    fd, tmp_path = tempfile.mkstemp(suffix='.py', prefix='z3_solve_')
    try:
        try:
            f = os.fdopen(fd, 'w', encoding='utf-8')
        except Exception:
            os.close(fd)
            raise
        with f:
            f.write(script)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path


def _handle_timeout(proc: subprocess.Popen, is_windows: bool) -> tuple:
    """
    处理子进程超时：终止进程树并尝试捕获部分输出。
    [R3-3] Windows 下使用 taskkill /T /F 终止进程树。
    [R2-6] 捕获部分 stdout/stderr 输出。

    返回 (None, None, None, partial_output)。
    """
    # [R3-3] Windows: taskkill /T /F 终止整个进程树
    if is_windows:
        try:
            subprocess.run(
                ['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                capture_output=True, timeout=10
            )
        except Exception:  # taskkill 可能因进程已退出而失败
            pass
    else:
        proc.kill()
    # 尝试获取部分输出
    partial_output = None
    try:
        stdout, stderr = proc.communicate(timeout=5)
        partial_output = (
            stdout.decode('utf-8', errors='replace'),
            stderr.decode('utf-8', errors='replace'),
        )
    except Exception:  # communicate 可能再次超时，跳过部分输出
        pass
    return (None, None, None, partial_output)


def _run_subprocess(tmp_path: str) -> tuple:
    """
    在独立进程中执行脚本，带超时保护。
    [R3-3] Windows 下使用进程组 + taskkill /T 终止进程树。
    [R2-6] 超时时捕获部分输出。

    返回 (stdout, stderr, returncode, timeout_partial_output)。
    超时时 returncode 为 None，timeout_partial_output 包含部分输出。
    """
    is_windows = sys.platform == 'win32'
    creationflags = _CREATE_PROCESS_GROUP if is_windows else 0

    proc = subprocess.Popen(
        [sys.executable, tmp_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )

    try:
        stdout, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
        return (stdout.decode('utf-8', errors='replace'),
                stderr.decode('utf-8', errors='replace'),
                proc.returncode, None)
    except subprocess.TimeoutExpired:
        return _handle_timeout(proc, is_windows)


def _format_result(stdout: str, stderr: str, returncode: int) -> str:
    """格式化正常执行结果。"""
    output_parts = []
    if stdout:
        output_parts.append("=== STDOUT ===\n" + stdout)
    if stderr:
        stderr_lines = stderr.splitlines()
        filtered = [l for l in stderr_lines
                    if 'pkg_resources is deprecated' not in l
                    and 'import pkg_resources' not in l]
        if filtered:
            output_parts.append("=== STDERR ===\n" + "\n".join(filtered))
    if returncode != 0:
        output_parts.append(f"=== EXIT CODE: {returncode} ===")
    return "\n".join(output_parts) if output_parts else "(no output)"


def _format_timeout(partial_output: Optional[tuple]) -> str:
    """格式化超时消息，附加部分输出（如有）。"""
    msg = (f"[TIMEOUT] solve_z3: exceeded {TIMEOUT_SECONDS} seconds, "
           f"process killed")
    if partial_output:
        partial_stdout, partial_stderr = partial_output
        if partial_stdout:
            msg += "\n=== PARTIAL STDOUT ===\n" + partial_stdout
        if partial_stderr:
            filtered = [l for l in partial_stderr.splitlines()
                        if 'pkg_resources is deprecated' not in l
                        and 'import pkg_resources' not in l]
            if filtered:
                msg += "\n=== PARTIAL STDERR ===\n" + "\n".join(filtered)
    return msg


def solve_z3(*args: str) -> str:
    """在独立进程中执行 Z3 约束脚本，5 分钟超时。"""
    try:
        if len(args) < 1:
            return (f"[ERROR] solve_z3: expected 1 argument "
                    f"(constraint_script), got {len(args)}")

        script = args[0]
        if not script.strip():
            return "[ERROR] solve_z3: empty script"

        tmp_path = _write_temp_script(script)
        try:
            stdout, stderr, returncode, partial = _run_subprocess(tmp_path)
            if returncode is not None:
                return _format_result(stdout, stderr, returncode)
            return _format_timeout(partial)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    except Exception as e:
        return f"[ERROR] solve_z3: {type(e).__name__}: {e}"
