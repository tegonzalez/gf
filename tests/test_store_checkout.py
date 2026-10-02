# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Shared store/checkout/consumer-link creation primitives.

Real-git pins for the machinery layer (no new command behavior):

* ``layout.repo_store(root, repo_url) -> Path``
* ``shelf.ensure_repo_store``  — ``git init --bare``; ``remote.origin.url``;
  first refspec line per ``--single-branch``; ``--filter=blob:none`` fetch
  (full-fetch fallback w/ warning when the server refuses filtering);
  store ``HEAD`` repointed to ``refs/remotes/origin/HEAD`` so the bare
  store's default symref cannot claim a branch.
* ``shelf.ensure_checkout`` — ``worktree add --no-checkout --detach``;
  record verification (admin dir + gitdir file); ``sparse-checkout set
  --cone`` union; ``checkout -B`` (branch) or detached (tag/commit); ``.git``
  gitfile removal; idempotent ``worktree lock``; teardown of only what the
  call created — never the store.
* ``shelf.ensure_consumer_link`` — relative symlink; retarget; rollback pair.
* refspec coverage read from the store's live ``remote.origin.fetch`` lines
  (append-only; no state-file record).
* per-checkout state record incl. bindings list.

Signature surface exercised:

    layout.repo_store(root, repo_url) -> Path
    shelf.ensure_repo_store(store, url, *, branch=None, ref=None, pins=(),
                            single_branch=False, depth=None, backend=None)
    shelf.ensure_checkout(co, ref, *, backend=None)   # union from state
    shelf.ensure_consumer_link(link, target)

