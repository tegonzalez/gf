# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`gf worktree add` post-add rollback over non-gf/non-OS errors (R13-3).

The rollback catch after `git worktree add` registered the new worktree
was broadened from `except (GitFoldersError, OSError)` to
`except Exception`: an exception outside the gf envelope families used
to escape as a bare traceback AND orphan the registered worktree — a
retry then trips over both the leftover registration and the tree.

UnicodeDecodeError arm (real git): `_resolve` reads only `gf.toml`, so
a `gf.local.toml` holding non-UTF-8 bytes is first touched by the
manifest copy's `src.read_text()` — after the add, inside
`_worktree_link_folders`. UnicodeDecodeError is a ValueError: outside
both arms of the old catch. Post-fix the add dies through the `gf:`
envelope and `git worktree list` shows no leftover registration.
Pre-fix signature: a UnicodeDecodeError traceback on stderr with the
new worktree still registered (and still on disk).

Injected TypeError arm (in-process, the same gf_inproc/MockGitBackend
plumbing the sibling rollback file uses): monkeypatching the link step
to raise a non-gf, non-OS error pins that the catch is broad — any
unexpected post-add exception rolls back, not only UnicodeDecodeError.
"""

import subprocess
from pathlib import Path

from conftest import gf, git, push_commit
from gf import cli


SHA1 = "1111111111111111111111111111111111111111"


def _registered_worktrees(parent: Path) -> list[str]:
    """Worktree paths registered for `parent` per `git worktree list`."""
    r = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(parent), capture_output=True, text=True, check=True)
    return [line.split(" ", 1)[1] for line in r.stdout.splitlines()
            if line.startswith("worktree ")]


def _parent_with_bad_local_manifest(tmp_path) -> Path:
    """A real parent repo carrying a `git_folder` binding (via
    `gf clone`) plus an UNTRACKED `gf.local.toml` whose bytes are not
    UTF-8 — never parsed before the add, so the post-add manifest copy
    is the first reader."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    (parent / "gf.local.toml").write_bytes(b"local caf\xe9\n")
    return parent


def test_add_non_utf8_local_manifest_rolls_back(tmp_path):
    """A non-UTF-8 `gf.local.toml` makes the post-add manifest copy raise
    UnicodeDecodeError — neither GitFoldersError nor OSError. The
    broadened catch must still roll the registered worktree back and
    die through the `gf:` envelope instead of tracebacking over a live
    registration."""
    parent = _parent_with_bad_local_manifest(tmp_path)
    wt = tmp_path / "wt"

    result = gf("-C", str(parent), "worktree", "add", str(wt), check=False)

    assert result.returncode == 1
    # `git worktree add` streamed "Preparing worktree" — the failure is
    # post-registration, so the remaining asserts pin the rollback.
    assert "Preparing worktree" in result.stderr
    # The catch routes the UnicodeDecodeError through die()'s `gf:`
    # envelope — clean message, no interpreter traceback.
    assert "gf: 'utf-8' codec can't decode byte 0xe9" in result.stderr
    assert "Traceback" not in result.stderr
    # The rollback dropped the registration AND the tree — no orphan.
    assert _registered_worktrees(parent) == [str(parent.resolve())]
    assert not wt.exists()


def _plain_parent(fs, backend, root: str = "/parent") -> Path:
    """A mock-backed parent with a `git_folder` entry; the seeded repo
    supplies the HEAD the mock's `worktree add` resolves."""
    parent = Path(root)
    (parent / ".git").mkdir(parents=True)
    (parent / "gf.toml").write_text(
        '[[git_folder]]\n'
        'name = "zz"\n'
        'path = "vendor/zz"\n'
        'url = "/upstream"\n'
        'ref = "master"\n'
    )
    repo = backend.seed(str(parent))
    backend.add_commit(repo, SHA1, {"README": "root"})
    repo.refs["refs/heads/master"] = SHA1
    return parent


def _registered(backend, parent: Path, wt: Path):
    """The mock's Worktree record for `wt` under `parent`, or None."""
    return backend._find_worktree(backend.repos[str(parent)], str(wt))


def _remove_calls(backend) -> list[tuple]:
    """`worktree remove` argv tuples the backend saw, in order."""
    return [args for args, _ in backend.calls
            if args[:2] == ("worktree", "remove")]


def test_add_injected_link_step_error_rolls_back(
        fs, gf_inproc, mock_backend, monkeypatch):
    """A non-gf, non-OS exception escaping the link step — a TypeError
    injected in place of `_worktree_link_folders` — takes the same
    broadened catch: one `remove --force` rolls the registered worktree
    back and the error dies through the `gf:` envelope."""
    parent = _plain_parent(fs, mock_backend)
    wt = Path("/wt")

    def _boom(parent, new_parent, folders, force):
        raise TypeError("injected non-gf, non-OS link-step failure")

    monkeypatch.setattr(cli, "_worktree_link_folders", _boom)

    r = gf_inproc("-C", str(parent), "worktree", "add", str(wt),
                  backend=mock_backend, check=False)

    assert r.returncode == 1
    # die()'s envelope: `gf:` prefix, the injected message, no traceback.
    assert r.stderr.startswith("gf: ")
    assert "injected non-gf, non-OS link-step failure" in r.stderr
    assert "Traceback" not in r.stderr
    # The rollback remove ran (unlocked tree: a single --force suffices)
    # and dropped both the registration and the tree — no orphan.
    assert _remove_calls(mock_backend) == [
        ("worktree", "remove", "--force", str(wt)),
    ]
    assert _registered(mock_backend, parent, wt) is None
    assert not wt.exists()
