# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""P1 preservation witnesses for `gf pull` — completion, refusal, and
fetch-namespace classes (Change DC-DOC-PLAN-007, obligations O-update /
O-refusal / O-fetch / O-inspect).

Oracle: docs/gf-testing.md "Preservation witness oracle". Every witness
captures the protected surface through real `git` before the stimulus,
drives `gf pull` through the real `python -m gf` entry — the in-process
`_LogBackend` driver is used only where the call log itself is the
observation ("one fetch per store", "every fetch carries --no-tags",
"no `checkout -B`/`-f` is ever issued") and still executes real git via
GitCliBackend — then re-observes. Fixture-minted SHAs and bytes are the
exact-comparison source.

Expected behavior derives only from the admitted documents:

- gf-spec.md `gf pull`: a dirty worktree refuses with exit 3 unless
  `--autostash` (`stash push -u` / `pop --index` restores the index
  partition after success AND after every post-stash failure); branch
  bindings integrate `origin/<branch>` fast-forward-only — strictly
  behind advances, up-to-date is a no-op, strictly ahead keeps its local
  commits, divergence refuses with the truthful ahead/behind state and
  the deliberate recoveries; `--rebase` is the explicit recovery;
  `--force` is gone entirely; a checkout that would strand local commits
  must leave them durably reachable or refuse before it runs.
- gf-spec.md "Fetch refspecs": every `gf` fetch passes `--no-tags`; `+`
  (force) is permitted only into `refs/remotes/origin/*`; a needed tag's
  appended line is non-forced so a moved upstream tag is declined and
  surfaced instead of overwriting `refs/tags/*`; a pre-existing `+` line
  landing outside the mirror namespace is unsafe legacy state that
  refuses before any fetch (gf-arch.md GF-D17 `_assert_safe_refspecs`).
- gf-spec.md `gf status --remote` + "Drift algorithm": truthful
  `clean`/`ahead`/`behind`/`diverged`/`missing` plus `-dirty` suffixes,
  computed from local remote-tracking refs only.
- gf-spec.md "Error handling": exit 1 validation/refused update
  (diverged branch, moved tag), 2 network/git failure, 3 dirty
  worktree; a `gf:` envelope names the folder's name, path, and the
  failed operation.
- gf-arch.md GF-D16 / GF-D23 + P1 gate: `merge --ff-only` attach
  integration; `checkout -B`/`checkout -f`/`pull --force` are gone — an
  unsafe transition refuses rather than falling back to them; before a
  transition that could orphan the outgoing HEAD (and on the diverged
  refusal) the per-checkout retention ref `refs/worktree/gf-retained`
  holds the outgoing HEAD.

Nothing in this file consults `src/gf/` to decide expected behavior.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from conftest import checkout_snapshot, gf, git
from gf import cli, layout
from gf.backends import GitCliBackend


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
    """Bare upstream: docs/api/x.txt + tools/t.txt + .gitignore
    (`ignored.txt`) on master, tag v1 on the init commit."""
    up = tmp_path / name
    _git("init", "-q", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text("api on master\n")
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text("tool on master\n")
    (work / ".gitignore").write_text("ignored.txt\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")
    _git("-C", work, "tag", "v1")
    _git("-C", work, "push", "-q", "origin", "v1")
    return up


def _advance(up: Path, tmp_path: Path, tag: str = "adv") -> str:
    """Push one commit to upstream master touching x.txt and t.txt;
    return the pushed tip sha."""
    work = tmp_path / f"_adv_{tag}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api" / "x.txt").write_text(f"api {tag}\n")
    (work / "tools" / "t.txt").write_text(f"tool {tag}\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", tag)
    _git("-C", work, "push", "-q", "origin", "master")
    return _out("-C", work, "rev-parse", "HEAD")


