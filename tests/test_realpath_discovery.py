# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Realpath-aware discovery, selection, and link ownership.

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

`in_gf_wt`/`owns_consumer_link`, the `find_parent_root` in-wt `.git`
guard, and `select_children`'s innermost-binding rule are the seams
whose regression these pins exist to catch; `resolve_checkout`
implements the `.gf/wt` realpath walk they also guard.
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
    Also creates the store worktree record and `.<key>.state` so the
    seeded tree matches spec "Subfolder-binding layout".
    """
    wt = root / ".gf" / "wt" / RK / key
    store = root / ".gf" / "repos" / RK / "git"
    for sd in subdirs:
        (wt / sd).mkdir(parents=True, exist_ok=True)
    (store / "worktrees" / key).mkdir(parents=True, exist_ok=True)
    # a HEAD file so is_git_folder_child-style checks see a gitdir
    (store / "worktrees" / key / "HEAD").write_text("ref: refs/heads/x\n")
    (wt.parent / f".{key}.state").write_text("subdir\n")
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
    assert co.state == wt.parent / ".master.state"


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
# The realpath class guard every discovery path needs.


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
# drives the link-only `rm` refusal — pinned now, `rm` behavior
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


# --- manifest.binding_root: the declared-path anchor (R9-1) -------------------
# `cmd_pull` anchors a source-relative `url` at the root under which the
# binding's declared manifest `path` spells the resolved child — never at
# the nearest `.git` ancestor of `child.parent` (the pre-fix walk a foreign
# `git init` between child and root hijacked). An ancestor qualifies only
# when BOTH `(anc / path).resolve() == child` and a root marker exists
# there (`.git` entry or `gf.toml` file); otherwise it is skipped, and a
# walk with no qualifying ancestor returns None — the caller's
# invoking-root fallback.


def test_binding_root_matching_ancestor_is_declaring_root(tmp_path):
    """The ancestor under which the declared `path` spells `child` is the
    binding root. Both marker arms qualify it: a `.git` repo root, and a
    `gf.toml` file alone (a manifest-owning root need not be a repo)."""
    root = _parent_root(tmp_path)
    child = root / "nested" / "kid"
    child.mkdir(parents=True)
    assert manifest.binding_root(child, "nested/kid") == root

    # the `gf.toml` marker arm — no `.git` on this root at all
    plain = tmp_path / "plain"
    child2 = plain / "nested" / "kid"
    child2.mkdir(parents=True)
    (plain / "gf.toml").write_text("")
    assert manifest.binding_root(child2, "nested/kid") == plain


def test_binding_root_absolute_path_is_never_owned(tmp_path):
    """An absolute manifest `path` has no root-relative spelling — the
    walk is skipped entirely and the caller falls back to the invoking
    root."""
    root = _parent_root(tmp_path)
    child = root / "nested" / "kid"
    child.mkdir(parents=True)
    assert manifest.binding_root(child, str(child)) is None
    assert manifest.binding_root(child, str(root / "nested" / "kid")) is None


def test_binding_root_foreign_git_ancestor_cannot_claim(tmp_path):
    """The R9-1 discriminator: a foreign `.git` between the child and the
    declaring root does NOT claim the anchor — `nested` passes the marker
    test but `(nested / "nested/kid")` resolves to a different path, so
    the walk continues to `p`, which satisfies both. The countercheck
    pins honest ownership: spelled as `nested`'s own `kid`, `nested` IS
    the root under which it resolves — the guard rejects wrong-spelling
    ancestors, not nearer ones."""
    root = _parent_root(tmp_path)              # declaring root — `.git`
    nested = root / "nested"
    (nested / ".git").mkdir(parents=True)      # foreign repo marker
    child = nested / "kid"
    child.mkdir()

    # the manifest path is spelled from `root`; `nested` cannot claim it
    assert manifest.binding_root(child, "nested/kid") == root
    # ...while `nested`'s own spelling of the child honestly answers
    # `nested` — if it were the declaring root, it would own the anchor
    assert manifest.binding_root(child, "kid") == nested


def test_binding_root_nonmarker_ancestor_returns_none(tmp_path):
    """A `path`-matching ancestor WITHOUT `.git` or `gf.toml` is no root
    at all — skipped, and with no other qualifying ancestor the walk
    returns None so `cmd_pull` falls back to the invoking root."""
    plain = tmp_path / "plain"
    child = plain / "nested" / "kid"
    child.mkdir(parents=True)
    # `plain` satisfies (plain / "nested/kid").resolve() == child but
    # carries neither marker; `plain/nested` misses the spelling, and no
    # higher ancestor's `nested/kid` spelling lands on `child`
    assert manifest.binding_root(child, "nested/kid") is None


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


# --- select_children: aliased bindings on one map -----------------------------
# R7-A: two bindings whose consumer paths link the SAME checkout subdir
# alias one map — identical realpath map and identical depth. Spec "Target
# selection rules" + the `gf rm`/`status`/`ls` arg rows (gf-spec L271/L286/
# L298): "each argument selects the git-folder whose consumer path matches
# or contains the argument" — a spelled operand names its binding lexically,
# so `one` and `two` stay distinct under selection even though their maps
# coincide. Pre-fix signature (verified): `gf rm one` removed BOTH links and
# manifest entries; `gf status one` / `gf ls one` printed both rows.


def _seed_alias_pair(tmp_path):
    """`one` and `two` consumer links onto the SAME `<ck>/docs` map: two
    bindings, one realpath target, equal map depth. Returns
    (root, wt, manifest_dict)."""
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root, subdirs=("docs",))
    _rel_link(root / "one", wt / "docs")
    _rel_link(root / "two", wt / "docs")
    man = _manifest(
        ("one", f"{REPO_URL}/docs", "latest", "one"),
        ("two", f"{REPO_URL}/docs", "latest", "two"),
    )
    return root, wt, man


def test_select_spelled_operand_names_its_own_alias(tmp_path):
    """A spelled arg names ITS binding: `one` selects only `one`, `two`
    only `two`, and an arg inside one's consumer path (`one/sub`)
    still names `one` alone — the arg row's "consumer path matches or
    contains the argument" clause (spec L271). The coincident map must
    not collapse the two bindings into one selection."""
    root, wt, man = _seed_alias_pair(tmp_path)

    assert _names(select_children(root, root, ["one"], man)) == ["one"]
    assert _names(select_children(root, root, ["two"], man)) == ["two"]
    assert _names(select_children(root, root, ["one/sub"], man)) == [
        "one"]


def test_select_cwd_inside_alias_names_that_binding(tmp_path):
    """No-arg in-child context (spec row 1: "cwd inside a child →
    operate on that child"): a cwd spelled through `one`'s consumer
    link selects `one` alone even though the shared map holds `two`
    at the same depth."""
    root, wt, man = _seed_alias_pair(tmp_path)

    assert _names(
        select_children(root, root / "one", [], man)) == ["one"]
    assert _names(
        select_children(root, root / "one" / "sub", [], man)) == ["one"]
    assert _names(
        select_children(root, root / "two", [], man)) == ["two"]


def test_select_below_target_keeps_every_alias(tmp_path):
    """The at-or-below arm is untouched: an operand naming a parent of
    both bindings still selects all children below it (spec rows 2-3:
    "all children below it") — lexical naming narrows a SHARED-map
    collision, it never prunes a true below-the-operand set."""
    root, wt, man = _seed_alias_pair(tmp_path)

    assert _names(select_children(root, root, [], man)) == ["one", "two"]
    assert _names(
        select_children(root, root, ["."], man)) == ["one", "two"]


def test_select_physical_map_path_selects_every_alias(tmp_path):
    """Documented residual (spec L473): a path inside the physical
    `.gf/wt` checkout selects every binding whose map contains it — a
    physical operand names NO consumer path, so it cannot discriminate
    the aliases and both match at the same (innermost) realpath depth.
    Pinned as intended behavior, not as the bug."""
    root, wt, man = _seed_alias_pair(tmp_path)

    for arg in (str(wt / "docs"), str(wt / "docs" / "inner")):
        assert _names(
            select_children(root, root, [arg], man)) == ["one", "two"]
    # the same residual holds for a physical no-arg cwd
    assert _names(
        select_children(root, wt / "docs", [], man)) == ["one", "two"]


def test_select_lexical_only_among_innermost_realpath(tmp_path):
    """Ordering guard: lexical naming breaks ties INSIDE the realpath
    innermost set — it does not precede realpath containment. Spelled
    through the OUTER link into the deeper map, `v/docs/api/sub` still
    selects the innermost binding `api` (spec's nested example); while
    an operand that IS a non-innermost binding's consumer path still
    selects it (`v/docs` → `docs`; `v/api` → `api`)."""
    root, wt, man = _seed_two_binding_nested(tmp_path)

    # spelled through the outer link, the operand realpaths inside the
    # deeper `api` map: the lexically-named `docs` is not innermost —
    # innermost still wins (an unconditional lexical pass would pick
    # `docs` here — the exact ordering failure this pin guards).
    assert _names(
        select_children(root, root, ["v/docs/api/sub"], man)) == ["api"]
    # a spelled operand naming the deeper binding directly selects it
    assert _names(
        select_children(root, root, ["v/api/sub"], man)) == ["api"]
    # a spelled operand that IS a shallower binding's consumer path
    # selects that binding — its map contains the operand even though
    # a deeper map exists elsewhere
    assert _names(
        select_children(root, root, ["v/docs"], man)) == ["docs"]
    # through the outer link into the deeper map, realpath innermost
    # decides here too
    assert _names(
        select_children(root, root, ["v/docs/api"], man)) == ["api"]


def test_select_spelled_containment_when_no_map_matches(tmp_path):
    """Spelled-containment fallback: an arg lexically inside a binding's
    consumer path but realpathing OUTSIDE every map (through a mid-path
    link in the checked-out content) still names that binding — the
    lexical consumer path contains the operand (spec L271's "contains"),
    so selection answers the spelled binding rather than falling to the
    below-the-target set, which would find nothing."""
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root, subdirs=("docs/api", "tools"))
    _rel_link(root / "vendor" / "api", wt / "docs" / "api")
    # nested consumer spellings: whole-repo `x` (real dir) contains
    # link binding `x/y` — lexical containment, not map nesting
    (root / "x").mkdir()
    _rel_link(root / "x" / "y", wt / "tools")
    # mid-path links inside the checked-out content redirect the
    # spelled operand's realpath out of every binding's map
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _rel_link(wt / "docs" / "api" / "escape", elsewhere)
    _rel_link(wt / "tools" / "escape", elsewhere)
    man = _manifest(
        ("api", f"{REPO_URL}/docs/api", "latest", "vendor/api"),
        ("x", REPO_URL, "latest", "x"),
        ("y", f"{REPO_URL}/tools", "latest", "x/y"),
    )

    # the spelled consumer path still names its binding when the
    # operand's realpath escapes every map
    assert _names(select_children(
        root, root, ["vendor/api/escape/leaf"], man)) == ["api"]
    # with nested consumer paths spelling the operand, the lexically
    # innermost container answers
    assert _names(select_children(
        root, root, ["x/y/escape/leaf"], man)) == ["y"]


# --- select_children: dot-segment operands and the `_below` arm (R8-B) --------
# spec "Target selection rules" (L473): "Every path argument identifies by
# spelled consumer-path identity only after lexical normalization — `.`/`..`
# segments resolve by directory identity." An operand like `sub/../vendor`
# names no consumer path and no map; selection falls to the at-or-below arm,
# where the operand must compare NORMALIZED — `sub/../vendor` is `vendor`, so
# every binding below it answers. Lexical normalization is operand identity
# only, never a `..`-resolution rule: a `..` that pops a mid-path consumer
# LINK composes by realpath (the pop lands inside the link target's tree),
# which the map arm keeps by holding the raw operand.
#
# Pre-fix signature (verified against HEAD's shelf.py via a PYTHONPATH
# overlay): `sub/../vendor` selected `[]` — the raw-target compare held `..`
# literally — while every control and both mid-path-link guards below already
# passed.


def _seed_vendor_pair(tmp_path):
    """`vendor/api` + `vendor/tools` bindings on one shared checkout plus
    an empty `sub/` dir — the lexical detour a `sub/../vendor` operand
    pops out of. Returns (root, wt, manifest_dict)."""
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root, subdirs=("docs/api", "tools"))
    _rel_link(root / "vendor" / "api", wt / "docs" / "api")
    _rel_link(root / "vendor" / "tools", wt / "tools")
    (root / "sub").mkdir()
    man = _manifest(
        ("api", f"{REPO_URL}/docs/api", "latest", "vendor/api"),
        ("tools", f"{REPO_URL}/tools", "latest", "vendor/tools"),
    )
    return root, wt, man


def test_select_dotdot_spelling_selects_below_normalized_parent(tmp_path):
    """`sub/../vendor` selects every binding below `vendor` — the operand
    realpaths above every map (inside-set empty) and names no consumer
    path (spelled-set empty), so the `_below` arm answers and its
    compare must use the normalized operand (spec L473 directory
    identity). The absolute spelling resolves identically. Pre-fix: the
    raw-target compare held `..` literally and selected `[]` (verified).
    """
    root, wt, man = _seed_vendor_pair(tmp_path)

    assert _names(
        select_children(root, root, ["sub/../vendor"], man)
    ) == ["api", "tools"]
    assert _names(
        select_children(
            root, root, [str(root / "sub" / ".." / "vendor")], man)
    ) == ["api", "tools"]


def test_select_dotdot_popping_own_link_names_lexical_parent(tmp_path):
    """`vendor/api/..` selects ALL `vendor` descendants — the operand's
    lexical normalization lands on `vendor`, so the lexical arm answers
    both bindings even though the operand's realpath (the `vendor/api`
    link pop lands on `wt/docs`) contains only the `api` map — the
    realpath arm alone would answer `[api]` (the verified pre-fix
    signature), never `tools`."""
    root, wt, man = _seed_vendor_pair(tmp_path)

    assert _names(
        select_children(root, root, ["vendor/api/.."], man)
    ) == ["api", "tools"]


def test_select_dotdot_arms_controls_unchanged(tmp_path):
    """Controls: the arms that already resolved dot-segments keep their
    answers — plain `vendor` below-selection, the binding's own
    spelling, `./vendor/api` (inside the consumer path after
    normalization), the `x/../api` detour landing on the consumer path
    (inside/spelled arms), the name fallback, and no-arg below-cwd."""
    root, wt, man = _seed_vendor_pair(tmp_path)

    assert _names(
        select_children(root, root, ["vendor"], man)) == ["api", "tools"]
    assert _names(
        select_children(root, root, ["vendor/api"], man)) == ["api"]
    assert _names(
        select_children(root, root, ["./vendor/api"], man)) == ["api"]
    assert _names(
        select_children(root, root, ["vendor/x/../api"], man)) == ["api"]
    # no path match at all -> the git-folder name fallback still answers
    assert _names(
        select_children(root, root, ["api"], man)) == ["api"]
    # no-arg selection below cwd is untouched
    assert _names(
        select_children(root, root / "vendor", [], man)
    ) == ["api", "tools"]


def test_select_midpath_link_dotdot_composes_by_realpath(tmp_path):
    """PRESERVE guard — the normpath-everywhere discriminator: `..`
    popping a mid-path consumer LINK composes by realpath, not
    lexically. `v/api` links into the checkout, so `v/api/..` realpaths
    to `wt/docs` — inside the `docs` map — and selects `docs`. Lexical
    normalization would instead name `v` and select every binding below
    it (`[api, docs]`); the fix normalizes only the lexical arm so this
    keeps answering by realpath (spec L473: inside-a-child is decided
    by realpath matching). Passes before and after the fix."""
    root, wt, man = _seed_two_binding_nested(tmp_path)

    assert _names(
        select_children(root, root, ["v/api/.."], man)) == ["docs"]


def test_select_below_map_arm_keeps_realpath_after_link_pop(tmp_path):
    """R8-B same guard forced through `_below`'s map arm: the `v/api`
    link carries the operand into the checkout, but the manifest's
    consumer paths live under `vendor/` — no spelled or inside match
    fires, so only realpath composition (`v/api/..` → `wt/docs`,
    containing the `api` map) selects `api`. A `_below` that normalized
    the operand for its map arm too would find nothing (`wt/docs/api`
    is not below lexical `root/v`). Passes before and after the fix —
    the realpath arm kept the raw operand."""
    root = _parent_root(tmp_path)
    wt = _seed_checkout(root, subdirs=("docs/api", "tools"))
    _rel_link(root / "v" / "api", wt / "docs" / "api")
    _rel_link(root / "vendor" / "api", wt / "docs" / "api")
    _rel_link(root / "vendor" / "tools", wt / "tools")
    man = _manifest(
        ("api", f"{REPO_URL}/docs/api", "latest", "vendor/api"),
        ("tools", f"{REPO_URL}/tools", "latest", "vendor/tools"),
    )

    assert _names(
        select_children(root, root, ["v/api/.."], man)) == ["api"]


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
