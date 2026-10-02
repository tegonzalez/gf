# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Receiving tests for slice P2R.S3 — realpath-aware discovery,
selection, and link ownership.

Seeded-layout tests: the `.gf` store/worktree tree, consumer links,
worktree-add links, and `.git` markers are constructed directly on
tmp_path — no git binary, no network. Authorities:

- spec "Target selection rules" (docs/gf-spec.md): realpath decides
  "inside a child" for subfolder bindings — a cwd or arg inside a
  consumer link OR inside the physical `.gf/wt` checkout selects that
  binding; nested bindings (e.g. `docs` and `docs/api` on one checkout)
  select the INNERMOST for no-arg selection; a physical path under
  `<root>/.gf` resolves through the consumer link's realpath, so a
  `cd -P` physical cwd still selects the owning binding; a consumer
  link is owned by the parent root whose `.gf/wt` contains its
  realpath, and is a linked child in any other parent worktree;
  `rm` requires explicit selection in a non-child context.
- spec "Physical layout" / `.gf` directory: bindings resolve their
  checkout from the consumer link's realpath — no network, no state
  lookup — and `gf worktree add` symlinks to the source worktree's
  consumer link (so a later retarget propagates).
- spec `gf worktree add`: links point at the source consumer link,
  not the resolved checkout; `gf rm` cannot run from a linked child —
  the owning worktree is the parent root whose `.gf/wt` contains the
  link's realpath.
- arch GF-D14 + the Discovery contract (`_resolve(cwd)` walk-up only):
  ownership follows the realpath; discovery never treats a path inside
  a `.gf/wt` tree — including a dir containing `.git` — as a parent
  root.

Pins target the spec-visible seams the plan row names —
`layout.in_gf_wt`, `layout.owns_consumer_link`, `layout.resolve_checkout`,
`manifest.find_parent_root`, `shelf.select_children` — at contract
level (what a path class resolves/selects/owns). No internal shape
beyond those named surfaces is asserted. `in_gf_wt(p)` is pinned as a
bool; `owns_consumer_link(root, link)` as a bool.

Expected RED at the pre-S3 baseline: `in_gf_wt`/`owns_consumer_link`
do not exist yet, `find_parent_root` has no in-wt `.git` guard, and
`select_children` returns every overlapping binding rather than the
innermost — all designed reds. `resolve_checkout` already implements
the `.gf/wt` realpath walk; its pins are regression guards expected
green.
"""

import os
import re
import subprocess
from pathlib import Path

from gf import layout, manifest
from gf.layout import repo_key, resolve_checkout
from gf.shelf import select_children


REPO_URL = "https://github.com/foo/libfoo"
RK = repo_key(REPO_URL)  # pinned elsewhere; used here only for paths


# --- seeded-layout helpers ------------------------------------------------


def _parent_root(tmp_path: Path, name: str = "root") -> Path:
    """A parent repo root: `.git` dir + (optional) manifest handled by tests."""
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    return root


def _seed_checkout(root: Path, key: str = "master",
                   subdirs=("docs/api",)) -> Path:
    """Seed `<root>/.gf` with one repo store + one worktree checkout.

    Returns the checkout worktree dir `<root>/.gf/wt/<rk>/<key>`.
    Also creates the store worktree record and `<key>.state` so the
    seeded tree matches spec "Subfolder-binding layout".
    """
    wt = root / ".gf" / "wt" / RK / key
    store = root / ".gf" / "repos" / RK / "git"
    for sd in subdirs:
        (wt / sd).mkdir(parents=True, exist_ok=True)
    (store / "worktrees" / key).mkdir(parents=True, exist_ok=True)
    # a HEAD file so is_git_folder_child-style checks see a gitdir
    (store / "worktrees" / key / "HEAD").write_text("ref: refs/heads/x\n")
    (wt.parent / f"{key}.state").write_text("subdir\n")
    return wt


def _rel_link(link: Path, target: Path) -> Path:
    """Create the relative symlink spec "Subfolder-binding layout" requires."""
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(target, link.parent), link)
    return link


def _manifest(*entries) -> dict:
    return {"git_folder": [
        {"name": n, "url": u, "ref": r, "path": p}
        for n, u, r, p in entries
    ]}


def _names(selected: list[dict]) -> list[str]:
    return sorted(s["name"] for s in selected)


# --- resolve_checkout: link → checkout mapping -----------------------------
# Regression guards — spec "Physical layout": an existing subfolder
# binding resolves from its consumer link's realpath (no network, no
# state lookup); worktree-add links follow into the source store.


def test_resolve_consumer_link_maps_to_checkout(tmp_path):
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root)
    store = root / ".gf" / "repos" / RK / "git"
    link = _rel_link(root / "vendor" / "api", wt / "docs" / "api")

    co = resolve_checkout(link)
    assert co.work_tree == wt
    assert co.gitdir == store / "worktrees" / "master"
    assert co.common_dir == store
    assert co.subdir == "docs/api"
    assert co.state == wt.parent / "master.state"


def test_resolve_path_deep_inside_link_keeps_full_subdir(tmp_path):
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root)
    link = _rel_link(root / "vendor" / "api", wt / "docs" / "api")

    co = resolve_checkout(link / "inner" / "x.txt")
    assert co.work_tree == wt
    assert co.subdir == "docs/api/inner/x.txt"


def test_resolve_physical_checkout_path_maps_to_checkout(tmp_path):
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root, subdirs=("docs/api", "tools"))

    co = resolve_checkout(wt / "tools")
    assert co.work_tree == wt
    assert co.subdir == "tools"


def test_resolve_worktree_add_link_follows_into_source_store(tmp_path):
    """spec 'gf worktree add': the placed link targets the source
    worktree's consumer link; resolution follows it into the source
    parent's `.gf/wt` checkout (realpath chain)."""
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root)
    consumer = _rel_link(root / "vendor" / "api", wt / "docs" / "api")

    wt2 = _parent_root(tmp_path, name="wt2")
    linked = _rel_link(wt2 / "vendor" / "api", consumer)

    co = resolve_checkout(linked)
    assert co.work_tree == wt
    assert co.subdir == "docs/api"