def _retag(up: Path, tmp_path: Path, tag: str) -> str:
    """Move upstream tag `tag` to a fresh commit; return its new sha."""
    work = tmp_path / f"_retag_{tag}"
    _git("clone", "-q", str(up), str(work))
    _git("-C", work, "checkout", "-q", "-b", f"side-{tag}")
    (work / "docs" / "api" / f"{tag}-moved.txt").write_text("moved\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", f"moved {tag}")
    sha = _out("-C", work, "rev-parse", "HEAD")
    _git("-C", work, "tag", "-f", tag)
    _git("-C", work, "push", "-qf", "origin", tag)
    return sha


def _clone_lib(tmp_path: Path, *extra: str) -> tuple[Path, Path, Path]:
    """(parent, upstream, child) — a whole-repo `vendor/lib` binding."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    r = gf("-C", str(parent), "clone", str(up), "vendor/lib",
           *extra, check=False)
    if r.returncode != 0:
        pytest.fail(f"setup: clone rc={r.returncode}:\n"
                    f"{r.stdout}\n{r.stderr}")
    child = parent / "vendor" / "lib"
    if not (child / ".gf" / "git" / "HEAD").is_file():
        pytest.fail("setup: whole-repo child has no .gf/git gitdir")
    return parent, up, child


def _clone_pair(parent: Path, up: Path) -> None:
    """Two subfolder bindings of one upstream sharing the master
    checkout of one repo store."""
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")


def _co(parent: Path, up: Path, key: str, subdir: str = "docs/api"):
    return layout.subfolder_checkout(parent, str(up), key, subdir)


def _store(parent: Path, up: Path) -> Path:
    return layout.repo_store(parent, str(up))


def _child_git(child: Path, *args) -> subprocess.CompletedProcess:
    """git addressed at the whole-repo child's own gitdir + worktree."""
    return _git("--git-dir", str(child / ".gf" / "git"),
                "--work-tree", str(child), *args)


def _co_git(co, *args) -> subprocess.CompletedProcess:
    """git addressed at a shared checkout's admin dir + worktree."""
    return _git("--git-dir", str(co.gitdir),
                "--work-tree", str(co.work_tree), *args)


def _cap_child(child: Path) -> dict:
    return checkout_snapshot(child / ".gf" / "git", child)


def _cap_co(co) -> dict:
    return checkout_snapshot(co.gitdir, co.work_tree)


def _head_of(gitdir: Path) -> str:
    return _out("--git-dir", gitdir, "rev-parse", "HEAD")


