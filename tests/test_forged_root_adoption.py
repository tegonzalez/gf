# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""R13-1 — a `.gf/wt`-shaped realpath ancestor adopts only a PROVEN root.

`layout.owning_root` derives the owning root from a
`*/.gf/wt/<repo-key>/<checkout-key>` ancestor of the consumer path's
realpath — the routing that lets a pull entered through a
`gf worktree add` link chain operate on the source root. But
`os.path.realpath` resolves nonexistent components LEXICALLY, so
committed tree content — a mid-path symlink the checkout materialized —
can spell that shape under a root that has no gf storage and was never a
gf root. The shape match alone never adopts: the derived root must prove
real — a `.git` entry (dir, gitfile, or symlink: `find_parent_root`'s own
marker, which git never commits into a tree), or a `HEAD` at the
matching `<root>/.gf/repos/<repo-key>/git` store — and a root inside a
`.gf` tree is never a root. An unproven shape falls to
`manifest.binding_root`, then the invoking root, where the pull's
consumer-path routing refusal applies.

The rider pinned here too: `manifest.binding_root` refuses an ancestor
inside a `.gf` tree even when the declared `path` resolves there and a
`gf.toml` materialized at it — cone mode materializes repo-root files
into the checkout, so marker + spelling can coincide inside `.gf`, but
gf's own storage is never a declaring root.

Authorities: docs/gf-spec.md `gf pull` — the path-resolves-outside
bullet's anchor-adoption sentences ("adopted from the consumer path's
realpath only when it proves real", "falls back to the root under which
the declared `path` resolves, then the invoking root, where the refusal
applies") — and the Security bullet ("the owning root itself is adopted
from a `.gf/wt`-shaped realpath ancestor only when that root proves
real"); goals GF-G2 and constraints "The object/reference store for a
git-folder must live inside the discovered parent worktree".

Pins:

- THE P1 signature (real git, committed hostile tree): parent commits a
  mid-path symlink `sub -> <tmp>/outside/.gf/wt/<r>/master` (mode
  120000) plus a manifest binding `path = "sub/master/libs/api"` on a
  real upstream subdir. `gf pull` exits 1 through the `gf:` envelope,
  the refusal says "outside", and `<tmp>/outside` is never created —
  zero bytes written under it. PRE-FIX at base 85856af this pull
  returned 0 — "Pulled api" — having created the repo store, the shared
  checkout, the state record, and the consumer link under `outside/`
  (`outside/.gf/repos/<key>/git` etc.); verified pre-fix by stashing the
  fix.
- The same forged shape pointed INTO the workspace — committed
  `sub -> o/.gf/wt/<r>/master` with `o/` an in-root dir carrying the
  materialized `.gf/wt` shape: adoption declined (no `.git`, no matching
  store under `o/`), `binding_root` recovers the real parent, the
  store+checkout build under `parent/.gf` (in-workspace writes), and
  the link layer's `in_gf_tree(link_parent)` refuses the link — rc 1
  through the `gf:` envelope; `o/` gains nothing. PRE-FIX this pull
  adopted `o` and wrote `o/.gf/repos/<key>/git` + `o/.gf/wt/<key>/` +
  `o/sub/libs/api` (rc 0).
- Unit pins over `layout.owning_root` (seeded layout, no git binary):
  `.git` dir/gitfile/symlink roots and a matching-key store-only root
  adopt; an unproven root, a `gf.toml`-only root, a `.gf`-interior root
  (even with `.git` + store planted), and a wrong-repo-key store all
  return None — plus the `binding_root` `.gf`-interior rider.
- Legit-adoption controls are pinned where they live rather than
  duplicated: a `gf worktree add` + pull through the added worktree's
  link chain —
  tests/test_consumer_path_containment.py::test_pull_through_added_worktree_link_chain_still_serves
  (and the worktree_links family); a wiped-`.gf` root adopting via
  `.git` for the dead-link rebuild —
  tests/test_pull_recovery.py::test_pull_rebuilds_wiped_store_checkout_and_serves.

