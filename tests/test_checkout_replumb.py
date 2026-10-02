# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Receiving tests for slice P2R.S1 — checkout-resolver re-plumb.

Every git operation in `src/gf/` is being re-plumbed to consume a
`layout.Checkout` resolved once, with whole-repo command behavior
byte-identical. The 253 existing tests are the unedited receiver; this
file adds only discriminating coverage for spec'd *whole-repo* behavior
that no existing test pinned. All tests must pass at the
`7174803`+`8ed4c4e` baseline and unchanged after the refactor.

Expectations derive only from docs/gf-spec.md (not from the
implementation):

- "Reference model": `commit` is a pinned SHA, `tag` a pinned tag
  resolved to its SHA, `branch` a floating ref resolving to the remote
  branch tip, `latest` a floating ref resolving to the remote default
  branch tip.
- `gf clone`: a tag or commit effective ref is checked out in a detached
  HEAD; `--single-branch` is ignored for tag/commit refs; `--depth` only
  affects the initial fetch ("subsequent `gf pull` operations fetch
  normally"); a path that is already a git-folder succeeds after adding
  the manifest entry without re-cloning or overwriting the existing
  worktree; a path that exists, is not empty, and is not a git-folder
  fails; when the child directory already existed (but was not a
  git-folder) a failed clone removes only the `.gf/` directory.
- `gf pull`: tag/commit refs run `git checkout <resolved-sha>`
  (`--force` adds `-f`); `--rebase --force` force-recreates the local
  branch at HEAD (`git checkout -f -B <branch> HEAD`) before the rebase;
  `--autostash` leaves the stash in place and errors clearly when the
  pop conflicts; relative local URLs resolve against the parent repo
  root; a local `gf` child resolves to its inner gitdir.
- `gf status` / `gf ls`: `branch` is the current local branch, or empty
  brackets for a detached HEAD.
- `gf status --remote`: drift is local-only; for `latest` the resolved
  SHA comes from `refs/remotes/origin/HEAD` when it is a valid symbolic
  ref, otherwise `refs/remotes/origin/main`, otherwise
  `refs/remotes/origin/master`; a `tag`/`commit` resolves to the peeled
  SHA from local refs. Error-handling: a git failure exits 2.
- "Target selection rules": `gf <cmd>` with no args inside a child
  operates on that child; a path argument that is a parent of multiple
  children operates on all children below it.
- `gf rm`: with no arguments and cwd inside a child, removes that child;
  unregistering a manifest entry whose child directory is absent leaves
  nothing to convert and still empties the manifest entry.
- `gf sh` / `gf git`: GIT_DIR points to the child's gitdir and
  GIT_WORK_TREE to the child's working root; with no command a shell is
  started; a `-C` after the subcommand is passed through to `git`.
- `gf worktree add`: the current `gf.toml` and `gf.local.toml` are
  copied to the new worktree, and commands run against the symlinked
  git-folder resolve to the source child and operate on it.
"""

import os
import re
import subprocess
from pathlib import Path

from conftest import gf, git, push_branch, push_commit

SHA1 = "1111111111111111111111111111111111111111"
SHA2 = "2222222222222222222222222222222222222222"
WILDCARD_FETCH = "+refs/heads/*:refs/remotes/origin/*"


# --- fixtures --------------------------------------------------------


def _parent(fs, root: str = "/parent") -> Path:
    """Fake parent git repo with an empty gf.toml (mock-backend tests)."""
    parent = Path(root)
    parent.mkdir(parents=True, exist_ok=True)
    (parent / ".git").mkdir(parents=True, exist_ok=True)
    (parent / "gf.toml").write_text("git_folder = []\n")
    return parent


def _seed_upstream(mock_backend, path: str = "/upstream", files: dict | None = None):
    """Seed a bare upstream repo on master; return the mock Repo."""
    repo = mock_backend.seed(path, bare=True, mirror=False)
    mock_backend.add_commit(repo, SHA1, files or {"a.txt": "hello"})
    repo.refs["refs/heads/master"] = SHA1
    repo.head_ref = "refs/heads/master"
    return repo


def _real_upstream(tmp_path: Path) -> Path:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")
    return upstream


def _real_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _git_out(*args, cwd: Path) -> str:
    """Run real git and return stripped stdout."""
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return r.stdout.strip()


def _push_tag(remote: Path, name: str) -> None:
    """Create a lightweight tag on the remote's current master tip."""
    work = remote.parent / "_tag_work"
    if work.exists():
        import shutil

        shutil.rmtree(work)
    git("clone", str(remote), str(work), cwd=remote.parent)
    git("tag", name, cwd=work)
    git("push", "origin", name, cwd=work)
    import shutil

    shutil.rmtree(work)


# --- gf clone ---------------------------------------------------------


def test_clone_tag_ref_detaches_head(tmp_path):
    upstream = _real_upstream(tmp_path)
    _push_tag(upstream, "v1")
    tag_sha = _git_out("rev-parse", "v1^{}", cwd=upstream)
    push_commit(upstream, "update", "update")  # master moves past the tag

    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib", "-b", "v1")
    child = parent / "vendor" / "lib"

    # The pinned tag — not the newer branch tip — is checked out detached.
    assert (child / "a.txt").read_text() == "hello"
    head = gf("-C", str(child), "sh", "-c", "git rev-parse HEAD").stdout.strip()
    assert head == tag_sha
    abbrev = gf(
        "-C", str(child), "sh", "-c", "git rev-parse --abbrev-ref HEAD"
    ).stdout.strip()
    assert abbrev == "HEAD"

    # status and ls show empty brackets for a detached HEAD.
    result = gf("-C", str(parent), "status")
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[\]", result.stdout)
    result = gf("-C", str(parent), "ls")
    assert re.search(
        r"lib\s+" + re.escape(str(upstream)) + r"\s+\[\]\s+" + tag_sha[:7],
        result.stdout,
    )
    # Drift resolves the tag to its peeled SHA: clean.
    result = gf("-C", str(parent), "status", "--remote")
    assert re.search(
        r"lib\s+" + re.escape(str(upstream)) + r"\s+\[\]\s+clean", result.stdout
    )


def test_pull_on_tag_ref_stays_pinned(tmp_path):
    upstream = _real_upstream(tmp_path)
    _push_tag(upstream, "v1")
    tag_sha = _git_out("rev-parse", "v1^{}", cwd=upstream)

    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib", "-b", "v1")
    child = parent / "vendor" / "lib"

    push_commit(upstream, "update", "update")
    gf("-C", str(parent), "pull")

    # A tag is a pinned ref: pull re-checks out the tag's SHA.
    assert (child / "a.txt").read_text() == "hello"
    head = gf("-C", str(child), "sh", "-c", "git rev-parse HEAD").stdout.strip()
    assert head == tag_sha


def test_commit_sha_ref_detaches_and_stays_pinned(tmp_path):
    upstream = _real_upstream(tmp_path)
    sha = _git_out("rev-parse", "refs/heads/master", cwd=upstream)
    push_commit(upstream, "update", "update")  # master moves past the sha

    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib", "-b", sha)
    child = parent / "vendor" / "lib"

    # The pinned commit — not the newer tip — is checked out detached.
    assert (child / "a.txt").read_text() == "hello"
    head = gf("-C", str(child), "sh", "-c", "git rev-parse HEAD").stdout.strip()
    assert head == sha
    result = gf("-C", str(parent), "status")
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[\]", result.stdout)


def test_clone_onto_existing_git_folder_registers_without_reclone(
    fs, gf_inproc, mock_backend
):
    parent = _parent(fs)
    _seed_upstream(mock_backend)

    # `lib` is already a whole-repo git-folder (`.gf/git` with a HEAD) but
    # has no manifest entry.
    child = parent / "lib"
    fs.create_file(
        str(child / ".gf" / "git" / "HEAD"), contents="ref: refs/heads/master\n"
    )
    (child / "keep.txt").write_text("pre-existing")

    r = gf_inproc(
        "-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend
    )
    assert r.returncode == 0, r.stderr
    assert "Cloned lib" in r.stdout
    manifest_text = (parent / "gf.toml").read_text()
    assert 'name = "lib"' in manifest_text
    assert 'path = "lib"' in manifest_text

    # It does not re-clone or overwrite the existing worktree.
    git_ops = [c[0][0] for c in mock_backend.calls]
    assert "fetch" not in git_ops
    assert "checkout" not in git_ops
    assert not (child / "a.txt").exists()
    assert (child / "keep.txt").read_text() == "pre-existing"


def test_clone_fails_on_nonempty_non_git_folder_path(fs, gf_inproc, mock_backend):
    parent = _parent(fs)
    _seed_upstream(mock_backend)
    child = parent / "lib"
    child.mkdir()
    (child / "existing.txt").write_text("I was here first")

    r = gf_inproc(
        "-C", str(parent), "clone", "/upstream", "lib",
        backend=mock_backend, check=False,
    )
    assert r.returncode == 1
    assert "already exists" in r.stderr or "not empty" in r.stderr

    # The directory and its content are left in place; nothing registers.
    assert (child / "existing.txt").read_text() == "I was here first"
    assert not (child / ".gf").exists()
    assert 'name = "lib"' not in (parent / "gf.toml").read_text()
    assert "fetch" not in [c[0][0] for c in mock_backend.calls]


def test_clone_failure_removes_only_dot_gf_from_existing_empty_dir(
    fs, gf_inproc, mock_backend
):
    parent = _parent(fs)
    child = parent / "lib"
    child.mkdir()  # the path existed before the command, but empty

    r = gf_inproc(
        "-C", str(parent), "clone", "/no-such-repo", "lib",
        backend=mock_backend, check=False,
    )
    assert r.returncode == 2  # git/network failure
    # The pre-existing directory remains; the `.gf` gf created is removed.
    assert child.is_dir()
    assert not (child / ".gf").exists()


def test_clone_single_branch_ignored_for_tag_ref(fs, gf_inproc, mock_backend):
    parent = _parent(fs)
    repo = _seed_upstream(mock_backend)
    mock_backend.tag(repo, "v1", SHA1)

    gf_inproc(
        "-C", str(parent), "clone", "/upstream", "lib",
        "-b", "v1", "--single-branch", backend=mock_backend,
    )
    # --single-branch narrows only branch/latest clones; for a tag ref the
    # wildcard fetch refspec stands.
    refspec_writes = [
        c[0] for c in mock_backend.calls
        if c[0][0] == "config" and c[0][1] == "remote.origin.fetch"
    ]
    assert refspec_writes
    assert refspec_writes[-1] == ("config", "remote.origin.fetch", WILDCARD_FETCH)


def test_clone_depth_does_not_apply_to_later_pull_fetches(
    fs, gf_inproc, mock_backend
):
    parent = _parent(fs)
    _seed_upstream(mock_backend)
    gf_inproc(
        "-C", str(parent), "clone", "/upstream", "lib", "--depth", "1",
        backend=mock_backend,
    )
    clone_fetches = [c[0] for c in mock_backend.calls if c[0][0] == "fetch"]
    assert any("--depth=1" in c for c in clone_fetches)

    mock_backend.calls.clear()
    gf_inproc("-C", str(parent), "pull", backend=mock_backend)
    pull_fetches = [c[0] for c in mock_backend.calls if c[0][0] == "fetch"]
    assert pull_fetches
    assert all(not any(a.startswith("--depth") for a in c) for c in pull_fetches)


def test_latest_uses_remote_default_branch(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_branch(upstream, "main", "main content")
    # The remote's default branch is `main`; no `master` exists.
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=upstream)

    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"
    assert (child / "main.txt").read_text() == "main content"

    result = gf("-C", str(parent), "status")
    assert re.search(
        r"lib\s+" + re.escape(str(upstream)) + r"\s+\[main\]", result.stdout
    )
    result = gf("-C", str(parent), "status", "--remote")
    assert re.search(
        r"lib\s+" + re.escape(str(upstream)) + r"\s+\[main\]\s+clean",
        result.stdout,
    )


# --- gf pull ----------------------------------------------------------


def test_pull_rebase_force_discards_dirty_and_updates(tmp_path):
    upstream = _real_upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"

    (child / "a.txt").write_text("dirty change")  # dirty tracked file
    push_commit(upstream, "update", "update")

    # `checkout -f -B <branch> HEAD` discards the dirty change, then the
    # rebase lands the remote tip.
    gf("-C", str(parent), "pull", "--rebase", "--force")
    assert (child / "a.txt").read_text() == "update"


def test_pull_autostash_pop_conflict_keeps_stash(tmp_path):
    upstream = _real_upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"

    # A local edit to a tracked file the update also changes.
    (child / "a.txt").write_text("local conflicting change")
    push_commit(upstream, "update", "upstream change")

    result = gf("-C", str(parent), "pull", "--autostash", check=False)
    assert result.returncode == 2  # git failure exit code
    assert "stash" in result.stderr.lower()
    # The stash is left in place so the user can resolve and pop manually.
    stash = gf("-C", str(child), "sh", "-c", "git stash list")
    assert stash.stdout.strip() != ""


def test_pull_inside_child_updates_only_that_child(fs, gf_inproc, mock_backend):
    parent = _parent(fs)
    upstream_repo = _seed_upstream(mock_backend)
    gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)
    gf_inproc("-C", str(parent), "clone", "/upstream", "lib2", backend=mock_backend)

    mock_backend.add_commit(
        upstream_repo, SHA2, {"a.txt": "update"}, parents=[SHA1]
    )
    upstream_repo.refs["refs/heads/master"] = SHA2

    # No args, cwd inside a child: only that child is updated.
    r = gf_inproc("-C", str(parent / "lib"), "pull", backend=mock_backend)
    assert r.returncode == 0, r.stderr
    assert (parent / "lib" / "a.txt").read_text() == "update"
    assert (parent / "lib2" / "a.txt").read_text() == "hello"


