# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Bounded acceptance witnesses for the final-review preservation defects.

Oracle: GF-G10 and gf-constraints.md "Do not implicitly discard or
overwrite user work", refined by the Change 007 correction clauses.
The real CLI and fixture-owned local Git repositories are the observation
boundary. Fixture bytes and commits supply expectations; implementation
output supplies neither an oracle nor a golden. These cases establish
Linux preservation behavior, not macOS evidence or receiving acceptance.
"""

import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from conftest import checkout_snapshot, gf, git_probe_env
from gf import layout


def _git(root, *args, input=None):
    result = subprocess.run(
        ["git", "-C", str(root), *map(str, args)], input=input,
        capture_output=True, text=True, env=git_probe_env(), check=True)
    return result.stdout.strip()


def _parent(tmp_path, name="parent"):
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    (root / "gf.toml").write_text("git_folder = []\n")
    (root / ".gitignore").write_text(".gf/\nchild\nignored.txt\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "parent seed")
    return root


def _upstream(tmp_path):
    root = tmp_path / "upstream"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "docs").mkdir()
    (root / "docs" / "file.txt").write_text("upstream base\n")
    (root / "docs" / "cache").mkdir()
    (root / "docs" / "cache" / "base.txt").write_text("tracked base\n")
    (root / ".gitignore").write_text(
        "docs/cache/private.txt\ndocs/ignored.dat\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "upstream seed")
    _git(root, "tag", "v1")
    return root


def _clone(tmp_path, form="whole", ref=None):
    upstream = _upstream(tmp_path)
    parent = _parent(tmp_path)
    url = upstream if form == "whole" else upstream / "docs"
    gf("-C", str(parent), "clone", str(url), "child",
       *(("-b", ref) if ref else ()))
    child = parent / "child"
    co = (layout.whole_repo_checkout(child) if form == "whole" else
          layout.subfolder_checkout(parent, str(upstream),
                                    "ref=" + quote(ref, safe="") if ref else "master", "docs"))
    return parent, upstream, child, co


def _co_git(co, *args, input=None):
    result = subprocess.run(
        ["git", "--git-dir", str(co.gitdir), "--work-tree", str(co.work_tree),
         *map(str, args)], input=input, capture_output=True, text=True,
        env=git_probe_env(), check=True)
    return result.stdout.strip()


def _snapshot(co):
    return checkout_snapshot(co.gitdir, co.work_tree)


def _record_bytes(paths):
    return {str(path): path.read_bytes() if path.exists() else None for path in paths}


def _assert_private_named(co, sha, path, content):
    assert _co_git(co, "for-each-ref", "--contains", sha,
                   "--format=%(refname)"), "private commit has no durable named ref"
    assert _co_git(co, "show", f"{sha}:{path}") == content.strip()


@pytest.mark.parametrize("state", ["private-ref", "dangling-commit", "private-config", "staged-index", "foreign-metadata"])
def test_unborn_placeholder_private_state_refuses_conversion(tmp_path, state):
    upstream = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "child", "--url", str(upstream / "docs"))
    child = parent / "child"
    co = layout.whole_repo_checkout(child)
    private = None
    if state in ("private-ref", "dangling-commit"):
        tree = _co_git(co, "mktree", input="")
        private = _co_git(co, "commit-tree", tree, "-m", "private unborn history")
        if state == "private-ref":
            _co_git(co, "update-ref", "refs/heads/private", private)
    elif state == "private-config":
        _co_git(co, "config", "user.private-note", "retain this setting")
    elif state == "staged-index":
        blob = subprocess.run(
            ["git", "--git-dir", str(co.gitdir), "hash-object", "-w", "--stdin"],
            input="private indexed bytes\n", text=True, capture_output=True,
            env=git_probe_env(), check=True).stdout.strip()
        _co_git(co, "update-index", "--add", "--cacheinfo", f"100644,{blob},private.txt")
    else:
        (child / ".gf" / "private-note").write_text("foreign metadata must survive\n")
    before = _snapshot(co)
    index = (co.gitdir / "index").read_bytes() if (co.gitdir / "index").exists() else None
    config = (co.gitdir / "config").read_bytes()
    manifest = (parent / "gf.toml").read_bytes()

    result = gf("-C", str(parent), "pull", check=False)

    assert result.returncode != 0, result.stdout + result.stderr
    assert not child.is_symlink()
    assert _snapshot(co) == before
    assert (co.gitdir / "config").read_bytes() == config
    assert ((co.gitdir / "index").read_bytes() if (co.gitdir / "index").exists() else None) == index
    assert (parent / "gf.toml").read_bytes() == manifest
    if private:
        assert _co_git(co, "cat-file", "-t", private) == "commit"
    if state == "foreign-metadata":
        assert (child / ".gf" / "private-note").read_text() == "foreign metadata must survive\n"


def test_pristine_unborn_placeholder_converts_without_losing_work(tmp_path):
    upstream = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "child", "--url", str(upstream / "docs"))
    gf("-C", str(parent), "pull")
    assert (parent / "child").is_symlink()
    assert (parent / "child" / "file.txt").read_text() == "upstream base\n"


@pytest.mark.parametrize("proof", ["missing", "corrupt"])
def test_placeholder_without_usable_initialization_proof_refuses(tmp_path, proof):
    upstream = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "child", "--url", str(upstream / "docs"))
    child = parent / "child"
    co = layout.whole_repo_checkout(child)
    if proof == "missing":
        co.state.unlink(missing_ok=True)
    else:
        co.state.write_text("not = [valid TOML\n")
    before = _snapshot(co)
    records = _record_bytes([co.state, co.gitdir / "config", co.gitdir / "HEAD"])
    result = gf("-C", str(parent), "pull", check=False)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not child.is_symlink()
    assert _snapshot(co) == before
    assert _record_bytes([co.state, co.gitdir / "config", co.gitdir / "HEAD"]) == records


def test_template_seeded_private_history_is_not_disposable_placeholder(tmp_path):
    """Creation and unchanged bytes do not prove a user template empty."""
    upstream = _upstream(tmp_path)
    parent = _parent(tmp_path)
    donor = tmp_path / "template-donor"
    donor.mkdir()
    _git(donor, "init", "-q")
    (donor / "private.txt").write_text("private template history\n")
    _git(donor, "add", ".")
    _git(donor, "commit", "-qm", "private template commit")
    private = _git(donor, "rev-parse", "HEAD")
    template = tmp_path / "private-template"
    template.mkdir()
    shutil.copytree(donor / ".git" / "objects", template / "objects")
    (template / "refs" / "heads").mkdir(parents=True)
    (template / "refs" / "heads" / "private").write_text(private + "\n")
    with (Path(os.environ["HOME"]) / ".gitconfig").open("a") as config:
        config.write(f"\n[init]\n\ttemplateDir = {template}\n")

    gf("-C", str(parent), "init", "child", "--url", str(upstream / "docs"))
    child = parent / "child"
    co = layout.whole_repo_checkout(child)
    # This is a real activation witness: native init copied the user's
    # private ref and objects even though HEAD remains unborn.
    assert _co_git(co, "rev-parse", "refs/heads/private") == private
    assert _co_git(co, "show", f"{private}:private.txt") == "private template history"
    before = _snapshot(co)
    config_before = (co.gitdir / "config").read_bytes()

    result = gf("-C", str(parent), "pull", check=False)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "gf:" in result.stderr and "Traceback" not in result.stderr
    assert not child.is_symlink()
    assert _snapshot(co) == before
    assert (co.gitdir / "config").read_bytes() == config_before
    assert _co_git(co, "show", f"{private}:private.txt") == "private template history"


@pytest.mark.parametrize("form", ["whole", "subfolder"])
@pytest.mark.parametrize("autostash", [False, True])
def test_ignored_leaf_survives_incoming_ancestor_file(tmp_path, form, autostash):
    parent, upstream, child, co = _clone(tmp_path, form)
    docs = child / "docs" if form == "whole" else child
    private = docs / "cache" / "private.txt"
    private.write_text("irreplaceable ignored leaf\n")
    _co_git(co, "config", "diff.relative", "true")
    assert "!! docs/cache/private.txt" in _co_git(co, "status", "--porcelain", "--ignored")
    before = _snapshot(co)
    _git(upstream, "rm", "docs/cache/base.txt")
    (upstream / "docs" / "cache").write_text("incoming ancestor file\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-qm", "directory to file")

    result = gf("-C", str(docs), "pull", *(("--autostash",) if autostash else ()), check=False)

    if not autostash:
        assert result.returncode == 3, result.stdout + result.stderr
        assert private.read_text() == "irreplaceable ignored leaf\n"
        assert _snapshot(co)["head"] == before["head"]
        assert _snapshot(co)["staged"] == before["staged"]
        assert _snapshot(co)["stash"] == before["stash"]
    else:
        # An ignored directory cannot be restored over the new tracked
        # ancestor file. The truthful conflict must retain its work in
        # a named stash; an arbitrary nonzero response proves nothing.
        assert result.returncode != 0
        assert "stash" in result.stderr.lower()
        assert "gf autostash" in _co_git(co, "stash", "list")
        assert _co_git(co, "show", "stash@{0}^3:docs/cache/private.txt") == "irreplaceable ignored leaf"
        assert _co_git(co, "show", "HEAD:docs/cache") == "incoming ancestor file"


@pytest.mark.parametrize("form", ["whole", "subfolder"])
@pytest.mark.parametrize("autostash", [False, True])
def test_ignored_file_gated_before_existing_branch_attachment(tmp_path, form, autostash):
    parent, upstream, child, co = _clone(tmp_path, form)
    remote_tip = _git(upstream, "rev-parse", "HEAD")
    path = co.work_tree / "docs" / "ignored.dat"
    path.write_text("committed on local master\n")
    _co_git(co, "add", "-f", "docs/ignored.dat")
    _co_git(co, "commit", "-qm", "ahead master tracks ignored path")
    local_tip = _co_git(co, "rev-parse", "HEAD")
    _co_git(co, "checkout", "-qb", "side", remote_tip)
    path.write_text("irreplaceable ignored work\n")
    assert _co_git(co, "diff", "--name-only", "HEAD", "origin/master") == ""

    result = gf("-C", str(parent), "pull", *(("--autostash",) if autostash else ()), check=False)

    if not autostash:
        assert result.returncode == 3, result.stdout + result.stderr
        assert path.read_text() == "irreplaceable ignored work\n"
        assert _co_git(co, "symbolic-ref", "--short", "HEAD") == "side"
        assert _co_git(co, "rev-parse", "refs/heads/master") == local_tip
    else:
        assert result.returncode != 0
        assert "stash" in result.stderr.lower()
        assert "gf autostash" in _co_git(co, "stash", "list")
        assert _co_git(co, "show", "stash@{0}^3:docs/ignored.dat") == "irreplaceable ignored work"
        assert _co_git(co, "rev-parse", "HEAD") == local_tip


@pytest.mark.parametrize("force", [False, True])
def test_parent_worktree_remove_refuses_ignored_user_file(tmp_path, force):
    parent = _parent(tmp_path)
    target = tmp_path / "side"
    gf("-C", str(parent), "worktree", "add", str(target), "-b", "side")
    (target / "ignored.txt").write_text("irreplaceable parent ignored work\n")
    assert _git(target, "status", "--porcelain") == ""
    head = _git(target, "rev-parse", "HEAD")
    registration = _git(parent, "worktree", "list", "--porcelain")

    result = gf("-C", str(parent), "worktree", "remove", str(target),
                *(("--force",) if force else ()), check=False)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "ignored" in result.stderr.lower()
    assert (target / "ignored.txt").read_text() == "irreplaceable parent ignored work\n"
    assert _git(target, "rev-parse", "HEAD") == head
    assert _git(parent, "worktree", "list", "--porcelain") == registration


def test_clean_parent_worktree_with_linked_out_child_removes_safely(tmp_path):
    parent, upstream, child, co = _clone(tmp_path)
    # Make the parent worktree clean; its child is an ignored linked-out
    # consumer in the new worktree, rather than target-owned work.
    _git(parent, "add", "gf.toml")
    _git(parent, "commit", "-qm", "record child")
    target = tmp_path / "side"
    gf("-C", str(parent), "worktree", "add", str(target), "-b", "side")
    assert (target / "child").is_symlink()
    before = _snapshot(co)
    gf("-C", str(parent), "worktree", "remove", str(target))
    assert not target.exists()
    assert _snapshot(co) == before


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("protected", ["detached-commit", "former-detached-history", "worktree-ref",
                                      "worktree-config", "assume-unchanged", "skip-worktree"])
def test_parent_worktree_remove_keeps_clean_private_git_state(tmp_path, protected, force):
    parent = _parent(tmp_path)
    target = tmp_path / "side"
    gf("-C", str(parent), "worktree", "add", str(target), "-b", "side")
    if protected in ("detached-commit", "former-detached-history"):
        _git(target, "checkout", "-q", "--detach")
        (target / "private.txt").write_text("private detached parent history\n")
        _git(target, "add", "private.txt")
        _git(target, "commit", "-qm", "private detached commit")
        private = _git(target, "rev-parse", "HEAD")
        assert _git(target, "for-each-ref", "--contains", private,
                    "--format=%(refname)") == ""
        if protected == "former-detached-history":
            _git(target, "checkout", "-q", "side")
            head_log = Path(_git(target, "rev-parse", "--absolute-git-dir")) / "logs" / "HEAD"
            raw = head_log.read_bytes()
            assert b"private detached commit" in raw
            head_log.write_bytes(raw.replace(b"private detached commit", b"private detached commit\xff"))
    elif protected == "worktree-ref":
        _git(target, "update-ref", "refs/worktree/private", "HEAD")
    elif protected == "worktree-config":
        _git(parent, "config", "extensions.worktreeConfig", "true")
        _git(target, "config", "--worktree", "user.private-note", "private worktree setting")
    else:
        _git(target, "update-index", "--" + protected, ".gitignore")
        (target / ".gitignore").write_text("private flagged work\n")
    assert _git(target, "status", "--porcelain") == ""
    head = _git(target, "rev-parse", "HEAD")
    refs = _git(target, "for-each-ref", "--format=%(refname) %(objectname)")
    config = _git(target, "config", "--list")
    registration = _git(parent, "worktree", "list", "--porcelain")

    result = gf("-C", str(parent), "worktree", "remove", str(target),
                *(("--force",) if force else ()), check=False)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "gf:" in result.stderr and "Traceback" not in result.stderr
    assert target.exists()
    assert _git(target, "rev-parse", "HEAD") == head
    assert _git(target, "for-each-ref", "--format=%(refname) %(objectname)") == refs
    assert _git(target, "config", "--list") == config
    assert _git(parent, "worktree", "list", "--porcelain") == registration
    if protected in ("detached-commit", "former-detached-history"):
        assert _git(target, "show", f"{private}:private.txt") == "private detached parent history"
    if protected in ("assume-unchanged", "skip-worktree"):
        assert (target / ".gitignore").read_text() == "private flagged work\n"


@pytest.mark.parametrize("form", ["whole", "subfolder"])
def test_multiple_detached_tips_remain_named_after_repeated_pulls_and_unmap(tmp_path, form):
    parent, upstream, child, co = _clone(tmp_path, form, "v1")
    peer = None
    if form == "subfolder":
        _git(upstream, "tag", "v2")
        gf("-C", str(parent), "clone", str(upstream / "docs"), "peer", "-b", "v2")
        peer = layout.subfolder_checkout(parent, str(upstream), "ref=v2", "docs")
        peer_before = _snapshot(peer)
    tips = []
    for number in (1, 2):
        path = f"docs/private-{number}.txt"
        content = f"private detached work {number}\n"
        (co.work_tree / path).write_text(content)
        _co_git(co, "add", path)
        _co_git(co, "commit", "-qm", f"detached private {number}")
        tips.append((_co_git(co, "rev-parse", "HEAD"), path, content))
        gf("-C", str(parent), "pull")
        for sha, old_path, old_content in tips:
            _assert_private_named(co, sha, old_path, old_content)
            if peer:
                assert not _co_git(peer, "for-each-ref", "--contains", sha,
                                   "--format=%(refname)")
        if peer:
            assert _snapshot(peer) == peer_before
    gf("-C", str(parent), "pull")
    retained_before = _co_git(co, "for-each-ref", "--format=%(refname) %(objectname)")
    gf("-C", str(parent), "pull")
    assert _co_git(co, "for-each-ref", "--format=%(refname) %(objectname)") == retained_before
    gf("-C", str(parent), "rm", "child")
    for sha, path, content in tips:
        if form == "whole":
            assert _git(child, "for-each-ref", "--contains", sha, "--format=%(refname)")
            assert _git(child, "show", f"{sha}:{path}") == content.strip()
        else:
            _assert_private_named(co, sha, path, content)


def test_legacy_retention_ref_survives_next_detached_transition(tmp_path):
    parent, upstream, child, co = _clone(tmp_path, "whole", "v1")
    (co.work_tree / "docs" / "legacy.txt").write_text("legacy private history\n")
    _co_git(co, "add", "docs/legacy.txt")
    _co_git(co, "commit", "-qm", "legacy retained tip")
    legacy = _co_git(co, "rev-parse", "HEAD")
    _co_git(co, "update-ref", "refs/worktree/gf-retained", legacy)
    _co_git(co, "checkout", "-q", "--detach", "v1")
    (co.work_tree / "docs" / "new.txt").write_text("new private history\n")
    _co_git(co, "add", "docs/new.txt")
    _co_git(co, "commit", "-qm", "new detached tip")
    current = _co_git(co, "rev-parse", "HEAD")

    gf("-C", str(parent), "pull")

    _assert_private_named(co, legacy, "docs/legacy.txt", "legacy private history\n")
    _assert_private_named(co, current, "docs/new.txt", "new private history\n")


def test_foreign_root_consumer_refuses_without_mutating_locked_owner(tmp_path):
    parent, upstream, child, co = _clone(tmp_path, "subfolder")
    caller = _parent(tmp_path, "unrelated-caller")
    (caller / "gf.toml").write_bytes((parent / "gf.toml").read_bytes())
    (caller / "child").symlink_to(os.path.relpath(child, caller))
    (upstream / "docs" / "file.txt").write_text("new remote bytes\n")
    _git(upstream, "commit", "-qam", "advance")
    before = _snapshot(co)
    owner_manifest = (parent / "gf.toml").read_bytes()
    caller_manifest = (caller / "gf.toml").read_bytes()
    owner_records = _record_bytes([
        co.state, co.common_dir / "config", co.common_dir / "FETCH_HEAD"])
    common = Path(_git(parent, "rev-parse", "--git-common-dir"))
    common = common if common.is_absolute() else parent / common
    with (common / "gf.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        result = subprocess.run(
            [sys.executable, "-m", "gf", "-C", str(caller), "pull"],
            text=True, capture_output=True, timeout=5)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "gf:" in result.stderr
    assert _snapshot(co) == before
    assert _record_bytes([
        co.state, co.common_dir / "config", co.common_dir / "FETCH_HEAD"]) == owner_records
    assert (parent / "gf.toml").read_bytes() == owner_manifest
    assert (caller / "gf.toml").read_bytes() == caller_manifest


def test_same_parent_worktree_consumer_updates_verified_owner(tmp_path):
    parent, upstream, child, co = _clone(tmp_path, "subfolder")
    target = tmp_path / "side"
    gf("-C", str(parent), "worktree", "add", str(target), "-b", "side")
    assert (target / "child").is_symlink()
    (upstream / "docs" / "file.txt").write_text("same family advance\n")
    _git(upstream, "commit", "-qam", "advance")
    gf("-C", str(target / "child"), "pull")
    assert (child / "file.txt").read_text() == "same family advance\n"
    assert _co_git(co, "rev-parse", "HEAD") == _git(upstream, "rev-parse", "HEAD")


@pytest.mark.parametrize("form", ["whole", "subfolder"])
@pytest.mark.parametrize("nested", [False, True])
def test_autostash_cwd_preserves_index_work_and_metadata(tmp_path, form, nested):
    parent, upstream, child, co = _clone(tmp_path, form)
    docs = child / "docs" if form == "whole" else child
    (docs / "staged.txt").write_text("staged work\n")
    _co_git(co, "add", "docs/staged.txt")
    (docs / "file.txt").write_text("unstaged work\n")
    (docs / "ignored.dat").write_text("ignored work\n")
    (docs / "untracked.txt").write_text("untracked work\n")
    index_before = _co_git(co, "diff", "--cached", "--binary")
    (upstream / "remote.txt").write_text("remote unrelated advance\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-qm", "advance unrelated path")
    cwd = docs / "cache" if nested else parent

    result = gf("-C", str(cwd), "pull", "--autostash", check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _co_git(co, "rev-parse", "HEAD") == _git(upstream, "rev-parse", "HEAD")
    assert _co_git(co, "diff", "--cached", "--binary") == index_before
    assert (docs / "staged.txt").read_text() == "staged work\n"
    assert (docs / "file.txt").read_text() == "unstaged work\n"
    assert (docs / "ignored.dat").read_text() == "ignored work\n"
    assert (docs / "untracked.txt").read_text() == "untracked work\n"
    assert _co_git(co, "stash", "list") == ""
    assert co.gitdir.is_dir()
    assert _co_git(co, "config", "--get", "remote.origin.url") == str(upstream)


@pytest.mark.parametrize("form", ["whole", "subfolder"])
def test_nested_autostash_conflict_keeps_named_recovery_work(tmp_path, form):
    parent, upstream, child, co = _clone(tmp_path, form)
    docs = child / "docs" if form == "whole" else child
    (docs / "file.txt").write_text("private conflicting edit\n")
    (docs / "ignored.dat").write_text("private ignored work\n")
    (upstream / "docs" / "file.txt").write_text("upstream conflicting edit\n")
    _git(upstream, "commit", "-qam", "conflicting advance")

    result = gf("-C", str(docs / "cache"), "pull", "--autostash", check=False)

    assert result.returncode != 0
    assert "stash" in result.stderr.lower()
    assert "gf autostash" in _co_git(co, "stash", "list")
    assert "stash pop --index" in result.stderr
    assert _co_git(co, "rev-parse", "HEAD") == _git(upstream, "rev-parse", "HEAD")
    assert _co_git(co, "show", "stash@{0}:docs/file.txt") == "private conflicting edit"
    assert _co_git(co, "show", "stash@{0}^3:docs/ignored.dat") == "private ignored work"
    assert co.gitdir.is_dir()
