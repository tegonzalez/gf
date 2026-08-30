import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RunResult:
    """Result of a command run by `run_command`."""

    returncode: int
    stdout: str
    stderr: str


def _write_bytes_to(stream, data: bytes) -> None:
    """Write raw bytes to `stream`, handling both real files and StringIO."""
    if hasattr(stream, "buffer"):
        try:
            stream.buffer.write(data)
            stream.buffer.flush()
            return
        except (OSError, AttributeError):
            pass
    stream.write(data.decode(errors="replace"))
    try:
        stream.flush()
    except (OSError, AttributeError):
        pass


def _shovel(src_fd: int, dst, chunks: list[bytes]) -> None:
    """Copy data from `src_fd` to `dst` while collecting it in `chunks`."""
    while True:
        try:
            data = os.read(src_fd, 8192)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
        try:
            _write_bytes_to(dst, data)
        except OSError:
            pass


def _cwd_arg(cwd: Path | str | None) -> Any:
    return str(cwd) if cwd is not None else None


def run_command(
    cmd: list[str],
    env: dict[str, str],
    *,
    mode: str,
    cwd: Path | str | None = None,
) -> RunResult:
    """Run a command in one of three modes.

    - `exec`: replace the current process with the command. Returns only on failure.
    - `capture`: run the command and collect stdout/stderr without echoing.
    - `stream`: echo stdout/stderr as it arrives while still collecting it.
    """
    if mode == "exec":
        if cwd is not None:
            os.chdir(cwd)
        try:
            os.execvpe(cmd[0], cmd, env)
        except FileNotFoundError:
            return RunResult(127, "", f"{cmd[0]}: command not found")
        except OSError as e:
            return RunResult(126, "", f"{cmd[0]}: {e}")
        # execvpe only returns if mocked in tests.
        return RunResult(0, "", "")

    if mode == "capture":
        try:
            result = subprocess.run(
                cmd,
                env=env,
                cwd=_cwd_arg(cwd),
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return RunResult(127, "", f"{cmd[0]}: command not found")
        return RunResult(result.returncode, result.stdout, result.stderr)

    if mode == "stream":
        try:
            p = subprocess.Popen(
                cmd,
                env=env,
                cwd=_cwd_arg(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            return RunResult(127, "", f"{cmd[0]}: command not found")
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        out_thread = threading.Thread(
            target=_shovel,
            args=(p.stdout.fileno(), sys.stdout, stdout_chunks),
            daemon=True,
        )
        err_thread = threading.Thread(
            target=_shovel,
            args=(p.stderr.fileno(), sys.stderr, stderr_chunks),
            daemon=True,
        )
        out_thread.start()
        err_thread.start()
        out_thread.join()
        err_thread.join()
        returncode = p.wait()
        p.stdout.close()
        p.stderr.close()
        return RunResult(
            returncode,
            b"".join(stdout_chunks).decode(errors="replace"),
            b"".join(stderr_chunks).decode(errors="replace"),
        )

    raise ValueError(f"unknown run_command mode: {mode}")