def test_pull_with_parent_dir_arg_updates_all_children_below(
    fs, gf_inproc, mock_backend
):
    parent = _parent(fs)
    upstream_repo = _seed_upstream(mock_backend)
    gf_inproc(
        "-C", str(parent), "clone", "/upstream", "vendor/a", backend=mock_backend
    )
    gf_inproc(
        "-C", str(parent), "clone", "/upstream", "vendor/b", backend=mock_backend
    )

    mock_backend.add_commit(
        upstream_repo, SHA2, {"a.txt": "update"}, parents=[SHA1]
    )
    upstream_repo.refs["refs/heads/master"] = SHA2

    # `vendor` is a parent of multiple children: all below it update.
    r = gf_inproc("-C", str(parent), "pull", "vendor", backend=mock_backend)
    assert r.returncode == 0, r.stderr
    assert (parent / "vendor" / "a" / "a.txt").read_text() == "update"
    assert (parent / "vendor" / "b" / "a.txt").read_text() == "update"


def test_pull_force_on_tag_ref_uses_forced_detached_checkout(
    fs, gf_inproc, mock_backend
):
    parent = _parent(fs)
    repo = _seed_upstream(mock_backend)
    mock_backend.tag(repo, "v1", SHA1)
    gf_inproc(
        "-C", str(parent), "clone", "/upstream", "lib", "-b", "v1",
        backend=mock_backend,
    )

    (parent / "lib" / "a.txt").write_text("dirty")
    mock_backend.calls.clear()
    gf_inproc("-C", str(parent), "pull", "--force", backend=mock_backend)

    # For a tag/commit ref --force runs `git checkout -f <sha>`.
    checkout_calls = [
        c[0] for c in mock_backend.calls if c[0][0] == "checkout"
    ]
    assert checkout_calls == [("checkout", "-f", SHA1)]
    assert (parent / "lib" / "a.txt").read_text() == "hello"