Authorities: spec Subfolder-binding layout (L79-99), Fetch refspecs,
Checkout integrity, clone mechanics (L221); arch GF-D5/D7/D8/D12/D13 +
Store-and-checkout contract; constraints: gitfile removal, refspec
append-only, second-checkout, never rebuild over files.
"""

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from gf import layout, shelf
from gf.backends import GitCliBackend, GitResult
from gf.exceptions import GitError, GitFoldersError


# ---------------------------------------------------------------------------
# helpers


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    """Run real git; return the CompletedProcess."""
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True,
    )
    if check and r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} rc={r.returncode}:\n{r.stderr}"
        )
    return r


def _out(*args) -> str:
    return _git(*args).stdout.strip()


def _store(root: Path, url: str) -> Path:
    """The spec-mandated store gitdir path for ``url`` under ``root``."""
    return root / ".gf" / "repos" / layout.repo_key(url) / "git"


def _admin(store: Path, key: str) -> Path:
    return store / "worktrees" / key


def _refspec_lines(store: Path) -> list[str]:
    r = _git("--git-dir", store, "config", "--get-all", "remote.origin.fetch",
             check=False)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _sha(store: Path, ref: str) -> str:
    return _out("--git-dir", store, "rev-parse", "--verify", ref)


def _files_under(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


@pytest.fixture
def upstream(tmp_path) -> str:
    """Bare upstream with real cone-dir content, master + dev + tag v1."""
    remote = tmp_path / "upstream.git"
    _git("init", "--bare", str(remote))
    scratch = tmp_path / "scratch"
    _git("clone", str(remote), str(scratch))
    (scratch / "docs/api").mkdir(parents=True)
    (scratch / "docs/api/x.txt").write_text("api\n")
    (scratch / "tools/x").mkdir(parents=True)
    (scratch / "tools/x/y.txt").write_text("tool\n")
    (scratch / "root.txt").write_text("root\n")
    _git("-C", scratch, "add", "-A")
    _git("-C", scratch, "commit", "-m", "c1")
    _git("-C", scratch, "push", "origin", "master")
    _git("-C", scratch, "branch", "dev")
    _git("-C", scratch, "push", "origin", "dev")
    _git("-C", scratch, "tag", "v1")
    _git("-C", scratch, "push", "origin", "v1")
    return str(remote)


@pytest.fixture
def root(tmp_path) -> Path:
    """Parent root that is itself a plain git repository."""
    r = tmp_path / "parent"
    _git("init", str(r))
    return r


@pytest.fixture
def store(root, upstream) -> Path:
    s = _store(root, upstream)
    shelf.ensure_repo_store(s, upstream)
    return s


@pytest.fixture
def co_docs(root, upstream):
    return layout.subfolder_checkout(root, upstream, "master", "docs/api")


@pytest.fixture
def co_tools(root, upstream):
    return layout.subfolder_checkout(root, upstream, "master", "tools/x")


# ---------------------------------------------------------------------------
# layout.repo_store


def test_repo_store_maps_repo_url_to_store_gitdir(root, upstream):
    """layout.repo_store(root, url) → <root>/.gf/repos/<rk>/git (spec L84)."""
    s = layout.repo_store(root, upstream)
    assert Path(os.fspath(s)) == _store(root, upstream)


# ---------------------------------------------------------------------------
# shelf.ensure_repo_store


def test_ensure_repo_store_bare_origin_wildcard_refspec(root, upstream, store):
    """init --bare + remote.origin.url + wildcard refspec + fetched refs
    (spec Subfolder-binding layout, Fetch refspecs; GF-D5)."""
    assert _out("--git-dir", store, "rev-parse", "--is-bare-repository") == "true"
    assert _out("--git-dir", store, "config", "--get", "remote.origin.url") \
        == upstream
    assert _refspec_lines(store) == [
        "+refs/heads/*:refs/remotes/origin/*",
    ]
    master = _out("--git-dir", upstream, "rev-parse", "refs/heads/master")
    assert _sha(store, "refs/remotes/origin/master") == master
    assert _sha(store, "refs/tags/v1") == _out(
        "--git-dir", upstream, "rev-parse", "refs/tags/v1")


def test_ensure_repo_store_single_branch_first_refspec(root, upstream):
    """--single-branch narrows the first refspec line to the resolved
    branch (spec Fetch refspecs; GF-D7)."""
    s = _store(root, upstream)
    shelf.ensure_repo_store(s, upstream, branch="dev", single_branch=True)
    assert _refspec_lines(s) == [
        "+refs/heads/dev:refs/remotes/origin/dev",
    ]
    assert _sha(s, "refs/remotes/origin/dev") == _out(
        "--git-dir", upstream, "rev-parse", "refs/heads/dev")
    assert _git("--git-dir", s, "rev-parse", "--verify",
                "refs/remotes/origin/master", check=False).returncode != 0


def test_ensure_repo_store_second_call_idempotent(store, upstream):
    """Re-ensure leaves config byte-identical: one wildcard line, no dupes."""
    before = _refspec_lines(store)
    shelf.ensure_repo_store(store, upstream)
    assert _refspec_lines(store) == before


def test_ensure_repo_store_join_appends_uncovered_branch_refspec(root, upstream):
    """A later binding needing an uncovered branch appends that branch's
    line via config --add — existing lines are never rewritten or removed
    (spec Fetch refspecs; constraint refspec append-only)."""
    s = _store(root, upstream)
    shelf.ensure_repo_store(s, upstream, branch="dev", single_branch=True)
    assert _refspec_lines(s) == ["+refs/heads/dev:refs/remotes/origin/dev"]

    shelf.ensure_repo_store(s, upstream, branch="master")
    assert _refspec_lines(s) == [
        "+refs/heads/dev:refs/remotes/origin/dev",
        "+refs/heads/master:refs/remotes/origin/master",
    ]

    # A covered branch appends nothing — append-only, never duplicated.
    shelf.ensure_repo_store(s, upstream, branch="dev")
    assert _refspec_lines(s) == [
        "+refs/heads/dev:refs/remotes/origin/dev",
        "+refs/heads/master:refs/remotes/origin/master",
    ]


class _RefuseFilterOnce(GitCliBackend):
    """Real git backend that refuses the first ``fetch --filter=`` once.

    Injects the spec's "server refuses filtered fetch" refusal
    deterministically for both backend conventions: raises GitError when
    the caller uses ``check=True``, returns rc=1 when it inspects
    ``check=False`` results. Everything else delegates to real git.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.refused = 0

    def git(self, *args, **kwargs):
        argv = tuple(map(str, args))
        self.calls.append(argv)
        if "fetch" in argv and any(a.startswith("--filter") for a in argv):
            if self.refused == 0:
                self.refused += 1
                if kwargs.get("check", True):
                    raise GitError(
                        "git fetch --filter=blob:none failed: "
                        "server does not support --filter")
                return GitResult(
                    returncode=1, stdout="",
                    stderr="server does not support --filter")
        return super().git(*args, **kwargs)