Real git over a local bare upstream via the `gf` subprocess for the
integration arms; pure seeded-layout unit pins for the proof matrix.
"""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import gf, git
from gf import layout, manifest


# ---------------------------------------------------------------------------
# helpers — setup failures use pytest.fail, never AssertionError
#   (tests/test_consumer_path_containment.py convention)


def _git(*args, check: bool = True, input: str | None = None
         ) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True,
        input=input)
    if check and r.returncode != 0:
        pytest.fail(
            f"setup: git {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stderr}")
    return r


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", "-q", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root\n")
    git("add", "README", cwd=parent)
    git("commit", "-qm", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str = "upstream") -> Path:
    """Bare upstream with docs/api/x.txt + tools/t.txt on master."""
    up = tmp_path / name
    _git("init", "-q", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text("api on master\n")
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text(f"tool in {name}\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")
    return up


def _toml_value(v) -> str:
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return repr(v).lower() if isinstance(v, bool) else repr(v)


def _write_manifest(parent: Path, entries: list[dict]) -> None:
    """Hand-spell `gf.toml` — the fixture writes what the CLI produces
    so the committed manifest is the hostile tree's own content."""
    lines = ["git_folder = [\n"]
    for e in entries:
        fields = ", ".join(f"{k} = {_toml_value(v)}"
                           for k, v in e.items())
        lines.append(f"    {{ {fields} }},\n")
    lines.append("]\n")
    (parent / "gf.toml").write_text("".join(lines))


def _commit(parent: Path, message: str = "hostile tree") -> None:
    """Commit the parent's worktree as-is — the hostile tree a victim's
    checkout materializes (a mode-120000 `sub` link included)."""
    _git("-C", str(parent), "add", "-A")
    _git("-C", str(parent), "commit", "-qm", message)


def _committed_mode(parent: Path, rel: str) -> str:
    """The committed entry mode for `rel` (e.g. '120000' for a link)."""
    out = _git("-C", str(parent), "ls-files", "-s", rel).stdout.strip()
    return out.split()[0] if out else ""


