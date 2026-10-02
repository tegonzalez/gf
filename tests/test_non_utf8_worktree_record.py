# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Corrupt worktree `gitdir` record → treated absent, never a traceback.

A subfolder binding's shared checkout is anchored by the worktree record
git writes at `<root>/.gf/repos/<repo-key>/git/worktrees/<checkout-key>/
gitdir` — a plain-text file naming `<work_tree>/.git`. `gf pull`
consults it through `shelf._checkout_record_valid` (the per-key
`existed` probe and `ensure_checkout`'s create/reuse branch); its
`read_text().strip()` decodes UTF-8, so a record carrying non-UTF-8
bytes raised UnicodeDecodeError — a ValueError — and an unreadable
record raised OSError. Neither is a GitFoldersError, so pre-fix both
escaped `pull_shared_bindings`' per-folder attribution and main()'s
`gf:` net as a raw traceback.

The fix wraps the read in `try/except (OSError, ValueError) → return
False` — the same read guard `_checkout_record_dir` already had — so a
corrupt record counts as ABSENT. `ensure_checkout` then sees the still
populated checkout directory and raises the existing record-missing
refusal inside the binding's `pull failed for git-folder` envelope:
rc 1, `gf:`-prefixed, no Traceback — gf never rebuilds over existing
files, so the refusal is the correct loss surface, not a wipe.

Payload convention mirrors test_non_utf8_envelope.py: a lone 0xe9 byte
appended to the record's valid content — 0xe9 opens a three-byte UTF-8
sequence the following bytes cannot complete, so the decode fails even
though the prefix is the true record path.

Pre-fix signature (verified by reverting the worktree diff and running
the discriminating arm): `gf pull` exits 1 with a `Traceback` ending in
`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9`, raised from
`_checkout_record_valid`'s `read_text` before any checkout work. The
control arms pass both ways: a valid record keeps the pull green, and
the unit comparison/missing arms predate this fix.
"""

import contextlib
import subprocess
from pathlib import Path

import pytest

from conftest import gf, git
from gf import layout, shelf


# A lone 0xe9 byte: invalid UTF-8 in any position — appended to valid
# content so the payload proves even a well-formed prefix does not
# reach the parser.
_NON_UTF8 = b"x \xe9\n"


# ---------------------------------------------------------------------------
# helpers — setup failures use pytest.fail, never AssertionError


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        pytest.fail(
            f"setup: git {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stderr}")
    return r


def _setup_gf(*args) -> subprocess.CompletedProcess:
    r = gf(*args, check=False)
    if r.returncode != 0:
        pytest.fail(
            f"setup: gf {' '.join(args)} rc={r.returncode}:\n"
            f"{r.stdout}\n{r.stderr}")
    return r


def _parent(tmp_path: Path) -> Path:
    """A real parent git repo with one commit and no gf.toml."""
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path) -> Path:
    """Bare upstream with `docs/api` on master — the subfolder-binding
    source the record pins clone."""
    up = tmp_path / "upstream"
    _git("init", "-q", "--bare", str(up))
    work = tmp_path / "_seed_upstream"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text("api on master")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")
    return up


@contextlib.contextmanager
def _locked(path: Path, mode: int):
    """Hold `path` at `mode` for the block, restoring its mode after."""
    old = path.stat().st_mode & 0o777
    path.chmod(mode)
    try:
        yield
    finally:
        path.chmod(old)


def _clone_subfolder(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Clone `docs/api` → `vendor/api` and return
    (parent, work_tree, gitdir_file) — the shared checkout at
    `.gf/wt/<repo-key>/<checkout-key>` and its git-written record at
    `.gf/repos/<repo-key>/git/worktrees/<checkout-key>/gitdir`."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up / "docs" / "api"),
              "vendor/api")

    records = sorted(
        (parent / ".gf" / "repos").glob("*/git/worktrees/*/gitdir"))
    if len(records) != 1:
        pytest.fail(
            f"setup: expected one worktree gitdir record, "
            f"found {records}")
    checkouts = [p for p in (parent / ".gf" / "wt").glob("*/*")
                 if p.is_dir()]
    if len(checkouts) != 1:
        pytest.fail(
            f"setup: expected one shared checkout, found {checkouts}")
    return parent, checkouts[0], records[0]


# ---------------------------------------------------------------------------
# real-git pins — the command envelope


def test_pull_non_utf8_worktree_record_refuses_rebuild(tmp_path):
    """Corrupt the shared checkout's `gitdir` record with a non-UTF-8
    payload: `gf pull` counts the record absent and reaches the existing
    record-missing refusal — rc 1, the `gf: pull failed for git-folder
    'api' (vendor/api)` envelope ending in `refusing to rebuild over
    existing files`, no Traceback/UnicodeDecodeError. The populated
    checkout keeps serving the consumer link — nothing is rebuilt over
    it."""
    parent, wt, gitdir_file = _clone_subfolder(tmp_path)
    # keep the record's own valid bytes as the prefix — the appended
    # 0xe9 line is what fails the decode
    gitdir_file.write_bytes(gitdir_file.read_bytes() + _NON_UTF8)

    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert "UnicodeDecodeError" not in r.stderr
    # pull's streamed fetch lines may precede the error — assert the
    # `gf:` envelope per-line, not at offset 0
    assert any(line.startswith("gf: ") for line in r.stderr.splitlines()), \
        r.stderr
    for needle in (
        "pull failed for git-folder 'api' (vendor/api)",
        # the refusal names both the populated checkout and the record
        # dir it can no longer prove
        str(wt.resolve()),
        str(gitdir_file.parent.resolve()),
        "missing from repo store",
        "refusing to rebuild over existing files",
        "worktree repair",
    ):
        assert needle in r.stderr, (needle, r.stderr)
    # the refusal did its job: the existing checkout was never rebuilt
    # or wiped — the consumer link still serves its materialized file
    assert (parent / "vendor" / "api" / "x.txt").read_text().strip() == \
        "api on master"


def test_pull_valid_worktree_record_pulls(tmp_path):
    """Control: a `gitdir` record naming the checkout's `.git` keeps
    `gf pull` green — the discriminating arm differs only in the
    record's bytes. The record is rewritten, not merely left in place,
    so the rewrite itself is proven inert."""
    parent, wt, gitdir_file = _clone_subfolder(tmp_path)
    gitdir_file.write_text(f"{wt.resolve() / '.git'}\n")

    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled api" in r.stdout
    assert "Traceback" not in r.stderr


# ---------------------------------------------------------------------------
# unit pins — `_checkout_record_valid` itself: a record that cannot be
# read, decodes wrong, or names a different work tree all count absent


def _store_checkout(tmp_path: Path) -> layout.Checkout:
    """A subfolder Checkout mapped under tmp_path with its record dir
    created — `_checkout_record_valid` touches only the `gitdir` file,
    so no git store is needed at this seam."""
    co = layout.subfolder_checkout(
        tmp_path, "https://host.invalid/repo", "master", "docs/api")
    co.gitdir.mkdir(parents=True)
    return co


class TestCheckoutRecordValid:
    def test_valid_record(self, tmp_path):
        """Control: the record naming `<work_tree>/.git` is valid —
        the failure arms below do not pass by always returning False."""
        co = _store_checkout(tmp_path)
        (co.gitdir / "gitdir").write_text(
            f"{co.work_tree / '.git'}\n")
        assert shelf._checkout_record_valid(co) is True

    def test_record_naming_foreign_worktree_is_invalid(self, tmp_path):
        """The path comparison still discriminates: a record naming a
        DIFFERENT work tree is invalid — the wrapped read did not widen
        validity to file-existence alone."""
        co = _store_checkout(tmp_path)
        (co.gitdir / "gitdir").write_text(
            f"{tmp_path / 'elsewhere' / '.git'}\n")
        assert shelf._checkout_record_valid(co) is False

    def test_missing_record_is_invalid(self, tmp_path):
        """Absent gitdir file → invalid (the arm the corrupt record now
        joins)."""
        co = _store_checkout(tmp_path)
        assert shelf._checkout_record_valid(co) is False

    def test_non_utf8_record_is_invalid(self, tmp_path):
        """The ValueError arm — pre-fix this raised UnicodeDecodeError
        out of the probe."""
        co = _store_checkout(tmp_path)
        (co.gitdir / "gitdir").write_bytes(
            f"{co.work_tree / '.git'}\n".encode() + _NON_UTF8)
        assert shelf._checkout_record_valid(co) is False

    def test_unreadable_record_is_invalid(self, tmp_path):
        """The OSError arm — EACCES on the record counts absent too
        (the same failure a wiped-permission record produces)."""
        co = _store_checkout(tmp_path)
        gitdir_file = co.gitdir / "gitdir"
        gitdir_file.write_text(f"{co.work_tree / '.git'}\n")
        with _locked(gitdir_file, 0o000):
            assert shelf._checkout_record_valid(co) is False