def _retained(gitdir: Path) -> str | None:
    """`refs/worktree/gf-retained` in this gitdir, or None."""
    r = _git("--git-dir", gitdir, "rev-parse", "--verify", "-q",
             "refs/worktree/gf-retained", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def _stash_list(child: Path) -> str:
    return _child_git(child, "stash", "list").stdout


def _reachable(gitdir: Path, sha: str, ref: str) -> bool:
    """`sha` is an ancestor of `ref` in `gitdir` (reachability probe)."""
    return _git("--git-dir", gitdir, "merge-base", "--is-ancestor",
                sha, ref, check=False).returncode == 0


def _commit_child(child: Path, name: str, content: str,
                  msg: str) -> str:
    """Commit `name` inside a whole-repo child; return the new HEAD."""
    (child / name).write_text(content)
    gf("-C", str(child), "git", "add", name)
    gf("-C", str(child), "git", "commit", "-qm", msg)
    return _out("--git-dir", child / ".gf" / "git", "rev-parse", "HEAD")


class _LogBackend(GitCliBackend):
    """Real-git backend recording (argv, kwargs) of every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict]] = []

    def git(self, *args, **kwargs):
        self.calls.append((tuple(map(str, args)), kwargs))
        return super().git(*args, **kwargs)


def _run(parent: Path, capsys, *args, backend=None):
    """In-process `gf -C <parent> <args>` on the real fs (real git)."""
    old = Path.cwd()
    try:
        try:
            code = cli.main(["-C", str(parent), *map(str, args)],
                            backend=backend)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    finally:
        os.chdir(old)
    return code or 0, capsys.readouterr().out


def _forbidden_transitions(calls) -> list[tuple]:
    """Calls carrying work-losing transitions GF-D16 removed:
    `checkout -B`, `checkout -f`, `reset --hard`."""
    bad = []
    for args, _kw in calls:
        if "-B" in args:
            bad.append(args)
        elif args[:1] == ("checkout",) and "-f" in args:
            bad.append(args)
        elif args[:1] == ("reset",) and "--hard" in args:
            bad.append(args)
    return bad


def _envelope(r, name: str, path: str, op: str = "pull") -> str:
    """Assert the `gf:` refusal envelope names folder name, path, op;
    return combined output for further checks."""
    err = r.stderr + r.stdout
    assert "gf:" in err, err
    assert f"{op} failed for git-folder '{name}'" in err, err
    assert f"({path})" in err, err
    return err


# ---------------------------------------------------------------------------
# completion — strictly-behind child fast-forwards preserving all work


def test_pull_behind_clean_child_fast_forwards(tmp_path):
    """completion · whole-repo · all five categories.

    A strictly-behind clean child advances to the upstream tip by
    fast-forward; a fixture-authored ignored file (not reported by
    `status --porcelain`, so not "dirty") survives in place; the mirror
    ref lands at the new tip; the child stays an ordinary repository
    whose history answers normally.
    """
    parent, up, child = _clone_lib(tmp_path)
    ignored = child / "ignored.txt"          # covered by upstream .gitignore
    ignored.write_text("fixture-authored ignored bytes\n")
    before = _cap_child(child)
    old_head = before["head"]
    new_tip = _advance(up, tmp_path, "adv")

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    after = _cap_child(child)
    # intended effect — a real fast-forward of the local branch
    assert after["head"] == new_tip
    assert _reachable(child / ".gf" / "git", old_head, "HEAD")
    assert _out("--git-dir", child / ".gf" / "git", "rev-parse",
                "refs/heads/master") == new_tip
    # protected bytes: ignored file and all fixture files intact; the
    # upstream update is the only byte change
    assert after["bytes"]["ignored.txt"] == \
        before["bytes"]["ignored.txt"]
    assert (child / "docs" / "api" / "x.txt").read_text() == "api adv\n"
    # index/stash: clean partition, no residue
    assert after["porcelain"] == ""
    assert after["stash"] == ""
    # repository identity: the child's own gitdir, real upstream origin
    assert _out("--git-dir", child / ".gf" / "git", "config", "--get",
                "remote.origin.url") == str(up)
    assert not (child / ".git").exists()
    # provenance usability: history answers normally on the result
    assert _out("--git-dir", child / ".gf" / "git", "log", "--oneline",
                "-1")


def test_pull_behind_integrates_with_merge_ff_only(tmp_path, capsys):
    """completion · mechanism witness: the integration issues
    `merge --ff-only` and never a work-losing `checkout -B`, `checkout
    -f`, or `reset --hard` (GF-D16; the forbidden spellings would drop
    local commits to reflog-only reachability or discard dirty bytes).
    Every fetch carries `--no-tags` (GF-D17)."""
    parent, _up, _child = _clone_lib(tmp_path)
    _advance(_up, tmp_path, "adv")

    rec = _LogBackend()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code == 0

    merges = [c for c in rec.calls if "merge" in c[0]]
    assert any("--ff-only" in c[0] for c in merges), \
        [c[0] for c in rec.calls]
    fetches = [c for c in rec.calls if "fetch" in c[0]]
    assert fetches, "expected at least one fetch"
    assert all("--no-tags" in c[0] for c in fetches), \
        [c[0] for c in fetches]
    assert _forbidden_transitions(rec.calls) == [], \
        _forbidden_transitions(rec.calls)


def test_pull_autostash_restores_index_partition_and_work(tmp_path):
    """completion · whole-repo: `--autostash` over staged + unstaged +
    untracked + ignored work. The update applies, `stash pop --index`
    restores the staged/unstaged partition (the staged file is still
    staged, not merely present), every work kind's bytes survive, and no
    `gf autostash` entry is left behind."""
    parent, up, child = _clone_lib(tmp_path)

    staged = child / "docs" / "api" / "staged.txt"
    staged.write_text("staged bytes\n")
    gf("-C", str(child), "git", "add", "docs/api/staged.txt")
    # unstaged edit on a file the upstream advance does not touch, so
    # the pop does not conflict
    (child / ".gitignore").write_text("ignored.txt\nlocal-edit\n")
    (child / "untracked.txt").write_text("untracked bytes\n")
    ignored = child / "ignored.txt"
    ignored.write_text("ignored bytes\n")

    _advance(up, tmp_path, "adv")
    r = gf("-C", str(parent), "pull", "--autostash", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    # intended effect applied
    assert (child / "docs" / "api" / "x.txt").read_text() == "api adv\n"
    # index partition restored: the staged file is still STAGED
    staged_names = _child_git(
        child, "diff", "--cached", "--name-only").stdout
    assert "docs/api/staged.txt" in staged_names.splitlines(), \
        staged_names
    porcelain = _child_git(child, "status", "--porcelain").stdout
    assert re.search(r"^A  docs/api/staged\.txt$", porcelain, re.M), \
        porcelain
    assert re.search(r"^ M \.gitignore$", porcelain, re.M), porcelain
    assert re.search(r"^\?\? untracked\.txt$", porcelain, re.M), \
        porcelain
    # bytes survived
    assert staged.read_text() == "staged bytes\n"
    assert (child / ".gitignore").read_text() == \
        "ignored.txt\nlocal-edit\n"
    assert (child / "untracked.txt").read_text() == "untracked bytes\n"
    assert ignored.read_text() == "ignored bytes\n"
    # no orphaned autostash
    assert _stash_list(child) == "", _stash_list(child)


def test_pull_ahead_child_keeps_local_commits(tmp_path):
    """completion · whole-repo: a strictly-ahead local branch is an
    up-to-date no-op — HEAD and `refs/heads/master` do not move, the
    local commit stays reachable from the branch, and `status --remote`
    reports `ahead`."""
    parent, up, child = _clone_lib(tmp_path)
    local_sha = _commit_child(child, "local.txt", "local commit\n",
                              "local work")
    before = _cap_child(child)

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    after = _cap_child(child)
    assert after["head"] == local_sha
    assert after["refs"]["refs/heads/master"] == local_sha
    assert _reachable(child / ".gf" / "git", local_sha,
                      "refs/heads/master")
    assert after["porcelain"] == before["porcelain"] == ""
    # drift reports the truthful relation
    s = gf("-C", str(parent), "status", "--remote", check=False)
    assert s.returncode == 0, s.stderr
    assert re.search(
        r"lib\s+" + re.escape(str(up)) + r"\s+\[master\]\s+ahead(\s|$)",
        s.stdout), s.stdout


# ---------------------------------------------------------------------------
# completion — shared-checkout (subfolder) form


def test_pull_shared_checkout_one_fetch_per_store(tmp_path, capsys):
    """completion · subfolder: two bindings on one repo store are
    updated by exactly one store fetch — which carries `--no-tags` —
    and one apply per shared checkout; both bindings serve the new tip."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    _advance(up, tmp_path, "adv")

    rec = _LogBackend()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out

    store = _store(parent, up)
    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, f"fetches: {[c[0] for c in fetches]}"
    assert all("--no-tags" in c[0] for c in fetches), \
        [c[0] for c in fetches]
    # all fetches in the pull carry --no-tags (child paths included)
    assert all("--no-tags" in c[0]
               for c in rec.calls if "fetch" in c[0])
    assert _forbidden_transitions(rec.calls) == [], \
        _forbidden_transitions(rec.calls)

    assert (parent / "vendor" / "api" / "x.txt").read_text() == \
        "api adv\n"
    assert (parent / "vendor" / "tools" / "t.txt").read_text() == \
        "tool adv\n"


def test_pull_shared_checkout_autostash_covers_whole_checkout(tmp_path):
    """completion · subfolder: `--autostash` acts on the whole shared
    checkout — a staged file inside one mapping, an unstaged edit in a
    cone-materialized root file, and an untracked file physically
    outside every mapped subdir all stash and restore; both served
    bindings move together; the checkout's stash list ends empty."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    co = _co(parent, up, "master")

    staged = parent / "vendor" / "api" / "staged.txt"
    staged.write_text("staged in api\n")
    gf("-C", str(parent / "vendor" / "api"), "git", "add", "staged.txt")
    # cone mode materializes repo-root files; edit one there
    (co.work_tree / ".gitignore").write_text("ignored.txt\nedit\n")
    # untracked dirt physically inside the checkout but outside the cone
    root_dirt = co.work_tree / "root-dirty.txt"
    root_dirt.write_text("uncommitted at checkout root\n")

    _advance(up, tmp_path, "adv")
    r = gf("-C", str(parent), "pull", "--autostash", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled api" in r.stdout
    assert "Pulled tools" in r.stdout

    # the index partition survived the stash round-trip
    cached = _co_git(co, "diff", "--cached", "--name-only").stdout
    assert "docs/api/staged.txt" in cached.splitlines(), cached
    # every work kind restored — including the out-of-cone dirt
    assert staged.read_text() == "staged in api\n"
    assert (co.work_tree / ".gitignore").read_text() == \
        "ignored.txt\nedit\n"
    assert root_dirt.read_text() == "uncommitted at checkout root\n"
    assert _co_git(co, "stash", "list").stdout == ""
    # intended effect: both bindings serve the advanced tip
    assert (parent / "vendor" / "api" / "x.txt").read_text() == \
        "api adv\n"
    assert (parent / "vendor" / "tools" / "t.txt").read_text() == \
        "tool adv\n"


# ---------------------------------------------------------------------------
# refusal — dirty worktree, both binding forms


def test_pull_dirty_refuses_unchanged_whole_repo(tmp_path):
    """refusal · whole-repo: dirty child, no `--autostash` → exit 3
    inside a `gf:` envelope naming name/path/operation; bytes, refs,
    and the index/stash partition are byte-identical to the pre-state
    (the dirty check precedes the fetch, so even the mirror is
    untouched); no stash entry was ever taken."""
    parent, _up, child = _clone_lib(tmp_path)
    (child / "docs" / "api" / "x.txt").write_text("dirty change\n")
    staged = child / "staged.txt"
    staged.write_text("staged\n")
    gf("-C", str(child), "git", "add", "staged.txt")
    (child / "untracked.txt").write_text("untracked\n")
    before = _cap_child(child)

    _advance(_up, tmp_path, "adv")
    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    err = _envelope(r, "lib", "vendor/lib")
    # the refusal names the actual condition (governed wording, not a
    # literal pin)
    assert "dirty" in err.lower() or "uncommitted" in err.lower(), err

    after = _cap_child(child)
    assert after["bytes"] == before["bytes"]
    assert after["porcelain"] == before["porcelain"]
    assert after["staged"] == before["staged"]
    assert after["head"] == before["head"]
    assert after["refs"] == before["refs"]   # fetch never ran
    assert after["stash"] == ""


def test_pull_dirty_refuses_all_served_bindings_shared_checkout(
        tmp_path):
    """refusal · subfolder: one dirty file inside one mapped subdir
    makes the shared checkout refuse for every binding it serves —
    exit 3, no `Pulled` line for either binding, the checkout's HEAD
    and both bindings' bytes are unchanged, the dirt survives."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    co = _co(parent, up, "master")
    before = _cap_co(co)
    (parent / "vendor" / "api" / "dirty.txt").write_text(
        "uncommitted\n")

    _advance(up, tmp_path, "adv")
    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    err = r.stderr + r.stdout
    assert "gf:" in err, err
    assert "dirty" in err.lower() or "uncommitted" in err.lower(), err
    assert "Pulled" not in r.stdout, r.stdout

    after = _cap_co(co)
    assert after["head"] == before["head"]
    assert after["refs"]["refs/heads/master"] == \
        before["refs"]["refs/heads/master"]
    assert (parent / "vendor" / "api" / "dirty.txt").read_text() == \
        "uncommitted\n"
    assert (parent / "vendor" / "api" / "x.txt").read_text() == \
        "api on master\n"
    assert (parent / "vendor" / "tools" / "t.txt").read_text() == \
        "tool on master\n"


# ---------------------------------------------------------------------------
# refusal — diverged branch


def test_pull_diverged_refuses_truthful_and_retains_head(tmp_path):
    """refusal · whole-repo: a diverged local branch refuses with exit
    1 — the envelope names the folder and reports the truthful
    ahead/behind state plus the deliberate recoveries (`--rebase`);
    HEAD, the local branch, and the index are untouched, the local
    commit stays reachable from `master`, and the retention ref
    `refs/worktree/gf-retained` holds the outgoing HEAD (gf-arch.md
    failure-modes row)."""
    parent, up, child = _clone_lib(tmp_path)
    local_sha = _commit_child(child, "local.txt", "local commit\n",
                              "local work")
    _advance(up, tmp_path, "adv")
    before = _cap_child(child)

    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    err = _envelope(r, "lib", "vendor/lib")
    low = err.lower()
    # the refusal names the true relation, not a generic failure
    assert "ahead" in low and "behind" in low or "diverg" in low, err
    # the deliberate recoveries are reported
    assert "rebase" in low, err

    after = _cap_child(child)
    # user refs and HEAD unmoved; the mirror ref may have advanced —
    # the fetch is declared coverage and is what makes the count true
    assert after["head"] == before["head"]
    assert after["refs"]["refs/heads/master"] == \
        before["refs"]["refs/heads/master"]
    assert _reachable(child / ".gf" / "git", local_sha,
                      "refs/heads/master")
    assert after["porcelain"] == before["porcelain"] == ""
    assert after["stash"] == ""
    # retention ref holds the outgoing HEAD
    assert _retained(child / ".gf" / "git") == local_sha, (
        "refs/worktree/gf-retained:", _retained(child / ".gf" / "git"))
    # the truthful drift class follows
    s = gf("-C", str(parent), "status", "--remote", check=False)
    assert s.returncode == 0, s.stderr
    assert re.search(
        r"lib\s+" + re.escape(str(up)) +
        r"\s+\[master\]\s+diverged(\s|$)", s.stdout), s.stdout


# ---------------------------------------------------------------------------
# refusal — `--force` is gone entirely


def test_pull_force_flag_is_gone(tmp_path):
    """refusal · whole-repo: `gf pull --force` no longer exists — the
    flag is rejected before any git work runs and a dirty child's
    bytes/refs/index stay byte-identical."""
    parent, _up, child = _clone_lib(tmp_path)
    (child / "docs" / "api" / "x.txt").write_text("dirty change\n")
    before = _cap_child(child)

    r = gf("-C", str(parent), "pull", "--force", check=False)

    assert r.returncode != 0
    err = (r.stderr + r.stdout).lower()
    assert "force" in err or "unrecognized" in err, (
        r.returncode, r.stdout, r.stderr)
    assert "Pulled" not in r.stdout
    after = _cap_child(child)
    assert after["bytes"] == before["bytes"]
    assert after["refs"] == before["refs"]
    assert after["porcelain"] == before["porcelain"]
    assert after["stash"] == ""


# ---------------------------------------------------------------------------
# refusal — forced legacy refspec lands outside the mirror namespace


def test_pull_forced_refspec_refuses_before_fetch_whole_repo(tmp_path):
    """refusal · whole-repo: a pre-existing `+refs/tags/*:refs/tags/*`
    line in the child gitdir is unsafe legacy state — `gf pull` refuses
    before any fetch (the mirror tip is unmoved although upstream
    advanced), the offending line is left verbatim (never rewritten),
    and nothing else changes (GF-D17 `_assert_safe_refspecs`)."""
    parent, up, child = _clone_lib(tmp_path)
    gitdir = child / ".gf" / "git"
    _child_git(child, "config", "--add", "remote.origin.fetch",
               "+refs/tags/*:refs/tags/*")
    before = _cap_child(child)
    mirror_before = _out("--git-dir", gitdir, "rev-parse",
                         "refs/remotes/origin/master")

    _advance(up, tmp_path, "adv")
    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    err = _envelope(r, "lib", "vendor/lib")
    assert "refspec" in err.lower() or "+refs" in err, err

    # refused BEFORE the fetch: the mirror tip did not advance
    assert _out("--git-dir", gitdir, "rev-parse",
                "refs/remotes/origin/master") == mirror_before
    # the unsafe line is retained verbatim, never silently rewritten
    lines = _out("--git-dir", gitdir, "config", "--get-all",
                 "remote.origin.fetch").splitlines()
    assert "+refs/tags/*:refs/tags/*" in lines, lines
    after = _cap_child(child)
    assert after["bytes"] == before["bytes"]
    assert after["head"] == before["head"]
    assert after["refs"]["refs/heads/master"] == \
        before["refs"]["refs/heads/master"]
    assert after["porcelain"] == before["porcelain"] == ""


def test_pull_forced_refspec_refuses_before_fetch_store(tmp_path):
    """refusal · subfolder: the same unsafe `+` line seeded into the
    shared repo store's config refuses the pull for the bindings the
    store serves — before any fetch — and is never rewritten."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    store = _store(parent, up)
    _git("--git-dir", store, "config", "--add", "remote.origin.fetch",
         "+refs/tags/*:refs/tags/*")
    co = _co(parent, up, "master")
    head_before = _head_of(co.gitdir)
    mirror_before = _out("--git-dir", store, "rev-parse",
                         "refs/remotes/origin/master")

    _advance(up, tmp_path, "adv")
    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    err = r.stderr + r.stdout
    assert "gf:" in err, err
    assert "refspec" in err.lower() or "+refs" in err, err
    assert "Pulled" not in r.stdout

    assert _out("--git-dir", store, "rev-parse",
                "refs/remotes/origin/master") == mirror_before
    lines = _out("--git-dir", store, "config", "--get-all",
                 "remote.origin.fetch").splitlines()
    assert "+refs/tags/*:refs/tags/*" in lines, lines
    assert _head_of(co.gitdir) == head_before
    assert (parent / "vendor" / "api").is_symlink()


# ---------------------------------------------------------------------------
# refusal — moved upstream tag


def test_pull_moved_upstream_tag_refuses_keeps_local_tag(tmp_path):
    """refusal · whole-repo: a pinned-tag binding whose upstream tag
    moved refuses the update — the non-forced `refs/tags/<ref>` line
    declines the fetch of the moved tag, `gf` surfaces it instead of
    overwriting the user's `refs/tags/*`, and the local tag bytes
    stay."""
    parent, up, child = _clone_lib(tmp_path, "-b", "v1")
    gitdir = child / ".gf" / "git"
    tag_before = _out("--git-dir", gitdir, "rev-parse", "refs/tags/v1")
    head_before = _head_of(gitdir)

    _retag(up, tmp_path, "v1")
    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    err = _envelope(r, "lib", "vendor/lib")
    assert "v1" in err or "tag" in err.lower(), err

    # local tag bytes unchanged — never force-moved
    assert _out("--git-dir", gitdir, "rev-parse",
                "refs/tags/v1") == tag_before
    assert _head_of(gitdir) == head_before
    # the coverage line that carries the tag is non-forced
    lines = _out("--git-dir", gitdir, "config", "--get-all",
                 "remote.origin.fetch").splitlines()
    assert any(line == "refs/tags/v1:refs/tags/v1" for line in lines), \
        lines
    assert not any(line.startswith("+refs/tags/") for line in lines), \
        lines


def test_pull_moved_upstream_tag_refuses_store(tmp_path):
    """refusal · subfolder: a `ref=v1` binding's store carries the
    non-forced tag line; a moved upstream tag refuses the pull and the
    store's `refs/tags/v1` stays at the recorded sha — the checkout's
    detached HEAD is untouched."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api",
       "-b", "v1")
    store = _store(parent, up)
    tag_before = _out("--git-dir", store, "rev-parse", "refs/tags/v1")
    co = _co(parent, up, "ref=v1")
    head_before = _head_of(co.gitdir)

    _retag(up, tmp_path, "v1")
    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    err = r.stderr + r.stdout
    assert "gf:" in err, err

    assert _out("--git-dir", store, "rev-parse",
                "refs/tags/v1") == tag_before
    lines = _out("--git-dir", store, "config", "--get-all",
                 "remote.origin.fetch").splitlines()
    assert any(line == "refs/tags/v1:refs/tags/v1" for line in lines), \
        lines
    assert not any(line.startswith("+refs/tags/") for line in lines), \
        lines
    assert _head_of(co.gitdir) == head_before


