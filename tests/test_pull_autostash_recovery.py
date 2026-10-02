# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`gf pull --autostash` restores the autostash when the update fails
after `stash push` (F11 ruling).

`update_child`'s autostash arm runs `git stash push -u -m "gf autostash"`
before the guarded update block and pops the stash both on success and
inside the `except GitFoldersError` recovery. `_set_child_origin` — the
first git write inside that block — once ran BEFORE the `try`, so a
failing `git remote set-url` skipped the recovery pop entirely: the pull
still errored rc=2, but the dirt stayed stashed (`stash@{0}: On master:
gf autostash` orphaned in the child's gitdir) and the worktree kept the
stashed file's reverted bytes — silently "losing" the local work the
flag promised to preserve.

The discriminating witness dirties a whole-repo child (a tracked
modification AND an untracked file, covering both `stash push -u`
restore paths) and induces the `remote set-url` failure with a
pre-placed `<child>/.gf/git/config.lock` — `remote get-url` is a read
and still succeeds, so the failure lands exactly on the `set-url` write
inside `_set_child_origin`, after the stash push.

Pins:
- failure arm: `gf pull --autostash` → rc=2 naming the binding and the
  `remote set-url` failure; the child's stash list is EMPTY (the except
  popped it) and both dirty files' contents are restored, not reverted.
  Pre-fix signature (verified against the pre-fix `update_child`): rc=2,
  `stash@{0}: On master: gf autostash` orphaned, `docs/api/x.txt` back
  at upstream bytes, `local.txt` gone.
- controls: a dirty pull WITHOUT `--autostash` still refuses rc=3
  (DirtyError, dirt untouched, no stash created); a successful
  `--autostash` still applies upstream and pops cleanly (empty stash
  list); a clean pull is unaffected.

Real git over a local bare upstream via the `gf` subprocess — the
whole-repo child's gitdir lives at `<child>/.gf/git` (no `.git` link),
so the stash probe spells `--git-dir` directly.
"""

import subprocess
from pathlib import Path

import pytest

from conftest import gf, git


# ---------------------------------------------------------------------------
# helpers — setup failures use pytest.fail, never AssertionError
#   (tests/test_consumer_path_containment.py convention)


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        pytest.fail(
            f"setup: git {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stderr}")
    return r


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", "-q", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root\n")
    git("add", "README", cwd=parent)
    git("commit", "-qm", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str = "upstream") -> Path:
    """Bare upstream with docs/api/x.txt + tools/t.txt on master."""
    up = tmp_path / name
    _git("init", "-q", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text("api on master\n")
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text("tool on master\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")
    return up


def _advance(up: Path, tmp_path: Path, tag: str = "adv") -> None:
    """Push one more commit to upstream master."""
    work = tmp_path / f"_adv_{tag}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api" / "x.txt").write_text(f"api {tag}\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", tag)
    _git("-C", work, "push", "-q", "origin", "master")


def _clone_lib(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(parent, upstream, child) — parent serves a whole-repo
    `vendor/lib` binding cloned from upstream; `child` is a real
    directory whose gitdir is `child/.gf/git` (no `.git` file)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    r = gf("-C", str(parent), "clone", str(up), "vendor/lib",
           check=False)
    if r.returncode != 0:
        pytest.fail(f"setup: clone rc={r.returncode}:\n"
                    f"{r.stdout}\n{r.stderr}")
    child = parent / "vendor" / "lib"
    if not (child / ".gf" / "git" / "HEAD").is_file():
        pytest.fail("setup: whole-repo child has no .gf/git gitdir")
    return parent, up, child


def _stash_list(child: Path) -> str:
    """The child checkout's `git stash list` output ("" when none).

    The whole-repo child has no `.git` — its gitdir is `.gf/git` — so
    the probe addresses the gitdir directly rather than `-C <child>`.
    """
    return _git("--git-dir", str(child / ".gf" / "git"),
                "stash", "list").stdout


# ---------------------------------------------------------------------------
# F11 — a failed update must not orphan the autostash


def test_pull_autostash_failed_set_url_restores_stash(tmp_path):
    """Dirty child + induced `_set_child_origin` failure: a pre-placed
    `.gf/git/config.lock` lets the `remote get-url` read pass and fails
    the `remote set-url` write — the first git write after `stash push`.
    The pull errors rc=2 (GitError), the autostash is popped by the
    `except GitFoldersError` recovery (empty stash list), and both dirty
    files' contents are restored — not left at the stash-reverted bytes.
    Pre-fix the `set-url` ran outside the try: same rc=2, but
    `stash@{0}: ... gf autostash` was orphaned, `x.txt` stayed at
    upstream bytes and `local.txt` stayed deleted."""
    parent, _up, child = _clone_lib(tmp_path)
    gitdir = child / ".gf" / "git"

    # Dirt in both stash-covered forms: a tracked modification (stash
    # push reverts its bytes) and an untracked file (`-u` removes it).
    (child / "docs" / "api" / "x.txt").write_text("dirty change\n")
    (child / "local.txt").write_text("local work\n")

    # The induced failure: the lock makes `remote set-url` fail while
    # the preceding `remote get-url` read still succeeds.
    (gitdir / "config.lock").write_text("")

    r = gf("-C", str(parent), "pull", "--autostash", check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "pull failed for git-folder 'lib'" in err, err
    assert "remote set-url" in err, err

    # The pin: no orphaned autostash, and the worktree holds the dirty
    # bytes again — restored, not reverted.
    assert _stash_list(child) == "", (
        f"autostash orphaned: {_stash_list(child)}")
    assert (child / "docs" / "api" / "x.txt").read_text() == \
        "dirty change\n"
    assert (child / "local.txt").read_text() == "local work\n"


# ---------------------------------------------------------------------------
# controls — neighboring autostash/dirty behavior is unchanged


def test_pull_dirty_child_without_autostash_still_refuses(tmp_path):
    """Control: without `--autostash`/`--force` the dirty check still
    refuses rc=3 (DirtyError) BEFORE any stash — the dirt is untouched
    and no stash entry was ever created."""
    parent, _up, child = _clone_lib(tmp_path)
    (child / "docs" / "api" / "x.txt").write_text("dirty change\n")

    r = gf("-C", str(parent), "pull", check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert "dirty" in err, err
    assert "pull failed for git-folder 'lib'" in err, err
    assert (child / "docs" / "api" / "x.txt").read_text() == \
        "dirty change\n"
    assert _stash_list(child) == ""


def test_pull_autostash_success_leaves_no_stash(tmp_path):
    """Control: a successful `--autostash` still applies the upstream
    advance and pops the stash cleanly — the untracked dirt is back and
    the child's stash list is empty (the pop consumed the entry)."""
    parent, up, child = _clone_lib(tmp_path)
    _advance(up, tmp_path)
    (child / "local.txt").write_text("local work\n")

    r = gf("-C", str(parent), "pull", "--autostash", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled lib" in r.stdout
    assert (child / "docs" / "api" / "x.txt").read_text() == "api adv\n"
    assert (child / "local.txt").read_text() == "local work\n"
    assert _stash_list(child) == ""


def test_pull_clean_child_unaffected(tmp_path):
    """Control: a clean child pulls the upstream advance as before —
    the moved `_set_child_origin` changes nothing on the happy path."""
    parent, up, child = _clone_lib(tmp_path)
    _advance(up, tmp_path)

    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled lib" in r.stdout
    assert (child / "docs" / "api" / "x.txt").read_text() == "api adv\n"
