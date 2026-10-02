# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import contextlib
import hashlib
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
    # gf's ambient-env scrub now strips the GIT_CONFIG_* file-redirect
    # vars (GIT_CONFIG/GLOBAL/SYSTEM) from every spawned git, so the
    # suite pins its config through a redirected HOME instead: git
    # always reads $HOME/.gitconfig, a channel the scrub cannot close
    # (HOME is not a GIT_* name). The GIT_CONFIG_* vars are dropped
    # outright so the fixtures' own unscrubbed `git` calls resolve the
    # same file a gf child would.
    home = tmp_path / "home"
    home.mkdir()
    # Belt-and-braces: git itself refuses every non-local transport
    # (verified on git 2.47.3 — https/ssh/git:// exit 128 "transport not
    # allowed"); local paths and user-invoked file:// stay allowed.
    (home / ".gitconfig").write_text(
        "[init]\n\tdefaultBranch = master\n"
        "[protocol]\n\tallow = never\n"
        '[protocol "file"]\n\tallow = user\n'
    )
    monkeypatch.setenv("HOME", str(home))
    # git reads `$XDG_CONFIG_HOME/git/config` ahead of `~/.gitconfig`;
    # aim it at an empty dir so a host XDG config cannot leak in.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        monkeypatch.delenv(key, raising=False)


@contextlib.contextmanager
def deny_file_transport(tmp_path, monkeypatch):
    """Refuse git's file transport inside the block, then restore.

    Rewrites the suite's $HOME-anchored `.gitconfig` — the channel every
    spawned git still reads now that the ambient-env scrub strips the
    GIT_CONFIG_* env vars outright (a swapped GIT_CONFIG_GLOBAL value
    would reach only the fixtures' own unscrubbed `git` calls, never a
    gf child). The deny content restates the `_git_env` contract because
    the file fully replaces the pinned global config.
    """
    gitconfig = Path(os.environ["HOME"]) / ".gitconfig"
    prev = gitconfig.read_text()
    gitconfig.write_text(
        "[init]\n\tdefaultBranch = master\n"
        "[protocol]\n\tallow = never\n"
        '[protocol "file"]\n\tallow = never\n')
    try:
        yield
    finally:
        gitconfig.write_text(prev)


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


# ---------------------------------------------------------------------------
# Preservation-witness helpers (Change DC-DOC-PLAN-007).
#
# These probes observe a checkout's protected surface through real `git`
# only — never through `gf` — so a witness's before/after comparison is
# independent of the product under test (docs/gf-testing.md
# "Preservation witness oracle"). All of them are read-only.


# Ambient `GIT_*` names a preservation probe must never inherit — the
# repo-pointer/index/object/config-source/init/command-path/pathspec
# family gf-spec.md line ~346 requires gf itself to scrub. A witness
# that ran its probes under a test-injected GIT_INDEX_FILE or GIT_DIR
# would observe the injected target, not the checkout — the same
# wrong-target failure mode the identity witnesses assert against.
_GIT_PROBE_BLOCK = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_INDEX_VERSION", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_SHALLOW_FILE",
    "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE",
    "GIT_NAMESPACE", "GIT_PREFIX", "GIT_QUARANTINE_PATH",
    "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
    "GIT_TEMPLATE_DIR", "GIT_DEFAULT_HASH",
    "GIT_DEFAULT_INITIAL_BRANCH_NAME",
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF",
    "GIT_DIFF_OPTS",
    "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_WORK_TREE_CONFIG",
}
_GIT_PROBE_BLOCK_PREFIX = (
    "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_TEST_")


def git_probe_env() -> dict:
    """The suite env minus every ambient `GIT_*` pointer/injection name
    — what gf promises its spawned git sees, applied to the witness's
    own probes so they always observe the named repository."""
    return {
        k: v for k, v in os.environ.items()
        if not (k in _GIT_PROBE_BLOCK
                or k.startswith(_GIT_PROBE_BLOCK_PREFIX))
    }


def git_out(*args, cwd: Path | None = None) -> str:
    """Run real git, return stdout; raise on a non-zero exit."""
    r = git_try(*args, cwd=cwd)
    if r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stderr}")
    return r.stdout


def git_try(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run real git without checking the exit code."""
    return subprocess.run(
        ["git", *map(str, args)],
        cwd=str(cwd) if cwd else None,
        env=git_probe_env(),
        capture_output=True, text=True)


def hash_tree(root: Path) -> dict[str, str]:
    """Map each file under `root` to the sha256 of its bytes.

    Top-level `.gf`/`.git` entries are skipped: they are metadata, not
    work bytes — a child's `.gf/git` gitdir and a linked checkout's
    records are observed through the git probes below, not file hashes.
    """
    root = Path(root)
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] in (".gf", ".git"):
            continue
        if path.is_symlink():
            out[rel.as_posix()] = "symlink:" + os.readlink(path)
        elif path.is_file():
            out[rel.as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return out


def checkout_snapshot(gitdir: Path, work_tree: Path) -> dict:
    """Capture the protected surface of one checkout for a preservation
    witness (docs/gf-testing.md oracle categories): index/stash
    partition, HEAD, and every ref — paired with a `hash_tree` pass for
    protected bytes. `gitdir` is the checkout's own gitdir (`child/.gf/
    git` whole-repo, `<store>/worktrees/<key>` for a shared checkout);
    `work_tree` is the materialized tree.
    """
    addr = ("--git-dir", str(gitdir), "--work-tree", str(work_tree))
    head = git_try(*addr, "rev-parse", "HEAD")
    refs = git_try(*addr, "for-each-ref",
                   "--format=%(refname) %(objectname)")
    return {
        "bytes": hash_tree(work_tree),
        "porcelain": git_try(*addr, "status", "--porcelain").stdout,
        "staged": git_try(*addr, "diff", "--cached", "--name-only").stdout,
        "stash": git_try(*addr, "stash", "list").stdout,
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "refs": dict(
            line.split(" ", 1)
            for line in refs.stdout.splitlines() if " " in line),
    }
