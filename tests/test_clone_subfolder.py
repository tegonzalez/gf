# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`gf clone`/`gf init` creating subfolder bindings through the seam.

Real-git pins exercised through the landed machinery (`layout.repo_store`,
`shelf.ensure_repo_store`/`ensure_checkout`/`ensure_consumer_link`,
`rollback_consumer_link`).

Authorities: spec `gf clone`/`gf init`, "URL resolution", "Parent and
child gitignore handling", "Subfolder-binding layout", L221 mechanics;
arch GF-D6/D12; constraints: manifest schema, refspec append-only.
"""

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from conftest import gf, git
from gf import shelf


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} rc={r.returncode}:\n{r.stderr}")
    return r


def _git_out(*args) -> str:
    return _git(*args).stdout.strip()


# --- fixtures ----------------------------------------------------------


def _real_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path) -> Path:
    """Bare upstream. master tree: docs/api/x.txt, docs/top.md (leading-
    dir file of the mapped docs/api), tools/y.txt, src/s.txt (mapped),
    root.txt (root file), unmapped/u.txt, and same-named-at-depth trees
    lib/src/l.txt + vendor/docs/api/v.txt. dev adds docs/api/dev.txt;
    branch `ref=v1` adds docs/api/refeq.txt; tag v1 sits on master."""
    up = tmp_path / "upstream"
    up.mkdir()
    git("init", "--bare", cwd=up)
    work = tmp_path / "_seed"
    git("clone", str(up), str(work), cwd=tmp_path)
    for d in ("docs/api", "tools", "unmapped", "src", "lib/src",
              "vendor/docs/api"):
        (work / d).mkdir(parents=True, exist_ok=True)
    (work / "docs" / "api" / "x.txt").write_text("api on master")
    (work / "docs" / "top.md").write_text("leading-dir file")
    (work / "tools" / "y.txt").write_text("tool on master")
    (work / "src" / "s.txt").write_text("src on master")
    (work / "root.txt").write_text("root file")
    (work / "unmapped" / "u.txt").write_text("unmapped dir")
    (work / "lib" / "src" / "l.txt").write_text("lib/src marker")
    (work / "vendor" / "docs" / "api" / "v.txt").write_text(
        "vendor/docs/api marker")
    git("add", "-A", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("push", "-q", "origin", "master", cwd=work)
    for branch, marker in (("dev", "dev.txt"), ("ref=v1", "refeq.txt")):
        git("checkout", "-q", "-b", branch, "master", cwd=work)
        (work / "docs" / "api" / marker).write_text(f"api on {branch}")
        git("add", "-A", cwd=work)
        git("commit", "-m", branch, cwd=work)
        git("push", "-q", "origin", branch, cwd=work)
        git("checkout", "-q", "master", cwd=work)
    git("tag", "v1", cwd=work)
    git("push", "-q", "origin", "v1", cwd=work)
    return up


def _repo_key_dir(parent: Path) -> Path:
    wt = parent / ".gf" / "wt"
    entries = [p for p in wt.iterdir() if p.is_dir()]
    assert len(entries) == 1, f"expected one repo store, found {entries}"
    assert re.fullmatch(r"upstream-[0-9a-f]{8}", entries[0].name)
    return entries[0]


def _advance(upstream: Path, tmp_path: Path, tag: str) -> None:
    """Push one more commit to upstream master (docs/api marker)."""
    work = tmp_path / f"_adv_{tag}"
    git("clone", str(upstream), str(work), cwd=tmp_path)
    (work / "docs" / "api" / "x.txt").write_text(f"api {tag}")
    git("add", "-A", cwd=work)
    git("commit", "-m", tag, cwd=work)
    git("push", "origin", "master", cwd=work)


def _checkout_dirs(rk: Path) -> list[str]:
    return sorted(p.name for p in rk.iterdir() if p.is_dir())


def _store(parent: Path, rk: Path) -> Path:
    return parent / ".gf" / "repos" / rk.name / "git"


def _refspec_lines(store: Path) -> list[str]:
    r = subprocess.run(
        ["git", "--git-dir", str(store), "config", "--get-all",
         "remote.origin.fetch"],
        capture_output=True, text=True,
    )
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _sparse(store: Path, key: str) -> str:
    f = store / "worktrees" / key / "info" / "sparse-checkout"
    return f.read_text() if f.is_file() else ""


# --- receiving checks ---------------------------------------------------


def test_three_subfolders_share_one_store_one_checkout(tmp_path):
    """Three subfolders of one repo → one store, one checkout, three
    links; the cone-mode sparse checkout materializes every mapped
    folder in full, the allowed root/leading-dir files, and NO other
    folder trees — in particular no same-named folder at another depth
    (requester's verbatim acceptance criterion + spec Subfolder-binding
    layout / clone mechanics "sparse-checked-out in cone mode"; row
    item 1).

    Criterion (verbatim): "Through each consumer link, the user sees
    exactly the mapped folder's contents. The hidden checkout contains
    every mapped folder in full and no other folder trees. In
    particular it never contains a same-named folder at another depth
    (lib/src when src is mapped, vendor/docs/api when docs/api is
    mapped). Files cone mode materializes by design are allowed in the
    hidden checkout: files at the repo root, and files directly inside
    each parent folder of a mapped folder (e.g. docs/top.md when
    docs/api is mapped). They are never visible through a link."
    """
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)

    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/api")
    gf("-C", str(parent), "clone", str(upstream / "tools"),
       "vendor/tools", "-b", "master")
    gf("-C", str(parent), "clone", str(upstream / "src"), "vendor-src")

    rk = _repo_key_dir(parent)
    assert _checkout_dirs(rk) == ["master"]
    store = _store(parent, rk)
    checkout = rk / "master"

    # links land inside the one shared checkout's mapped subdirs
    api_link = parent / "vendor" / "api"
    tools_link = parent / "vendor" / "tools"
    src_link = parent / "vendor-src"
    assert api_link.resolve() == (checkout / "docs" / "api").resolve()
    assert tools_link.resolve() == (checkout / "tools").resolve()
    assert src_link.resolve() == (checkout / "src").resolve()

    # every mapped folder materializes in full
    assert (checkout / "docs" / "api" / "x.txt").is_file()
    assert (checkout / "tools" / "y.txt").is_file()
    assert (checkout / "src" / "s.txt").is_file()

    # allowed cone materialization: root files + leading-dir files
    assert (checkout / "root.txt").is_file()
    assert (checkout / "docs" / "top.md").is_file()

    # no other folder trees — including same-named folders at another
    # depth: lib/src when src is mapped, vendor/docs/api when docs/api
    # is mapped
    assert not (checkout / "unmapped").exists()
    assert not (checkout / "lib").exists()
    assert not (checkout / "vendor").exists()

    # cone-mode patterns carry the bound subdirs' entries and nothing
    # for the unmapped/same-named trees (`set --cone` writes `/<dir>/`)
    sparse = _sparse(store, "master")
    lines = [ln.strip() for ln in sparse.splitlines()]
    assert "/docs/api/" in lines
    assert "/tools/" in lines
    assert "/src/" in lines
    for name in ("unmapped", "lib", "vendor"):
        assert name not in sparse

    # through each consumer link the user sees exactly the mapped
    # folder's contents: leading-dir and same-named-tree files are
    # never visible through a link
    assert (api_link / "x.txt").is_file()
    assert not (api_link / "top.md").exists()
    assert not (api_link / "v.txt").exists()
    assert (src_link / "s.txt").is_file()
    assert not (src_link / "l.txt").exists()


def test_plain_git_inside_link_resolves_to_parent(tmp_path):
    """Plain git inside a consumer link resolves to the parent repo
    (spec Subfolder-binding layout; GF-D8; row item)."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/api")
    link = parent / "vendor" / "api"
    assert link.is_symlink()
    top = _git_out("-C", str(link), "rev-parse", "--show-toplevel")
    assert Path(top).resolve() == parent.resolve()


