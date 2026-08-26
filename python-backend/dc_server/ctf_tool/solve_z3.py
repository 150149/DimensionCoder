
import os
import sys
import subprocess
import tempfile
from typing import Optional

TIMEOUT_SECONDS = 300

_CREATE_PROCESS_GROUP = 0x00000200

def _write_temp_script(script: str) -> str:
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
    if is_windows:
        try:
            subprocess.run(
                ['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                capture_output=True, timeout=10
            )
        except Exception:
            pass
    else:
        proc.kill()
    partial_output = None
    try:
        stdout, stderr = proc.communicate(timeout=5)
        partial_output = (
            stdout.decode('utf-8', errors='replace'),
            stderr.decode('utf-8', errors='replace'),
        )
    except Exception:
        pass
    return (None, None, None, partial_output)

def _run_subprocess(tmp_path: str) -> tuple:
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