def test_pull_rebase_on_tag_ref_uses_plain_checkout(fs, gf_inproc, mock_backend):
    parent = _parent(fs)
    repo = _seed_upstream(mock_backend)
    mock_backend.tag(repo, "v1", SHA1)
    gf_inproc(
        "-C", str(parent), "clone", "/upstream", "lib", "-b", "v1",
        backend=mock_backend,
    )

    mock_backend.calls.clear()
    r = gf_inproc("-C", str(parent), "pull", "--rebase", backend=mock_backend)
    assert r.returncode == 0, r.stderr

    # `--rebase` applies only to branch refs; a tag ref gets
    # `git checkout <resolved-sha>` and no rebase runs.
    assert "rebase" not in [c[0][0] for c in mock_backend.calls]
    checkout_calls = [
        c[0] for c in mock_backend.calls if c[0][0] == "checkout"
    ]
    assert checkout_calls == [("checkout", SHA1)]


def test_pull_resolves_relative_local_url_against_parent_root(
    fs, gf_inproc, mock_backend
):
    parent = _parent(fs)
    _seed_upstream(mock_backend)
    (parent / "gf.toml").write_text(
        '[[git_folder]]\nname = "lib"\nurl = "../upstream"\n'
        'ref = "latest"\npath = "vendor/lib"\n'
    )
    (parent / "vendor").mkdir()

    # From `vendor/` a cwd-relative `../upstream` would miss `/upstream`;
    # resolution is against the parent repo root.
    r = gf_inproc(
        "-C", str(parent / "vendor"), "pull", backend=mock_backend
    )
    assert r.returncode == 0, r.stderr
    child = parent / "vendor" / "lib"
    assert (child / "a.txt").read_text() == "hello"
    assert (child / ".gf" / "git" / "HEAD").is_file()