def test_depth_and_single_branch_only_at_store_creation(tmp_path):
    """--depth/--single-branch act only when this clone creates the store:
    a joining clone ignores --single-branch (refspec line for its branch
    is appended, never narrowed) and ignores --depth (no shallow file)
    (spec clone flags + Fetch refspecs; row item)."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)

    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/api", "--single-branch")
    rk = _repo_key_dir(parent)
    store = _store(parent, rk)
    assert _refspec_lines(store) == [
        "+refs/heads/master:refs/remotes/origin/master"]

    # Joining binding: --single-branch and --depth are ignored for the
    # existing store; the needed branch's line is appended.
    gf("-C", str(parent), "clone", str(upstream / "tools"),
       "vendor/tools", "-b", "dev", "--single-branch", "--depth", "1")
    assert _refspec_lines(store) == [
        "+refs/heads/master:refs/remotes/origin/master",
        "+refs/heads/dev:refs/remotes/origin/dev",
    ]
    assert not (store / "shallow").exists()


def test_reclone_after_rm_finds_uncommitted_work_in_place(tmp_path):
    """Spec L221's explicit scenario: a binding removed with `gf rm` and
    added back finds its uncommitted work in place — joining re-links
    only, never re-checks out (row item)."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/api")
    dirty = parent / "vendor" / "api" / "dirty.txt"
    dirty.write_text("uncommitted\n")

    gf("-C", str(parent), "rm", "vendor/api")
    assert not (parent / "vendor" / "api").exists()

    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/api")
    assert (parent / "vendor" / "api" / "dirty.txt").read_text() == (
        "uncommitted\n")


def test_failed_clone_tears_down_only_what_it_created(tmp_path):
    """A clone that fails after creating its checkout removes that
    checkout and never the shared store or existing checkouts (spec L221
    failure cleanup; row item)."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/api")
    rk = _repo_key_dir(parent)
    store = _store(parent, rk)

    # block/api cannot be created: `block` is a real file — the consumer
    # link step fails AFTER the dev checkout was created.
    (parent / "block").write_text("file in the way\n")
    r = gf("-C", str(parent), "clone", str(upstream / "tools"),
           "block/api", "-b", "dev", check=False)
    assert r.returncode != 0

    # the checkout this clone created is torn down; store + master stay
    assert not (rk / "dev").exists()
    assert not (store / "worktrees" / "dev").exists()
    assert (rk / "master").is_dir()
    assert (store / "worktrees" / "master").is_dir()
    assert not (parent / "block" / "api").exists()
    manifest = (parent / "gf.toml").read_text()
    assert 'path = "block/api"' not in manifest


def test_branch_named_ref_equals_coexists_with_tag(tmp_path):
    """Checkout-key disjointness at CLI level: branch literally named `ref=v1` keys
    `ref%3Dv1`; tag `v1` keys `ref=v1` — two disjoint checkouts serving
    their own refs (spec Reference model; GF-D7)."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)

    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/api-branch", "-b", "ref=v1")
    gf("-C", str(parent), "clone", str(upstream / "tools"),
       "vendor/api-tag", "-b", "v1")

    rk = _repo_key_dir(parent)
    assert _checkout_dirs(rk) == ["ref%3Dv1", "ref=v1"]
    assert (parent / "vendor" / "api-branch" / "refeq.txt").read_text() == (
        "api on ref=v1")
    assert (parent / "vendor" / "api-tag" / "y.txt").read_text() == (
        "tool on master")


def test_whole_repo_clone_unchanged(tmp_path):
    """Whole-repo clone regression: `.gf/git` inside the child, no
    `.gf/` store or `.gf/wt` at the parent root (spec: the layouts do not
    share object storage; row item)."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/wr")
    assert (parent / "vendor" / "wr" / ".gf" / "git").is_dir()
    assert not (parent / ".gf").exists()


def test_clone_prints_gitignore_recommendations(tmp_path):
    """gitignore recommendations: `add "<path>" to .gitignore` without a
    trailing slash for the binding path, plus `add ".gf/" to .gitignore`
    once when the clone first creates the parent-root store dir (spec
    'Parent and child gitignore handling'; row item)."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    r1 = gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
            "vendor/api")
    assert 'add "vendor/api" to .gitignore' in r1.stdout
    assert 'add ".gf/" to .gitignore' in r1.stdout

    r2 = gf("-C", str(parent), "clone", str(upstream / "tools"),
            "vendor/tools", "-b", "master")
    assert 'add "vendor/tools" to .gitignore' in r2.stdout
    assert 'add ".gf/" to .gitignore' not in r2.stdout

    # recommendations only — never written
    assert not (parent / ".gitignore").exists()


