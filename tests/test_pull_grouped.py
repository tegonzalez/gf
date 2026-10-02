# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`gf pull` grouped composition + init-child first-pull conversion.

Exercised on the real filesystem through `cli.main`: the command surface
is live; the grouping behaviors below are what this file pins.

Row scope (verbatim): ``cmd_pull``/``shelf`` partition selected bindings
by resolved form — whole-repo keeps existing per-child order
(dirty-check → fetch → apply); subfolder groups by ``co.common_dir`` →
one fetch per store (ensure-store + refspec coverage first), groups by
checkout key → one dirty check / --autostash / apply per
checkout → per-binding link create/retarget + state + one output line
per binding served + "(moved with X)" marks; override repo-URL change →
new store; key change → retarget link; retarget drops the moved binding
from the vacated checkout's recorded bindings; init-child conversion:
``rev-parse --verify HEAD`` rc + files-other-than-``.gf`` test → empty
converts, else refuse deleting nothing.

Authorities: spec `gf pull` (L239-243) + "Update algorithm" (L485-499) +
`gf init` (L301-315) + "URL resolution" record rules + constraint L546;
arch GF-D10/D11/D12/D13; plan D10.

Call logs use a wrapped real GitCliBackend in-process (`cli.main(...,
backend=rec)`) on the real filesystem — the same observable contract as
the mock-backend call log, without the fake-fs divergences the mock
model shows for shared-store layouts (probe: `ref=latest` keying, strict
re-lock on re-add).
"""

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from conftest import gf, git
from gf import cli, layout, shelf
from gf.backends import GitCliBackend


# ---------------------------------------------------------------------------
# helpers


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} rc={r.returncode}:\n{r.stderr}")
    return r


def _out(*args) -> str:
    return _git(*args).stdout.strip()


class _LogBackend(GitCliBackend):
    """Real-git backend recording (argv, kwargs) of every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict]] = []

    def git(self, *args, **kwargs):
        self.calls.append((tuple(map(str, args)), kwargs))
        return super().git(*args, **kwargs)


def _run(parent: Path, capsys, *args, backend=None):
    """In-process `gf -C <parent> <args>` on the real fs.

    Returns (exit_code, stdout). `-C` chdirs inside cli.main; restore.
    """
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