def test_pull_resolves_local_gf_child_url_to_inner_gitdir(tmp_path):
    upstream = _real_upstream(tmp_path)
    parent = _real_parent(tmp_path)

    # `a` is a real gf child: its gitdir is `a/.gf/git` and the directory
    # itself carries no `.git`.
    gf("-C", str(parent), "clone", str(upstream), "vendor/a")
    child_a = parent / "vendor" / "a"
    assert not (child_a / ".git").exists()

    (parent / "gf.toml").write_text(
        (parent / "gf.toml").read_text()
        + f'[[git_folder]]\nname = "lib"\nurl = "{child_a}"\n'
        + 'ref = "latest"\npath = "vendor/lib"\n'
    )

    gf("-C", str(parent), "pull")
    child = parent / "vendor" / "lib"
    assert (child / "a.txt").read_text() == "hello"
    assert (child / ".gf" / "git" / "HEAD").is_file()
    # The fetch worked because the child's URL resolved to its gitdir.
    origin = gf(
        "-C", str(child), "git", "remote", "get-url", "origin"
    ).stdout.strip()
    assert origin == str((child_a / ".gf" / "git").resolve())


# --- gf status / drift ------------------------------------------------


def test_status_remote_latest_falls_back_origin_main_then_master(
    fs, gf_inproc, mock_backend
):
    parent = _parent(fs)
    _seed_upstream(mock_backend)
    gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)
    child_repo = mock_backend._repo(
        str((parent / "lib" / ".gf" / "git").resolve())
    )

    # No valid `origin/HEAD` symref: `latest` falls back to `origin/main`.
    child_repo.symrefs.pop("refs/remotes/origin/HEAD", None)
    mock_backend.add_commit(
        child_repo, SHA2, {"a.txt": "update"}, parents=[SHA1]
    )
    child_repo.refs["refs/remotes/origin/main"] = SHA2

    r = gf_inproc(
        "-C", str(parent), "status", "--remote", backend=mock_backend
    )
    assert r.returncode == 0, r.stderr
    # HEAD (master@SHA1) differs from the main tip: behind, not clean.
    assert re.search(r"lib\s+/upstream\s+\[master\]\s+behind", r.stdout)

    # With `origin/main` absent the fallback reaches `origin/master`.
    del child_repo.refs["refs/remotes/origin/main"]
    r = gf_inproc(
        "-C", str(parent), "status", "--remote", backend=mock_backend
    )
    assert re.search(r"lib\s+/upstream\s+\[master\]\s+clean", r.stdout)


