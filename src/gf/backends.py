# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .exceptions import GitError
from .runner import run_command


@dataclass
class GitResult:
    """Result of a git command."""
    returncode: int
    stdout: str
    stderr: str


class GitBackend(Protocol):
    """Facade for executing git commands.

    The real implementation spawns `git`. The mock implementation records
    calls and returns pre-configured responses for tests.
    """

    def git(
        self,
        *args: Any,
        cwd: Path | None = None,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
        check: bool = True,
        stream: bool = False,
    ) -> GitResult:
        ...

    def git_capture(
        self,
        *args: Any,
        cwd: Path | None = None,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
    ) -> str:
        ...


class GitCliBackend:
    """Default git backend that spawns the `git` CLI."""

    def git(
        self,
        *args: Any,
        cwd: Path | None = None,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
        check: bool = True,
        stream: bool = False,
    ) -> GitResult:
        env = os.environ.copy()
        if git_dir is not None:
            env["GIT_DIR"] = str(git_dir)
        if work_tree is not None:
            env["GIT_WORK_TREE"] = str(work_tree)
        if cwd is not None:
            env["PWD"] = str(cwd)
        cmd = ["git"] + [str(a) for a in args]
        try:
            result = run_command(cmd, env, mode="stream" if stream else "capture", cwd=cwd)
        except FileNotFoundError as e:
            raise GitError(f"git {' '.join(cmd[1:])} failed: {e}")
        if check and result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip()
            raise GitError(f"git {' '.join(cmd[1:])} failed: {msg}")
        return GitResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)

    def git_capture(
        self,
        *args: Any,
        cwd: Path | None = None,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
    ) -> str:
        return self.git(*args, cwd=cwd, git_dir=git_dir, work_tree=work_tree).stdout