def _calls_matching(rec: _LogBackend, *verbs) -> list:
    return [c for c in rec.calls if any(v in c[0] for v in verbs)]


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str = "upstream",
              marker: str = "api on master") -> Path:
    """Bare upstream with docs/api/x.txt + tools/t.txt, dev branch, tag v1."""
    up = tmp_path / name
    _git("init", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", str(up), str(work))
    (work / "docs/api").mkdir(parents=True)
    (work / "docs/api/x.txt").write_text(marker)
    (work / "tools").mkdir()
    (work / "tools/t.txt").write_text(f"tool in {name}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "init")
    _git("-C", work, "push", "origin", "master")
    _git("-C", work, "checkout", "-q", "-b", "dev")
    (work / "docs/api/dev.txt").write_text(f"dev in {name}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "dev")
    _git("-C", work, "push", "origin", "dev")
    _git("-C", work, "tag", "v1", "master")
    _git("-C", work, "push", "origin", "v1")
    return up


def _advance(up: Path, tmp_path: Path, tag: str) -> None:
    """Push one more commit to upstream master."""
    work = tmp_path / f"_adv_{tag}"
    _git("clone", str(up), str(work))
    (work / "docs/api/x.txt").write_text(f"api {tag}")
    (work / "tools/t.txt").write_text(f"tool {tag}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", tag)
    _git("-C", work, "push", "origin", "master")


def _co(parent: Path, up: Path, key: str, subdir: str) -> layout.Checkout:
    return layout.subfolder_checkout(parent, str(up), key, subdir)


def _state(parent: Path, up: Path, key: str) -> dict:
    co = _co(parent, up, key, "docs/api")
    return tomllib.loads(co.state.read_text())


def _clone_pair(parent: Path, up: Path) -> None:
    """Two bindings on one upstream sharing the `master` checkout."""
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")


def _refspec_lines(gitdir: Path) -> list[str]:
    """Live `remote.origin.fetch` values — the coverage record."""
    return _out("--git-dir", gitdir, "config", "--get-all",
                "remote.origin.fetch").splitlines()


def _push_orphan_tag(up: Path, tmp_path: Path, tag: str) -> str:
    """Push `tag` naming a commit no advertised branch reaches.

    The marker commit is made on a local-only `side-<tag>` branch and the
    tag alone is pushed: upstream advertises `refs/tags/<tag>` for a
    commit `refs/heads/*` coverage plus tag auto-follow can never land.
    Returns the tagged sha.
    """
    work = tmp_path / f"_orphan_{tag}"
    _git("clone", str(up), str(work))
    _git("-C", work, "checkout", "-q", "-b", f"side-{tag}")
    (work / "docs" / "api" / f"{tag}.txt").write_text(f"orphan {tag}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-q", "-m", tag)
    sha = _out("-C", work, "rev-parse", "HEAD")
    _git("-C", work, "tag", tag)
    _git("-C", work, "push", "-q", "origin", tag)
    return sha


def _push_deleted_branch_tip(up: Path, tmp_path: Path, branch: str) -> str:
    """Push `branch` then delete it upstream; return its tip sha — a
    commit no advertised ref names (kept by upstream until gc; a local
    upstream answers a `fetch --no-tags origin <sha>` want for it)."""
    work = tmp_path / f"_del_{branch}"
    _git("clone", str(up), str(work))
    _git("-C", work, "checkout", "-q", "-b", branch)
    (work / "docs" / "api" / f"{branch}.txt").write_text(f"{branch} tip")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-q", "-m", branch)
    sha = _out("-C", work, "rev-parse", "HEAD")
    _git("-C", work, "push", "-q", "origin", branch)
    _git("-C", work, "push", "-q", "origin", "--delete", branch)
    return sha


# ---------------------------------------------------------------------------
# grouped pull


def test_pull_one_fetch_per_store_one_apply_per_checkout(tmp_path, capsys):
    """One `fetch` per repo store and one apply per shared checkout across
    N bindings (spec Update algorithm L490-495; row items)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    _advance(up, tmp_path, "adv")

    rec = _LogBackend()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code == 0

    store = layout.repo_store(parent, str(up))
    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, f"fetches: {[c[0] for c in fetches]}"

    admin = store / "worktrees" / "master"
    applies = [c for c in rec.calls
               if "checkout" in c[0] and c[1].get("git_dir") == admin]
    assert len(applies) == 1, f"applies: {[c[0] for c in applies]}"

    # one dirty check over the shared checkout, plus the ignored-work
    # gate's `--ignored` read of the same checkout (spec Update
    # algorithm — both are once-per-checkout, never per-binding)
    stats = [c[0] for c in rec.calls
             if "status" in c[0] and c[1].get("git_dir") == admin]
    plain = [c for c in stats if "--ignored" not in c]
    ignored = [c for c in stats if "--ignored" in c]
    assert len(plain) == 1, f"status calls: {stats}"
    assert len(ignored) == 1, f"status calls: {stats}"

    assert (parent / "vendor/api/x.txt").read_text().strip() == "api adv"
    assert (parent / "vendor/tools/t.txt").read_text().strip() == "tool adv"


def test_pull_selected_binding_marks_comoved_sibling(tmp_path, capsys):
    """A shared checkout serves every binding linked into it: selecting
    one binding updates the whole checkout and the unselected sibling is
    printed and marked `(moved with <selected>)` (spec L240; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    _advance(up, tmp_path, "adv")

    code, out = _run(parent, capsys, "pull", "vendor/api")
    assert code == 0
    assert "Pulled api" in out
    sibling = next(
        (ln for ln in out.splitlines() if "tools" in ln), None)
    assert sibling is not None, f"no sibling line in {out!r}"
    assert "moved with api" in sibling
    # the whole checkout moved: the sibling's files updated too
    assert (parent / "vendor/tools/t.txt").read_text().strip() == "tool adv"


def test_pull_whole_repo_dirty_check_before_fetch_order(tmp_path, capsys):
    """Whole-repo order is preserved: dirty-check → fetch → apply
    (spec Update algorithm; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "lib")
    _advance(up, tmp_path, "adv")

    rec = _LogBackend()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code == 0
    child_gitdir = parent / "lib" / ".gf" / "git"
    child_calls = [c for c in rec.calls
                   if c[1].get("git_dir") == child_gitdir]
    order = {v: next(i for i, c in enumerate(child_calls)
                    if v in c[0])
             for v in ("status", "fetch", "checkout")}
    assert order["status"] < order["fetch"] < order["checkout"]


def test_pull_dirty_whole_repo_aborts_before_fetch(tmp_path, capsys):
    """The dirty check precedes the fetch: a dirty whole-repo child aborts
    without fetching (spec Update algorithm order; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "lib")
    _advance(up, tmp_path, "adv")
    (parent / "lib" / "dirty.txt").write_text("uncommitted\n")

    rec = _LogBackend()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code != 0
    child_gitdir = parent / "lib" / ".gf" / "git"
    assert not [c for c in rec.calls
                if "fetch" in c[0] and c[1].get("git_dir") == child_gitdir]
    assert (parent / "lib" / "dirty.txt").read_text() == "uncommitted\n"


def test_pull_dirty_shared_checkout_blocks_both_bindings(tmp_path, capsys):
    """One dirty check per checkout: a dirty sibling blocks the shared
    checkout — with no --autostash both bindings abort and the
    checkout stays put (spec L496; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    old_sha = _out("--git-dir",
                   layout.repo_store(parent, str(up))
                   / "worktrees" / "master", "rev-parse", "HEAD")
    _advance(up, tmp_path, "adv")
    (parent / "vendor" / "api" / "dirty.txt").write_text("uncommitted\n")

    code, out = _run(parent, capsys, "pull")
    assert code != 0
    assert "Pulled" not in out
    assert _out("--git-dir", layout.repo_store(parent, str(up))
                / "worktrees" / "master", "rev-parse", "HEAD") == old_sha
    assert (parent / "vendor" / "api" / "dirty.txt").read_text() == (
        "uncommitted\n")


# ---------------------------------------------------------------------------
# F-C — the dirty check covers the whole materialized checkout


def test_pull_dirty_outside_mapped_subdirs_blocks_and_autostash_recovers(
        tmp_path):
    """F-C receiving scenario: the shared checkout's dirty check covers
    the whole materialized worktree, not the union of mapped subdirs.
    Cone mode materializes repo-root files and files directly inside
    each ancestor directory of a mapped subdir, so an untracked file at
    the checkout root (or inside `docs/`, the ancestor of `docs/api`) is
    physically inside the checkout while lying outside every mapping.
    An unforced `gf pull` stops with the dirty error (exit 3) and
    changes nothing; `gf pull --rebase --autostash` stashes, applies and
    restores (spec Update algorithm dirty-check step, clarified to the
    materialized checkout; ruling F-C)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    wt = _co(parent, up, "master", "docs/api").work_tree
    admin = layout.repo_store(parent, str(up)) / "worktrees" / "master"
    old_sha = _out("--git-dir", admin, "rev-parse", "HEAD")
    _advance(up, tmp_path, "adv")

    # dirt outside the union cone but inside the materialized checkout:
    # a repo-root file and a file directly inside the ancestor `docs/`
    root_dirt = wt / "root-dirty.txt"
    anc_dirt = wt / "docs" / "top.md"
    root_dirt.write_text("uncommitted at root\n")
    anc_dirt.write_text("uncommitted in docs/\n")

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 3
    assert "dirty" in r.stderr
    assert "Pulled" not in r.stdout
    # nothing applied: checkout stays put, served files and dirt intact
    assert _out("--git-dir", admin, "rev-parse", "HEAD") == old_sha
    assert (parent / "vendor" / "api" / "x.txt").read_text().strip() == (
        "api on master")
    assert (parent / "vendor" / "tools" / "t.txt").read_text().strip() == (
        "tool in upstream")
    assert root_dirt.read_text() == "uncommitted at root\n"
    assert anc_dirt.read_text() == "uncommitted in docs/\n"

    r = gf("-C", str(parent), "pull", "--rebase", "--autostash",
           check=False)
    assert r.returncode == 0, r.stderr
    assert "Pulled api" in r.stdout
    assert _out("--git-dir", admin, "rev-parse", "HEAD") != old_sha
    assert (parent / "vendor" / "api" / "x.txt").read_text().strip() == (
        "api adv")
    assert (parent / "vendor" / "tools" / "t.txt").read_text().strip() == (
        "tool adv")
    # the stash round-trips the out-of-cone dirt
    assert root_dirt.read_text() == "uncommitted at root\n"
    assert anc_dirt.read_text() == "uncommitted in docs/\n"


def test_pull_dirty_inside_mapped_subdir_still_blocks(tmp_path):
    """F-C no-regression: dirt INSIDE a mapped subdir still blocks the
    pull — widening the check to the whole materialized checkout did not
    drop the binding's own scope (spec Update algorithm dirty-check
    step; ruling F-C)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    admin = layout.repo_store(parent, str(up)) / "worktrees" / "master"
    old_sha = _out("--git-dir", admin, "rev-parse", "HEAD")
    _advance(up, tmp_path, "adv")
    dirt = (_co(parent, up, "master", "docs/api").work_tree
            / "docs" / "api" / "dirty.txt")
    dirt.write_text("uncommitted inside\n")

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 3
    assert "dirty" in r.stderr
    assert "Pulled" not in r.stdout
    assert _out("--git-dir", admin, "rev-parse", "HEAD") == old_sha
    assert dirt.read_text() == "uncommitted inside\n"


def test_pull_uses_recorded_resolution_no_remote_probe(tmp_path, capsys):
    """Pull takes repo URL/subdir from the recorded resolution — no
    `ls-remote`/`remote set-head` re-probe of the remote (spec Update
    algorithm step 1 + Drift-algorithm local-only spirit; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)

    rec = _LogBackend()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code == 0
    rec.calls.clear()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code == 0
    assert not [c for c in rec.calls if "ls-remote" in c[0]]
    assert not [c for c in rec.calls
                if c[0][:2] == ("remote", "set-head")]


def test_pull_fetches_each_repo_store_exactly_once(tmp_path, capsys):
    """Two repo stores in one manifest: one `gf pull` fetches each store
    exactly once and updates every binding's files (spec Update
    algorithm L494-495 "one fetch per repo store"; ruling R2 — the
    single-store pin above did not prove the grouping composes across
    stores)."""
    up_a = _upstream(tmp_path, "up_a", "api in A")
    up_b = _upstream(tmp_path, "up_b", "api in B")
    parent = _parent(tmp_path)
    _clone_pair(parent, up_a)
    gf("-C", str(parent), "clone", str(up_b / "docs/api"),
       "vendor/api-b")
    gf("-C", str(parent), "clone", str(up_b / "tools"),
       "vendor/tools-b", "-b", "master")
    _advance(up_a, tmp_path, "advA")
    _advance(up_b, tmp_path, "advB")

    rec = _LogBackend()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code == 0

    stores = {layout.repo_store(parent, str(up)) for up in (up_a, up_b)}
    for store in stores:
        fetches = [c for c in rec.calls
                   if "fetch" in c[0] and c[1].get("git_dir") == store]
        assert len(fetches) == 1, (
            f"{store}: fetches {[c[0] for c in fetches]}")
    assert len([c for c in rec.calls if "fetch" in c[0]]) == 2

    assert (parent / "vendor/api/x.txt").read_text().strip() == \
        "api advA"
    assert (parent / "vendor/tools/t.txt").read_text().strip() == \
        "tool advA"
    assert (parent / "vendor/api-b/x.txt").read_text().strip() == \
        "api advB"
    assert (parent / "vendor/tools-b/t.txt").read_text().strip() == \
        "tool advB"


def test_pull_uncovered_branch_on_single_branch_store_fetches_once(
        tmp_path, capsys):
    """Residual-1 witness (R3/R6): a store created by `--single-branch`
    holds only the master's refspec. When an override retargets the
    binding to a locally uncovered explicit branch (`dev`), `gf pull`
    must append that branch's refspec line AND run exactly one store
    fetch — the `ls-remote` existence check for the uncovered branch
    must not replace the fetch (a store that probed-and-skipped would
    leave the binding unapplied)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api",
       "--single-branch")

    store = layout.repo_store(parent, str(up))
    refspecs_before = _out("--git-dir", store, "config", "--get-all",
                           "remote.origin.fetch").splitlines()
    if "+refs/heads/*:refs/remotes/origin/*" in refspecs_before:
        pytest.fail(f"setup: single-branch store unexpectedly carries "
                    f"the wildcard refspec {refspecs_before}")
    if _git("--git-dir", store, "show-ref", "--verify",
            "refs/remotes/origin/dev", check=False).returncode == 0:
        pytest.fail("setup: refs/remotes/origin/dev already in store")

    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "dev"\n')
    rec = _LogBackend()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out

    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, (
        f"expected exactly one fetch on {store}, "
        f"got {[c[0] for c in fetches]}")
    refspecs_after = _out("--git-dir", store, "config", "--get-all",
                          "remote.origin.fetch").splitlines()
    assert "+refs/heads/dev:refs/remotes/origin/dev" in refspecs_after, \
        refspecs_after
    assert set(refspecs_before) <= set(refspecs_after), (
        refspecs_before, refspecs_after)   # append-only

    api = parent / "vendor" / "api"
    assert api.is_symlink()
    assert (api / "dev.txt").read_text().strip() == f"dev in upstream"


def test_pull_latest_on_single_branch_store_fetches_default_once(
        tmp_path, capsys):
    """F2: the mirror of the pin above — a store created by
    `-b dev --single-branch` covers only `dev`, while upstream's default
    is `master` (never fetched: a narrow store's origin/HEAD is absent
    or dangles over it). Retargeting the binding to `latest` must
    resolve the remote default, append master's refspec line
    (append-only), run exactly ONE store fetch, and serve the binding
    from a `master`-keyed checkout — not die resolving `latest`
    against a store that never fetched it."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api",
       "-b", "dev", "--single-branch")

    store = layout.repo_store(parent, str(up))
    refspecs_before = _out("--git-dir", store, "config", "--get-all",
                           "remote.origin.fetch").splitlines()
    if "+refs/heads/dev:refs/remotes/origin/dev" not in refspecs_before:
        pytest.fail(f"setup: single-branch store missing dev refspec "
                    f"{refspecs_before}")
    if _git("--git-dir", store, "show-ref", "--verify",
            "refs/remotes/origin/master",
            check=False).returncode == 0:
        pytest.fail("setup: refs/remotes/origin/master already in store")

    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "latest"\n')
    rec = _LogBackend()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert "Pulled api" in out

    # exactly one store fetch for the whole pull
    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, (
        f"expected exactly one fetch on {store}, "
        f"got {[c[0] for c in fetches]}")

    # refspec append-only: dev's line stays, master's is added so the
    # fetched default branch is covered next time
    refspecs_after = _out("--git-dir", store, "config", "--get-all",
                          "remote.origin.fetch").splitlines()
    assert "+refs/heads/master:refs/remotes/origin/master" in \
        refspecs_after, refspecs_after
    assert set(refspecs_before) <= set(refspecs_after), (
        refspecs_before, refspecs_after)

    # `latest` keys by the resolved branch name: the binding is served
    # from a `master`-keyed checkout (a `ref=latest` key would be the
    # detached-ref spelling)
    co_master = _co(parent, up, "master", "docs/api")
    assert co_master.work_tree.is_dir()
    api = parent / "vendor" / "api"
    assert api.is_symlink()
    assert api.resolve() == (
        co_master.work_tree / "docs" / "api").resolve()
    # master's tree, not the dev checkout's: x.txt present, dev.txt not
    assert (api / "x.txt").read_text().strip() == "api on master"
    assert not (api / "dev.txt").exists()


# ---------------------------------------------------------------------------
# override moves


def test_override_repo_url_moves_binding_and_vacates(tmp_path):
    """An override that changes the repo URL moves the binding to another
    store: the link retargets, the new store's record gains the binding,
    the vacated checkout's recorded bindings drop it, and the vacated
    checkout's files — incl. uncommitted work — are untouched
    (spec Update algorithm + L179 record rules; row item).

    Reworked for F-C: `api` still moves cleanly, but the sibling `tools`
    pull on the still-shared up_a checkout then hits the physically-
    remaining untracked `dirty.txt` under the whole-materialized-
    checkout dirty check — a sibling pull must not apply a ref to a
    worktree holding uncommitted work, so the pull stops with the dirty
    error (exit 3) after `api` was already served. Removing the dirt
    lets the sibling through, and the moved end state stays intact."""
    up_a = _upstream(tmp_path, "up_a", "api in A")
    up_b = _upstream(tmp_path, "up_b", "api in B")
    parent = _parent(tmp_path)
    _clone_pair(parent, up_a)
    co_a = _co(parent, up_a, "master", "docs/api")
    dirty = co_a.work_tree / "docs" / "api" / "dirty.txt"
    dirty.write_text("uncommitted in A\n")
    old_sha_a = _out("--git-dir", co_a.gitdir, "rev-parse", "HEAD")
    tools_t = (co_a.work_tree / "tools" / "t.txt").read_text()

    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\n'
        f'url = "{up_b}/docs/api"\n')
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 3
    assert "dirty" in r.stderr
    # the moved binding was served before the sibling checkout's block
    assert "Pulled api" in r.stdout
    assert "Pulled tools" not in r.stdout

    co_b = _co(parent, up_b, "master", "docs/api")
    assert (parent / "vendor" / "api").resolve() == (
        co_b.work_tree / "docs/api").resolve()
    assert (parent / "vendor" / "api" / "x.txt").read_text().strip() == (
        "api in B")

    # records: vacated checkout drops the binding, new store gains it
    recs_a = str(_state(parent, up_a, "master")["bindings"])
    assert "docs/api" not in recs_a and "tools" in recs_a
    assert "docs/api" in str(_state(parent, up_b, "master")["bindings"])

    # the still-shared checkout did not move; the uncommitted work that
    # blocked the sibling's apply survives untouched
    assert _out("--git-dir", co_a.gitdir, "rev-parse", "HEAD") == old_sha_a
    assert (co_a.work_tree / "tools" / "t.txt").read_text() == tools_t
    assert (parent / "vendor" / "tools").resolve() == (
        co_a.work_tree / "tools").resolve()
    assert dirty.read_text() == "uncommitted in A\n"

    # lifting the dirt lets the blocked sibling pull through — and the
    # moved binding's end state was complete, not half-applied
    dirty.unlink()
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, r.stderr
    assert "Pulled tools" in r.stdout
    assert (parent / "vendor" / "api").resolve() == (
        co_b.work_tree / "docs/api").resolve()
    assert (parent / "vendor" / "tools").resolve() == (
        co_a.work_tree / "tools").resolve()


def test_override_ref_retargets_link_and_vacates_record(tmp_path, capsys):
    """An override that changes the effective checkout key retargets the
    consumer link to the other checkout and drops the moved binding from
    the vacated checkout's recorded bindings (spec Update algorithm; row
    item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)

    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "dev"\n')
    code, _ = _run(parent, capsys, "pull")
    assert code == 0

    co_dev = _co(parent, up, "dev", "docs/api")
    assert (parent / "vendor" / "api").resolve() == (
        co_dev.work_tree / "docs/api").resolve()
    assert (parent / "vendor" / "api" / "dev.txt").read_text().strip() == (
        f"dev in upstream")

    recs = str(_state(parent, up, "master")["bindings"])
    assert "docs/api" not in recs and "tools" in recs
    assert "docs/api" in str(_state(parent, up, "dev")["bindings"])


# ---------------------------------------------------------------------------
# per-checkout state record


def _admin_head(parent: Path, up: Path, key: str) -> str:
    """HEAD SHA of the `<key>` checkout of `up`'s repo store."""
    store = layout.repo_store(parent, str(up))
    return _out("--git-dir", store / "worktrees" / key,
                "rev-parse", "HEAD")


def test_checkout_state_records_served_bindings_and_resolved_head(
        tmp_path, capsys):
    """The per-checkout `wt/<rk>/.<key>.state` record (spec L179-181) tracks the
    checkout's HEAD SHA in `resolved` and exactly the served manifest
    bindings' subdirs in `bindings` — after the creating clone, after a
    joining clone, after pull, and after a binding vacates the checkout
    (ruling R2: exact-set, where earlier pins were substring
    membership)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)

    # The creating clone records the served set and the checked-out SHA.
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    st = _state(parent, up, "master")
    assert set(st["bindings"]) == {"docs/api"}
    assert st["resolved"] == _admin_head(parent, up, "master")

    # A joining clone widens the recorded set to exactly the two served
    # subdirs; `resolved` still names the shared checkout's HEAD.
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")
    st = _state(parent, up, "master")
    assert set(st["bindings"]) == {"docs/api", "tools"}
    assert st["resolved"] == _admin_head(parent, up, "master")

    # After pull, `resolved` tracks the checkout's moved HEAD.
    _advance(up, tmp_path, "adv")
    code, _ = _run(parent, capsys, "pull")
    assert code == 0
    st = _state(parent, up, "master")
    assert st["resolved"] == _admin_head(parent, up, "master")
    assert set(st["bindings"]) == {"docs/api", "tools"}

    # A binding that vacates the checkout drops out of its recorded set
    # exactly; the checkout it joined records it.
    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "dev"\n')
    code, _ = _run(parent, capsys, "pull")
    assert code == 0
    st = _state(parent, up, "master")
    assert set(st["bindings"]) == {"tools"}
    assert st["resolved"] == _admin_head(parent, up, "master")
    st = _state(parent, up, "dev")
    assert set(st["bindings"]) == {"docs/api"}
    assert st["resolved"] == _admin_head(parent, up, "dev")


# ---------------------------------------------------------------------------
# init-child first-pull conversion


def test_init_empty_child_converts_to_consumer_link(tmp_path):
    """`gf init` + first `gf pull` with a subfolder URL converts an empty
    child (no commits, no files other than `.gf`) into a consumer link
    (spec L241 + constraint L546; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "vendor/api",
       "--url", str(up / "docs/api"), "-b", "master")
    child = parent / "vendor" / "api"
    assert child.is_dir() and not child.is_symlink()

    gf("-C", str(parent), "pull")
    assert child.is_symlink()
    co = _co(parent, up, "master", "docs/api")
    assert child.resolve() == (co.work_tree / "docs/api").resolve()
    assert (child / "x.txt").read_text().strip() == "api on master"


def test_init_child_with_files_refuses_and_deletes_nothing(tmp_path):
    """A child that gained files is refused — the pull stops with an
    error and deletes nothing (spec L241 + constraint L546; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "vendor/api",
       "--url", str(up / "docs/api"), "-b", "master")
    child = parent / "vendor" / "api"
    (child / "user.txt").write_text("user work\n")

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode != 0
    assert child.is_dir() and not child.is_symlink()
    assert (child / "user.txt").read_text() == "user work\n"
    assert (child / ".gf").exists()


# ---------------------------------------------------------------------------
# F2 — a missing consumer link rejoins the grouped pull


def test_pull_missing_link_rejoins_shared_checkout_and_applies(
        tmp_path, capsys):
    """F2 (i): a selected binding whose consumer link is missing joins
    the EXISTING shared checkout through the grouped pull — the store's
    one fetch is followed by the checkout's apply, so the recreated link
    serves the new upstream HEAD.

    Defect shape pinned against: the rejoining binding was routed through
    `ensure_shared_binding` — it fetched the store but never applied,
    relinking the consumer path against the unmoved checkout."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "vendor" / "api").unlink()   # drop the consumer link only
    _advance(up, tmp_path, "adv")

    r = gf("-C", str(parent), "pull", "vendor/api")
    assert "Pulled api" in r.stdout
    # the shared checkout moved; the still-linked sibling rides along
    sibling = next(
        (ln for ln in r.stdout.splitlines() if "tools" in ln), None)
    assert sibling is not None and "moved with api" in sibling

    link = parent / "vendor" / "api"
    assert link.is_symlink()
    assert link.resolve() == (
        _co(parent, up, "master", "docs/api").work_tree
        / "docs/api").resolve()
    # the pin: fetched AND applied — files match the new upstream HEAD
    assert (link / "x.txt").read_text().strip() == "api adv"
    assert (parent / "vendor" / "tools" / "t.txt").read_text().strip() == (
        "tool adv")


def test_pull_two_missing_links_one_store_fetch(tmp_path, capsys):
    """F2 (ii): two bindings with missing consumer links on ONE repo
    store still cost exactly one `git fetch` on the store for the whole
    `gf pull` — the per-binding `ensure_shared_binding` detour fetched
    once per binding."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "vendor" / "api").unlink()
    (parent / "vendor" / "tools").unlink()
    _advance(up, tmp_path, "adv")

    rec = _LogBackend()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code == 0

    store = layout.repo_store(parent, str(up))
    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, f"fetches: {[c[0] for c in fetches]}"

    wt = _co(parent, up, "master", "docs/api").work_tree
    assert (parent / "vendor" / "api").resolve() == (
        wt / "docs/api").resolve()
    assert (parent / "vendor" / "tools").resolve() == (
        wt / "tools").resolve()
    assert (parent / "vendor" / "api" / "x.txt").read_text().strip() == (
        "api adv")
    assert (parent / "vendor" / "tools" / "t.txt").read_text().strip() == (
        "tool adv")


# ---------------------------------------------------------------------------
# R6-A — pinned-ref (tag/commit) coverage in the pull coverage loop
#
# A tag or bare commit never matches a `refs/heads/*` refspec line, and
# git's tag auto-follow lands only tags pointing into fetched history —
# so a tag whose commit no advertised branch reaches, or a commit no ref
# names, is invisible to the store/child fetch. The coverage loop probes
# the store, `ls-remote`s `refs/tags/<t>` upstream, `config --add`s the
# tag's NON-forced refspec line (append-only — `+` is permitted only
# into `refs/remotes/origin/*`, and a moved tag must be declined, never
# overwritten) so the one store fetch lands it; a missing commit gets a
# one-shot `fetch --no-tags origin <sha>`.
#
# Pre-fix signatures (verified against HEAD):
#   tag override on a store:  could not resolve ref 'v3'  (rc=1)
#   tag override on a child:  could not resolve ref 'v6'  (rc=1)
#   sha override on a child:  git checkout <sha> failed:
#                             fatal: unable to read tree (<sha>)  (rc=2)


def test_pull_tag_override_appends_refspec_and_lands_tag(
        tmp_path, capsys):
    """R6-A pull twin: `ref=v3` override where v3 tags a commit reachable
    from NO advertised branch. The wildcard-covered store's fetch cannot
    land it through auto-follow; the coverage loop must probe
    `refs/tags/v3` upstream, `config --add` the tag's line, and let the
    ONE store fetch land it — then the binding is served detached from a
    `ref=v3` checkout. The wildcard line survives verbatim, and a re-pull
    appends nothing (a landed tag is never re-covered)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    store = layout.repo_store(parent, str(up))
    assert _refspec_lines(store) == ["+refs/heads/*:refs/remotes/origin/*"]
    orphan_sha = _push_orphan_tag(up, tmp_path, "v3")
    if _git("--git-dir", store, "rev-parse", "--verify", "refs/tags/v3",
            check=False).returncode == 0:
        pytest.fail("setup: refs/tags/v3 already in store")

    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "v3"\n')
    rec = _LogBackend()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert "Pulled api" in out

    # the upstream tag probe ran, then the append — the tag rides the
    # pull's ONE store fetch (no extra fetch for the ref).
    calls = [c[0] for c in rec.calls]
    tag_probe = ("ls-remote", str(up), "refs/tags/v3")
    tag_append = ("config", "--add", "remote.origin.fetch",
                  "refs/tags/v3:refs/tags/v3")
    assert tag_probe in calls, calls
    assert tag_append in calls, calls
    assert calls.index(tag_probe) < calls.index(tag_append)
    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, f"fetches: {[c[0] for c in fetches]}"
    assert _refspec_lines(store) == [
        "+refs/heads/*:refs/remotes/origin/*",
        "refs/tags/v3:refs/tags/v3",
    ]

    # detached `ref=v3` checkout at the tagged sha; the link retargeted
    # and serves the orphan commit's tree.
    co = _co(parent, up, "ref=v3", "docs/api")
    assert (parent / "vendor" / "api").resolve() == (
        co.work_tree / "docs" / "api").resolve()
    assert (parent / "vendor" / "api" / "v3.txt").read_text() == (
        "orphan v3")
    assert (co.gitdir / "HEAD").read_text().strip() == orphan_sha
    st = _state(parent, up, "ref=v3")
    assert st["resolved"] == orphan_sha and st["ref"] == "v3"

    # `api` vacated the shared master checkout's record; tools stayed.
    assert set(_state(parent, up, "master")["bindings"]) == {"tools"}
    assert set(st["bindings"]) == {"docs/api"}

    # Idempotence: a re-pull over the landed tag re-covers nothing —
    # no `remote.origin.fetch` write of any kind, lines byte-identical.
    rec.calls.clear()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert "Pulled api" in out
    writes = [c[0] for c in rec.calls
              if c[0][0] == "config"
              and "--get-all" not in c[0] and "--get" not in c[0]
              and "remote.origin.fetch" in c[0]]
    assert not writes, f"refspec writes on re-pull: {writes}"
    assert _refspec_lines(store) == [
        "+refs/heads/*:refs/remotes/origin/*",
        "refs/tags/v3:refs/tags/v3",
    ]


def test_pull_whole_repo_orphan_tag_detaches_at_tagged_sha(
        tmp_path, capsys):
    """R6-A whole-repo twin: a `-b master` child (wildcard refspec) pulled
    with `ref=v6`, v6 tagging a commit reachable from NO branch. The
    child's fetch can't auto-follow it; `_fetch_and_checkout` covers the
    tag (its line appended to the child gitdir) and checks out detached.
    The re-pull additionally pins that `_set_child_origin` no longer dies
    on the now multi-valued `remote.origin.fetch` key."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "lib", "-b", "master")
    gitdir = parent / "lib" / ".gf" / "git"
    assert _refspec_lines(gitdir) == [
        "+refs/heads/*:refs/remotes/origin/*"]
    orphan_sha = _push_orphan_tag(up, tmp_path, "v6")

    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "lib"\nref = "v6"\n')
    rec = _LogBackend()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert "Pulled lib" in out

    assert _refspec_lines(gitdir) == [
        "+refs/heads/*:refs/remotes/origin/*",
        "refs/tags/v6:refs/tags/v6",
    ]
    assert _out("--git-dir", gitdir, "rev-parse", "HEAD") == orphan_sha
    # detached HEAD (abbrev-ref prints HEAD, not a branch name)
    assert _out("--git-dir", gitdir, "rev-parse", "--abbrev-ref",
                "HEAD") == "HEAD"
    assert (parent / "lib" / "docs" / "api" / "v6.txt").read_text() == (
        "orphan v6")

    # Re-pull: append-only coverage over a multi-valued key — no crash on
    # `config remote.origin.fetch`, no re-append, child stays detached at
    # the same sha.
    rec.calls.clear()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert "Pulled lib" in out
    writes = [c[0] for c in rec.calls
              if c[0][0] == "config"
              and "--get-all" not in c[0] and "--get" not in c[0]
              and "remote.origin.fetch" in c[0]]
    assert not writes, f"refspec writes on re-pull: {writes}"
    assert _refspec_lines(gitdir) == [
        "+refs/heads/*:refs/remotes/origin/*",
        "refs/tags/v6:refs/tags/v6",
    ]
    assert _out("--git-dir", gitdir, "rev-parse", "HEAD") == orphan_sha


def test_pull_whole_repo_commit_sha_fetches_sha(tmp_path, capsys):
    """R6-A commit arm: `ref=<sha>` of a deleted-branch tip names a commit
    no refspec covers. The child gitdir is not a promisor, so `cat-file
    -e` is a real miss and one `git fetch origin <sha>` lands it; the
    child then checks out detached at that sha. Pre-fix the pull died
    `git checkout <sha> failed: fatal: unable to read tree`."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "lib", "-b", "master")
    gitdir = parent / "lib" / ".gf" / "git"
    gone_sha = _push_deleted_branch_tip(up, tmp_path, "gone")
    if _git("--git-dir", gitdir, "cat-file", "-e", gone_sha,
            check=False).returncode == 0:
        pytest.fail("setup: deleted-branch tip already in child gitdir")

    (parent / "gf.local.toml").write_text(
        f'[[git_folder_override]]\nname = "lib"\nref = "{gone_sha}"\n')
    rec = _LogBackend()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert "Pulled lib" in out

    calls = [c[0] for c in rec.calls]
    probe = ("cat-file", "-e", gone_sha)
    # `git fetch --no-tags origin <sha>` — no refspec line can name a
    # bare sha, and every gf fetch carries --no-tags (spec Fetch
    # refspecs)
    sha_fetch = ("fetch", "--no-tags", "origin", gone_sha)
    assert probe in calls and sha_fetch in calls, calls
    assert calls.index(probe) < calls.index(sha_fetch)

    assert _out("--git-dir", gitdir, "rev-parse", "HEAD") == gone_sha
    assert _out("--git-dir", gitdir, "rev-parse", "--abbrev-ref",
                "HEAD") == "HEAD"
    assert (parent / "lib" / "docs" / "api" / "gone.txt"
            ).read_text() == "gone tip"
    # a bare sha needs no refspec line — coverage stays the wildcard
    assert _refspec_lines(gitdir) == [
        "+refs/heads/*:refs/remotes/origin/*"]


def test_pull_override_unknown_tag_fails_clean(tmp_path):
    """R6-A negative: `ref=v9` is neither a store branch nor an
    advertised `refs/heads/v9`/`refs/tags/v9` — coverage is left for the
    unchanged `could not resolve ref 'v9'` failure: nothing is appended,
    the link and checkout set are untouched."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    store = layout.repo_store(parent, str(up))
    link = parent / "vendor" / "api"
    target_before = os.readlink(link)
    lines_before = _refspec_lines(store)

    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "v9"\n')
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode != 0
    assert "could not resolve ref 'v9'" in r.stderr

    assert _refspec_lines(store) == lines_before
    assert os.readlink(link) == target_before
    co = _co(parent, up, "ref=v9", "docs/api")
    assert not co.work_tree.exists()
    assert not co.state.exists()


# ---------------------------------------------------------------------------
# R6-A — mock-land mechanism pin
#
# The mock's `ls-remote` filters trailing ref patterns the way real git
# does (a ref matches when it equals the pattern or ends with
# `/<pattern>`), so the tag probe is faithfully discriminating. What the
# mock pins here is the seam's argv shape: the probe→append order and
# `config --add` (never a rewrite) of the tag's NON-forced refspec line,
# and the one-shot `fetch --no-tags origin <sha>` for a missing commit.
# `_ensure_pinned_ref` is called directly on an in-memory store.


def test_ensure_pinned_ref_mock_argv_shape(mock_backend):
    SHA = "9" * 40
    store = Path("/store")
    up_repo = mock_backend.seed("/upstream", bare=True)
    mock_backend.add_commit(up_repo, SHA, {"a.txt": "x"})
    up_repo.refs["refs/heads/master"] = SHA
    mock_backend.tag(up_repo, "v3", SHA)

    # Tag upstream-advertised but absent from the store: local probes
    # miss, one upstream probe hits, the tag line is appended.
    shelf._ensure_pinned_ref(store, "/upstream", "v3", mock_backend)
    assert [c[0] for c in mock_backend.calls] == [
        ("rev-parse", "refs/tags/v3^{}"),
        ("show-ref", "--verify", "refs/heads/v3"),
        ("show-ref", "--verify", "refs/remotes/origin/v3"),
        ("ls-remote", "/upstream", "refs/tags/v3"),
        ("config", "--add", "remote.origin.fetch",
         "refs/tags/v3:refs/tags/v3"),
    ]

    # A landed tag is never re-covered: the local tag probe is the only
    # call.
    store_repo = mock_backend._repo(str(store.resolve()))
    store_repo.refs["refs/tags/v3"] = SHA
    mock_backend.calls.clear()
    shelf._ensure_pinned_ref(store, "/upstream", "v3", mock_backend)
    assert [c[0] for c in mock_backend.calls] == [
        ("rev-parse", "refs/tags/v3^{}")]

    # A missing 40-hex commit: `cat-file -e` probe (unmocked → miss),
    # then the one-shot sha fetch — and no refspec line, since none can
    # name a bare sha.
    mock_backend.calls.clear()
    shelf._ensure_pinned_ref(store, "/upstream", SHA, mock_backend)
    assert [c[0] for c in mock_backend.calls] == [
        ("cat-file", "-e", SHA),
        ("fetch", "--no-tags", "origin", SHA),
    ]

    # `latest` stays with the branch machinery: no calls at all.
    mock_backend.calls.clear()
    shelf._ensure_pinned_ref(store, "/upstream", "latest", mock_backend)
    assert mock_backend.calls == []

    # A ref upstream advertises as neither branch nor tag: probes run,
    # nothing is appended — the caller's `could not resolve ref` stands.
    mock_backend.seed("/empty-up", bare=True)
    mock_backend.calls.clear()
    shelf._ensure_pinned_ref(store, "/empty-up", "v9", mock_backend)
    calls = [c[0] for c in mock_backend.calls]
    assert calls[-1] == ("ls-remote", "/empty-up", "refs/tags/v9")
    assert not [c for c in calls if "config" in c]


# ---------------------------------------------------------------------------
# R7-C — coverage-aware `--single-branch` narrowing on whole-repo clone
#
# `_ensure_pinned_ref` runs before the child fetch, so a ref that is BOTH
# `refs/heads/<x>` and `refs/tags/<x>` upstream leaves the gitdir's
# `remote.origin.fetch` multi-valued (wildcard + the tag's appended line)
# by the time the `--single-branch` narrowing write runs. The write must
# stay append-only: skip when the narrowed line is already present, a
# plain write only while the lone wildcard stands, `config --add` when
# other live lines exist — a bare `config` write on the multi-valued key
# dies `cannot overwrite multiple values` (spec "Fetch refspecs": lines
# are never removed or rewritten).
#
# Pre-fix signature (verified against HEAD):
#   -b x --single-branch on a heads/x+tags/x upstream:
#       rc=2, git config fails "cannot overwrite multiple values"


def _push_colliding_branch_tag(up: Path, tmp_path: Path, name: str) -> str:
    """Push `refs/heads/<name>` AND `refs/tags/<name>` on DIFFERENT
    commits — an upstream-side branch/tag name collision. The branch tip
    carries `docs/api/<name>-branch.txt`; the tag names the master tip,
    whose tree lacks the marker. Returns the branch tip sha."""
    work = tmp_path / f"_coll_{name}"
    _git("clone", str(up), str(work))
    _git("-C", work, "checkout", "-q", "-b", name)
    (work / "docs" / "api" / f"{name}-branch.txt").write_text(
        f"{name} branch tip")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-q", "-m", f"{name} branch")
    branch_sha = _out("-C", work, "rev-parse", "HEAD")
    _git("-C", work, "push", "-q", "origin", f"refs/heads/{name}")
    _git("-C", work, "checkout", "-q", "master")
    _git("-C", work, "tag", name)
    _git("-C", work, "push", "-q", "origin", f"refs/tags/{name}")
    return branch_sha


def test_clone_single_branch_collision_adds_alongside_tag_line(
        tmp_path):
    """R7-C collision arm: `-b x --single-branch` on an upstream that
    advertises `refs/heads/x` AND `refs/tags/x`. The pin coverage appends
    the tag's line pre-fetch (the empty gitdir holds neither ref), so the
    narrowing must `config --add` the branch line next to the live lines
    — never a plain `config` write, which dies rc 2 `cannot overwrite
    multiple values`. The BRANCH wins the checkout (the branch is
    attached and `merge --ff-only` integrates `origin/x`), and the
    wildcard, tag and branch lines coexist verbatim.
    """
    up = _upstream(tmp_path)
    branch_sha = _push_colliding_branch_tag(up, tmp_path, "x")
    parent = _parent(tmp_path)

    gf("-C", str(parent), "clone", str(up), "lib", "-b", "x",
       "--single-branch")
    lib = parent / "lib"
    gitdir = lib / ".gf" / "git"

    # never clobbered: the tag's pin line and the narrowed branch line
    # coexist under the still-live wildcard — all append-only.
    assert _refspec_lines(gitdir) == [
        "+refs/heads/*:refs/remotes/origin/*",
        "refs/tags/x:refs/tags/x",
        "+refs/heads/x:refs/remotes/origin/x",
    ]
    # both upstream refs landed locally
    assert _out("--git-dir", gitdir, "rev-parse",
                "refs/remotes/origin/x") == branch_sha
    _git("--git-dir", gitdir, "rev-parse", "--verify", "refs/tags/x")

    # the branch wins the checkout: HEAD is a local `x` at the branch
    # tip, and the tree carries the branch-only marker the tag's commit
    # lacks.
    assert _out("--git-dir", gitdir, "rev-parse", "HEAD") == branch_sha
    assert _out("--git-dir", gitdir, "symbolic-ref", "-q",
                "HEAD") == "refs/heads/x"
    assert (lib / "docs" / "api" / "x-branch.txt").read_text() == (
        "x branch tip")


def test_clone_single_branch_no_collision_still_narrows(tmp_path):
    """R7-C no-collision twin: `-b dev --single-branch` where upstream
    advertises `dev` as a branch only — the coverage-aware write keeps
    the plain narrowing: the lone `remote add` wildcard is replaced by
    exactly the branch's line (spec `gf clone` --single-branch)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)

    gf("-C", str(parent), "clone", str(up), "lib", "-b", "dev",
       "--single-branch")
    lib = parent / "lib"
    gitdir = lib / ".gf" / "git"

    assert _refspec_lines(gitdir) == [
        "+refs/heads/dev:refs/remotes/origin/dev"]
    assert _out("--git-dir", gitdir, "rev-parse", "--abbrev-ref",
                "HEAD") == "dev"
    assert (lib / "docs" / "api" / "dev.txt").read_text() == (
        "dev in upstream")


# ---------------------------------------------------------------------------
# R7-C — pinned-ref coverage on the store-CREATE arm (call shape)
#
# The end-to-end pin for a pinned tag/commit on a FRESH store lives in
# test_clone_subfolder (pre-fix: `could not resolve ref`); the sha arm
# needs the backend call log — a `--filter=blob:none` store is a
# promisor, so `worktree add`/`checkout` lazily pulls a missing object
# even without gf's coverage, and only the gf-issued
# `fetch --no-tags origin <sha>`
# (and the tag's probe→append→one-creating-fetch ordering) discriminates
# the create arm (spec "Fetch refspecs": the tag's line lands with the
# creating fetch; a needed commit sha "is fetched directly with
# `git fetch --no-tags origin <sha>`").


def test_clone_create_store_tag_rides_creating_fetch(tmp_path, capsys):
    """R7-C create-arm tag arm at the mechanism boundary: `-b v3` on a
    fresh store, `v3` an orphan tag upstream. The pin coverage probes
    `refs/tags/v3` via `ls-remote` and `--add`s its line BEFORE the
    store's creating fetch, so that one fetch lands the tag — no
    per-ref second fetch, no later top-up. Pre-fix: rc 1 `could not
    resolve ref 'v3'`, and neither the tag probe nor the append ever
    ran."""
    up = _upstream(tmp_path)
    orphan_sha = _push_orphan_tag(up, tmp_path, "v3")
    parent = _parent(tmp_path)

    rec = _LogBackend()
    code, out = _run(parent, capsys, "clone", str(up / "docs/api"),
                     "vendor/o", "-b", "v3", backend=rec)
    assert code == 0, out

    store = layout.repo_store(parent, str(up))
    calls = [c[0] for c in rec.calls]
    tag_probe = ("ls-remote", str(up), "refs/tags/v3")
    tag_append = ("config", "--add", "remote.origin.fetch",
                  "refs/tags/v3:refs/tags/v3")
    create_fetch = ("fetch", "--filter=blob:none", "--no-tags",
                    "origin")
    assert tag_probe in calls and tag_append in calls, calls
    assert create_fetch in calls, calls
    # probe → append → the one creating fetch, in that order
    assert (calls.index(tag_probe) < calls.index(tag_append)
            < calls.index(create_fetch))
    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, f"fetches: {[c[0] for c in fetches]}"

    assert _refspec_lines(store) == [
        "+refs/heads/*:refs/remotes/origin/*",
        "refs/tags/v3:refs/tags/v3",
    ]
    assert _out("--git-dir", store, "rev-parse",
                "refs/tags/v3^{}") == orphan_sha
    co = _co(parent, up, "ref=v3", "docs/api")
    assert (co.gitdir / "HEAD").read_text().strip() == orphan_sha
    assert (parent / "vendor" / "o" / "v3.txt").read_text() == "orphan v3"


def test_clone_create_store_fetches_pinned_commit_sha(tmp_path, capsys):
    """R7-C create-arm commit arm: `-b <sha>` names a deleted branch's
    tip — a commit no refspec can cover. The store's `cat-file -e` probe
    misses (a promisor store would lazily fetch the object later anyway,
    so only the call log discriminates): the pin coverage pulls it with
    a one-shot `fetch --no-tags origin <sha>` ahead of the creating
    fetch, then
    the checkout detaches at it. Refspec coverage stays the wildcard —
    no line can name a bare sha."""
    up = _upstream(tmp_path)
    gone_sha = _push_deleted_branch_tip(up, tmp_path, "gone")
    parent = _parent(tmp_path)

    rec = _LogBackend()
    code, out = _run(parent, capsys, "clone", str(up / "docs/api"),
                     "vendor/sha", "-b", gone_sha, backend=rec)
    assert code == 0, out

    store = layout.repo_store(parent, str(up))
    calls = [c[0] for c in rec.calls]
    probe = ("cat-file", "-e", gone_sha)
    sha_fetch = ("fetch", "--no-tags", "origin", gone_sha)
    assert probe in calls and sha_fetch in calls, calls
    assert calls.index(probe) < calls.index(sha_fetch)

    # a bare sha needs no refspec line — coverage stays the wildcard
    assert _refspec_lines(store) == ["+refs/heads/*:refs/remotes/origin/*"]
    _git("--git-dir", store, "cat-file", "-e", gone_sha)

    co = _co(parent, up, f"ref={gone_sha}", "docs/api")
    assert (co.gitdir / "HEAD").read_text().strip() == gone_sha
    assert (parent / "vendor" / "sha" / "gone.txt").read_text() == (
        "gone tip")


# ---------------------------------------------------------------------------
# R7-C pull arm — pinned-ref coverage when `gf pull` creates the store
#
# A `gf init <path> --url <up>/<subdir> -b <ref>` placeholder converted by
# the first `gf pull` makes the PULL create the repo store. The create arm
# needs the same pin coverage the clone path has: a tag-pinned binding's
# NON-forced `refs/tags/<t>:refs/tags/<t>` line must be in place BEFORE
# the one
# creating fetch — a store this pull created gets no second fetch, so an
# append that lands after it is coverage no fetch ever reads. The pull
# resolves every binding's branch/key via `_binding_branch` before the
# store exists and feeds each non-branch binding's `ref` into
# `ensure_repo_store(pins=...)`, whose per-pin `_ensure_pinned_ref`
# coverage rides the creating fetch (the clone mechanism, grouped).
#
# Pre-fix signature (verified against 74298d6):
#   init -b <orphan-tag> + pull: rc 1, "could not resolve ref 'v3'" —
#   the coverage loop appended the tag's line only AFTER the creating
#   fetch, leaving a store holding a refspec no fetch ever ran.


def test_pull_create_store_orphan_tag_pin_rides_creating_fetch(tmp_path):
    """R7-C pull twin of the clone-side orphan pin: `gf init vendor/o
    --url <up>/docs/api -b v3` records the binding without fetching,
    then `gf pull` creates the store, lands `refs/tags/v3` with the one
    creating fetch, and serves the orphan commit's tree detached through
    a `ref=v3` checkout. rc 0, `Pulled o`, store refspecs exactly the
    wildcard + the tag line. Pre-fix: rc 1 `could not resolve ref 'v3'`.
    """
    up = _upstream(tmp_path)
    orphan_sha = _push_orphan_tag(up, tmp_path, "v3")
    parent = _parent(tmp_path)

    # init writes the manifest and a placeholder gitdir only — no fetch,
    # no repo store.
    gf("-C", str(parent), "init", "vendor/o",
       "--url", str(up / "docs/api"), "-b", "v3")
    child = parent / "vendor" / "o"
    assert child.is_dir() and not child.is_symlink()
    assert not (parent / ".gf").exists()
    manifest = tomllib.loads((parent / "gf.toml").read_text())
    (entry,) = [f for f in manifest["git_folder"]
                if f["path"] == "vendor/o"]
    assert entry["url"] == str(up / "docs/api")
    assert entry["ref"] == "v3"

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, r.stderr
    assert "Pulled o" in r.stdout

    store = layout.repo_store(parent, str(up))
    # append-only coverage: wildcard verbatim + exactly the pin's line.
    assert _refspec_lines(store) == [
        "+refs/heads/*:refs/remotes/origin/*",
        "refs/tags/v3:refs/tags/v3",
    ]
    # the pin landed — the tag names the orphan commit in the store.
    assert _out("--git-dir", store, "rev-parse",
                "refs/tags/v3^{}") == orphan_sha

    # the consumer link serves the tagged tree from a detached
    # `ref=v3` checkout — v3.txt exists only on the orphan commit.
    co = _co(parent, up, "ref=v3", "docs/api")
    assert (co.gitdir / "HEAD").read_text().strip() == orphan_sha
    assert child.is_symlink()
    assert child.resolve() == (co.work_tree / "docs" / "api").resolve()
    assert (child / "v3.txt").read_text() == "orphan v3"
    assert (child / "x.txt").read_text() == "api on master"


def test_pull_create_store_tag_pin_probe_append_one_fetch(
        tmp_path, capsys):
    """R7-C ordering arm: on the pull that creates the store, the tag's
    pin coverage runs inside `ensure_repo_store` — `ls-remote` probe,
    then `config --add`, then the ONE `--filter=blob:none --no-tags`
    creating
    fetch — with no post-append top-up fetch (spec "Fetch refspecs":
    a needed tag's line lands with the creating fetch). Pre-fix the
    append ran in the coverage loop AFTER the creating fetch, and the
    pull still died `could not resolve ref`."""
    up = _upstream(tmp_path)
    _push_orphan_tag(up, tmp_path, "v3")
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "vendor/o",
       "--url", str(up / "docs/api"), "-b", "v3")

    rec = _LogBackend()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out

    store = layout.repo_store(parent, str(up))
    calls = [c[0] for c in rec.calls]
    tag_probe = ("ls-remote", str(up), "refs/tags/v3")
    tag_append = ("config", "--add", "remote.origin.fetch",
                  "refs/tags/v3:refs/tags/v3")
    create_fetch = ("fetch", "--filter=blob:none", "--no-tags",
                    "origin")
    assert tag_probe in calls and tag_append in calls, calls
    assert create_fetch in calls, calls
    # probe → append → the one creating fetch, in that order
    assert (calls.index(tag_probe) < calls.index(tag_append)
            < calls.index(create_fetch))
    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, f"fetches: {[c[0] for c in fetches]}"


def test_pull_create_store_branch_and_tag_pins_one_fetch(
        tmp_path, capsys):
    """R7-C grouped arm: two init bindings share one fresh store — `b`
    pinned to branch `master`, `o` pinned to orphan tag `v3`. One
    `gf pull` resolves both before the store exists, carries v3's pin
    into the one creating fetch, and materializes each under its own
    checkout key — `master` and `ref=v3`. Exactly one store fetch.
    Pre-fix: `Pulled b` then rc 1 `could not resolve ref 'v3'`."""
    up = _upstream(tmp_path)
    orphan_sha = _push_orphan_tag(up, tmp_path, "v3")
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "vendor/b",
       "--url", str(up / "tools"), "-b", "master")
    gf("-C", str(parent), "init", "vendor/o",
       "--url", str(up / "docs/api"), "-b", "v3")

    rec = _LogBackend()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert "Pulled b" in out and "Pulled o" in out

    store = layout.repo_store(parent, str(up))
    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, f"fetches: {[c[0] for c in fetches]}"
    assert _refspec_lines(store) == [
        "+refs/heads/*:refs/remotes/origin/*",
        "refs/tags/v3:refs/tags/v3",
    ]

    # each binding materialized under its own checkout key
    assert (parent / "vendor" / "b").resolve() == (
        _co(parent, up, "master", "tools").work_tree / "tools").resolve()
    assert (parent / "vendor" / "b" / "t.txt").read_text().strip() == (
        "tool in upstream")
    co = _co(parent, up, "ref=v3", "docs/api")
    assert (co.gitdir / "HEAD").read_text().strip() == orphan_sha
    assert (parent / "vendor" / "o").resolve() == (
        co.work_tree / "docs" / "api").resolve()
    assert (parent / "vendor" / "o" / "v3.txt").read_text() == (
        "orphan v3")