def test_resolve_plain_path_is_whole_repo(tmp_path):
    root = _parent_root(tmp_path)
    _seed_checkout(root)
    plain = root / "vendor" / "lib"
    plain.mkdir(parents=True)

    co = resolve_checkout(plain)
    assert co.work_tree == plain
    assert co.gitdir == plain / ".gf" / "git"
    assert co.subdir == ""


# --- layout.in_gf_wt: inside-a-`.gf/wt` predicate ---------------------------
# New S3 seam (plan locus) — the realpath class guard every
# discovery path needs.


def test_in_gf_wt_classifies_realpath_locations(tmp_path):
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root)
    consumer = _rel_link(root / "vendor" / "api", wt / "docs" / "api")
    wt2 = _parent_root(tmp_path, name="wt2")
    linked = _rel_link(wt2 / "vendor" / "api", consumer)
    foreign = _rel_link(root / "vendor" / "x", tmp_path / "elsewhere")

    in_gf_wt = layout.in_gf_wt

    # inside the physical checkout tree
    assert in_gf_wt(wt)
    assert in_gf_wt(wt / "docs" / "api")
    # through consumer links (incl. second-worktree link chains)
    assert in_gf_wt(consumer)
    assert in_gf_wt(linked)
    # a link that dangles INTO the wt tree still realpaths inside it
    dangling = _rel_link(root / "vendor" / "gone", wt / "deleted-dir")
    assert in_gf_wt(dangling)
    # everything else is outside
    assert not in_gf_wt(root)
    assert not in_gf_wt(root / "vendor")
    assert not in_gf_wt(root / ".gf")
    # the bare repo store is NOT a checkout worktree
    assert not in_gf_wt(root / ".gf" / "repos" / RK / "git")
    assert not in_gf_wt(root / ".gf" / "repos" / RK / "git" / "worktrees" / "master")
    assert not in_gf_wt(foreign)
    assert not in_gf_wt(tmp_path / "elsewhere")


# --- layout.owns_consumer_link: ownership predicate -------------------------
# arch GF-D14 / spec "Target selection rules": a consumer link is
# owned by the parent root whose `.gf/wt` contains its realpath;
# in any other parent worktree it is a linked child. This predicate
# drives the S8 link-only `rm` refusal — pinned now, `rm` behavior
# itself is out of scope here.