# ---------------------------------------------------------------------------
# inspection — the drift vocabulary is the truthful history relation


def _drift_state(parent: Path, name: str, url: Path) -> str:
    r = gf("-C", str(parent), "status", "--remote", check=False)
    assert r.returncode == 0, r.stderr
    m = re.search(
        rf"{name}\s+{re.escape(str(url))}\s+\[(\w*)\]\s+(\S+)",
        r.stdout)
    assert m, r.stdout
    return m.group(2)


def test_status_remote_truthful_drift_vocabulary(tmp_path):
    """O-inspect witness · whole-repo: `status --remote` reports the
    admitted vocabulary — `clean`, `local-dirty`, `behind`,
    `behind-dirty`, `ahead`, `ahead-dirty`, `diverged`,
    `diverged-dirty` — each the truthful rev-list relation plus the
    dirty suffix (gf-spec.md Drift algorithm)."""
    parent, up, child = _clone_lib(tmp_path)

    assert _drift_state(parent, "lib", up) == "clean"

    (child / "docs" / "api" / "x.txt").write_text("dirty\n")
    assert _drift_state(parent, "lib", up) == "local-dirty"
    _child_git(child, "checkout", "--", "docs/api/x.txt")

    _advance(up, tmp_path, "adv")
    gf("-C", str(child), "git", "fetch", "-q", "origin")
    assert _drift_state(parent, "lib", up) == "behind"

    (child / "docs" / "api" / "x.txt").write_text("dirty\n")
    assert _drift_state(parent, "lib", up) == "behind-dirty"
    _child_git(child, "checkout", "--", "docs/api/x.txt")

    _commit_child(child, "local.txt", "local\n", "local work")
    assert _drift_state(parent, "lib", up) == "diverged"

    (child / "docs" / "api" / "x.txt").write_text("dirty\n")
    assert _drift_state(parent, "lib", up) == "diverged-dirty"
    _child_git(child, "checkout", "--", "docs/api/x.txt")

    # a second child, clean local-ahead, then ahead-dirty
    up2 = _upstream(tmp_path, "upstream2")
    gf("-C", str(parent), "clone", str(up2), "vendor/lib2")
    child2 = parent / "vendor" / "lib2"
    _commit_child(child2, "local.txt", "local\n", "local work")
    assert _drift_state(parent, "lib2", up2) == "ahead"
    (child2 / "docs" / "api" / "x.txt").write_text("dirty\n")
    assert _drift_state(parent, "lib2", up2) == "ahead-dirty"
