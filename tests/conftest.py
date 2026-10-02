# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import contextlib
import io
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from gf import cli
from gf.backends import GitCliBackend
from mock_git import MockGitBackend


@pytest.fixture
def tmp_path():
    """Per-test scratch directory under tests/fixtures/tmp."""
    base = Path(__file__).parent / "fixtures" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / uuid.uuid4().hex
    path.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _git_env(tmp_path, monkeypatch):
    """Hermetic git identity and a host-independent default branch."""
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "gf-test")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "test@git-folders")
    gitconfig = tmp_path / "gitconfig"
    # Belt-and-braces: git itself refuses every non-local transport
    # (verified on git 2.47.3 — https/ssh/git:// exit 128 "transport not
    # allowed"); local paths and user-invoked file:// stay allowed.
    gitconfig.write_text(
        "[init]\n\tdefaultBranch = master\n"
        "[protocol]\n\tallow = never\n"
        '[protocol "file"]\n\tallow = user\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


class Result:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def gf(*args, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run the real `gf` via subprocess (integration tests)."""
    cmd = [sys.executable, "-m", "gf", *args]
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(
            f"gf {' '.join(args)} failed (rc={result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    return result


@pytest.fixture
def gf_inproc(fs):
    """In-process gf runner with captured stdout/stderr and optional mock backend."""
    def _run(*args, cwd: Path | str | None = None, backend=None, check: bool = True):
        old_cwd = os.getcwd()
        if cwd is not None:
            os.chdir(str(cwd))
        out = io.StringIO()
        err = io.StringIO()
        code = None
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(list(args), backend=backend)
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        finally:
            os.chdir(old_cwd)
        r = Result(code or 0, out.getvalue(), err.getvalue())
        if check and r.returncode != 0:
            raise AssertionError(
                f"gf {' '.join(args)} failed (rc={r.returncode}):\n{r.stdout}\n{r.stderr}"
            )
        return r
    return _run


@pytest.fixture
def mock_backend():
    return MockGitBackend()


def git(*args, cwd: Path) -> None:
    """Run the real git CLI in a fixture."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True)


def push_commit(remote: Path, message: str, content: str, branch: str = "master") -> None:
    """Push a new commit to the bare remote by editing a throwaway clone."""
    work = remote.parent / "_work"
    if work.exists():
        shutil.rmtree(work)
    git("clone", str(remote), str(work), cwd=remote.parent)
    (work / "a.txt").write_text(content)
    git("add", "a.txt", cwd=work)
    git("commit", "-m", message, cwd=work)
    git("push", "origin", branch, cwd=work)
    shutil.rmtree(work)


def push_branch(remote: Path, branch: str, content: str) -> None:
    work = remote.parent / "_work_branch"
    if work.exists():
        shutil.rmtree(work)
    git("clone", str(remote), str(work), cwd=remote.parent)
    git("checkout", "-b", branch, cwd=work)
    (work / f"{branch}.txt").write_text(content)
    git("add", f"{branch}.txt", cwd=work)
    git("commit", "-m", f"{branch} content", cwd=work)
    git("push", "origin", branch, cwd=work)
    shutil.rmtree(work)
