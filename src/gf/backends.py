# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .exceptions import GitError
from .runner import run_command


# Ambient GIT_* variables that must never pass through to spawned `git`
# subprocesses. Each one silently retargets repository state
# (GIT_DIR/GIT_WORK_TREE/GIT_COMMON_DIR, the index and object stores,
# namespaces, grafts, quarantine), redirects repo discovery (ceiling
# dirs, cross-filesystem), relocates the exec path, injects config
# (GIT_CONFIG_COUNT/PARAMETERS and the GIT_CONFIG_KEY_*/GIT_CONFIG_VALUE_*
# pairs), changes pathspec semantics, substitutes a whole config file
# (GIT_CONFIG/GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM — an ambient
# `url.<base>.insteadOf` in the named file redirects every fetch),
# changes object formats or ref semantics (GIT_DEFAULT_HASH wedges a
# sha256 store against a sha1 upstream; GIT_DEFAULT_INITIAL_BRANCH_NAME
# points the child's HEAD symref at a branch the upstream lacks;
# GIT_INDEX_VERSION, GIT_SHALLOW_FILE, GIT_NO_REPLACE_OBJECTS), plants
# executable hooks through the template copy every `git init`/
# `init --bare` performs (GIT_TEMPLATE_DIR), or runs a caller-chosen
# program (GIT_SSH, GIT_EXTERNAL_DIFF — the latter execs through the
# `gf git diff` passthrough). GIT_TEST_* is git's own test-injection
# class — never a legitimate ambient input. A `gf` run inside a foreign
# repo's environment (a `git submodule`-style hook, direnv, a test
# harness) would otherwise operate on that repo instead of gf's own
# checkouts. gf's deliberate overlays (the `env` argument, `git_dir`,
# `work_tree`, `PWD`) are applied by callers AFTER this scrub, so they
# always win.
_BLOCKED_ENV_EXACT = frozenset({
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_QUARANTINE_PATH",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_EXEC_PATH",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_DEFAULT_HASH",
    "GIT_DEFAULT_INITIAL_BRANCH_NAME",
    "GIT_INDEX_VERSION",
    "GIT_SHALLOW_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_TEMPLATE_DIR",
    "GIT_SSH",
    "GIT_EXTERNAL_DIFF",
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
})
_BLOCKED_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_TEST_")


def clean_environ() -> dict[str, str]:
    """Return `os.environ` minus the ambient repo-redirecting GIT_* vars.

    Deliberately kept: GIT_CONFIG_NOSYSTEM (it can only REMOVE the
    system config source, never redirect one — tests/conftest.py still
    pins it), and user identity/transport choices a child `git` should
    still honor — GIT_AUTHOR_*/GIT_COMMITTER_*,
    GIT_SSH_COMMAND/GIT_SSH_VARIANT (the legacy exec-only GIT_SSH is
    blocked), GIT_ASKPASS, GIT_TERMINAL_PROMPT,
    GIT_SSL_*/GIT_HTTP_*/GIT_PROXY_*, GIT_EDITOR, GIT_PAGER,
    GIT_OPTIONAL_LOCKS, GIT_TRACE*. The config FILE channel is
    unaffected: git still reads $HOME/.gitconfig and
    $XDG_CONFIG_HOME/git/config in every spawned child, so the suite's
    pins now travel through a redirected HOME rather than
    GIT_CONFIG_GLOBAL.
    """
    return {
        k: v for k, v in os.environ.items()
        if k not in _BLOCKED_ENV_EXACT
        and not k.startswith(_BLOCKED_ENV_PREFIXES)
    }


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
        env: dict[str, str] | None = None,
    ) -> GitResult:
        ...

    def git_capture(
        self,
        *args: Any,
        cwd: Path | None = None,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
        env: dict[str, str] | None = None,
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
        env: dict[str, str] | None = None,
    ) -> GitResult:
        merged = clean_environ()
        if env:
            merged.update(env)
        if git_dir is not None:
            merged["GIT_DIR"] = str(git_dir)
            # `git_dir` marks a gf-managed gitdir: every op on it is
            # hook-free. The config-injection channel pins
            # core.hooksPath at the null device, so committed hooks,
            # planted hooks and any core.hooksPath redirection (a
            # config include, ~/.gitconfig, a malicious upstream's
            # suggestion) all resolve to a directory whose hook names
            # never exist. The triple is written AFTER
            # `merged.update(env)` so a caller-supplied env cannot
            # reintroduce hooks, and ambient GIT_CONFIG_* vars are
            # already scrubbed by clean_environ, so index 0 cannot
            # collide with an inherited pair. cwd-only calls keep the
            # user's own repo hooks, and `_git_env_for_child`
            # passthroughs (`gf sh`/`git`/`diff`/`log`) never set
            # git_dir here — user-invoked git keeps its hooks.
            merged["GIT_CONFIG_COUNT"] = "1"
            merged["GIT_CONFIG_KEY_0"] = "core.hooksPath"
            merged["GIT_CONFIG_VALUE_0"] = os.devnull
        if work_tree is not None:
            merged["GIT_WORK_TREE"] = str(work_tree)
        if cwd is not None:
            merged["PWD"] = str(cwd)
        cmd = ["git"] + [str(a) for a in args]
        try:
            result = run_command(cmd, merged, mode="stream" if stream else "capture", cwd=cwd)
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
        env: dict[str, str] | None = None,
    ) -> str:
        return self.git(
            *args, cwd=cwd, git_dir=git_dir, work_tree=work_tree, env=env,
        ).stdout
