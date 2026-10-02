"""Integration tests for `gf worktree add`."""
# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import os
import re
from pathlib import Path

import pytest

from conftest import git, push_commit, gf


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
