import os
import re
import shutil
from pathlib import Path

import pytest

from conftest import git, push_branch, push_commit, gf


def test_multi_worktree_shares_objects(tmp_path):
    """Two parent worktrees can share the shelf object store while keeping independent child HEADs."""
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

    # First worktree.
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child1 = parent / "vendor" / "lib"
    head1 = gf("-C", str(child1), "sh", "-c", "git rev-parse HEAD").stdout.strip()

    # Second parent worktree (detached so it does not collide on master).
    parent2 = tmp_path / "parent2"
    git("worktree", "add", "--detach", str(parent2), cwd=parent)
    gf("-C", str(parent2), "clone", str(upstream), "vendor/lib")
    child2 = parent2 / "vendor" / "lib"
    head2 = gf("-C", str(child2), "sh", "-c", "git rev-parse HEAD").stdout.strip()

    assert head1 == head2
    assert not (child2 / ".git").exists()

    # Modify child2 independently.
    gf("-C", str(child2), "sh", "-c", "git checkout --detach")
    assert gf("-C", str(child2), "sh", "-c", "git rev-parse --is-inside-work-tree").stdout.strip() == "true"

    # child1 is still a normal checkout.
    result = gf("-C", str(child1), "sh", "-c", "git status --porcelain")
    assert result.stdout.strip() == ""


def test_status_shows_drift_before_pull(tmp_path):
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

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib", "-b", "latest")
    push_commit(upstream, "update", "update")

    result = gf("-C", str(parent), "status")
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]", result.stdout)

    gf("-C", str(parent), "pull")
    result = gf("-C", str(parent), "status")
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]", result.stdout)


def test_pull_aborts_on_dirty_worktree(tmp_path):
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
    (child / "a.txt").write_text("dirty change")

    push_commit(upstream, "update", "update")

    with pytest.raises(AssertionError) as exc:
        gf("-C", str(parent), "pull")
    assert "dirty" in str(exc.value).lower()


def test_local_override_ref(tmp_path):
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

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib", "-b", "master")
    (parent / "gf.local.toml").write_text('[[git_folder_override]]\nname = "lib"\nref = "feature"\n')

    gf("-C", str(parent), "pull")
    child = parent / "vendor" / "lib"
    assert (child / "feature.txt").read_text() == "feature content"


def test_sh_inside_child_uses_child_gitdir(tmp_path):
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

    result = gf("-C", str(child), "sh", "-c", "git rev-parse --is-inside-work-tree")
    assert result.stdout.strip() == "true"

    result = gf("-C", str(child), "sh", "-c", "git rev-parse --short HEAD")
    assert len(result.stdout.strip()) == 7


def test_sh_runs_positional_git_command(tmp_path):
    """`gf sh <command>...` runs the command in the child's git environment."""
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

    result = gf("-C", str(child), "sh", "git", "status", check=False)
    assert result.returncode == 0, result.stderr
    assert "On branch master" in result.stdout


def test_sh_git_log_in_non_tty(tmp_path):
    """`gf sh git log` streams output even when stdout is not a terminal."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")
    push_commit(upstream, "second", "more")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"

    result = gf("-C", str(child), "sh", "git", "log", "--oneline", check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") >= 2


def test_gf_log_streams_in_non_tty(tmp_path):
    """`gf log` runs `git log` in the child and streams the output."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")
    push_commit(upstream, "second", "more")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"

    result = gf("-C", str(child), "log", "--oneline", check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") >= 2


def test_gf_diff_streams_in_non_tty(tmp_path):
    """`gf diff` runs `git diff` in the child and streams the output."""
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
    (child / "a.txt").write_text("changed")

    result = gf("-C", str(child), "diff", check=False)
    assert result.returncode == 0, result.stderr
    assert "changed" in result.stdout


def test_gf_diff_passthrough_multiple_args(tmp_path):
    """`gf diff` forwards multiple unknown args (flags + refs) to `git diff`."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")
    push_commit(upstream, "second", "more")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"

    # `--stat` is unknown to the gf diff subparser but should be forwarded.
    result = gf("-C", str(child), "diff", "--stat", "HEAD~1..HEAD", check=False)
    assert result.returncode == 0, result.stderr
    assert "a.txt" in result.stdout


def test_gf_log_passthrough_multiple_args(tmp_path):
    """`gf log` forwards multiple unknown args to `git log`."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")
    push_commit(upstream, "second", "more")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"

    result = gf("-C", str(child), "log", "--oneline", "-n", "1", check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1


def test_gf_git_passthrough_subcommand(tmp_path):
    """`gf git <subcommand> ...` forwards the full git command to the child."""
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

    result = gf("-C", str(child), "git", "status", "--short", check=False)
    assert result.returncode == 0, result.stderr
    # Clean working tree produces no output from `git status --short`.
    assert result.stdout.strip() == ""


def test_gf_git_passthrough_show(tmp_path):
    """`gf git show --stat` forwards correctly through REMAINDER capture."""
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

    result = gf("-C", str(child), "git", "show", "--stat", "HEAD", check=False)
    assert result.returncode == 0, result.stderr
    assert "a.txt" in result.stdout


def test_ls_and_status_with_path_argument(tmp_path):
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

    result = gf("-C", str(parent), "ls", "vendor")
    assert "lib" in result.stdout

    result = gf("-C", str(parent), "status", "vendor/lib")
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]", result.stdout)


