import re
import tomllib
from pathlib import Path

import pytest

from conftest import git, push_commit, gf


def _load_state(child: Path) -> dict:
    path = child / ".gf" / "state"
    with open(path, "rb") as f:
        return tomllib.load(f)


def test_state_recorded_on_clone(tmp_path):
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
    st = _load_state(child)

    assert st["ref"] == "latest"
    assert st["url"] == str(upstream)
    assert st["override"] is False
    assert len(st["resolved"]) == 40

    head = gf("-C", str(child), "sh", "-c", "git rev-parse HEAD").stdout.strip()
    assert st["resolved"] == head


def test_state_updated_after_pull(tmp_path):
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
    first = _load_state(child)["resolved"]

    push_commit(upstream, "update", "update")
    gf("-C", str(parent), "pull")

    second = _load_state(child)["resolved"]
    assert second != first
    head = gf("-C", str(child), "sh", "-c", "git rev-parse HEAD").stdout.strip()
    assert second == head


def test_state_records_override(tmp_path):
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
    (parent / "gf.local.toml").write_text('[[git_folder_override]]\nname = "lib"\nref = "master"\n')

    gf("-C", str(parent), "pull")
    child = parent / "vendor" / "lib"
    st = _load_state(child)
    assert st["override"] is True
    assert st["ref"] == "master"


def test_pull_dirty_exit_code(tmp_path):
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
    (child / "a.txt").write_text("dirty")

    push_commit(upstream, "update", "update")
    result = gf("-C", str(parent), "pull", check=False)
    assert result.returncode == 3
    assert "dirty" in (result.stdout + result.stderr).lower()


def test_url_override_fetches_new_shelf(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    fork = tmp_path / "fork"
    fork.mkdir()
    git("clone", "--bare", str(upstream), str(fork), cwd=tmp_path)
    fork_work = tmp_path / "_work_fork"
    git("clone", str(fork), str(fork_work), cwd=tmp_path)
    (fork_work / "fork.txt").write_text("fork content")
    git("add", "fork.txt", cwd=fork_work)
    git("commit", "-m", "fork content", cwd=fork_work)
    git("push", "origin", "master", cwd=fork_work)

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"
    assert not (child / "fork.txt").exists()

    (parent / "gf.local.toml").write_text(f'[[git_folder_override]]\nname = "lib"\nurl = "{fork}"\n')
    gf("-C", str(parent), "pull")
    assert (child / "fork.txt").read_text() == "fork content"

    st = _load_state(child)
    assert st["url"] == str(fork)
    assert st["override"] is True


def test_clone_recommends_parent_gitignore(tmp_path):
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

    result = gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    assert not (parent / ".gitignore").exists()
    assert 'add "vendor/lib/" to .gitignore' in result.stdout


def test_status_shows_porcelain(tmp_path):
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
    (child / "a.txt").write_text("dirty")

    result = gf("-C", str(parent), "status")
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]", result.stdout)
    assert " M a.txt" in result.stdout


def test_pull_initializes_missing_child(tmp_path):
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

    (parent / "gf.toml").write_text(
        f'[[git_folder]]\nname = "lib"\nurl = "{upstream}"\nref = "latest"\npath = "vendor/lib"\n'
    )

    child = parent / "vendor" / "lib"
    assert not child.exists()
    result = gf("-C", str(parent), "pull")
    assert child.is_dir()
    assert (child / "a.txt").read_text() == "hello"
    assert not (parent / ".gitignore").exists()
    assert 'add "vendor/lib/" to .gitignore' in result.stdout


def test_pull_fails_on_non_git_folder_path(tmp_path):
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

    child = parent / "vendor" / "lib"
    child.mkdir(parents=True)
    (child / "existing.txt").write_text("I was here first")

    (parent / "gf.toml").write_text(
        f'[[git_folder]]\nname = "lib"\nurl = "{upstream}"\nref = "latest"\npath = "vendor/lib"\n'
    )

    result = gf("-C", str(parent), "pull", check=False)
    assert result.returncode == 1
    assert "not a git-folder" in (result.stdout + result.stderr).lower()


def test_pull_invalid_ref_exit_code(tmp_path):
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
    (parent / "gf.local.toml").write_text('[[git_folder_override]]\nname = "lib"\nref = "no-such-ref"\n')

    result = gf("-C", str(parent), "pull", check=False)
    assert result.returncode == 1
    assert "ref" in (result.stdout + result.stderr).lower()
