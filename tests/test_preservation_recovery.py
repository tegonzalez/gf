# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""P1 preservation witnesses for `gf pull` — recovery class and the
`refs/worktree/gf-retained-commits/<sha>` retention mechanism (Change DC-DOC-PLAN-007,
obligations O-recovery / O-update; gf-arch.md GF-D16/GF-D23).

Oracle: docs/gf-testing.md "Preservation witness oracle" — recovery
class requires all five observation categories plus a truthful
partial-effect report, and the named recovery path must reach a
`preserved` end.

Expected behavior derives only from the admitted documents:

- gf-spec.md `gf pull`: `git stash pop --index` restores the index
  partition after success AND after every update failure that follows
  the stash — "including a failed `origin` URL update — so a stash
  taken before the update is never orphaned. When the pop itself
  conflicts or fails, the stash is left in place and `gf` reports the
  state plus the manual recovery (resolve, then `git stash pop
  --index`), so the stashed work is never dropped."
- gf-arch.md failure modes: a pop conflict leaves the named stash
  entry retained with recovery instruction; an in-progress merge or
  rebase surfaces git's own refusal on the next pull — never a stacked
  operation; a killed mid-operation leaves inspectable state and no
  welded lock; a stranded `gf autostash` entry stays named in the
  checkout's stash list.
- gf-arch.md GF-D23: before a transition that could orphan the
  outgoing HEAD — attaching or switching branches, a detached move, a
  rebase — the outgoing HEAD is written to the per-checkout retention
  ref `refs/worktree/gf-retained-commits/<sha>`; a failed write refuses the
  transition.
- gf-spec.md "Error handling": exit 1 refused update, 2 network/git
  failure, 3 dirty worktree; partial effects are reported truthfully.

Recovery-arm note (documented assumption): the spec's parenthetical
names `git stash pop --index` as the manual recovery. With real git a
deterministically-conflicted pop cannot be re-run to completion — the
same merge conflict recurs while HEAD carries the conflicting update.
The user's pop is what already applied the stashed bytes (as conflict
markers / restored files); the manual sequence that reaches the
preserved end is resolve + `git stash drop`. The witness asserts the
doc-mandated observable contract — retained named stash entry, truthful
conflict report, recovery guidance that mentions the stash — then
completes recovery by resolve + drop and verifies the preserved end.

Nothing in this file consults `src/gf/` to decide expected behavior.
"""

import subprocess
from pathlib import Path

import pytest

from conftest import checkout_snapshot, deny_file_transport, gf, git
from gf import layout


# ---------------------------------------------------------------------------
# helpers — real-git fixtures, same style as test_pull_autostash_recovery


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        pytest.fail(
            f"setup: git {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stderr}")
    return r


def _out(*args) -> str:
    return _git(*args).stdout.strip()


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", "-q", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root\n")
    git("add", "README", cwd=parent)
    git("commit", "-qm", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str = "upstream") -> Path:
    """Bare upstream: docs/api/x.txt + tools/t.txt on master."""
    up = tmp_path / name
    _git("init", "-q", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text("api on master\n")
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text("tool on master\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")
    return up


def _advance(up: Path, tmp_path: Path, tag: str = "adv") -> str:
    """Push one commit to upstream master touching x.txt; return tip."""
    work = tmp_path / f"_adv_{tag}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api" / "x.txt").write_text(f"api {tag}\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", tag)
    _git("-C", work, "push", "-q", "origin", "master")
    return _out("-C", work, "rev-parse", "HEAD")


def _clone_lib(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(parent, upstream, child) — a whole-repo `vendor/lib` binding."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    r = gf("-C", str(parent), "clone", str(up), "vendor/lib",
           check=False)
    if r.returncode != 0:
        pytest.fail(f"setup: clone rc={r.returncode}:\n"
                    f"{r.stdout}\n{r.stderr}")
    child = parent / "vendor" / "lib"
    if not (child / ".gf" / "git" / "HEAD").is_file():
        pytest.fail("setup: whole-repo child has no .gf/git gitdir")
    return parent, up, child


def _child_git(child: Path, *args,
               check: bool = True) -> subprocess.CompletedProcess:
    """git addressed at the whole-repo child's own gitdir + worktree."""
    return _git("--git-dir", str(child / ".gf" / "git"),
                "--work-tree", str(child), *args, check=check)


def _co_git(co, *args, check: bool = True) -> subprocess.CompletedProcess:
    """git addressed at a shared checkout's admin dir + worktree."""
    return _git("--git-dir", str(co.gitdir),
                "--work-tree", str(co.work_tree), *args, check=check)