def test_clone_subfolder_of_local_gf_child_roundtrips(tmp_path):
    """A subfolder URL whose nearest repo boundary is a local `gf`
    whole-repo child records the child's FETCHABLE gitdir
    (`<child>/.gf/git`) as the repo store's `remote.origin.url` — a
    verbatim consumer-path URL is not a git dir and cannot be fetched
    (ruling F3: `_resolved_git_url` at the store-create / ls-remote
    seams). The binding then round-trips: `gf pull` after upstream
    advances lands the new content through the child."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "lib")
    child_gitdir = parent / "lib" / ".gf" / "git"
    assert (child_gitdir / "HEAD").is_file()

    gf("-C", str(parent), "clone",
       str(parent / "lib" / "docs" / "api"), "vendor/api")

    # the store's origin names a real git dir: the gf child's inner
    # gitdir, not the worktree directory that merely contains it
    repos = parent / ".gf" / "repos"
    stores = [p for p in repos.iterdir() if p.is_dir()]
    assert len(stores) == 1, f"expected one repo store, found {stores}"
    assert stores[0].name.startswith("lib-")
    url = _git_out("--git-dir", str(stores[0] / "git"),
                   "config", "remote.origin.url")
    assert url.endswith(".gf/git"), url
    assert (Path(url) / "HEAD").is_file()

    api = parent / "vendor" / "api"
    assert api.is_symlink()
    assert (api / "x.txt").read_text().strip() == "api on master"

    # round-trip: advancing upstream, then `gf pull` — the whole-repo
    # child updates first, and the subfolder store fetches the new
    # commit from the child's inner gitdir
    _advance(upstream, tmp_path, "adv")
    gf("-C", str(parent), "pull")
    assert (parent / "lib" / "docs" / "api" / "x.txt"
            ).read_text().strip() == "api adv"
    assert (api / "x.txt").read_text().strip() == "api adv"


# ---------------------------------------------------------------------------
# F1 — duplicate consumer path (round-2 ruling)


def test_clone_refuses_consumer_path_already_bound(tmp_path):
    """F1: `gf clone <same-upstream>/docs/api vendor/api -n alias` while
    `api` owns `vendor/api` must REFUSE — the consumer path is already a
    recorded binding path. The duplicate check compares the lexical
    consumer spelling, never the `.gf/wt/...` realpath the existing link
    resolves to: on refusal the manifest still holds exactly the
    original binding (`path = "vendor/api"`, never an interior
    spelling), and the live link is untouched."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/api")
    link = parent / "vendor" / "api"
    target_before = os.readlink(link)
    manifest_before = (parent / "gf.toml").read_bytes()

    r = gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
           "vendor/api", "-n", "alias", check=False)
    assert r.returncode != 0

    # the manifest is exactly the original binding — byte-identical and
    # still spelling the lexical consumer path (the defect signature is
    # a recorded `path` pointing inside `.gf/wt`)
    assert (parent / "gf.toml").read_bytes() == manifest_before
    entries = tomllib.loads(manifest_before.decode())["git_folder"]
    assert [(f["name"], f["path"]) for f in entries] == [
        ("api", "vendor/api")]
    assert not any(f["path"].startswith(".gf") for f in entries)

    # the live consumer link is untouched
    assert link.is_symlink()
    assert os.readlink(link) == target_before


