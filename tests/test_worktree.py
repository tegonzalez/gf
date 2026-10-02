"""Integration tests for `gf worktree add`."""
# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import os
import re
import subprocess
from pathlib import Path

import pytest

from conftest import git, push_commit, gf


def _git_out(*args, cwd: Path) -> str:
    """Real git with captured stdout (fixture helper)."""
    r = subprocess.run(
        ["git", *map(str, args)], cwd=str(cwd),
        capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _setup_parent_with_git_folder(tmp_path):
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
    git("add", "gf.toml", cwd=parent)
    git("commit", "-m", "add git-folder", cwd=parent)

    return parent, upstream


def test_worktree_add_creates_relative_symlinks(tmp_path):
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "feature"

    result = gf(
        "-C", str(parent), "worktree", "add", str(new_parent), "-b", "feature",
        check=False,
    )
    assert result.returncode == 0, result.stderr

    assert (new_parent / ".git").exists()
    new_child = new_parent / "vendor" / "lib"
    assert new_child.is_symlink()
    source_child = parent / "vendor" / "lib"
    assert new_child.resolve() == source_child.resolve()

    link_target = os.readlink(new_child)
    assert not os.path.isabs(link_target)


def test_worktree_add_status_and_log_in_linked_worktree(tmp_path):
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(new_parent), "-b", "feature")

    result = gf("-C", str(new_parent), "status", check=False)
    assert result.returncode == 0, result.stderr
    assert "lib" in result.stdout

    result = gf("-C", str(new_parent / "vendor" / "lib"), "log", "--oneline", check=False)
    assert result.returncode == 0, result.stderr
    assert "init" in result.stdout


def test_worktree_add_diff_follows_link_to_source_child(tmp_path):
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(new_parent), "-b", "feature")

    source_child = parent / "vendor" / "lib"
    (source_child / "a.txt").write_text("changed")

    result = gf("-C", str(new_parent / "vendor" / "lib"), "diff", check=False)
    assert result.returncode == 0, result.stderr
    assert "changed" in result.stdout


def test_worktree_add_rm_guarded_for_linked_child(tmp_path):
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(new_parent), "-b", "feature")

    result = gf("-C", str(new_parent), "rm", "vendor/lib", check=False)
    assert result.returncode != 0
    assert "symlinked child" in result.stderr


def test_worktree_list_lists_worktrees_and_links(tmp_path):
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(new_parent), "-b", "feature")

    result = gf("-C", str(parent), "worktree", "list", check=False)
    assert result.returncode == 0, result.stderr
    assert str(parent) in result.stdout
    assert str(new_parent) in result.stdout
    # The feature worktree has the git-folder linked via a relative symlink.
    assert "lib -> " in result.stdout


def test_worktree_list_porcelain(tmp_path):
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(new_parent), "-b", "feature")

    result = gf("-C", str(parent), "worktree", "list", "--porcelain", check=False)
    assert result.returncode == 0, result.stderr
    assert f"worktree {parent}" in result.stdout
    assert f"worktree {new_parent}" in result.stdout
    assert result.stdout.count("HEAD ") >= 2
    assert "branch refs/heads/feature" in result.stdout
    assert "git-folder lib " in result.stdout


def test_worktree_list_verbose_includes_head(tmp_path):
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(new_parent), "-b", "feature")

    result = gf("-C", str(parent), "worktree", "list", "--verbose", check=False)
    assert result.returncode == 0, result.stderr
    # Verbose output includes the HEAD sha in parentheses.
    assert re.search(r"\([0-9a-f]{7,}\)", result.stdout)


def test_worktree_remove_preserves_source_child(tmp_path):
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(new_parent), "-b", "feature")

    source_child = parent / "vendor" / "lib"
    assert (source_child / ".gf" / "git" / "HEAD").is_file()

    result = gf("-C", str(parent), "worktree", "remove", str(new_parent), check=False)
    assert result.returncode == 0, result.stderr

    # The source git-folder child is untouched.
    assert (source_child / ".gf" / "git" / "HEAD").is_file()
    assert (source_child / "a.txt").is_file()
    # The worktree directory is gone.
    assert not new_parent.exists()