# --- gf rm ------------------------------------------------------------


def test_rm_inside_child_removes_that_child(fs, gf_inproc, mock_backend):
    parent = _parent(fs)
    _seed_upstream(mock_backend)
    gf_inproc(
        "-C", str(parent), "clone", "/upstream", "vendor/lib",
        backend=mock_backend,
    )
    child = parent / "vendor" / "lib"

    # No args, cwd inside a child: remove that child.
    r = gf_inproc("-C", str(child), "rm", backend=mock_backend)
    assert r.returncode == 0, r.stderr
    # The whole-repo child is converted back to a normal git repo; the
    # worktree files are preserved.
    assert (child / ".git" / "HEAD").is_file()
    assert not (child / ".gf").exists()
    assert (child / "a.txt").read_text() == "hello"
    assert "git_folder = []" in (parent / "gf.toml").read_text()


def test_rm_unregisters_manifest_entry_whose_child_is_missing(
    fs, gf_inproc, mock_backend
):
    parent = _parent(fs)
    (parent / "gf.toml").write_text(
        '[[git_folder]]\nname = "lib"\nurl = "/upstream"\n'
        'ref = "latest"\npath = "vendor/lib"\n'
    )

    r = gf_inproc(
        "-C", str(parent), "rm", "vendor/lib", backend=mock_backend
    )
    assert r.returncode == 0, r.stderr
    assert "git_folder = []" in (parent / "gf.toml").read_text()


