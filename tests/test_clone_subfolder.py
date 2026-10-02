# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""P2R.S5 — `gf clone`/`gf init` creating subfolder bindings through the seam.

Receiving checks against the landed machinery (`layout.repo_store`,
`shelf.ensure_repo_store`/`ensure_checkout`/`ensure_consumer_link`,
`rollback_consumer_link` at `475c381`): commands do not route through it
yet, so every pin here is designed-red except the whole-repo regression.

Authorities: spec `gf clone`/`gf init`, "URL resolution", "Parent and
child gitignore handling", "Subfolder-binding layout", L221 mechanics;
arch GF-D6/D12; constraints: manifest schema, refspec append-only.
"""

import re
import subprocess
from pathlib import Path

from conftest import gf, git


def _git_out(*args) -> str:
    return subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True,
        check=True).stdout.strip()


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


def test_two_subfolders_share_one_store_one_checkout(tmp_path):
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
    """S2 disjointness at CLI level: branch literally named `ref=v1` keys
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