def test_clone_binds_unrecorded_consumer_link_path(tmp_path):
    """F1 positive twin: a consumer path that IS a symlink into
    `.gf/wt/<rk>/<ck>/<subdir>` but is NOT recorded in the manifest is
    still bindable — the duplicate-path guard compares the lexical
    consumer path, it does not refuse a path merely for resolving
    inside `.gf`. The clone records the spelled path."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/api")
    rk = _repo_key_dir(parent)

    # an unrecorded consumer-link-shaped symlink into the same
    # shared-checkout subdir (e.g. left by a hand-edited manifest or a
    # retired binding record)
    stale = parent / "vendor" / "stale"
    stale.symlink_to(
        os.path.relpath(rk / "master" / "docs" / "api", stale.parent))
    assert stale.is_symlink()

    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/stale")

    entries = tomllib.loads(
        (parent / "gf.toml").read_text())["git_folder"]
    by_name = {f["name"]: f["path"] for f in entries}
    assert by_name == {"api": "vendor/api", "stale": "vendor/stale"}

    # the path keeps its consumer-link shape and still serves the
    # mapped subdir through the shared checkout
    assert stale.is_symlink()
    assert stale.resolve() == (rk / "master" / "docs" / "api").resolve()
    assert (stale / "x.txt").read_text().strip() == "api on master"


# ---------------------------------------------------------------------------
# Round-3 ruling — a committed file named HEAD is not a bare-repo marker


def test_head_file_inside_repo_is_not_bare_boundary(tmp_path):
    """A working repo's tracked folder may contain an ordinary committed
    file named `HEAD` — it is content, not a bare-repository marker. The
    local URL walk-up must not terminate at `docs/` (spec "URL
    resolution" rule 1: the boundary is the nearest *repository*
    directory — a `.git` entry, a bare repository, or a `.gf/git`
    child): `gf clone <R>/docs` resolves the boundary at `R`, records
    the repo store's `remote.origin.url` as `R` (not `R/docs`), and the
    consumer link materializes the mapped folder's content. Equivalent
    formulation: `resolve_repo_url("<R>/docs")` returns
    `(str(R), "docs")`.
    """
    repo = tmp_path / "R"
    repo.mkdir()
    git("init", cwd=repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "HEAD").write_text("ordinary committed file\n")
    (repo / "docs" / "index.md").write_text("docs index\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "init", cwd=repo)
    parent = _real_parent(tmp_path)

    # the boundary resolves at R — not at the HEAD-carrying docs/
    repo_url, subdir = shelf.resolve_repo_url(
        str(repo / "docs"), parent_root=parent)
    assert Path(repo_url) == repo.resolve()
    assert subdir == "docs"

    gf("-C", str(parent), "clone", str(repo / "docs"), "vendor/docs")

    # one repo store, keyed on R and fetching from R — never R/docs
    repos = parent / ".gf" / "repos"
    stores = [p for p in repos.iterdir() if p.is_dir()]
    assert len(stores) == 1, f"expected one repo store, found {stores}"
    assert stores[0].name.startswith("R-")
    url = _git_out("--git-dir", str(stores[0] / "git"),
                   "config", "remote.origin.url")
    assert Path(url) == repo.resolve()

    # the consumer link materializes the mapped folder's content — the
    # file named HEAD arrives through the link as ordinary content
    link = parent / "vendor" / "docs"
    assert link.is_symlink()
    assert (link / "index.md").read_text() == "docs index\n"
    assert (link / "HEAD").read_text() == "ordinary committed file\n"


# ---------------------------------------------------------------------------
# F-A — worktree-record collision rollback (round-3 ruling)


def test_record_collision_rollback_keeps_foreign_worktree(tmp_path):
    """Worktree-record collision rollback (F-A receiving pin; spec L221:
    "a repo store or checkout that was pre-existing or already serving
    other bindings is always retained").

    When a foreign worktree already holds `<store>/worktrees/<key>`,
    gf's `git worktree add` for checkout key `<key>` silently creates
    `<key>1` instead and the record verification fails — the checkout
    was never created. Rollback must undo only what the failed call
    made (the `<key>1` record and the checkout dir): the foreign
    `worktrees/<key>` record and the worktree it names stay usable, no
    suffixed record leaks, and no consumer link or checkout state is
    left behind for the failed binding.
    """
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/api")

    rk = _repo_key_dir(parent)
    store = _store(parent, rk)
    checkout = rk / "master"
    state_file = rk / ".master.state"
    link = parent / "vendor" / "api"

    # Strip everything gf made for the `master` checkout so `gf pull`
    # must create it again: consumer link, checkout dir, record, state.
    link.unlink()
    shutil.rmtree(checkout)
    shutil.rmtree(store / "worktrees" / "master")
    state_file.unlink()

    # A foreign worktree takes the `master` record name — its record's
    # `gitdir` names the foreign worktree's own `.git`, never gf's.
    foreign = tmp_path / "scratch" / "master"
    _git("--git-dir", store, "worktree", "add", "--detach",
         foreign, "origin/master")
    foreign_head = _git_out("-C", foreign, "rev-parse", "HEAD")

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode != 0
    assert "did not create the expected worktree record" in r.stderr

    # The foreign record still names — and still serves — its own
    # worktree, and the shared repo store stays intact.
    record = store / "worktrees" / "master"
    assert Path(record.joinpath("gitdir").read_text().strip()) == \
        foreign / ".git"
    assert f"worktree {foreign}" in _git_out(
        "--git-dir", store, "worktree", "list", "--porcelain")
    assert _git_out("-C", foreign, "rev-parse", "HEAD") == foreign_head
    assert (store / "HEAD").is_file()

    # No `<key>1`-style record leaked by the failed creation.
    assert [p.name for p in (store / "worktrees").iterdir()] == ["master"]

    # Nothing of the failed binding lingers — no checkout dir, no state
    # file, no consumer link (lexists also catches a dangling link).
    assert not os.path.lexists(checkout)
    assert not state_file.exists()
    assert not os.path.lexists(link)


# ---------------------------------------------------------------------------
# R6-B — a local `url` resolving into `.gf` interior is refused (round-6
# ruling). `.gf` roots gf's private layout — a child's `git`/`state`, a
# parent's `repos` stores and `wt` checkout trees — never a repository
# nor repository content a consumer spelling may resolve into (spec
# "URL resolution" rule 1: the walk-up terminates at the nearest
# *repository* directory; arch GF-D8: a `.git` entry inside `.gf/wt` is
# the removed gitfile's position or upstream content, not a boundary).
# The refusal surfaces verbatim on BOTH clone and pull with the
# resolver's clear message (exit code 1, the GitFoldersError family) —
# never degrading to a raw git fetch failure — and leaves no child,
# link, store, or manifest entry behind.


def _parent(tmp_path: Path, name: str) -> Path:
    """A consumer parent repo under a chosen name (`_real_parent` shape:
    `git init` + one root commit) — needed when a scenario holds TWO
    parents: the one whose `.gf` interior is spelled and the one
    cloning."""
    parent = tmp_path / name
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text(f"root {name}")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _manifest_entries(parent: Path) -> list[dict]:
    gf_toml = parent / "gf.toml"
    if not gf_toml.is_file():
        return []
    return tomllib.loads(gf_toml.read_text()).get("git_folder", [])


def test_clone_refuses_consumer_link_url(tmp_path):
    """R6-B discriminating arm: `gf clone <other-parent>/<consumer-link>
    x` must REFUSE. The consumer link's realpath is a managed checkout
    inside `.gf/wt` — gf's private layout, not a repository (spec URL
    resolution rule 1; GF-D8). Verified pre-fix signature: rc 0, `x` a
    dangling symlink into `.gf/wt/...`, the bogus binding recorded — a
    silent wrong-success. Post-fix: rc 1, the refusal names the `.gf`
    interior, and nothing is left behind — no child dir or link at `x`,
    no `.gf` storage at the cloning parent, no manifest entry."""
    upstream = _upstream(tmp_path)
    parent_a = _parent(tmp_path, "parent-a")
    gf("-C", str(parent_a), "clone", str(upstream / "docs" / "api"),
       "vendor/api")
    link = parent_a / "vendor" / "api"
    assert link.is_symlink()

    parent_b = _parent(tmp_path, "parent-b")
    r = gf("-C", str(parent_b), "clone", str(link), "x", check=False)
    assert r.returncode == 1
    assert "is inside a '.gf' directory" in r.stderr

    # nothing of the refused binding is left behind: no consumer path,
    # no `.gf` storage, no manifest entry
    assert not os.path.lexists(parent_b / "x")
    assert not (parent_b / ".gf").exists()
    assert _manifest_entries(parent_b) == []

    # the source binding is untouched
    assert (link / "x.txt").read_text().strip() == "api on master"


def test_clone_refuses_direct_gf_wt_spelling(tmp_path):
    """R6-B: the same refusal when the `url` spells the `.gf/wt`
    interior directly — `<A>/.gf/wt/<repo-key>/<checkout>/docs/api` —
    not merely when it arrives through a consumer link."""
    upstream = _upstream(tmp_path)
    parent_a = _parent(tmp_path, "parent-a")
    gf("-C", str(parent_a), "clone", str(upstream / "docs" / "api"),
       "vendor/api")
    rk = _repo_key_dir(parent_a)
    interior = rk / "master" / "docs" / "api"
    assert interior.is_dir()
    # the spelling names the very directory the consumer link serves
    assert interior.resolve() == (parent_a / "vendor" / "api").resolve()

    parent_b = _parent(tmp_path, "parent-b")
    r = gf("-C", str(parent_b), "clone", str(interior), "x", check=False)
    assert r.returncode == 1
    assert "is inside a '.gf' directory" in r.stderr
    assert not os.path.lexists(parent_b / "x")
    assert not (parent_b / ".gf").exists()
    assert _manifest_entries(parent_b) == []


def test_pull_refuses_init_placeholder_url_inside_gf(tmp_path):
    """R6-B pull surface (init placeholder): a `gf init --url` binding
    whose local `url` names another parent's consumer link fails
    `gf pull` with the resolver's clear refusal — rc 1, the
    GitFoldersError surface — NOT a raw git fetch failure (rc 2). The
    placeholder child and the manifest entry are unchanged."""
    upstream = _upstream(tmp_path)
    parent_a = _parent(tmp_path, "parent-a")
    gf("-C", str(parent_a), "clone", str(upstream / "docs" / "api"),
       "vendor/api")

    parent_b = _parent(tmp_path, "parent-b")
    gf("-C", str(parent_b), "init", "ph",
       "--url", str(parent_a / "vendor" / "api"))

    r = gf("-C", str(parent_b), "pull", check=False)
    assert r.returncode == 1
    assert "is inside a '.gf' directory" in r.stderr

    # the placeholder is untouched: still a real directory holding only
    # the init gitdir — never stripped to a consumer link
    ph = parent_b / "ph"
    assert ph.is_dir() and not ph.is_symlink()
    assert (ph / ".gf" / "git" / "HEAD").is_file()
    assert [(e["name"], e["path"]) for e in _manifest_entries(parent_b)
            ] == [("ph", "ph")]


def test_pull_refuses_overridden_url_inside_gf(tmp_path):
    """R6-B pull surface (established subfolder binding): a `url`
    override (`gf.local.toml`, the spec's supported override channel)
    retargeting the binding at `.gf` interior is refused when the pull
    resolves the changed url — same rc 1 refusal, and the live binding
    is left exactly as recorded: consumer link, checkout, and content
    all intact."""
    upstream = _upstream(tmp_path)
    parent_a = _parent(tmp_path, "parent-a")
    gf("-C", str(parent_a), "clone", str(upstream / "docs" / "api"),
       "vendor/api")

    parent_b = _parent(tmp_path, "parent-b")
    gf("-C", str(parent_b), "clone", str(upstream / "docs" / "api"), "s")
    link = parent_b / "s"
    target_before = os.readlink(link)

    (parent_b / "gf.local.toml").write_text(
        "[[git_folder_override]]\n"
        'name = "s"\n'
        f'url = "{parent_a / "vendor" / "api"}"\n'
    )

    r = gf("-C", str(parent_b), "pull", check=False)
    assert r.returncode == 1
    assert "is inside a '.gf' directory" in r.stderr

    # nothing changed: the link still serves the original content
    assert link.is_symlink()
    assert os.readlink(link) == target_before
    assert (link / "x.txt").read_text().strip() == "api on master"


def test_clone_from_child_gf_git_leaf_resolves(tmp_path):
    """R6-B boundary exception: `gf clone <child>/.gf/git x` — the leaf
    ITSELF is the repository boundary (the child's fetchable inner
    gitdir), so it self-resolves per the `_resolved_git_url` precedent
    and the clone is an ordinary whole-repo child."""
    upstream = _upstream(tmp_path)
    parent_a = _parent(tmp_path, "parent-a")
    gf("-C", str(parent_a), "clone", str(upstream), "lib")
    gitdir = parent_a / "lib" / ".gf" / "git"
    assert (gitdir / "HEAD").is_file()

    parent_b = _parent(tmp_path, "parent-b")
    gf("-C", str(parent_b), "clone", str(gitdir), "x")
    assert (parent_b / "x" / ".gf" / "git" / "HEAD").is_file()
    # whole-repo content materializes through the fetched gitdir
    assert (parent_b / "x" / "docs" / "api" / "x.txt"
            ).read_text().strip() == "api on master"


def test_repo_store_gitdir_leaf_still_resolves(tmp_path):
    """A REAL repo store's gitdir `<root>/.gf/repos/<rk>/git` remains a
    self-resolving repository boundary (the ruling's second exception):
    resolution returns the gitdir itself as the repo URL, while the
    `.gf/wt` checkout it serves stays refused (covered above)."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/api")
    store = _store(parent, _repo_key_dir(parent))
    assert (store / "HEAD").is_file()

    repo_url, subdir = shelf.resolve_repo_url(str(store),
                                              parent_root=parent)
    assert Path(repo_url) == store.resolve()
    assert subdir == ""


def test_clone_controls_repo_containing_committed_gf_dir(tmp_path):
    """Non-`.gf` control + lexical ruling: a normal repo that happens to
    carry COMMITTED `.gf` content resolves and clones normally for
    leaves outside `.gf` — while a leaf at-or-inside that `.gf` subtree
    is refused anyway (the refusal is lexical, "at-or-inside a `.gf`
    subtree", not conditioned on gf's private layout)."""
    repo = tmp_path / "R"
    repo.mkdir()
    git("init", cwd=repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "index.md").write_text("docs index\n")
    (repo / ".gf" / "notes").mkdir(parents=True)
    (repo / ".gf" / "notes" / "n.txt").write_text(
        "committed .gf content\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "init", cwd=repo)
    parent = _real_parent(tmp_path)

    # ordinary leaves resolve: committed `.gf` content does not poison
    # the repository's own resolution
    gf("-C", str(parent), "clone", str(repo / "docs"), "vendor/docs")
    assert (parent / "vendor" / "docs" / "index.md").read_text() == (
        "docs index\n")

    # but a leaf at-or-inside a `.gf` subtree refuses even here
    r = gf("-C", str(parent), "clone", str(repo / ".gf" / "notes"),
           "x", check=False)
    assert r.returncode == 1
    assert "is inside a '.gf' directory" in r.stderr
    assert not os.path.lexists(parent / "x")
    assert [e["path"] for e in _manifest_entries(parent)] == [
        "vendor/docs"]
# R6-A — pinned-ref (tag/commit) coverage on the store-join path
#
# A store narrowed by `--single-branch` carries only `+refs/heads/<b>:...`
# refspec lines, and git's tag auto-follow lands only tags pointing into
# fetched history — a tag on an unfetched branch's commit is invisible to
# a joining `gf clone -b <tag>`. The join must probe the store for the
# ref, `ls-remote` the upstream for `refs/tags/<t>`, `config --add` the
# tag's refspec line (append-only — existing lines never move), and let
# the same join fetch land it. Pre-fix signature: `could not resolve ref
# 'v2'` — the join fetch ran but nothing ever covered the tag.


def _diverged_upstream(tmp_path: Path) -> Path:
    """Bare upstream with diverged `master`/`feature` and tag `v2` pinned
    on the feature tip — a commit master's history cannot reach, so a
    `master`-only store fetch never lands `v2`'s ref or objects."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    work = tmp_path / "_seed"
    git("clone", str(upstream), str(work), cwd=tmp_path)
    (work / "docs" / "api").mkdir(parents=True)
    (work / "tools").mkdir()
    (work / "docs" / "api" / "x.txt").write_text("api on master")
    (work / "tools" / "t.txt").write_text("tool on master")
    git("add", "-A", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("push", "-q", "origin", "master", cwd=work)
    git("checkout", "-q", "-b", "feature", cwd=work)
    (work / "docs" / "api" / "feat.txt").write_text("api on feature")
    git("add", "-A", cwd=work)
    git("commit", "-m", "feature", cwd=work)
    git("push", "-q", "origin", "feature", cwd=work)
    git("tag", "v2", cwd=work)
    git("push", "-q", "origin", "v2", cwd=work)
    git("checkout", "-q", "master", cwd=work)
    # master advances past the fork point: the histories diverge.
    (work / "docs" / "api" / "x2.txt").write_text("api on master2")
    git("add", "-A", cwd=work)
    git("commit", "-m", "master2", cwd=work)
    git("push", "-q", "origin", "master", cwd=work)
    return upstream


def test_join_clone_covers_tag_on_unfetched_branch(tmp_path):
    """R6-A join arm: a `--single-branch` store covering only `master`
    cannot reach tag `v2` (pinned on the diverged `feature` tip). The
    joining `-b v2` clone must append `+refs/tags/v2:refs/tags/v2` —
    leaving the narrowed master line verbatim — land the tag with the
    same fetch, and serve a detached `ref=v2` checkout through the
    consumer link. Pre-fix the join died `could not resolve ref 'v2'`.
    """
    upstream = _diverged_upstream(tmp_path)
    parent = _real_parent(tmp_path)

    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/a", "--single-branch", "-b", "master")
    rk = _repo_key_dir(parent)
    store = _store(parent, rk)
    assert _refspec_lines(store) == [
        "+refs/heads/master:refs/remotes/origin/master"]
    assert _git("--git-dir", store, "rev-parse", "--verify",
                "refs/tags/v2", check=False).returncode != 0

    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/b", "-b", "v2")

    # append-only coverage: the narrowed master line survives verbatim;
    # the tag's line is added, never a rewrite or removal.
    assert _refspec_lines(store) == [
        "+refs/heads/master:refs/remotes/origin/master",
        "+refs/tags/v2:refs/tags/v2",
    ]

    # the link materializes v2's tree — feature marker present, the
    # post-fork master commit absent — from a detached ref=v2 checkout.
    link = parent / "vendor" / "b"
    assert link.resolve() == (rk / "ref=v2" / "docs" / "api").resolve()
    assert (link / "feat.txt").read_text() == "api on feature"
    assert not (link / "x2.txt").exists()
    v2_sha = _git_out("--git-dir", store, "rev-parse", "refs/tags/v2^{}")
    admin_head = (
        store / "worktrees" / "ref=v2" / "HEAD").read_text().strip()
    assert admin_head == v2_sha  # detached: the record names a bare sha

    manifest = tomllib.loads((parent / "gf.toml").read_text())
    by_path = {f["path"]: f["ref"] for f in manifest["git_folder"]}
    assert by_path["vendor/b"] == "v2"

    # A landed tag persists: a later join at the same ref re-covers
    # nothing — the refspec set is byte-identical, no duplicate line.
    gf("-C", str(parent), "clone", str(upstream / "tools"),
       "vendor/c", "-b", "v2")
    assert _refspec_lines(store) == [
        "+refs/heads/master:refs/remotes/origin/master",
        "+refs/tags/v2:refs/tags/v2",
    ]
    assert (parent / "vendor" / "c" / "t.txt").read_text() == (
        "tool on master")


def test_join_clone_unknown_tag_fails_clean(tmp_path):
    """R6-A negative twin: a `-b v9` join against a store that cannot
    cover it — and an upstream that never advertised `refs/tags/v9` —
    still fails `could not resolve ref 'v9'`; nothing is appended and
    nothing half-made lingers."""
    upstream = _diverged_upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/a", "--single-branch", "-b", "master")
    rk = _repo_key_dir(parent)
    store = _store(parent, rk)
    lines_before = _refspec_lines(store)

    r = gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
           "vendor/b", "-b", "v9", check=False)
    assert r.returncode != 0
    assert "could not resolve ref 'v9'" in r.stderr

    assert _refspec_lines(store) == lines_before
    assert not os.path.lexists(parent / "vendor" / "b")
    assert not (rk / "ref=v9").exists()
    assert not (rk / ".ref=v9.state").exists()


# ---------------------------------------------------------------------------
# R7-B — a spelled clone/init/worktree TARGET whose realpath lands inside
# the parent's `.gf` storage is refused (round-7 ruling). `.gf` roots gf's
# private layout — a child's `git`/`state`, a parent's `repos` stores and
# `wt` checkout trees — never a place a consumer path may create or clone
# into (the destination-side twin of R6-B's url guard; spec "URL
# resolution" boundary + arch GF-D8). The guard compares the resolved
# target: a `.gf/...` spelling or an ancestor consumer link that resolves
# into a shared checkout is refused (rc 1, "resolves inside gf-managed
# storage (.gf)"), while the leaf stays lexical — a leaf that IS an
# existing consumer link keeps its idempotent re-add path. A refusal
# leaves nothing behind: no child dir or link, no manifest entry, nothing
# inside `.gf/wt`.


def _gf_tree(root: Path) -> dict:
    """Every path under `<root>/.gf` → ('dir',)/('link', target)/
    ('file', bytes) — byte-identical snapshot."""
    base = root / ".gf"
    snap = {}
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).as_posix()
        if p.is_symlink():
            snap[rel] = ("link", os.readlink(p))
        elif p.is_dir():
            snap[rel] = ("dir",)
        else:
            snap[rel] = ("file", p.read_bytes())
    return snap


def test_clone_refuses_target_resolving_into_gf_checkout(tmp_path):
    """R7-B discriminating arm: with `vendor/docs` an existing consumer
    link into the shared checkout, `gf clone <up> vendor/docs/nested`
    must REFUSE — the spelled target resolves through the link and lands
    inside `.gf/wt`, gf-managed storage (spec URL-resolution boundary
    applied to the destination; GF-D8). Verified pre-fix signature: rc 0
    and the manifest recorded `path = ".gf/wt/<rk>/<ck>/docs/nested"` —
    a whole-repo child planted inside the managed checkout that `gf rm`
    then refused to touch. Post-fix: rc 1 with the gf-storage refusal,
    and nothing is left behind — no manifest entry, nothing inside
    `.gf/wt`, gf.toml and the `.gf` tree byte-identical."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs"), "vendor/docs")
    rk = _repo_key_dir(parent)
    gf_before = _gf_tree(parent)
    manifest_before = (parent / "gf.toml").read_bytes()

    r = gf("-C", str(parent), "clone", str(upstream),
           "vendor/docs/nested", check=False)
    assert r.returncode == 1
    assert "resolves inside gf-managed storage (.gf)" in r.stderr

    # nothing of the refused binding is left behind: no child inside the
    # managed checkout, no manifest entry, no store state — gf.toml and
    # the whole `.gf` tree are byte-identical
    assert not os.path.lexists(rk / "master" / "docs" / "nested")
    assert _gf_tree(parent) == gf_before
    assert (parent / "gf.toml").read_bytes() == manifest_before
    assert [(e["name"], e["path"]) for e in _manifest_entries(parent)
            ] == [("docs", "vendor/docs")]