def test_owns_consumer_link_owned_linked_foreign_dangling(tmp_path):
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root)
    consumer = _rel_link(root / "vendor" / "api", wt / "docs" / "api")

    owns = layout.owns_consumer_link

    # the parent root whose .gf/wt holds the realpath owns the link
    assert owns(root, consumer)

    # a second-worktree link realpaths into root's wt: owned by root,
    # a linked child to wt2 (spec 'gf rm' ownership rule)
    wt2 = _parent_root(tmp_path, name="wt2")
    linked = _rel_link(wt2 / "vendor" / "api", consumer)
    assert owns(root, linked)
    assert not owns(wt2, linked)

    # a link into a DIFFERENT parent root's wt is foreign to root
    other = _parent_root(tmp_path, name="other")
    other_wt = _seed_checkout(other)
    foreign = _rel_link(root / "vendor" / "f", other_wt / "docs" / "api")
    assert not owns(root, foreign)
    assert owns(other, foreign)

    # a link pointing outside any .gf/wt is not a consumer link
    outside = _rel_link(root / "vendor" / "out", tmp_path / "elsewhere")
    assert not owns(root, outside)

    # a link dangling INTO root's wt still realpaths inside it
    dangling = _rel_link(root / "vendor" / "gone", wt / "deleted-dir")
    assert owns(root, dangling)

    # a plain directory is not a consumer link at all
    realdir = root / "vendor" / "real"
    realdir.mkdir()
    assert not owns(root, realdir)


# --- manifest.find_parent_root: the in-wt `.git` guard -----------------------
# spec Discovery contract: walk-up from cwd for the parent `.git`;
# a `.git` marker inside a `.gf/wt` tree — inside a checkout — is not
# a parent-repo marker.


def test_find_parent_root_skips_git_markers_inside_gf_wt(tmp_path):
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root)
    consumer = _rel_link(root / "vendor" / "api", wt / "docs" / "api")

    # .git markers inside the checkout tree must not root the parent:
    # a worktree gitfile at the checkout root, and a nested repo inside
    # the checked-out content.
    (wt / ".git").mkdir()
    nested = wt / "docs" / "api"
    (nested / ".git").mkdir()

    expected = root.resolve()
    assert manifest.find_parent_root(nested) == expected
    assert manifest.find_parent_root(wt) == expected
    assert manifest.find_parent_root(root / ".gf" / "wt" / RK) == expected
    # through the consumer link (unresolved walk hits link/.git —
    # realpath lands inside wt and must be skipped)
    assert manifest.find_parent_root(consumer) == expected
    assert manifest.find_parent_root(consumer / "sub") == expected


def test_find_parent_root_unchanged_for_normal_tree(tmp_path):
    root = _parent_root(tmp_path)
    _seed_checkout(root)
    deep = root / "vendor" / "lib" / "x"
    deep.mkdir(parents=True)
    assert manifest.find_parent_root(deep) == root.resolve()
    # a `.git` marker OUTSIDE any `.gf/wt` tree still roots a nested
    # repo — the guard is scoped to checkout trees only
    inner = root / "plain" / "repo"
    (inner / ".git").mkdir(parents=True)
    assert manifest.find_parent_root(inner / "sub") == inner.resolve()


# --- shelf.select_children: realpath selection ------------------------------
# spec "Target selection rules" rows as seeded scenarios.


def _seed_two_binding_nested(tmp_path):
    """`docs` and `docs/api` bindings sharing one checkout (spec's
    innermost example). The nesting lives in the CHECKOUT tree —
    consumer paths `v/docs` and `v/api` are siblings; a path inside
    `v/api` realpaths inside BOTH bindings' link targets.
    Returns (root, wt, manifest_dict)."""
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root, subdirs=("docs", "docs/api"))
    _rel_link(root / "v" / "docs", wt / "docs")
    _rel_link(root / "v" / "api", wt / "docs" / "api")
    man = _manifest(
        ("docs", f"{REPO_URL}/docs", "latest", "v/docs"),
        ("api", f"{REPO_URL}/docs/api", "latest", "v/api"),
    )
    return root, wt, man


def test_select_by_consumer_link_path_args(tmp_path):
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root)
    _rel_link(root / "vendor" / "api", wt / "docs" / "api")
    man = _manifest(("api", f"{REPO_URL}/docs/api", "latest", "vendor/api"))

    # an arg at the link, or pointing inside it, selects the binding
    for arg in ("vendor/api", "vendor/api/inner"):
        assert _names(select_children(root, root, [arg], man)) == ["api"]