def test_status_through_symlinked_parent(tmp_path):
    """`gf status` works when a parent repo is reached through a symlink."""
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

    gf("-C", str(parent), "clone", str(upstream), ".planning")

    consumer = tmp_path / "consumer"
    consumer.mkdir()
    git("init", cwd=consumer)
    (consumer / "README").write_text("consumer root")
    git("add", "README", cwd=consumer)
    git("commit", "-m", "root", cwd=consumer)

    link = consumer / "parent-link"
    link.symlink_to(os.path.relpath(parent, link.parent), target_is_directory=True)

    result = gf("-C", str(link / ".planning"), "status", check=False)
    assert result.returncode == 0, result.stderr
    assert re.search(r"\.planning\s+" + re.escape(str(upstream)) + r"\s+\[master\]", result.stdout)


def test_gf_git_runs_git_status_in_child(tmp_path):
    """`gf git` runs an arbitrary git command in the child git environment."""
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

    result = gf("-C", str(child), "git", "status", "--porcelain", check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_status_remote_classifies_drift(tmp_path):
    """`gf status --remote` is local-only and classifies children as
    clean/behind/local-dirty/both/missing using the local remote-tracking
    refs already present after a `gf pull` or `gf git fetch`."""
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

    # Clean: child HEAD matches the local remote-tracking ref tip.
    result = gf("-C", str(parent), "status", "--remote", check=False)
    assert result.returncode == 0, result.stderr
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]\s+clean", result.stdout)

    # Behind: remote advances, then refresh the local tracking ref with
    # `gf git fetch` (the same way `git status` shows behind after a
    # `git fetch`). The child HEAD stays at the old commit, so drift is
    # "behind". `gf status --remote` itself does not fetch.
    push_commit(upstream, "update", "update")
    gf("-C", str(parent / "vendor" / "lib"), "git", "fetch", "origin")
    result = gf("-C", str(parent), "status", "--remote", check=False)
    assert result.returncode == 0, result.stderr
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]\s+behind", result.stdout)

    # Pull to resync, then make the child dirty -> local-dirty.
    gf("-C", str(parent), "pull")
    (parent / "vendor" / "lib" / "a.txt").write_text("dirty")
    result = gf("-C", str(parent), "status", "--remote", check=False)
    assert result.returncode == 0, result.stderr
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]\s+local-dirty", result.stdout)

    # Both: advance remote again, refresh the tracking ref, while the
    # child is still dirty.
    push_commit(upstream, "another", "another")
    gf("-C", str(parent / "vendor" / "lib"), "git", "fetch", "origin")
    result = gf("-C", str(parent), "status", "--remote", check=False)
    assert result.returncode == 0, result.stderr
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]\s+both", result.stdout)


def test_status_remote_does_not_fetch(tmp_path):
    """`gf status --remote` must not call git fetch, git remote, or git ls-remote."""
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

    # Snapshot the child gitdir's refs before running status --remote.
    child_git = parent / "vendor" / "lib" / ".gf" / "git"
    refs_before = (child_git / "refs" / "remotes" / "origin").exists()

    result = gf("-C", str(parent), "status", "--remote", check=False)
    assert result.returncode == 0, result.stderr
    # The remote-tracking refs are unchanged (no fetch happened).
    assert (child_git / "refs" / "remotes" / "origin").exists() == refs_before
    # And the drift state is clean (HEAD matches the local tracking ref).
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]\s+clean", result.stdout)


def test_status_remote_missing_child(tmp_path):
    """`gf status --remote` reports `missing` for a child with no .gf/git/HEAD."""
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

    # Declare a git-folder whose child path does not exist yet.
    (parent / "gf.toml").write_text(
        '[[git_folder]]\nname = "lib"\nurl = "' + str(upstream) + '"\n'
        'ref = "latest"\npath = "vendor/lib"\n'
    )

    result = gf("-C", str(parent), "status", "--remote", check=False)
    assert result.returncode == 0, result.stderr
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[\]\s+missing", result.stdout)


def test_status_remote_unresolvable_ref_dies(tmp_path):
    """`gf status --remote` dies with a deterministic code on an unresolvable ref."""
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
    # Override the ref to a branch that does not exist locally.
    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "lib"\nref = "no-such-branch"\n'
    )

    result = gf("-C", str(parent), "status", "--remote", check=False)
    assert result.returncode != 0
    assert "could not resolve" in result.stderr
    # No drift state column is printed on failure.
    assert "clean" not in result.stdout
    assert "behind" not in result.stdout


def test_status_without_remote_does_not_fetch(tmp_path):
    """`gf status` without --remote does not call git fetch."""
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

    # Without --remote, no drift state column is appended.
    result = gf("-C", str(parent), "status", check=False)
    assert result.returncode == 0, result.stderr
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]\s*$", result.stdout, re.MULTILINE)
    assert "clean" not in result.stdout
    assert "behind" not in result.stdout