def test_init_refuses_target_resolving_into_gf_checkout(tmp_path):
    """R7-B init arm: `gf init vendor/docs/nested` must REFUSE — and,
    unlike the verified pre-fix signature (rc 1 that still LEAKED the
    `target.mkdir`'d `nested` dir inside the managed checkout), nothing
    may be created inside `.gf/wt`: `docs/nested` never appears in the
    checkout and no manifest entry is recorded."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs"), "vendor/docs")
    rk = _repo_key_dir(parent)

    r = gf("-C", str(parent), "init", "vendor/docs/nested", check=False)
    assert r.returncode == 1
    assert "resolves inside gf-managed storage (.gf)" in r.stderr

    # the pre-fix leak: `target.mkdir` had already planted `nested`
    # inside the shared checkout when the later step failed
    assert not os.path.lexists(rk / "master" / "docs" / "nested")
    assert [(e["name"], e["path"]) for e in _manifest_entries(parent)
            ] == [("docs", "vendor/docs")]


def test_clone_refuses_direct_gf_wt_spelling_as_target(tmp_path):
    """R7-B: the same refusal when the child path spells the `.gf/wt`
    interior directly — `.gf/wt/<rk>/<ck>/x` — not only when the
    resolution arrives through a consumer link."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs"), "vendor/docs")
    rk = _repo_key_dir(parent)
    interior = f".gf/wt/{rk.name}/master/x"

    r = gf("-C", str(parent), "clone", str(upstream), interior,
           check=False)
    assert r.returncode == 1
    assert "resolves inside gf-managed storage (.gf)" in r.stderr
    assert not os.path.lexists(rk / "master" / "x")
    assert [(e["name"], e["path"]) for e in _manifest_entries(parent)
            ] == [("docs", "vendor/docs")]


