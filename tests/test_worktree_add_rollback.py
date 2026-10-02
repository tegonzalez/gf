# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`gf worktree add` post-add-failure rollback retry (R10-6).

When the link step fails after `git worktree add` registered the new
worktree, the rollback `git worktree remove --force` can itself fail —
a locked worktree refuses a single `--force` and yields only to the
doubled `-ff`. The rollback therefore retries with `--force --force`;
when even that fails the die must name the leftover registration: the
ORIGINAL error plus `rollback of the new worktree also failed` plus the
recovery command `git worktree remove --force <path>` addressing the
orphan.

In-process via `gf_inproc` + `MockGitBackend` on pyfakefs. Remove
failures are injected by backend wrappers — the same seam
`test_error_fields.py`'s `_FailPorcelain`/`_FailRevParse` use — and the
mock's own `-ff` lock semantics (a `locked` worktree refuses a single
`--force`, yields to two). Real-git twin of the happy rollback:
R9-3's `test_add_tracked_dir_collision_rolls_back` in test_worktree.py.
"""

from pathlib import Path

from gf.backends import GitResult


SHA1 = "1111111111111111111111111111111111111111"


def _collision_parent(fs, backend, root: str = "/parent") -> Path:
    """A mock-backed parent whose git-folder path collides at the add
    destination.

    The manifest binds git-folder `zz` at `vendor/zz` while the parent
    HEAD commit TRACKS `vendor/zz/t.txt`: `worktree add` materializes a
    real directory exactly where the link step must place its symlink,
    so the post-add `_worktree_link_folders` raises the folder_error
    envelope and the rollback path runs.
    """
    parent = Path(root)
    (parent / ".git").mkdir(parents=True)
    (parent / "gf.toml").write_text(
        '[[git_folder]]\n'
        'name = "zz"\n'
        'path = "vendor/zz"\n'
        'url = "/upstream"\n'
        'ref = "master"\n'
    )
    # The source child reads as a git-folder child via `.gf/git/HEAD`.
    gitdir = parent / "vendor" / "zz" / ".gf" / "git"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/master\n")
    repo = backend.seed(str(parent))
    backend.add_commit(repo, SHA1, {
        "README": "root",
        "vendor/zz/t.txt": "tracked",
    })
    repo.refs["refs/heads/master"] = SHA1
    return parent


def _remove_calls(backend) -> list[tuple]:
    """`worktree remove` argv tuples the backend saw, in order."""
    return [args for args, _ in backend.calls
            if args[:2] == ("worktree", "remove")]


def _registered(backend, parent: Path, wt: Path):
    """The mock's Worktree record for `wt` under `parent`, or None."""
    return backend._find_worktree(backend.repos[str(parent)], str(wt))