def test_pull_existing_store_tag_binding_still_covered(tmp_path, capsys):
    """R7-C control (join arm unchanged): a tag-pinned init binding on a
    store that ALREADY exists keeps the join-side coverage — the loop
    probes `refs/tags/v3`, `--add`s its line, and the pull's one
    `_fetch_store` lands it. Passed pre-fix; stays green — the `pins`
    create-arm change must not disturb the join path."""
    up = _upstream(tmp_path)
    orphan_sha = _push_orphan_tag(up, tmp_path, "v3")
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/t",
       "-b", "master")   # creates the store before the pull
    store = layout.repo_store(parent, str(up))
    assert _refspec_lines(store) == ["+refs/heads/*:refs/remotes/origin/*"]

    gf("-C", str(parent), "init", "vendor/o",
       "--url", str(up / "docs/api"), "-b", "v3")
    rec = _LogBackend()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert "Pulled o" in out

    calls = [c[0] for c in rec.calls]
    tag_probe = ("ls-remote", str(up), "refs/tags/v3")
    tag_append = ("config", "--add", "remote.origin.fetch",
                  "refs/tags/v3:refs/tags/v3")
    assert tag_probe in calls and tag_append in calls, calls
    # join arm: the append precedes the pull's ONE store fetch.
    assert calls.index(tag_probe) < calls.index(tag_append)
    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, f"fetches: {[c[0] for c in fetches]}"

    assert _refspec_lines(store) == [
        "+refs/heads/*:refs/remotes/origin/*",
        "refs/tags/v3:refs/tags/v3",
    ]
    assert _out("--git-dir", store, "rev-parse",
                "refs/tags/v3^{}") == orphan_sha
    co = _co(parent, up, "ref=v3", "docs/api")
    assert (co.gitdir / "HEAD").read_text().strip() == orphan_sha
    assert (parent / "vendor" / "o" / "v3.txt").read_text() == (
        "orphan v3")


def test_pull_create_store_branch_binding_unchanged(tmp_path, capsys):
    """R7-C control (branch arm unchanged): `-b dev` on a fresh store —
    the explicit non-default branch resolves via `_binding_branch`'s
    upstream probe before the store exists, joins no pin, and the one
    creating fetch lands it under the `dev` key. Passed pre-fix (the
    coverage loop resolved the same branch from the fetched store) and
    stays green."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "vendor/api",
       "--url", str(up / "docs/api"), "-b", "dev")

    rec = _LogBackend()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert "Pulled api" in out

    store = layout.repo_store(parent, str(up))
    # a branch pin needs no extra line — coverage stays the wildcard.
    assert _refspec_lines(store) == ["+refs/heads/*:refs/remotes/origin/*"]
    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, f"fetches: {[c[0] for c in fetches]}"

    co = _co(parent, up, "dev", "docs/api")
    assert (parent / "vendor" / "api").resolve() == (
        co.work_tree / "docs" / "api").resolve()
    assert (parent / "vendor" / "api" / "dev.txt").read_text().strip() == (
        "dev in upstream")