def test_worktree_add_refuses_path_resolving_inside_gf(tmp_path):
    """R7-B worktree arm: `gf worktree add vendor/docs/wt2` must REFUSE
    — the destination resolves through the consumer link into the
    managed checkout. Verified pre-fix signature: rc 0, `git worktree
    add` planted a real worktree inside `.gf/wt`. Post-fix: rc 1 with
    the gf-storage refusal, no dir inside the checkout, no worktree
    record — while an outside destination still works (a worktree
    target was never required to live inside the parent)."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs"), "vendor/docs")
    rk = _repo_key_dir(parent)

    r = gf("-C", str(parent), "worktree", "add", "vendor/docs/wt2",
           check=False)
    assert r.returncode == 1
    assert "resolves inside gf-managed storage (.gf)" in r.stderr
    assert not os.path.lexists(rk / "master" / "docs" / "wt2")
    # no worktree record was planted: the parent still lists only its
    # main worktree
    wt_lines = [ln for ln in _git_out(
        "-C", str(parent), "worktree", "list", "--porcelain"
    ).splitlines() if ln.startswith("worktree ")]
    assert wt_lines == [f"worktree {parent.resolve()}"]

    # a destination outside `.gf` still works and links the binding
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    assert (wt2 / "vendor" / "docs").is_symlink()


def test_clone_leaf_consumer_link_still_rebinds(tmp_path):
    """R7-B lexical-leaf exception: a spelled leaf that IS itself a
    consumer link stays literal for the guard — the refusal compares
    the resolved parent chain, never the leaf's own realpath (a guard
    that resolved the leaf would refuse a realpath inside `.gf/wt`).
    With the manifest entry removed but the `vendor/docs` link still
    live, `gf clone <up>/docs vendor/docs` still SUCCEEDS and re-records
    the lexical consumer path."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs"), "vendor/docs")
    rk = _repo_key_dir(parent)

    gf("-C", str(parent), "rm", "vendor/docs")
    assert _manifest_entries(parent) == []

    # an unrecorded consumer-link leaf — e.g. a retired binding record
    # or hand edit left the live link while the manifest entry is gone
    link = parent / "vendor" / "docs"
    link.symlink_to(os.path.relpath(rk / "master" / "docs", link.parent))
    assert link.is_symlink()

    gf("-C", str(parent), "clone", str(upstream / "docs"), "vendor/docs")

    # the re-add records the spelled consumer path — never the `.gf/wt`
    # realpath the leaf resolves to — and the link keeps serving the
    # mapped folder through the shared checkout
    assert [(e["name"], e["path"]) for e in _manifest_entries(parent)
            ] == [("docs", "vendor/docs")]
    assert link.is_symlink()
    assert link.resolve() == (rk / "master" / "docs").resolve()
    assert (link / "api" / "x.txt").read_text().strip() == "api on master"


