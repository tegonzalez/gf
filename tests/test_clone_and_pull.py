# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import re
from pathlib import Path

import pytest

from conftest import git, push_branch, push_commit, gf


def test_clone_and_pull(tmp_path):
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
    child = parent / "vendor" / "lib"
    assert child.is_dir()
    assert (child / "a.txt").read_text() == "hello"
    assert (child / ".gf" / "git" / "HEAD").is_file()
    assert not (child / ".git").exists()

    push_commit(upstream, "update", "update")
    gf("-C", str(parent), "pull")
    assert (child / "a.txt").read_text() == "update"

    result = gf("-C", str(parent), "status")
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]", result.stdout)
    result = gf("-C", str(parent), "ls")
    assert "lib" in result.stdout

    result = gf("-C", str(child), "sh", "-c", "git rev-parse --short HEAD")
    assert result.returncode == 0
    assert len(result.stdout.strip()) == 7

    gf("-C", str(parent), "rm", "vendor/lib")
    assert child.is_dir()
    assert (child / ".git" / "HEAD").is_file()
    assert not (child / ".gf").exists()
    assert (parent / "gf.toml").read_text() == "git_folder = []\n"


def test_pull_rebase_keeps_local_commits(tmp_path):
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
    child = parent / "vendor" / "lib"

    # Make a local commit in the child using `gf git` so GIT_DIR is set.
    (child / "b.txt").write_text("local")
    gf("-C", str(child), "git", "add", "b.txt")
    gf("-C", str(child), "git", "commit", "-m", "local")

    # Upstream advances.
    push_commit(upstream, "update", "update")

    # Pull with --rebase should replay the local commit on top of the new upstream.
    gf("-C", str(parent), "pull", "--rebase")
    assert (child / "a.txt").read_text() == "update"
    assert (child / "b.txt").read_text() == "local"


def test_pull_force_updates_dirty_child(tmp_path):
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
    child = parent / "vendor" / "lib"

    # Dirty the child and advance the remote.
    (child / "a.txt").write_text("dirty change")
    push_commit(upstream, "update", "update")

    # Plain pull aborts because the child is dirty.
    with pytest.raises(AssertionError):
        gf("-C", str(parent), "pull")

    # --force overwrites the dirty file with the remote content.
    gf("-C", str(parent), "pull", "--force")
    assert (child / "a.txt").read_text() == "update"


def test_pull_autostash_restores_local_changes(tmp_path):
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
    child = parent / "vendor" / "lib"

    # Add a local untracked file and advance the remote.
    (child / "local.txt").write_text("local work")
    push_commit(upstream, "update", "update")

    gf("-C", str(parent), "pull", "--autostash")
    # The remote update applied.
    assert (child / "a.txt").read_text() == "update"
    # The stashed local work was restored.
    assert (child / "local.txt").read_text() == "local work"


def test_rm_all_unregisters_every_git_folder(tmp_path):
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
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib2")

    gf("-C", str(parent), "rm", "--all")

    for child in (parent / "vendor" / "lib", parent / "vendor" / "lib2"):
        assert (child / ".git" / "HEAD").is_file()
        assert not (child / ".gf").exists()
        assert (child / "a.txt").is_file()
    assert (parent / "gf.toml").read_text() == "git_folder = []\n"


def test_rm_all_requires_parent_root(tmp_path):
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
    child = parent / "vendor" / "lib"

    result = gf("-C", str(child), "rm", "--all", check=False)
    assert result.returncode != 0
    assert "parent repo root" in result.stderr
    # Nothing was removed.
    assert (child / ".gf" / "git" / "HEAD").is_file()


def test_clone_depth_creates_shallow_clone(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")
    push_commit(upstream, "second", "more")
    push_commit(upstream, "third", "even more")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib", "--depth", "1")
    child = parent / "vendor" / "lib"

    # A depth-1 clone has a shallow boundary; the file content is present.
    assert (child / "a.txt").read_text() == "even more"
    result = gf("-C", str(child), "git", "rev-parse", "--short", "HEAD")
    # The shallow clone's log only has one commit reachable from HEAD.
    log = gf("-C", str(child), "git", "log", "--oneline")
    assert log.stdout.count("\n") == 1


def test_clone_single_branch_narrows_fetch(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")
    push_branch(upstream, "feature", "feature content")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf(
        "-C", str(parent), "clone", str(upstream), "vendor/lib",
        "-b", "feature", "--single-branch",
    )
    child = parent / "vendor" / "lib"

    # The feature branch was checked out.
    assert (child / "feature.txt").read_text() == "feature content"
    # remote.origin.fetch is narrowed to the feature branch.
    fetch_refspec = gf(
        "-C", str(child), "git", "config", "remote.origin.fetch",
    ).stdout.strip()
    assert "feature" in fetch_refspec
    assert fetch_refspec == "+refs/heads/feature:refs/remotes/origin/feature"


def test_init_with_name_override(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "init", "vendor/lib", "-n", "custom-name")
    manifest_text = (parent / "gf.toml").read_text()
    assert 'name = "custom-name"' in manifest_text
    assert 'path = "vendor/lib"' in manifest_text


def test_init_with_url_override(tmp_path):
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

    gf("-C", str(parent), "init", "vendor/lib", "--url", str(upstream))
    manifest_text = (parent / "gf.toml").read_text()
    assert f'url = "{upstream}"' in manifest_text

    # A subsequent pull fetches from the configured URL.
    gf("-C", str(parent), "pull")
    child = parent / "vendor" / "lib"
    assert (child / "a.txt").read_text() == "hello"