# --- gf sh / gf git ---------------------------------------------------


def test_sh_exports_git_dir_and_work_tree(tmp_path):
    upstream = _real_upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"

    result = gf(
        "-C", str(child), "sh", "-c",
        'echo "$GIT_DIR"; echo "$GIT_WORK_TREE"',
    )
    lines = result.stdout.splitlines()
    assert lines[0] == str((child / ".gf" / "git").resolve())
    assert lines[1] == str(child.resolve())


def test_sh_with_no_command_runs_the_shell(tmp_path, monkeypatch):
    upstream = _real_upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"

    # `$SHELL` is spawned inside the child's git environment; `env` makes
    # the spawn observable without a terminal.
    monkeypatch.setenv("SHELL", "env")
    result = gf("-C", str(child), "sh", check=False)
    assert result.returncode == 0, result.stderr
    assert f"GIT_DIR={(child / '.gf' / 'git').resolve()}" in result.stdout
    assert f"GIT_WORK_TREE={child.resolve()}" in result.stdout


def test_git_trailing_c_is_forwarded_to_git(tmp_path):
    upstream = _real_upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"

    # A `-C` after the subcommand goes to `git`: `git -C <parent>` runs in
    # the parent dir with the child's GIT_DIR/GIT_WORK_TREE, so the
    # toplevel it reports is the child's working root. If gf had consumed
    # the option as its global -C it would chdir to the parent and die
    # "not inside a git-folder child".
    result = gf(
        "-C", str(child), "git", "-C", str(parent),
        "rev-parse", "--show-toplevel",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(child.resolve())


# --- gf worktree ------------------------------------------------------


def test_worktree_add_copies_local_overrides(tmp_path):
    upstream = _real_upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "lib"\nref = "master"\n'
    )

    feature = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(feature), "-b", "feature")

    assert (feature / "gf.toml").read_text() == (parent / "gf.toml").read_text()
    assert (feature / "gf.local.toml").read_text() == (
        parent / "gf.local.toml"
    ).read_text()


def test_pull_in_linked_worktree_updates_source_child(tmp_path):
    upstream = _real_upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    git("add", "gf.toml", cwd=parent)
    git("commit", "-m", "add git-folder", cwd=parent)

    feature = tmp_path / "feature"
    gf("-C", str(parent), "worktree", "add", str(feature), "-b", "feature")
    link_child = feature / "vendor" / "lib"
    assert link_child.is_symlink()

    push_commit(upstream, "update", "update")
    # Run inside the symlinked child: it resolves to the source child.
    gf("-C", str(link_child), "pull")

    source_child = parent / "vendor" / "lib"
    assert (source_child / "a.txt").read_text() == "update"
    # The link shares the source's working tree: same update visible.
    assert (link_child / "a.txt").read_text() == "update"
