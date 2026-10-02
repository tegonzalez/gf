# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""P1 preservation witnesses — repository-identity / wrong-target
observations under a conflicting ambient Git context (Change
DC-DOC-PLAN-007, obligation O-identity, "Conflicting ambient Git
context" coverage axis).

Every `git` process `gf` spawns runs on an environment scrubbed of
ambient `GIT_*` variables before `gf`'s own overlays apply (gf-spec.md
subfolder layout note; gf-arch.md `clean_environ`). A `gf pull` that
honored an inherited `GIT_DIR`/`GIT_WORK_TREE` would mutate the wrong
repository (`wrong-target` verdict); one that honored
`GIT_INDEX_FILE` would write the checkout's index partition to a
foreign path (work-loss surface). These witnesses inject the hostile
variables as controlled fixture input and observe that the bound
repository is the only thing mutated.

Real git over local bare upstreams via the `gf` subprocess; fixture
env injection per the `test_ambient_env_scrub.py` pattern. Nothing in
this file consults `src/gf/` to decide expected behavior.
"""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import checkout_snapshot, gf, git, git_probe_env


# ---------------------------------------------------------------------------
# helpers — real-git fixtures, same style as test_pull_autostash_recovery


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    # Probes and fixture setup both run on the scrubbed probe env: an
    # ambient GIT_* the test injects must never steer the observation
    # itself (the same wrong-target mode under witness).
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True,
        env=git_probe_env())
    if check and r.returncode != 0:
        pytest.fail(
            f"setup: git {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stderr}")
    return r


def _out(*args) -> str:
    return _git(*args).stdout.strip()


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", "-q", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root\n")
    git("add", "README", cwd=parent)
    git("commit", "-qm", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str = "upstream") -> Path:
    """Bare upstream with docs/api/x.txt on master."""
    up = tmp_path / name
    _git("init", "-q", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text("api on master\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")
    return up


def _advance(up: Path, tmp_path: Path, tag: str = "adv") -> None:
    work = tmp_path / f"_adv_{tag}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api" / "x.txt").write_text(f"api {tag}\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", tag)
    _git("-C", work, "push", "-q", "origin", "master")


def _clone_lib(tmp_path: Path) -> tuple[Path, Path, Path]:
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    r = gf("-C", str(parent), "clone", str(up), "vendor/lib",
           check=False)
    if r.returncode != 0:
        pytest.fail(f"setup: clone rc={r.returncode}:\n"
                    f"{r.stdout}\n{r.stderr}")
    return parent, up, parent / "vendor" / "lib"


def _foreign_repo(tmp_path: Path) -> Path:
    """A repo that shares no objects, refs, or worktree with the
    fixture's parent/child — the wrong target the ambient vars aim at."""
    foreign = tmp_path / "foreign"
    git("init", "-q", str(foreign), cwd=tmp_path)
    (foreign / "foreign.txt").write_text("foreign\n")
    git("add", "foreign.txt", cwd=foreign)
    git("commit", "-qm", "foreign", cwd=foreign)
    return foreign


# ---------------------------------------------------------------------------
# ambient GIT_DIR / GIT_WORK_TREE — the pull must bind the bound child


def test_pull_ignores_ambient_git_dir_and_work_tree(tmp_path,
                                                    monkeypatch):
    """`GIT_DIR`/`GIT_WORK_TREE` aimed at a foreign repo must not steer
    a single `gf`-spawned git call: the child still fast-forwards on its
    own gitdir, and the foreign repo is byte- and ref-identical
    afterwards (`wrong-target` would show either divergence)."""
    parent, up, child = _clone_lib(tmp_path)
    foreign = _foreign_repo(tmp_path)
    foreign_head = _out("-C", foreign, "rev-parse", "HEAD")
    foreign_porcelain = _out("-C", foreign, "status", "--porcelain")
    _advance(up, tmp_path, "adv")

    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    # the bound child moved — the foreign repo did not
    assert (child / "docs" / "api" / "x.txt").read_text() == "api adv\n"
    assert _out("-C", foreign, "rev-parse", "HEAD") == foreign_head
    assert _out("-C", foreign, "status", "--porcelain") == \
        foreign_porcelain


def test_pull_never_writes_ambient_git_index_file(tmp_path,
                                                  monkeypatch):
    """An ambient `GIT_INDEX_FILE` sentinel must never appear: every
    index-touching git the pull spawns (dirty check, checkout, merge,
    stash) binds the checkout's own index — a leaked var would read the
    missing sentinel as an empty index (everything reports dirty) or
    create it at the foreign path."""
    parent, up, child = _clone_lib(tmp_path)
    _advance(up, tmp_path, "adv")
    sentinel = tmp_path / "ambient-index"

    monkeypatch.setenv("GIT_INDEX_FILE", str(sentinel))
    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert not os.path.lexists(sentinel), (
        f"ambient GIT_INDEX_FILE was created by a pull-spawned git: "
        f"{sentinel}")
    # the real update still landed on the bound child
    assert (child / "docs" / "api" / "x.txt").read_text() == "api adv\n"
    snap = checkout_snapshot(child / ".gf" / "git", child)
    assert snap["porcelain"] == ""