class _LockAddedWorktree:
    """Backend wrapper: the worktree `worktree add` just registered is
    left LOCKED — the mock then refuses the single-force rollback
    `worktree remove` and yields only to the doubled `--force --force`."""

    def __init__(self, inner, target: Path):
        self._inner = inner
        self._target = str(Path(target).resolve())

    def git(self, *args, **kwargs):
        result = self._inner.git(*args, **kwargs)
        if args[:2] == ("worktree", "add"):
            for repo in self._inner.repos.values():
                for wt in repo.worktrees:
                    if wt.path == self._target and wt.locked is None:
                        wt.locked = "injected"
        return result

    def git_capture(self, *args, **kwargs):
        return self._inner.git_capture(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _FailWorktreeRemove:
    """Backend wrapper: every `worktree remove` of `target` fails — the
    arm where even the doubled-force rollback cannot drop the
    registration, so the user must be told the orphan's path.

    `removes` records each attempted remove argv (the refusals never
    reach the inner backend, so its own call log stays clean)."""

    def __init__(self, inner, target: Path):
        self._inner = inner
        self._target = str(Path(target).resolve())
        self.removes: list[tuple] = []

    def git(self, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            self.removes.append(args)
            if str(Path(args[-1]).resolve()) == self._target:
                # check=False callers see the git-level refusal as a
                # nonzero GitResult — what GitCliBackend itself returns.
                return GitResult(
                    1, "",
                    f"fatal: cannot remove '{self._target}': device busy")
        return self._inner.git(*args, **kwargs)

    def git_capture(self, *args, **kwargs):
        return self._inner.git_capture(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_rollback_retries_locked_worktree_with_double_force(
        fs, gf_inproc, mock_backend):
    """Post-add failure with the new worktree locked: the first
    `remove --force` is refused, the `-ff` retry succeeds — so the user
    sees the ORIGINAL link error (rc 1), never the orphan wording, and
    no tree or registration survives."""
    parent = _collision_parent(fs, mock_backend)
    wt = Path("/wt")

    r = gf_inproc("-C", str(parent), "worktree", "add", str(wt),
                  backend=_LockAddedWorktree(mock_backend, wt),
                  check=False)

    assert r.returncode == 1
    err = r.stderr + r.stdout
    # The original post-add error is the one reported.
    assert "worktree add failed for git-folder 'zz' (vendor/zz)" in err
    assert f"child path {wt / 'vendor' / 'zz'} already exists" in err
    # The single-force remove was refused; the doubled-force retry ran.
    assert _remove_calls(mock_backend) == [
        ("worktree", "remove", "--force", str(wt)),
        ("worktree", "remove", "--force", "--force", str(wt)),
    ]
    # No orphan wording: the rollback succeeded.
    assert "rollback of the new worktree also failed" not in err
    assert "git worktree remove --force" not in err
    # ...and no orphan: registration dropped, tree gone.
    assert _registered(mock_backend, parent, wt) is None
    assert not wt.exists()


def test_rollback_failure_names_orphan_and_recovery(
        fs, gf_inproc, mock_backend):
    """When BOTH rollback removes fail, the die carries the original
    error AND the rollback clause AND the recovery command naming the
    orphaned worktree path — the registration is still live, so the
    user needs all three to clear it."""
    parent = _collision_parent(fs, mock_backend)
    wt = Path("/wt")
    backend = _FailWorktreeRemove(mock_backend, wt)

    r = gf_inproc("-C", str(parent), "worktree", "add", str(wt),
                  backend=backend, check=False)

    assert r.returncode == 1
    err = r.stderr + r.stdout
    # Original error first, then the rollback detail and recovery hint.
    assert "worktree add failed for git-folder 'zz' (vendor/zz)" in err
    assert "rollback of the new worktree also failed" in err
    assert f"git worktree remove --force {wt}" in err
    # Both rollback attempts ran before the die.
    assert backend.removes == [
        ("worktree", "remove", "--force", str(wt)),
        ("worktree", "remove", "--force", "--force", str(wt)),
    ]
    # The orphan really is still registered and on disk.
    assert _registered(mock_backend, parent, wt) is not None
    assert wt.exists()


def test_rollback_single_force_no_retry_when_it_succeeds(
        fs, gf_inproc, mock_backend):
    """Control: an unimpeded post-add rollback issues ONE
    `remove --force` and stops — the `-ff` retry is conditional on
    failure, not unconditional."""
    parent = _collision_parent(fs, mock_backend)
    wt = Path("/wt")

    r = gf_inproc("-C", str(parent), "worktree", "add", str(wt),
                  backend=mock_backend, check=False)

    assert r.returncode == 1
    err = r.stderr + r.stdout
    assert "worktree add failed for git-folder 'zz' (vendor/zz)" in err
    assert _remove_calls(mock_backend) == [
        ("worktree", "remove", "--force", str(wt)),
    ]
    assert "rollback of the new worktree also failed" not in err
    assert _registered(mock_backend, parent, wt) is None
    assert not wt.exists()