def test_select_by_physical_checkout_path_arg(tmp_path):
    """A path under `<root>/.gf` resolves through the consumer link's
    realpath (spec realpath paragraph) — the physical checkout dir
    selects the binding."""
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root)
    _rel_link(root / "vendor" / "api", wt / "docs" / "api")
    man = _manifest(("api", f"{REPO_URL}/docs/api", "latest", "vendor/api"))

    assert _names(
        select_children(root, root, [str(wt / "docs" / "api")], man)
    ) == ["api"]


def test_select_innermost_of_nested_bindings(tmp_path):
    """spec: when cwd resolves inside more than one binding, no-arg
    selection chooses the innermost."""
    root, wt, man = _seed_two_binding_nested(tmp_path)

    # lexical cwd inside the deeper binding's consumer path
    assert _names(
        select_children(root, root / "v" / "api" / "sub", [], man)
    ) == ["api"]
    # physical `cd -P` cwd inside the shared checkout
    assert _names(
        select_children(root, wt / "docs" / "api" / "sub", [], man)
    ) == ["api"]
    # cwd inside only the outer binding selects it
    assert _names(
        select_children(root, wt / "docs" / "other", [], man)
    ) == ["docs"]
    # an explicit arg pointing inside the nested area picks innermost
    assert _names(
        select_children(root, root, ["v/api/sub"], man)
    ) == ["api"]
    # reached through the OUTER link's spelling — realpath still lands
    # inside the inner binding, so innermost still wins
    assert _names(
        select_children(root, root / "v" / "docs" / "api" / "sub", [], man)
    ) == ["api"]


def test_select_all_children_below_cwd(tmp_path):
    """spec row: no args outside a child — all children at/below cwd."""
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root, subdirs=("docs/api", "tools"))
    _rel_link(root / "vendor" / "api", wt / "docs" / "api")
    _rel_link(root / "vendor" / "tools", wt / "tools")
    man = _manifest(
        ("api", f"{REPO_URL}/docs/api", "latest", "vendor/api"),
        ("tools", f"{REPO_URL}/tools", "latest", "vendor/tools"),
    )

    assert _names(select_children(root, root / "vendor", [], man)) == [
        "api", "tools"]
    assert _names(
        select_children(root, root, ["vendor"], man)) == ["api", "tools"]


def test_select_lexical_dot_segments_in_arg(tmp_path):
    """spec path-identity: `..`/`.` segments resolve by directory
    identity — a lexical detour still names the same location."""
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root)
    _rel_link(root / "vendor" / "api", wt / "docs" / "api")
    man = _manifest(("api", f"{REPO_URL}/docs/api", "latest", "vendor/api"))

    assert _names(
        select_children(root, root, ["vendor/x/../api"], man)
    ) == ["api"]


def test_linked_child_is_selected_but_not_owned(tmp_path):
    """spec 'gf rm' ownership: selection from a second worktree still
    identifies the binding (by realpath) while ownership stays with
    the source parent root — the refusal basis cli passes to rm."""
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root)
    consumer = _rel_link(root / "vendor" / "api", wt / "docs" / "api")
    wt2 = _parent_root(tmp_path, name="wt2")
    linked = _rel_link(wt2 / "vendor" / "api", consumer)
    man = _manifest(("api", f"{REPO_URL}/docs/api", "latest", "vendor/api"))

    # the linked child's realpath still selects the binding...
    assert _names(
        select_children(wt2, wt2 / "vendor" / "api", [], man)) == ["api"]
    # ...but wt2 does not own the link — root does (GF-D14).
    assert layout.owns_consumer_link(root, linked)
    assert not layout.owns_consumer_link(wt2, linked)


# --- discovery paths do no work in processes ---------------------------------


def test_discovery_paths_make_no_subprocess_calls(tmp_path, monkeypatch):
    """arch Discovery contract: discovery/selection is pure path work —
    no network, no subprocess (mirrors the test_layout pin)."""
    def boom(*a, **k):
        raise AssertionError("subprocess in discovery path")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)

    root = _parent_root(tmp_path)
    wt = _seed_checkout(root)
    consumer = _rel_link(root / "vendor" / "api", wt / "docs" / "api")
    man = _manifest(("api", f"{REPO_URL}/docs/api", "latest", "vendor/api"))

    resolve_checkout(consumer)
    layout.in_gf_wt(consumer)
    layout.owns_consumer_link(root, consumer)
    manifest.find_parent_root(root / "vendor" / "api")
    select_children(root, root / "vendor" / "api", [], man)