def test_dot_segment_composition_follows_realpath(tmp_path):
    """R7-B dot-segment composition: the guard observes REALPATH, never
    a normpath pop of the spelled parent — `vendor/docs/../x` resolves
    `vendor/docs` through the consumer link into `.gf/wt` FIRST, so the
    `..` lands at the checkout root and the target is refused. The
    unaffected twin: `vendor/../plain` pops a REAL directory and stays
    an ordinary `gf init` inside the parent."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(upstream / "docs"), "vendor/docs")
    rk = _repo_key_dir(parent)

    r = gf("-C", str(parent), "clone", str(upstream),
           "vendor/docs/../x", check=False)
    assert r.returncode == 1
    assert "resolves inside gf-managed storage (.gf)" in r.stderr
    assert not os.path.lexists(rk / "master" / "x")
    assert not os.path.lexists(parent / "vendor" / "x")

    gf("-C", str(parent), "init", "vendor/../plain")
    assert (parent / "plain" / ".gf" / "git" / "HEAD").is_file()
    assert [(e["name"], e["path"]) for e in _manifest_entries(parent)
            ] == [("docs", "vendor/docs"), ("plain", "plain")]


def test_clone_and_init_plain_targets_unaffected(tmp_path):
    """R7-B controls: ordinary targets that do NOT resolve inside `.gf`
    are unaffected — `gf clone <up> vendor/child` plants a whole-repo
    child, `gf init newdir` a placeholder gitdir (no consumer-link
    ancestor and no `.gf` spelling)."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)

    gf("-C", str(parent), "clone", str(upstream), "vendor/child")
    assert (parent / "vendor" / "child" / ".gf" / "git").is_dir()
    assert not (parent / ".gf").exists()

    gf("-C", str(parent), "init", "newdir")
    assert (parent / "newdir" / ".gf" / "git" / "HEAD").is_file()
    assert [(e["name"], e["path"]) for e in _manifest_entries(parent)
            ] == [("child", "vendor/child"), ("newdir", "newdir")]
# R7-C — pinned-ref (tag/commit) coverage on the store-CREATE path
#
# A tag or bare commit never matches a `refs/heads/*` refspec line, and
# tag auto-follow lands only tags pointing into fetched history — so the
# fetch that CREATES a repo store misses a tag on an unreachable commit
# (`--depth` truncating below it counts) or a commit no ref names. The
# create arm must cover a pinned non-branch ref BEFORE the creating
# fetch: a tag upstream advertises gets `+refs/tags/<t>:refs/tags/<t>`
# appended (append-only — the wildcard line is never rewritten), so the
# single creating fetch lands it; a missing commit gets a one-shot
# `fetch origin <sha>` (spec "Fetch refspecs" + `gf clone` mechanics).
#
# Pre-fix signatures (verified against HEAD):
#   -b v1 --depth 1 (tag below the shallow tip): could not resolve ref
#       'v1'  (rc=1)
#   -b <orphan-tag>:                            could not resolve ref
#       'v3'  (rc=1)
#
# The bare-sha arm lives in test_pull_grouped with the backend call log:
# a `--filter=blob:none` store is a promisor, so `worktree add`/`checkout`
# lazily pulls a missing object even pre-fix — only the gf-issued
# `fetch origin <sha>` call discriminates the create arm's coverage.


