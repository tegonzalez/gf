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

F6 extension: when even the doubled-force remove reports failure, the
rollback re-lists `worktree list --porcelain` and reports the ACTUAL
registration state rather than assuming the remove's rc told the truth —
not registered -> the ORIGINAL error only; registered + tree gone ->
`git worktree prune`; registered + tree present -> `git worktree remove
--force <path>`; the re-check itself failed -> both remedies. The
wrappers below inject each remove/list seam shape (live-git twins: the
`/tmp/f6-live/bin-*` shims).
"""

import shutil
from pathlib import Path

from gf.backends import GitResult
from gf.exceptions import GitError


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


class _RemoveLandsButRc1:
    """Backend wrapper: `worktree remove` of `target` performs the REAL
    unregister — the inner mock drops the record, the tree and the admin
    dir — but the call reports rc=1 anyway: the shim that runs the real
    remove then exits 1 on a later cleanup step. The `-ff` retry then
    refuses 'not a working tree', still rc=1. A nonzero remove does NOT
    prove the registration survived.

    `removes` records each attempted remove argv, like
    `_FailWorktreeRemove`."""

    def __init__(self, inner, target: Path):
        self._inner = inner
        self._target = str(Path(target).resolve())
        self.removes: list[tuple] = []

    def git(self, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            self.removes.append(args)
            if str(Path(args[-1]).resolve()) == self._target:
                result = self._inner.git(*args, **kwargs)
                return GitResult(
                    1, result.stdout,
                    result.stderr or "shim: post-remove cleanup failed")
        return self._inner.git(*args, **kwargs)

    def git_capture(self, *args, **kwargs):
        return self._inner.git_capture(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _FailRemoveDropTree:
    """Backend wrapper: `worktree remove` of `target` fails (rc=1) AND
    the tree is gone while the registration record survives — the
    stale-record arm where the remedy is `git worktree prune`, not
    another remove that can only fail on the missing tree again.

    `removes` records each attempted remove argv."""

    def __init__(self, inner, target: Path):
        self._inner = inner
        self._target = str(Path(target).resolve())
        self.removes: list[tuple] = []

    def git(self, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            self.removes.append(args)
            if str(Path(args[-1]).resolve()) == self._target:
                # Drop the tree but never the registration: the inner
                # backend is not called, so `repo.worktrees` keeps the
                # record and `worktree list --porcelain` still lists it.
                shutil.rmtree(self._target, ignore_errors=True)
                return GitResult(
                    1, "",
                    f"fatal: remove partially failed for '{self._target}'")
        return self._inner.git(*args, **kwargs)

    def git_capture(self, *args, **kwargs):
        return self._inner.git_capture(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _FailWorktreeList:
    """Backend wrapper: `worktree list` fails — the post-remove
    registration re-check cannot determine whether the orphan is still
    registered. Compose under `_FailWorktreeRemove` so the rollback
    removes fail first and the re-check is reached."""

    def __init__(self, inner):
        self._inner = inner

    def git(self, *args, **kwargs):
        if args[:2] == ("worktree", "list"):
            raise GitError("git worktree list failed: exploded")
        return self._inner.git(*args, **kwargs)

    def git_capture(self, *args, **kwargs):
        if args[:2] == ("worktree", "list"):
            raise GitError("git worktree list failed: exploded")
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
    # Registered AND on disk names ONLY the remove remedy — `prune` is
    # the stale-record remedy and must not be suggested for a live tree.
    assert "git worktree prune" not in err
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


def test_rollback_remove_rc1_but_unregistered_reports_original_error(
        fs, gf_inproc, mock_backend):
    """F6 arm — both removes report rc=1 but the FIRST one actually ran
    the unregister (a shim that runs the real remove then exits 1): the
    porcelain re-check shows `wt` is NOT registered, so the die is the
    ORIGINAL post-add error alone — no rollback clause, no orphan
    wording, no recovery remedy."""
    parent = _collision_parent(fs, mock_backend)
    wt = Path("/wt")
    backend = _RemoveLandsButRc1(mock_backend, wt)

    r = gf_inproc("-C", str(parent), "worktree", "add", str(wt),
                  backend=backend, check=False)

    assert r.returncode == 1
    err = r.stderr + r.stdout
    # The original post-add error is the whole report.
    assert "worktree add failed for git-folder 'zz' (vendor/zz)" in err
    assert f"child path {wt / 'vendor' / 'zz'} already exists" in err
    # Both rollback removes ran; both reported rc=1 ...
    assert backend.removes == [
        ("worktree", "remove", "--force", str(wt)),
        ("worktree", "remove", "--force", "--force", str(wt)),
    ]
    # ... yet the re-check proved the unregister landed, so none of the
    # rollback-failure wording or remedies may appear.
    assert "rollback of the new worktree also failed" not in err
    assert "orphan" not in err
    assert "remove --force" not in err
    assert "prune" not in err
    # Ground truth: the registration really is gone, and so is the tree.
    assert _registered(mock_backend, parent, wt) is None
    assert not wt.exists()


def test_rollback_failure_registered_but_tree_gone_names_prune(
        fs, gf_inproc, mock_backend):
    """F6 arm — both removes fail, the registration record survives but
    the tree is already gone: the die must name `git worktree prune`
    (which drops exactly that stale record) and NOT `remove --force`
    (which can only fail on the missing tree again)."""
    parent = _collision_parent(fs, mock_backend)
    wt = Path("/wt")
    backend = _FailRemoveDropTree(mock_backend, wt)

    r = gf_inproc("-C", str(parent), "worktree", "add", str(wt),
                  backend=backend, check=False)

    assert r.returncode == 1
    err = r.stderr + r.stdout
    assert "worktree add failed for git-folder 'zz' (vendor/zz)" in err
    assert "rollback of the new worktree also failed" in err
    assert "git worktree prune" in err
    assert "git worktree remove --force" not in err
    assert backend.removes == [
        ("worktree", "remove", "--force", str(wt)),
        ("worktree", "remove", "--force", "--force", str(wt)),
    ]
    # Ground truth for this arm: record live, tree gone.
    assert _registered(mock_backend, parent, wt) is not None
    assert not wt.exists()


def test_rollback_failure_list_error_names_both_remedies(
        fs, gf_inproc, mock_backend):
    """F6 arm — removes fail AND the `worktree list --porcelain`
    re-check itself fails: the registration state is unknowable, so the
    die names the orphan and BOTH remedies — `remove --force <path>` for
    a live tree, `git worktree prune` for a leftover record."""
    parent = _collision_parent(fs, mock_backend)
    wt = Path("/wt")
    backend = _FailWorktreeRemove(_FailWorktreeList(mock_backend), wt)

    r = gf_inproc("-C", str(parent), "worktree", "add", str(wt),
                  backend=backend, check=False)

    assert r.returncode == 1
    err = r.stderr + r.stdout
    assert "worktree add failed for git-folder 'zz' (vendor/zz)" in err
    assert "rollback of the new worktree also failed" in err
    assert f"git worktree remove --force {wt}" in err
    assert "git worktree prune" in err
    assert backend.removes == [
        ("worktree", "remove", "--force", str(wt)),
        ("worktree", "remove", "--force", "--force", str(wt)),
    ]
    # The registration was never dropped — still the mock's truth.
    assert _registered(mock_backend, parent, wt) is not None