def test_ensure_repo_store_falls_back_to_full_fetch(root, upstream, capfd):
    """The spec's three-times-mandated fallback (L91, L221, L490): when the
    server refuses ``fetch --filter=blob:none``, gf retries a plain fetch,
    emits a warning, and the refs still land in the store (GF-D5/D7)."""
    fake = _RefuseFilterOnce()
    s = _store(root, upstream)
    shelf.ensure_repo_store(s, upstream, backend=fake)

    # the filtered attempt ran exactly once and a plain retry followed
    assert fake.refused == 1
    fetches = [c for c in fake.calls if "fetch" in c]
    assert any(not any(a.startswith("--filter") for a in c)
               for c in fetches), "no unfiltered retry fetch recorded"

    # a warning is emitted for the fallback
    captured = capfd.readouterr()
    assert "filter" in (captured.out + captured.err).lower()

    # refs landed despite the refusal
    master = _out("--git-dir", upstream, "rev-parse", "refs/heads/master")
    assert _sha(s, "refs/remotes/origin/master") == master


def test_ensure_repo_store_head_not_claiming_a_branch(store):
    """Store HEAD is repointed to refs/remotes/origin/HEAD (resolves
    through the remote symref — never under refs/heads/) so
    ``checkout -B master`` in a linked checkout is not refused
    (spec Checkout integrity paragraph; row receiving item)."""
    head = _out("--git-dir", store, "symbolic-ref", "HEAD")
    assert not head.startswith("refs/heads/")
    assert head.startswith("refs/remotes/origin/")


# ---------------------------------------------------------------------------
# shelf.ensure_checkout


def test_ensure_checkout_record_gitfile_removed_locked(root, store, co_docs):
    """worktree add --no-checkout --detach → record at
    <store>/worktrees/<key> with gitdir file → sparse cone → checkout -B →
    .git gitfile removed → worktree lock (spec Checkout integrity;
    GF-D8/D12; constraint gitfile)."""
    shelf.ensure_checkout(co_docs, "master")

    assert co_docs.work_tree.is_dir()
    admin = _admin(store, "master")
    assert admin.is_dir(), "git chose a different worktree record name"
    gitdir = (admin / "gitdir").read_text().strip()
    assert Path(gitdir) == (co_docs.work_tree / ".git")

    # .git gitfile removed — never under .gf/wt
    assert not (co_docs.work_tree / ".git").exists()
    assert not [p for p in (root / ".gf" / "wt").rglob("*")
                if p.name == ".git"]

    # locked — the record carries the `locked` file
    assert (admin / "locked").is_file()

    # sparse cone contains the binding subdir
    sparse = (admin / "info" / "sparse-checkout").read_text()
    assert "docs/api" in sparse

    # branch applied: HEAD == origin/master, on refs/heads/master
    master = _sha(store, "refs/remotes/origin/master")
    assert _out("--git-dir", admin, "rev-parse", "HEAD") == master
    assert _out("--git-dir", admin, "symbolic-ref", "HEAD") \
        == "refs/heads/master"

    # worktree list sees the locked record at the checkout path
    wt = _out("--git-dir", store, "worktree", "list", "--porcelain")
    assert f"worktree {co_docs.work_tree}" in wt
    assert "locked" in wt