def _cap_child(child: Path) -> dict:
    return checkout_snapshot(child / ".gf" / "git", child)


def _stash_list(child: Path) -> str:
    return _child_git(child, "stash", "list").stdout


def _commit_child(child: Path, name: str, content: str,
                  msg: str) -> str:
    """Commit `name` inside a whole-repo child via the documented
    `gf git` surface; return the new HEAD sha."""
    (child / name).write_text(content)
    gf("-C", str(child), "git", "add", name)
    gf("-C", str(child), "git", "commit", "-qm", msg)
    return _out("--git-dir", child / ".gf" / "git", "rev-parse", "HEAD")


def _retained(gitdir: Path, sha: str) -> str | None:
    """`refs/worktree/gf-retained-commits/<sha>` in this gitdir, or None."""
    r = _git("--git-dir", gitdir, "rev-parse", "--verify", "-q",
             f"refs/worktree/gf-retained-commits/{sha}", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def _reachable(gitdir: Path, sha: str, ref: str) -> bool:
    return _git("--git-dir", gitdir, "merge-base", "--is-ancestor",
                sha, ref, check=False).returncode == 0


# ---------------------------------------------------------------------------
# recovery — autostash pop conflict: the stash is never dropped by gf


def test_pull_autostash_pop_conflict_retains_named_stash(tmp_path):
    """recovery · whole-repo: the dirty edit collides with the upstream
    advance, so `stash pop --index` conflicts. `gf` exits nonzero inside
    a `gf:` envelope naming the binding and the retained stash — the
    `gf autostash` entry is still in the child's stash list (never
    dropped) — and reports the recovery. The manual recovery then
    reaches a preserved end: the user's resolution lands and the
    retained entry is consumed by the user's own git."""
    parent, up, child = _clone_lib(tmp_path)
    x = child / "docs" / "api" / "x.txt"
    x.write_text("local dirty\n")
    (child / "untracked.txt").write_text("untracked bytes\n")
    _advance(up, tmp_path, "adv")

    r = gf("-C", str(parent), "pull", "--autostash", check=False)

    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "gf:" in err, err
    assert "pull failed for git-folder 'lib'" in err, err
    assert "(vendor/lib)" in err, err
    # the report names the retained stash and the recovery
    assert "stash" in err.lower(), err
    assert "stash pop --index" in err, err

    # the stash entry is retained, named — the stashed work is not lost
    stash = _stash_list(child)
    assert "gf autostash" in stash, stash
    # the pop applied what it could: untracked work is back on disk
    assert (child / "untracked.txt").read_text() == "untracked bytes\n"
    # the update's effect is truthful — upstream content is present
    # (possibly inside conflict markers)
    assert "api adv" in x.read_text() or "<<<<<<<" in x.read_text()

    # the documented recovery reaches a preserved end: the user
    # resolves the conflicted file, marks it, and consumes the retained
    # stash entry with their own git (a literal second `stash pop
    # --index` re-conflicts deterministically — see the module
    # docstring's recovery-arm note).
    x.write_text("resolved: api adv + local dirty\n")
    _child_git(child, "add", "docs/api/x.txt")
    _child_git(child, "stash", "drop")
    assert _stash_list(child) == ""
    assert x.read_text() == "resolved: api adv + local dirty\n"
    # the retained work is staged, the update landed, nothing orphaned
    assert "docs/api/x.txt" in _child_git(
        child, "diff", "--cached", "--name-only").stdout


def test_pull_autostash_pop_conflict_retains_stash_shared_checkout(
        tmp_path):
    """recovery · subfolder: the same conflicted pop on a shared
    checkout retains the named `gf autostash` entry in the checkout's
    stash list — gf does not drop it — and resolve+drop reaches the
    preserved end through the checkout's own gitdir."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")
    co = layout.subfolder_checkout(parent, str(up), "master",
                                   "docs/api")

    x = parent / "vendor" / "api" / "x.txt"
    x.write_text("local dirty\n")
    _advance(up, tmp_path, "adv")

    r = gf("-C", str(parent), "pull", "--autostash", check=False)
    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "gf:" in err, err
    assert "stash" in err.lower(), err

    stash = _co_git(co, "stash", "list").stdout
    assert "gf autostash" in stash, stash

    x.write_text("resolved: api adv + local dirty\n")
    _co_git(co, "add", "docs/api/x.txt")
    _co_git(co, "stash", "drop")
    assert _co_git(co, "stash", "list").stdout == ""
    assert x.read_text() == "resolved: api adv + local dirty\n"


# ---------------------------------------------------------------------------
# recovery — update failure after the stash: never orphaned


def test_pull_autostash_fetch_failure_restores_stash(tmp_path):
    """recovery · whole-repo: the pull's fetch fails AFTER `stash push`
    (file transport denied inside the fixture) — the update-failure arm
    still runs `stash pop --index`, so the worktree holds the dirty
    bytes again, the index partition is restored, and the stash list is
    empty. Exit 2, `gf:` envelope, HEAD unmoved — truthful partial
    effect, no orphaned stash."""
    parent, up, child = _clone_lib(tmp_path)
    x = child / "docs" / "api" / "x.txt"
    x.write_text("dirty change\n")
    staged = child / "staged.txt"
    staged.write_text("staged bytes\n")
    gf("-C", str(child), "git", "add", "staged.txt")
    (child / "untracked.txt").write_text("untracked bytes\n")
    head_before = _out("--git-dir", child / ".gf" / "git",
                       "rev-parse", "HEAD")

    # `deny_file_transport` only touches the pinned $HOME gitconfig —
    # its monkeypatch parameter is unused, so None is a fine stand-in.
    with deny_file_transport(tmp_path, None):
        r = gf("-C", str(parent), "pull", "--autostash", check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "gf:" in err, err
    assert "pull failed for git-folder 'lib'" in err, err

    # stash restored, not orphaned — bytes and index partition back
    assert _stash_list(child) == "", _stash_list(child)
    assert x.read_text() == "dirty change\n"
    assert (child / "untracked.txt").read_text() == "untracked bytes\n"
    assert "staged.txt" in _child_git(
        child, "diff", "--cached", "--name-only").stdout
    assert _out("--git-dir", child / ".gf" / "git", "rev-parse",
                "HEAD") == head_before


# ---------------------------------------------------------------------------
# recovery — in-progress merge/rebase: refuse, never stack


def test_pull_refuses_during_merge_in_progress(tmp_path):
    """recovery · whole-repo: a mid-merge child (MERGE_HEAD + unmerged
    paths) makes the next pull refuse — the porcelain gate reports the
    conflicted state truthfully — and the in-progress merge is left for
    the user: MERGE_HEAD still present, conflicted bytes untouched,
    then `git merge --abort` restores a usable checkout."""
    parent, _up, child = _clone_lib(tmp_path)
    gf("-C", str(child), "git", "checkout", "-qb", "side")
    (child / "docs" / "api" / "x.txt").write_text("side edit\n")
    gf("-C", str(child), "git", "commit", "-qam", "side")
    gf("-C", str(child), "git", "checkout", "-q", "master")
    (child / "docs" / "api" / "x.txt").write_text("master edit\n")
    gf("-C", str(child), "git", "commit", "-qam", "master")
    m = gf("-C", str(child), "git", "merge", "side", check=False)
    if m.returncode == 0:
        pytest.fail("setup: expected merge conflict")
    gitdir = child / ".gf" / "git"
    if not (gitdir / "MERGE_HEAD").is_file():
        pytest.fail("setup: no in-progress merge")
    conflict_bytes = (child / "docs" / "api" / "x.txt").read_bytes()
    head_before = _out("--git-dir", gitdir, "rev-parse", "HEAD")
    porcelain_before = _child_git(child, "status", "--porcelain").stdout

    r = gf("-C", str(parent), "pull", check=False)
    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "gf:" in err, err
    # the refusal is truthful — the conflicted state is named, not a
    # stacked operation
    low = err.lower()
    assert ("dirty" in low or "uncommitted" in low or "merge" in low
            or "unmerged" in low), err

    # nothing stacked: the in-progress merge state is intact
    assert (gitdir / "MERGE_HEAD").is_file()
    assert _out("--git-dir", gitdir, "rev-parse", "HEAD") == head_before
    assert _child_git(child, "status", "--porcelain").stdout == \
        porcelain_before
    assert (child / "docs" / "api" / "x.txt").read_bytes() == \
        conflict_bytes

    # provenance usability: the user's own `merge --abort` recovers
    _child_git(child, "merge", "--abort")
    assert _child_git(child, "status", "--porcelain").stdout == ""


def test_pull_refuses_during_rebase_in_progress(tmp_path):
    """recovery · whole-repo: a mid-rebase child (rebase-merge/ state +
    conflicted file) refuses the next pull truthfully and the in-flight
    rebase survives for the user's own `--abort`."""
    parent, _up, child = _clone_lib(tmp_path)
    gf("-C", str(child), "git", "checkout", "-qb", "side")
    (child / "docs" / "api" / "x.txt").write_text("side edit\n")
    gf("-C", str(child), "git", "commit", "-qam", "side")
    gf("-C", str(child), "git", "checkout", "-q", "master")
    (child / "docs" / "api" / "x.txt").write_text("master edit\n")
    gf("-C", str(child), "git", "commit", "-qam", "master")
    rb = gf("-C", str(child), "git", "rebase", "side", check=False)
    if rb.returncode == 0:
        pytest.fail("setup: expected rebase to stop on conflict")
    gitdir = child / ".gf" / "git"
    in_progress = (gitdir / "rebase-merge").is_dir() or \
        (gitdir / "rebase-apply").is_dir()
    if not in_progress:
        pytest.fail("setup: no in-progress rebase markers")
    head_before = _out("--git-dir", gitdir, "rev-parse", "HEAD")
    porcelain_before = _child_git(child, "status",
                                  "--porcelain").stdout

    r = gf("-C", str(parent), "pull", check=False)
    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "gf:" in err, err

    # the in-progress rebase state is untouched
    assert (gitdir / "rebase-merge").is_dir() or \
        (gitdir / "rebase-apply").is_dir()
    assert _out("--git-dir", gitdir, "rev-parse", "HEAD") == head_before
    assert _child_git(child, "status", "--porcelain").stdout == \
        porcelain_before

    _child_git(child, "rebase", "--abort")
    assert _child_git(child, "status", "--porcelain").stdout == ""


# ---------------------------------------------------------------------------
# recovery — the retention ref guards every strand-risk transition


def test_pull_detached_head_move_writes_retention_ref(tmp_path):
    """recovery · whole-repo: local commits on a detached HEAD carry no
    ref. A pull that moves HEAD off them (attach to `master` + ff) would
    strand them — GF-D23 requires the per-checkout retention ref
    `refs/worktree/gf-retained-commits/<sha>` to hold the outgoing HEAD. After the
    pull the commits are durably reachable through it, never
    reflog-only."""
    parent, _up, child = _clone_lib(tmp_path)
    gf("-C", str(child), "git", "checkout", "-q", "--detach", "HEAD")
    stranded = _commit_child(child, "detached.txt", "detached work\n",
                             "detached commit")
    gitdir = child / ".gf" / "git"
    # sanity: the commit is reachable from nothing but the detached
    # HEAD — no named ref contains it (`for-each-ref` lists real refs
    # only; `branch --contains` would echo the HEAD-detached pseudo
    # entry)
    assert _out("--git-dir", gitdir, "for-each-ref",
                "--contains", stranded,
                "--format=%(refname)").strip() == ""

    _advance(_up, tmp_path, "adv")
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    assert _retained(gitdir, stranded) == stranded, (
        "expected immutable retention ref =", stranded,
        "got", _retained(gitdir, stranded))
    # the stranded commit is durably reachable through the retention ref
    assert _reachable(gitdir, stranded, f"refs/worktree/gf-retained-commits/{stranded}")
    # intended effect: attached to master at the new tip
    assert _out("--git-dir", gitdir, "rev-parse",
                "--abbrev-ref", "HEAD") == "master"
    assert (child / "docs" / "api" / "x.txt").read_text() == "api adv\n"
    # the stranded commit's bytes are usable provenance — the file does
    # not belong to master's tree, so it correctly leaves the worktree;
    # it must remain recoverable through the retained commit.
    assert _out("--git-dir", gitdir, "cat-file", "-p",
                f"{stranded}:detached.txt") == "detached work"


def test_pull_rebase_writes_retention_ref_for_outgoing_tip(tmp_path):
    """recovery · whole-repo: `gf pull --rebase` on a diverged branch is
    the explicit recovery — the rebase replays the local commit onto the
    new tip and the outgoing tip is retained under
    `refs/worktree/gf-retained-commits/<sha>` (a rebase is a strand-risk transition)."""
    parent, _up, child = _clone_lib(tmp_path)
    old_tip = _commit_child(child, "local.txt", "local commit\n",
                            "local work")
    new_tip = _advance(_up, tmp_path, "adv")
    gitdir = child / ".gf" / "git"

    r = gf("-C", str(parent), "pull", "--rebase", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    # the rebase landed: HEAD contains both the upstream tip and the
    # replayed local change
    assert _reachable(gitdir, new_tip, "HEAD")
    assert (child / "local.txt").read_text() == "local commit\n"
    assert _out("--git-dir", gitdir, "rev-parse", "HEAD") != old_tip
    # the outgoing (pre-rebase) tip is durably retained
    assert _retained(gitdir, old_tip) == old_tip
    assert _reachable(gitdir, old_tip, f"refs/worktree/gf-retained-commits/{old_tip}")