def _tree_bytes(root: Path) -> dict[str, tuple[str, object]]:
    """Every entry under `root` as {relpath: (kind, payload)} — files
    carry bytes, links their target, dirs a marker — so an equal dict
    means a byte-identical tree."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for entry in dirnames + filenames:
            p = Path(dirpath) / entry
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                snap[rel] = ("link", os.readlink(p))
            elif p.is_dir():
                snap[rel] = ("dir", "")
            else:
                snap[rel] = ("file", p.read_bytes())
    return snap


# ---------------------------------------------------------------------------
# P1 — a committed mid-path symlink forging `.gf/wt` OUTSIDE the workspace
#
# The hostile tree ships `sub` (a committed link, mode 120000) plus the
# manifest spelling through it. `sub` need not resolve anywhere real —
# realpath spells the `.gf/wt/<r>/<k>` ancestor lexically, which is the
# whole point of the fix: the shape alone must not adopt `outside` as a
# root. (Real git is required — the mock backend cannot express a
# committed symlink.)


def test_pull_committed_symlink_forging_outside_gf_wt_refuses(tmp_path):
    """P1 signature: `sub -> <tmp>/outside/.gf/wt/<r>/master` committed in
    the parent + `path = "sub/master/libs/api"` spells a child realpath
    whose `.gf/wt` ancestor names `outside` — a root that does not even
    exist. Adoption is declined, `binding_root` finds no declaring
    ancestor of the outside child, the anchor falls to the invoking
    parent, and the routing refusal fires BEFORE any fetch or write:
    rc 1, `gf:` envelope naming the binding and "outside", `outside`
    never created, and no `.gf` storage planted in-parent either.

    PRE-FIX (verified at base 85856af): the pull returned 0 —
    "Pulled api" — having created `outside/.gf/repos/<key>/git`, the
    `outside/.gf/wt/<key>/master` checkout + state, and the
    `outside/sub/master/libs/api` consumer link: every write the
    containment rule exists to prevent, landed outside the workspace.
    """
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    outside = tmp_path / "outside"
    _write_manifest(parent, [{
        "name": "api", "url": f"{up}/docs/api", "ref": "latest",
        "path": "sub/master/libs/api"}])
    # the committed mid-path symlink spells a `.gf/wt/<r>/<k>` ancestor
    # under a root that has no gf storage — and need not exist at all
    os.symlink(str(outside / ".gf" / "wt" / "forged" / "master"),
               parent / "sub")
    _commit(parent)
    if _committed_mode(parent, "sub") != "120000":
        pytest.fail("setup: `sub` did not commit as a symlink")

    r = gf("-C", str(parent), "pull", check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    # the `gf:` envelope on stderr carries the folder_error fields —
    # operation, binding name, manifest path — and the routing refusal
    assert "gf: pull failed for git-folder 'api' (sub/master/libs/api)" \
        in r.stderr, err
    assert "outside" in r.stderr, err

    # the forged root was never adopted — `outside` was never created
    # (zero bytes under it), and the refusal fired in routing before the
    # pull built any storage inside the parent
    assert not os.path.lexists(outside), (
        "gf pull wrote outside the workspace")
    assert not (parent / ".gf").exists(), (
        "the routing refusal should precede any storage write")


def test_pull_forged_gf_wt_shape_inside_workspace_declines(tmp_path):
    """Same forged shape pointed INTO the workspace: `o/` is in-root and
    carries a materialized `.gf/wt/<r>/<k>` shape (committed, so a
    checkout ships it) — but no `.git` and no matching store, so
    `owning_root` still declines to adopt it.

    `binding_root` then recovers the real parent — `(parent / path)`
    resolves to the child through `sub` — so the pull stays in-workspace:
    the store + checkout build under `parent/.gf`, and the LINK layer
    refuses: the spelled link's parent realpaths into `o/.gf` —
    `in_gf_tree(link_parent)` — rc 1 naming the binding. `o/` keeps only
    its planted keep file: no store, no checkout, no link under it.

    PRE-FIX the pull adopted `o` as the owning root and served the whole
    binding under it — rc 0, "Pulled api", `o/.gf/repos/<key>/git` +
    `o/.gf/wt/<key>/master` created and the link planted at
    `o/sub/libs/api`.
    """
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    o = parent / "o"
    _write_manifest(parent, [{
        "name": "api", "url": f"{up}/docs/api", "ref": "latest",
        "path": "sub/libs/api"}])
    # relative link: `sub` spells `o/.gf/wt/<r>/<k>` inside the tree —
    # and the shape is materialized, so adoption sees real dirs, not
    # just a lexical spell
    os.symlink(os.path.join("o", ".gf", "wt", "forged", "master"),
               parent / "sub")
    keep = o / ".gf" / "wt" / "forged" / "master" / "keep.txt"
    keep.parent.mkdir(parents=True)
    keep.write_text("planted checkout shape\n")
    _commit(parent)
    if _committed_mode(parent, "sub") != "120000":
        pytest.fail("setup: `sub` did not commit as a symlink")
    planted = _tree_bytes(o)

    r = gf("-C", str(parent), "pull", check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    # streamed fetch output shares stderr with the die() envelope —
    # match the `gf:` refusal line itself, not the stream's head
    assert "gf: pull failed for git-folder 'api' (sub/libs/api)" \
        in r.stderr, err
    assert "gf-managed storage" in r.stderr, err  # the link-layer refusal

    # `o` was never adopted — its tree is byte-identical to the planted
    # shape: no repo store, no checkout, no link under it
    assert _tree_bytes(o) == planted
    assert not (o / ".gf" / "repos").exists(), (
        "the pull created a repo store under the forged root")

    # the binding_root recovery sent every write to the real parent:
    # the pull reached the link layer, so the store + checkout exist
    # under `parent/.gf` — in-workspace — while the refused link itself
    # was never placed
    repos = list((parent / ".gf" / "repos").glob("*/git"))
    assert repos, "the pull should have built the store under the parent"
    assert not os.path.lexists(parent / "sub" / "libs" / "api")
    # the planted mid-path link is untouched
    assert os.readlink(parent / "sub") == os.path.join(
        "o", ".gf", "wt", "forged", "master")


# ---------------------------------------------------------------------------
# Unit pins — `layout.owning_root` proof matrix (seeded layout, no git)
#
# `subfolder_checkout` builds an honest `<root>/.gf/wt/<rk>/<key>`
# work_tree; the root's proof is the only variable. `owning_root`
# compares realpaths — assert against `os.path.realpath(root)`.

REPO = "https://example.com/lib"
RK = layout.repo_key(REPO)


def _co(root: Path, key: str = "master") -> layout.Checkout:
    """A store checkout whose work_tree is `<root>/.gf/wt/<RK>/<key>` —
    the shape `owning_root` either proves or declines."""
    return layout.subfolder_checkout(root, REPO, key, "")


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


@pytest.mark.parametrize(
    "marker", ["dir", "gitfile", "symlink"],
    ids=["git-dir", "git-gitfile", "git-symlink"])
def test_owning_root_adopts_root_marked_by_git(tmp_path, marker):
    """Legit adoption: a `.git` entry proves the root in every form
    `find_parent_root` accepts it — a directory (normal parent), a
    gitfile (`gf worktree add` / `git worktree` checkouts), or a
    symlink — none of which can be committed into a git tree. The
    checkout tree itself need not exist on disk."""
    root = tmp_path / "root"
    root.mkdir()
    if marker == "dir":
        (root / ".git").mkdir()
    elif marker == "gitfile":
        (root / ".git").write_text(
            "gitdir: /elsewhere/repo/.git/worktrees/root\n")
    else:
        (root / "real-git").mkdir()
        os.symlink("real-git", root / ".git")
    assert layout.owning_root(_co(root)) == _real(root)


def test_owning_root_adopts_store_only_root(tmp_path):
    """A root whose `.git` is gone but whose `.gf` storage is real still
    proves itself: the matching `<root>/.gf/repos/<rk>/git` store carries
    a `HEAD`, so the checkout under it is adopted (the dead-link /
    store-survives case)."""
    root = tmp_path / "root"
    store = root / ".gf" / "repos" / RK / "git"
    (store / "objects").mkdir(parents=True)
    (store / "HEAD").write_text("ref: refs/heads/master\n")
    co = _co(root)
    co.work_tree.mkdir(parents=True)
    assert layout.owning_root(co) == _real(root)


def test_owning_root_refuses_unproven_root(tmp_path):
    """The discriminating arm: the bare `.gf/wt/<r>/<k>` shape — a tree
    that exists but carries no `.git` and no matching store — is NOT
    adopted; nor does a `gf.toml` at that root prove anything (the
    manifest is committable content). PRE-FIX both spelled root."""
    root = tmp_path / "root"
    co = _co(root)
    co.work_tree.mkdir(parents=True)  # the shape exists; nothing proves it
    assert layout.owning_root(co) is None

    (root / "gf.toml").write_text("git_folder = []\n")
    assert layout.owning_root(co) is None


def test_owning_root_refuses_root_inside_gf_tree(tmp_path):
    """A root inside a `.gf` tree is never adopted — the `in_gf_tree`
    refusal precedes both proofs: even a `.git` dir AND the matching
    store planted under `outer/.gf/parking` cannot make it a root."""
    inner = tmp_path / "outer" / ".gf" / "parking"
    (inner / ".git").mkdir(parents=True)
    store = inner / ".gf" / "repos" / RK / "git"
    (store / "objects").mkdir(parents=True)
    (store / "HEAD").write_text("ref: refs/heads/master\n")
    co = _co(inner)  # work_tree = inner/.gf/wt/<RK>/master
    co.work_tree.mkdir(parents=True)
    assert layout.owning_root(co) is None


def test_owning_root_refuses_store_keyed_to_other_repo(tmp_path):
    """The store proof must key the SAME repository as the `.gf/wt`
    ancestor: a `repos/<other>/git` store under the root is no proof for
    a `wt/<RK>/...` checkout — the `<repo-key>` segments must match."""
    root = tmp_path / "root"
    other = root / ".gf" / "repos" / "other-deadbeef" / "git"
    (other / "objects").mkdir(parents=True)
    (other / "HEAD").write_text("ref: refs/heads/master\n")
    co = _co(root)
    co.work_tree.mkdir(parents=True)
    assert layout.owning_root(co) is None


def test_binding_root_never_anchors_inside_gf_tree(tmp_path):
    """The rider: an ancestor inside a `.gf` tree never owns the anchor
    even when BOTH marker arms coincide there — `libs/api` spelled from
    the checkout root resolves to `child`, and a `gf.toml` sits at that
    ancestor exactly as cone mode materializes an upstream's committed
    manifest. Without the rider the walk claims the `.gf`-interior
    ancestor; with it, no ancestor qualifies and the caller falls back
    to the invoking root."""
    root = tmp_path / "root"
    (root / ".git").mkdir(parents=True)
    co_root = root / ".gf" / "wt" / RK / "master"
    child = co_root / "libs" / "api"
    child.mkdir(parents=True)
    # the materialized manifest — a marker the `.gf` interior can hold
    (co_root / "gf.toml").write_text("git_folder = []\n")

    assert manifest.binding_root(child, "libs/api") is None
    # control: spelled honestly from the real root, the same child is
    # owned — the rider excludes `.gf` ancestors, not real ones
    os.symlink(os.path.relpath(co_root, root), root / "vend")
    assert manifest.binding_root(child, "vend/libs/api") == root