def _push_orphan_tag(up: Path, tmp_path: Path, tag: str) -> str:
    """Push `tag` naming a commit no advertised branch reaches.

    The marker commit is made on a local-only `side-<tag>` branch and the
    tag alone is pushed: upstream advertises `refs/tags/<tag>` for a
    commit `refs/heads/*` coverage plus tag auto-follow can never land.
    Returns the tagged sha. (The test_pull_grouped pattern, for the
    clone-create arm.)
    """
    work = tmp_path / f"_orphan_{tag}"
    git("clone", str(up), str(work), cwd=tmp_path)
    git("checkout", "-q", "-b", f"side-{tag}", cwd=work)
    (work / "docs" / "api" / f"{tag}.txt").write_text(f"orphan {tag}")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", tag, cwd=work)
    sha = _git_out("-C", work, "rev-parse", "HEAD")
    git("tag", tag, cwd=work)
    git("push", "-q", "origin", tag, cwd=work)
    return sha


def _push_colliding_branch_tag(up: Path, tmp_path: Path, name: str) -> str:
    """Push `refs/heads/<name>` AND `refs/tags/<name>` on DIFFERENT
    commits — an upstream-side branch/tag name collision. The branch tip
    carries `docs/api/<name>-branch.txt`; the tag names the master tip,
    whose tree lacks the marker. Returns the branch tip sha."""
    work = tmp_path / f"_coll_{name}"
    git("clone", str(up), str(work), cwd=tmp_path)
    git("checkout", "-q", "-b", name, cwd=work)
    (work / "docs" / "api" / f"{name}-branch.txt").write_text(
        f"{name} branch tip")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", f"{name} branch", cwd=work)
    branch_sha = _git_out("-C", work, "rev-parse", "HEAD")
    git("push", "-q", "origin", f"refs/heads/{name}", cwd=work)
    git("checkout", "-q", "master", cwd=work)
    git("tag", name, cwd=work)
    git("push", "-q", "origin", f"refs/tags/{name}", cwd=work)
    return branch_sha


def test_create_store_lands_tag_below_depth_boundary(tmp_path):
    """R7-C discriminating arm: tag `v1` sits on a commit OLDER than the
    master tip, so `gf clone <up>/docs -b v1 --depth 1` creates a store
    whose shallow fetch cannot auto-follow v1 — the tag's refspec line
    must be appended BEFORE the creating fetch so the same fetch lands
    it. rc 0, `refs/tags/v1` in the store, the link serves v1's tree
    from a detached `ref=v1` checkout. Pre-fix: rc 1 `could not resolve
    ref 'v1'` (spec "Fetch refspecs": a needed tag gets
    `+refs/tags/<ref>:refs/tags/<ref>` appended so the same fetch lands
    it; `--depth` applies only to the creating fetch)."""
    upstream = _upstream(tmp_path)
    _advance(upstream, tmp_path, "adv")  # v1 stays on the pre-advance tip
    parent = _real_parent(tmp_path)

    gf("-C", str(parent), "clone", str(upstream / "docs"),
       "vendor/v1", "-b", "v1", "--depth", "1")

    rk = _repo_key_dir(parent)
    store = _store(parent, rk)
    # append-only coverage: the wildcard survives verbatim and the tag's
    # line is added — never a rewrite or removal.
    assert _refspec_lines(store) == [
        "+refs/heads/*:refs/remotes/origin/*",
        "+refs/tags/v1:refs/tags/v1",
    ]
    assert (store / "shallow").exists()  # --depth did apply
    v1_sha = _git_out("--git-dir", store, "rev-parse", "refs/tags/v1^{}")
    admin_head = (
        store / "worktrees" / "ref=v1" / "HEAD").read_text().strip()
    assert admin_head == v1_sha  # detached: the record names a bare sha

    # the link materializes v1's tree — the post-tag advance is absent
    link = parent / "vendor" / "v1"
    assert link.resolve() == (rk / "ref=v1" / "docs").resolve()
    assert (link / "api" / "x.txt").read_text() == "api on master"
    assert (link / "top.md").read_text() == "leading-dir file"

    manifest = tomllib.loads((parent / "gf.toml").read_text())
    by_path = {f["path"]: f["ref"] for f in manifest["git_folder"]}
    assert by_path["vendor/v1"] == "v1"


def test_create_store_covers_orphan_tag(tmp_path):
    """R7-C orphan arm: `v3` tags a commit no advertised branch reaches
    (the `_push_orphan_tag` pattern), so even a wildcard store fetch
    cannot auto-follow it — the tag's refspec line must be appended
    pre-fetch. An ordinary subfolder `-b v3` clone succeeds and serves
    the orphan commit's tree detached. Pre-fix: rc 1 `could not resolve
    ref 'v3'`."""
    upstream = _upstream(tmp_path)
    orphan_sha = _push_orphan_tag(upstream, tmp_path, "v3")
    parent = _real_parent(tmp_path)

    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/o", "-b", "v3")

    rk = _repo_key_dir(parent)
    store = _store(parent, rk)
    assert _refspec_lines(store) == [
        "+refs/heads/*:refs/remotes/origin/*",
        "+refs/tags/v3:refs/tags/v3",
    ]
    assert _git_out("--git-dir", store, "rev-parse",
                    "refs/tags/v3^{}") == orphan_sha
    admin_head = (
        store / "worktrees" / "ref=v3" / "HEAD").read_text().strip()
    assert admin_head == orphan_sha  # detached at the tagged commit

    link = parent / "vendor" / "o"
    assert link.resolve() == (rk / "ref=v3" / "docs" / "api").resolve()
    assert (link / "v3.txt").read_text() == "orphan v3"
    assert (link / "x.txt").read_text() == "api on master"


def test_create_store_collision_branch_wins_no_tag_line(tmp_path):
    """R7-C subfolder side of the heads/tags collision: upstream
    advertises `refs/heads/x` AND `refs/tags/x`. A `-b x` clone resolves
    the ref as a branch — the pinned-ref coverage is skipped outright,
    so a `--single-branch` store gets exactly the branch's line (no tag
    line, no config error) and the checkout attaches branch x's tip.
    """
    upstream = _upstream(tmp_path)
    branch_sha = _push_colliding_branch_tag(upstream, tmp_path, "x")
    parent = _real_parent(tmp_path)

    gf("-C", str(parent), "clone", str(upstream / "docs" / "api"),
       "vendor/x", "-b", "x", "--single-branch")

    rk = _repo_key_dir(parent)
    store = _store(parent, rk)
    assert _refspec_lines(store) == [
        "+refs/heads/x:refs/remotes/origin/x"]

    # branch wins: the checkout is keyed `x`, attaches `refs/heads/x` at
    # the branch tip, and the link serves the branch's marker (absent
    # from the tag's commit).
    admin_head = (
        store / "worktrees" / "x" / "HEAD").read_text().strip()
    assert admin_head == "ref: refs/heads/x"
    assert branch_sha == _git_out(
        "--git-dir", store, "rev-parse", "refs/remotes/origin/x")
    link = parent / "vendor" / "x"
    assert link.resolve() == (rk / "x" / "docs" / "api").resolve()
    assert (link / "x-branch.txt").read_text() == "x branch tip"