def test_ensure_checkout_cone_union_on_join_and_state(root, store,
                                                      co_docs, co_tools):
    """Joining a second binding widens the cone to the union and writes the
    per-checkout state record incl. bindings list (spec: sparse-checked-out
    to the union of its bindings' subdirs; row state-record item)."""
    shelf.ensure_checkout(co_docs, "master")
    admin = _admin(store, "master")
    sparse1 = (admin / "info" / "sparse-checkout").read_text()
    assert "docs/api" in sparse1
    assert "tools/x" not in sparse1
    assert (co_docs.work_tree / "docs/api/x.txt").is_file()
    assert not (co_docs.work_tree / "tools/x/y.txt").exists()

    shelf.ensure_checkout(co_tools, "master")
    sparse2 = (admin / "info" / "sparse-checkout").read_text()
    assert "docs/api" in sparse2
    assert "tools/x" in sparse2
    assert (co_docs.work_tree / "tools/x/y.txt").is_file()

    # per-checkout state record at wt/<rk>/.<key>.state incl. bindings list
    data = tomllib.loads(co_docs.state.read_text())
    assert "bindings" in data
    blob = str(data["bindings"])
    assert "docs/api" in blob and "tools/x" in blob


def test_ensure_checkout_detached_for_tag_ref(root, upstream, store):
    """Tag/commit refs check out detached; the record lands under the ref
    checkout key (spec clone mechanics; `ref=` checkout keys)."""
    key = layout.checkout_key_for_ref("v1")
    co = layout.subfolder_checkout(root, upstream, key, "docs/api")
    shelf.ensure_checkout(co, "v1")
    admin = _admin(store, key)
    assert admin.is_dir()
    v1 = _out("--git-dir", upstream, "rev-parse", "refs/tags/v1^{commit}")
    assert _out("--git-dir", admin, "rev-parse", "HEAD") == v1
    # detached: HEAD is not a symref
    assert _git("--git-dir", admin, "symbolic-ref", "-q", "HEAD",
                check=False).returncode != 0
    assert not (co.work_tree / ".git").exists()
    assert (admin / "locked").is_file()


def test_ensure_checkout_one_store_one_record_per_key(store, co_docs, co_tools):
    """One store + one checkout per (repo, key): two bindings on the same
    key yield exactly one store gitdir and one worktrees/<key> record
    (row receiving item; GF-D5)."""
    shelf.ensure_checkout(co_docs, "master")
    shelf.ensure_checkout(co_tools, "master")
    records = [p.name for p in (store / "worktrees").iterdir()]
    assert records == ["master"]
    assert [p.name for p in store.parent.iterdir()] == ["git"]


def test_ensure_checkout_idempotent_and_lock_tolerates(store, co_docs):
    """Second ensure on the same checkout no-ops cleanly; the lock call
    tolerates an already-locked record (spec: left as is; row idempotence
    item)."""
    shelf.ensure_checkout(co_docs, "master")
    head1 = _out("--git-dir", _admin(store, "master"), "rev-parse", "HEAD")
    shelf.ensure_checkout(co_docs, "master")
    admin = _admin(store, "master")
    assert _out("--git-dir", admin, "rev-parse", "HEAD") == head1
    assert (admin / "locked").is_file()
    assert not (co_docs.work_tree / ".git").exists()


def test_ensure_checkout_never_rebuilds_over_existing_files(root, store,
                                                            co_docs):
    """A checkout directory with files is never rebuilt over
    (spec Checkout integrity last line; constraint no-rebuild-over-files)."""
    co_docs.work_tree.mkdir(parents=True)
    (co_docs.work_tree / "keep.txt").write_text("keep\n")
    with pytest.raises(GitFoldersError):
        shelf.ensure_checkout(co_docs, "master")
    assert (co_docs.work_tree / "keep.txt").read_text() == "keep\n"
    assert not _admin(store, "master").exists()