def test_worktree_remove_force(tmp_path):
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(new_parent), "-b", "feature")

    # Dirty the worktree so a plain remove would fail; --force should still work.
    (new_parent / "README").write_text("dirty")

    result = gf(
        "-C", str(parent), "worktree", "remove", str(new_parent), "--force",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not new_parent.exists()
    # Source child preserved.
    assert (parent / "vendor" / "lib" / ".gf" / "git" / "HEAD").is_file()


def test_worktree_remove_refuses_main_worktree(tmp_path):
    """`gf worktree remove` refuses to remove the main worktree."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)

    result = gf(
        "-C", str(parent), "worktree", "remove", str(parent),
        check=False,
    )
    assert result.returncode != 0
    assert "main worktree" in result.stderr
    # The main worktree is untouched.
    assert parent.exists()
    assert (parent / "vendor" / "lib" / ".gf" / "git" / "HEAD").is_file()


def test_worktree_remove_refuses_current_worktree(tmp_path):
    """`gf worktree remove` refuses to remove the worktree it is run from."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(new_parent), "-b", "feature")

    # Run the remove from inside the feature worktree itself.
    result = gf(
        "-C", str(new_parent), "worktree", "remove", str(new_parent),
        check=False,
    )
    assert result.returncode != 0
    assert "current worktree" in result.stderr
    # The worktree is still there.
    assert new_parent.exists()
    assert (parent / "vendor" / "lib" / ".gf" / "git" / "HEAD").is_file()


def test_worktree_remove_refuses_unlisted_path(tmp_path):
    """`gf worktree remove` refuses a path that is not a listed worktree."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)

    result = gf(
        "-C", str(parent), "worktree", "remove", str(tmp_path / "nope"),
        check=False,
    )
    assert result.returncode != 0
    assert "not a listed worktree" in result.stderr


def test_worktree_remove_restores_symlinks_on_failure(tmp_path):
    """If `git worktree remove` fails, unlinked git-folder symlinks are restored."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(new_parent), "-b", "feature")

    new_child = new_parent / "vendor" / "lib"
    assert new_child.is_symlink()
    link_target = os.readlink(new_child)

    # Dirty the worktree and try a plain (no --force) remove; git refuses.
    (new_parent / "README").write_text("dirty")
    result = gf(
        "-C", str(parent), "worktree", "remove", str(new_parent),
        check=False,
    )
    assert result.returncode != 0
    # The symlink was restored after the failed remove.
    assert new_child.is_symlink()
    assert os.readlink(new_child) == link_target
    # The source git-folder child is untouched.
    assert (parent / "vendor" / "lib" / ".gf" / "git" / "HEAD").is_file()
    # The worktree was not removed.
    assert new_parent.exists()


# ---------------------------------------------------------------------------
# R9-2 — the optional commitish must reach git AFTER the worktree path
#
# `cmd_worktree_add` previously spelled `git worktree add [opts]
# <commitish> <path>`: git took the commitish as the path and the real
# path as the commit-ish, dying `fatal: invalid reference: <path>`.
# These pins exercise the fixed spelling `[opts] <path> [<commit-ish>]`
# against real git.


def test_worktree_add_tag_commitish_checks_out_tagged_commit(tmp_path):
    """`gf worktree add <path> <tag>` checks out the tag's commit on a
    detached HEAD — and the usual link/manifest propagation is intact."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    # Tag an older commit so the worktree's HEAD provably differs from
    # the parent master HEAD.
    tagged = _git_out("rev-parse", "HEAD~1", cwd=parent)
    git("tag", "v1", tagged, cwd=parent)
    new_parent = tmp_path / "wt_tag"

    result = gf(
        "-C", str(parent), "worktree", "add", str(new_parent), "v1",
        check=False,
    )
    assert result.returncode == 0, result.stderr

    # HEAD is the tagged commit (detached), not master.
    assert _git_out("-C", new_parent, "rev-parse", "HEAD", cwd=parent) \
        == tagged
    assert _git_out(
        "-C", new_parent, "rev-parse", "--abbrev-ref", "HEAD",
        cwd=parent) == "HEAD"
    # gf propagation intact: manifest copied and the git-folder linked.
    assert (new_parent / "gf.toml").is_file()
    link = new_parent / "vendor" / "lib"
    assert link.is_symlink()
    assert link.resolve() == (parent / "vendor" / "lib").resolve()


def test_worktree_add_new_branch_at_commitish(tmp_path):
    """`gf worktree add <path> -b <name> <commit>` creates branch <name>
    AT <commit> in the new worktree."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    base = _git_out("rev-parse", "HEAD~1", cwd=parent)
    new_parent = tmp_path / "wt_branch"

    result = gf(
        "-C", str(parent), "worktree", "add", str(new_parent),
        "-b", "nb", base,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    assert _git_out("-C", new_parent, "rev-parse", "HEAD", cwd=parent) \
        == base
    assert _git_out(
        "-C", new_parent, "rev-parse", "--abbrev-ref", "HEAD",
        cwd=parent) == "nb"
    # The new branch ref in the shared repo points at the commitish.
    assert _git_out("-C", parent, "rev-parse", "nb", cwd=parent) == base
    assert (new_parent / "vendor" / "lib").is_symlink()


def test_worktree_add_bare_path_still_works(tmp_path):
    """Control: `gf worktree add <path>` with no commitish was never
    broken — it stays green and lands the worktree at HEAD."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    new_parent = tmp_path / "wt3"

    result = gf(
        "-C", str(parent), "worktree", "add", str(new_parent),
        check=False,
    )
    assert result.returncode == 0, result.stderr

    assert _git_out("-C", new_parent, "rev-parse", "HEAD", cwd=parent) \
        == _git_out("-C", parent, "rev-parse", "HEAD", cwd=parent)
    assert (new_parent / "gf.toml").is_file()
    link = new_parent / "vendor" / "lib"
    assert link.is_symlink()
    assert link.resolve() == (parent / "vendor" / "lib").resolve()


# ---------------------------------------------------------------------------
# Child-path collision remediation (R9-3)
#
# A child path that already exists at the destination — materialized by the
# checkout (a tracked directory) or created by the user beforehand — must fail
# WITHOUT orphaning the worktree: a pre-existing path is refused before
# `git worktree add` runs (pre-flight), and any post-add failure rolls the
# registered worktree back via `git worktree remove --force`. A real
# directory is never taken over, not even under `-f`.


def _registered_worktrees(parent: Path) -> list[str]:
    """Worktree paths registered for `parent` per `git worktree list`."""
    r = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(parent), capture_output=True, text=True, check=True)
    return [line.split(" ", 1)[1] for line in r.stdout.splitlines()
            if line.startswith("worktree ")]


def _setup_parent_with_tracked_folder_path(tmp_path):
    """Parent whose HEAD tracks `vendor/zz/t.txt` while `vendor/zz` is also
    a git-folder (`gf init`): `git worktree add` materializes a REAL
    directory at the exact path the link step must take over."""
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    child = parent / "vendor" / "zz"
    child.mkdir(parents=True)
    (child / "t.txt").write_text("tracked")
    git("add", "README", "vendor/zz/t.txt", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    gf("-C", str(parent), "init", "vendor/zz")
    return parent


def test_add_tracked_dir_collision_rolls_back(tmp_path):
    """The checkout materializes a real directory at the git-folder path:
    the add fails with the folder_error envelope AND the registered
    worktree is rolled back — no orphan, no leftover tree."""
    parent = _setup_parent_with_tracked_folder_path(tmp_path)
    wt = tmp_path / "wt"

    result = gf("-C", str(parent), "worktree", "add", str(wt), check=False)
    assert result.returncode == 1
    err = result.stderr + result.stdout
    # folder_error envelope: operation + git-folder name + manifest path.
    assert "worktree add failed for git-folder 'zz' (vendor/zz)" in err
    assert f"child path {wt / 'vendor' / 'zz'} already exists" in err
    # The failure happened after `git worktree add` ran — so the rollback
    # is what these assertions pin.
    assert "Preparing worktree" in err
    assert "Traceback" not in err
    # No orphaned worktree: the registration is dropped and the tree gone.
    assert _registered_worktrees(parent) == [str(parent.resolve())]
    assert not wt.exists()


def test_add_tracked_dir_collision_refused_under_force(tmp_path):
    """Under `-f` a real directory at the child path is still refused —
    unlink() cannot remove it — with a clean message, no traceback, and
    no orphaned worktree."""
    parent = _setup_parent_with_tracked_folder_path(tmp_path)
    wt = tmp_path / "wtf"

    result = gf(
        "-C", str(parent), "worktree", "add", str(wt), "-f", check=False)
    assert result.returncode == 1
    err = result.stderr + result.stdout
    assert "worktree add failed for git-folder 'zz' (vendor/zz)" in err
    assert "is a real directory" in err
    assert "Traceback" not in err
    assert "IsADirectoryError" not in err
    assert _registered_worktrees(parent) == [str(parent.resolve())]
    assert not wt.exists()


def test_add_preflight_refuses_preexisting_child_path(tmp_path):
    """A child path that exists BEFORE the add dies in pre-flight —
    `git worktree add` never runs ('Preparing worktree' absent), the
    pre-existing tree is untouched, and nothing is registered."""
    parent = _setup_parent_with_tracked_folder_path(tmp_path)
    wt3 = tmp_path / "wt3"
    (wt3 / "vendor" / "zz").mkdir(parents=True)
    (wt3 / "vendor" / "zz" / "mine.txt").write_text("mine")

    result = gf("-C", str(parent), "worktree", "add", str(wt3), check=False)
    assert result.returncode == 1
    err = result.stderr + result.stdout
    assert "worktree add failed for git-folder 'zz' (vendor/zz)" in err
    assert "already exists" in err
    # git never ran — no streamed add output, no git-level fatal.
    assert "Preparing worktree" not in err
    assert "fatal" not in err
    # The pre-existing directory and its content are untouched, and no
    # worktree was registered or materialized inside it.
    assert (wt3 / "vendor" / "zz" / "mine.txt").read_text() == "mine"
    assert not (wt3 / ".git").exists()
    assert _registered_worktrees(parent) == [str(parent.resolve())]


def test_add_clean_parent_places_links(tmp_path):
    """Control: with no collision, `gf worktree add` succeeds and the
    git-folder link is placed in the new worktree."""
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    gf("-C", str(parent), "init", "vendor/zz")

    ok = tmp_path / "ok"
    result = gf("-C", str(parent), "worktree", "add", str(ok), check=False)
    assert result.returncode == 0, result.stderr

    link = ok / "vendor" / "zz"
    assert link.is_symlink()
    assert link.resolve() == (parent / "vendor" / "zz").resolve()
    assert (ok / "gf.toml").is_file()


# ---------------------------------------------------------------------------
# Manifest-copy write-through remediation (R10-1)
#
# The post-add manifest copy used to `dst.write_text(...)` unconditionally.
# When the added branch's tree carries `gf.toml` (or `gf.local.toml`) as a
# SYMLINK, `git worktree add` materializes that link inside the new worktree
# and write_text would write THROUGH it — overwriting whatever file the link
# points at, which can live entirely outside the worktree. The copy now
# unlinks a symlink dst before writing, so the manifest always lands as a
# regular file, and refuses any other non-regular dst — e.g. a committed
# `gf.toml/` directory — via ValidationError so the add rolls the
# registered worktree back.


def _commit_on_evil(parent: Path, names: list[str], mutate) -> None:
    """Commit `mutate()`'s tree changes on a new `evil` branch, then
    return the parent's checkout to master.

    `names` scopes `git add -A --` so the add stages only the manifest
    paths being mutated — never the untracked `vendor/` git-folder tree.
    """
    git("checkout", "-b", "evil", cwd=parent)
    mutate()
    git("add", "-A", "--", *names, cwd=parent)
    git("commit", "-m", "evil tree", cwd=parent)
    git("checkout", "master", cwd=parent)


def test_add_symlinked_manifest_never_writes_through(tmp_path):
    """A committed `gf.toml -> victim` symlink must not let the manifest
    copy write through the link: the copy replaces it with a regular
    file and the outside victim stays byte-identical."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    victim = tmp_path / "victim"
    sentinel = b"VICTIM-SENTINEL: the manifest copy must never land here\n"
    victim.write_bytes(sentinel)

    def _link() -> None:
        (parent / "gf.toml").unlink()
        os.symlink(str(victim), parent / "gf.toml")

    _commit_on_evil(parent, ["gf.toml"], _link)
    parent_manifest = (parent / "gf.toml").read_bytes()

    wt = tmp_path / "wt"
    result = gf(
        "-C", str(parent), "worktree", "add", str(wt), "evil",
        check=False,
    )
    assert result.returncode == 0, result.stderr

    # The witness: the write-through target is byte-identical.
    assert victim.read_bytes() == sentinel
    dst = wt / "gf.toml"
    assert dst.is_file() and not dst.is_symlink()
    assert dst.read_bytes() == parent_manifest


def test_add_symlinked_local_manifest_never_writes_through(tmp_path):
    """Same refusal for `gf.local.toml`: a committed symlink at the local
    manifest path is replaced by a regular file, never written through."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    (parent / "gf.local.toml").write_text("# local manifest\n")
    git("add", "gf.local.toml", cwd=parent)
    git("commit", "-m", "local manifest", cwd=parent)
    victim = tmp_path / "victim-local"
    sentinel = b"VICTIM-LOCAL-SENTINEL: never write through\n"
    victim.write_bytes(sentinel)

    def _link() -> None:
        (parent / "gf.local.toml").unlink()
        os.symlink(str(victim), parent / "gf.local.toml")

    _commit_on_evil(parent, ["gf.local.toml"], _link)
    parent_local = (parent / "gf.local.toml").read_bytes()

    wt = tmp_path / "wt"
    result = gf(
        "-C", str(parent), "worktree", "add", str(wt), "evil",
        check=False,
    )
    assert result.returncode == 0, result.stderr

    assert victim.read_bytes() == sentinel
    dst = wt / "gf.local.toml"
    assert dst.is_file() and not dst.is_symlink()
    assert dst.read_bytes() == parent_local


def test_add_manifest_committed_directory_rolls_back(tmp_path):
    """A committed `gf.toml/` directory materializes a real directory at
    the manifest path: the copy refuses it (not regular, not a symlink)
    and the registered worktree is rolled back — no orphan, no tree."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)

    def _mkdir() -> None:
        (parent / "gf.toml").unlink()
        (parent / "gf.toml").mkdir()
        (parent / "gf.toml" / "nested.txt").write_text("dir")

    _commit_on_evil(parent, ["gf.toml"], _mkdir)
    assert (parent / "gf.toml").is_file()  # master restored the file

    wt = tmp_path / "wt"
    result = gf(
        "-C", str(parent), "worktree", "add", str(wt), "evil",
        check=False,
    )
    assert result.returncode == 1
    err = result.stderr + result.stdout
    # Clean refusal, not a raw OSError traceback.
    assert "gf.toml" in err
    assert "not a regular file" in err
    assert "Traceback" not in err
    # The failure happened after `git worktree add` registered — rollback
    # is what these assertions pin.
    assert "Preparing worktree" in err
    assert _registered_worktrees(parent) == [str(parent.resolve())]
    assert not wt.exists()


def test_add_local_manifest_committed_directory_rolls_back(tmp_path):
    """Same refusal for `gf.local.toml`: a committed directory at the
    local manifest path fails the add and the worktree rolls back."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    (parent / "gf.local.toml").write_text("# local manifest\n")
    git("add", "gf.local.toml", cwd=parent)
    git("commit", "-m", "local manifest", cwd=parent)

    def _mkdir() -> None:
        (parent / "gf.local.toml").unlink()
        (parent / "gf.local.toml").mkdir()
        (parent / "gf.local.toml" / "nested.txt").write_text("dir")

    _commit_on_evil(parent, ["gf.local.toml"], _mkdir)
    assert (parent / "gf.local.toml").is_file()

    wt = tmp_path / "wt"
    result = gf(
        "-C", str(parent), "worktree", "add", str(wt), "evil",
        check=False,
    )
    assert result.returncode == 1
    err = result.stderr + result.stdout
    assert "gf.local.toml" in err
    assert "not a regular file" in err
    assert "Traceback" not in err
    assert "Preparing worktree" in err
    assert _registered_worktrees(parent) == [str(parent.resolve())]
    assert not wt.exists()


def test_add_manifest_copy_over_checked_out_file(tmp_path):
    """Control: an ordinary add whose branch tracks a regular `gf.toml`
    still lands the parent's manifest as a regular file."""
    parent, _ = _setup_parent_with_git_folder(tmp_path)
    parent_manifest = (parent / "gf.toml").read_bytes()

    wt = tmp_path / "wt"
    result = gf(
        "-C", str(parent), "worktree", "add", str(wt), "-b", "control",
        check=False,
    )
    assert result.returncode == 0, result.stderr

    dst = wt / "gf.toml"
    assert dst.is_file() and not dst.is_symlink()
    assert dst.read_bytes() == parent_manifest
    # The git-folder link lands as usual.
    link = wt / "vendor" / "lib"
    assert link.is_symlink()
    assert link.resolve() == (parent / "vendor" / "lib").resolve()