def test_ensure_checkout_missing_record_errors_files_untouched(store,
                                                               co_docs):
    """unlock + prune removes the worktree record while the checkout files
    remain; the next ensure reports an error and leaves files untouched
    (spec Checkout integrity: missing record → error with recovery steps;
    row receiving item)."""
    shelf.ensure_checkout(co_docs, "master")
    _git("--git-dir", store, "worktree", "unlock", co_docs.work_tree)
    _git("--git-dir", store, "worktree", "prune", "-v", "--expire", "now")
    admin = _admin(store, "master")
    assert not admin.exists(), "probe assumption: prune removes the record"
    before = _files_under(co_docs.work_tree)
    with pytest.raises(GitFoldersError):
        shelf.ensure_checkout(co_docs, "master")
    assert _files_under(co_docs.work_tree) == before


def test_ensure_checkout_failed_ref_teardown_keeps_store(root, store,
                                                         co_docs):
    """On failure the call tears down only what it created — the repo
    store stays intact, no worktree record or checkout dir lingers
    (spec L221 mechanics: remove checkout this command created; retain the
    store; constraint second-checkout/teardown)."""
    refspec_before = _refspec_lines(store)
    with pytest.raises(GitFoldersError):
        shelf.ensure_checkout(co_docs, "no-such-ref-xyz")
    assert not _admin(store, "master").exists()
    assert not co_docs.work_tree.exists() or _files_under(
        co_docs.work_tree) == []
    assert store.is_dir()
    assert _refspec_lines(store) == refspec_before
    _sha(store, "refs/remotes/origin/master")  # store still serves fetches


# ---------------------------------------------------------------------------
# shelf.ensure_consumer_link


def test_ensure_consumer_link_is_relative_symlink(root, store, co_docs):
    """The consumer link is a relative symlink into the checkout's subdir
    (spec L88: relative symlink → .gf/wt/<rk>/<key>/<subdir>; GF-D13)."""
    shelf.ensure_checkout(co_docs, "master")
    link = root / "vendor/api"
    target = co_docs.work_tree / "docs/api"
    shelf.ensure_consumer_link(link, target)
    assert link.is_symlink()
    assert not os.readlink(link).startswith("/")
    assert link.resolve() == target.resolve()


def test_ensure_consumer_link_retargets(root, upstream, store, co_docs):
    """Re-ensuring a link repoints it — the worktree-add/re-join relink
    path (spec: joining an existing checkout re-links only; GF-D13)."""
    shelf.ensure_checkout(co_docs, "master")
    co_dev = layout.subfolder_checkout(root, upstream, "dev", "docs/api")
    shelf.ensure_checkout(co_dev, "dev")
    link = root / "vendor/api"
    shelf.ensure_consumer_link(link, co_docs.work_tree / "docs/api")
    assert link.resolve() == (co_docs.work_tree / "docs/api").resolve()
    shelf.ensure_consumer_link(link, co_dev.work_tree / "docs/api")
    assert link.resolve() == (co_dev.work_tree / "docs/api").resolve()
    assert not os.readlink(link).startswith("/")


def test_ensure_consumer_link_refuses_existing_directory(root, store,
                                                         co_docs):
    """The failure half of the rollback pair: a real directory at the
    consumer path is never replaced — error + preserved state (spec
    never-destructive semantics; GF-D13)."""
    shelf.ensure_checkout(co_docs, "master")
    link = root / "vendor/api"
    link.mkdir(parents=True)
    (link / "real.txt").write_text("real\n")
    with pytest.raises(GitFoldersError):
        shelf.ensure_consumer_link(link, co_docs.work_tree / "docs/api")
    assert link.is_dir() and not link.is_symlink()
    assert (link / "real.txt").read_text() == "real\n"


def test_plain_git_inside_consumer_link_resolves_to_parent(root, store,
                                                           co_docs):
    """Plain git inside the consumer link resolves to the parent repo —
    the removed-gitfile + link design (spec: plain git and .git-scanning
    tools inside a consumer link resolve to the parent; GF-D8)."""
    shelf.ensure_checkout(co_docs, "master")
    link = root / "vendor/api"
    shelf.ensure_consumer_link(link, co_docs.work_tree / "docs/api")
    top = _out("-C", link, "rev-parse", "--show-toplevel")
    assert Path(top).resolve() == root.resolve()
