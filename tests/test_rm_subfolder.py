# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`gf rm` subfolder semantics.

Exercised on the landed machinery (`remove_child`'s link branch +
`owns_consumer_link`, `cmd_rm`'s parent_root/refusal, the clone seam,
the grouped pull retarget); these pins verify the spec-visible
guarantees.

Scope: after rm, `<root>/.gf` byte-identical, only link + manifest
entry gone; re-clone of same url restores folder with uncommitted work;
refusal from non-owning parent worktree; dangling link still removable;
whole-repo rm unchanged (existing pins — nothing new).

Authorities: spec `gf rm` (L265-274: link+manifest entry only; checkout/
cone/store untouched incl. uncommitted work; owning-root-only removal;
atomic manifest replace AFTER unlink), L403 area (`gf rm` cannot run
from a symlinked child); arch GF-D9/D14; constraint "Do not delete user
worktrees". The chain-through-link pin (worktree-add link targets the
source consumer link so retargets propagate) lives in
test_worktree_links.py, not here.
"""

import os
import subprocess
import tomllib
from pathlib import Path

from conftest import gf, git


# ---------------------------------------------------------------------------
# helpers


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} rc={r.returncode}:\n{r.stderr}")
    return r


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str = "upstream",
              marker: str = "api on master") -> Path:
    up = tmp_path / name
    _git("init", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text(marker)
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text(f"tool in {name}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "init")
    _git("-C", work, "push", "origin", "master")
    return up


def _gf_tree(root: Path) -> dict:
    """Every path under `<root>/.gf` mapped to ('dir',) / ('link', target)
    / ('file', bytes) — the byte-identical snapshot."""
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


def _manifest_names(parent: Path) -> set[str]:
    data = tomllib.loads((parent / "gf.toml").read_text())
    return {f["name"] for f in data.get("git_folder", [])}


def _clone_pair(parent: Path, up: Path) -> None:
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")


# ---------------------------------------------------------------------------
# receiving pins


def test_rm_removes_only_link_and_entry_gf_byte_identical(tmp_path):
    """`gf rm` on a subfolder binding removes only the consumer link and
    the manifest entry — the whole `.gf` tree (store, checkout, state,
    sparse cone, uncommitted work inside the checkout) is byte-identical
    (spec L271; GF-D9; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "vendor" / "api" / "dirty.txt").write_text("uncommitted\n")
    before = _gf_tree(parent)

    gf("-C", str(parent), "rm", "vendor/api")

    link = parent / "vendor" / "api"
    assert not os.path.lexists(link)
    assert _manifest_names(parent) == {"tools"}
    assert _gf_tree(parent) == before, ".gf tree changed on rm"
    # sibling binding untouched and still served
    assert (parent / "vendor" / "tools" / "t.txt").is_file()


def test_rm_from_second_worktree_refuses_with_envelope(tmp_path):
    """`gf rm` cannot run from a symlinked child in another parent
    worktree — the linked child is not owned by that worktree's `.gf/wt`
    (spec L403 + L271 owning-root-only; GF-D14; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    assert (wt2 / "vendor" / "api").is_symlink()

    manifest_before = (wt2 / "gf.toml").read_bytes()
    r = gf("-C", str(wt2), "rm", "vendor/api", check=False)
    assert r.returncode != 0
    # folder_error envelope: name + path + operation
    err = r.stderr + r.stdout
    assert "api" in err and "vendor/api" in err and "rm" in err
    # nothing removed anywhere
    assert os.path.lexists(wt2 / "vendor" / "api")
    assert os.path.lexists(parent / "vendor" / "api")
    assert (wt2 / "gf.toml").read_bytes() == manifest_before
    assert _manifest_names(parent) == {"api", "tools"}


def test_rm_dangling_consumer_link_still_removable(tmp_path):
    """A consumer link whose checkout vanished is still removable —
    ownership resolves lexically on the dangling realpath (spec L271;
    GF-D14 'dangling resolves lexically'; row item)."""
    import shutil
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    wt = parent / ".gf" / "wt"
    rk = next(p for p in wt.iterdir() if p.is_dir())
    shutil.rmtree(rk / "master")  # checkout dir gone; links now dangle

    link = parent / "vendor" / "api"
    assert link.is_symlink() and not link.exists()  # dangling
    gf("-C", str(parent), "rm", "vendor/api")
    assert not os.path.lexists(link)
    assert _manifest_names(parent) == {"tools"}


def test_rm_inside_owned_consumer_link_cwd(tmp_path):
    """`gf rm` with no args inside the OWNED consumer link selects that
    binding (cwd realpath inside the owner's `.gf/wt`) — the
    symlinked-child refusal applies only to other worktrees' links
    (spec target-selection exception vs L403; row-adjacent)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    r = gf("-C", str(parent / "vendor" / "api"), "rm", check=False)
    assert r.returncode == 0
    assert not os.path.lexists(parent / "vendor" / "api")
    assert _manifest_names(parent) == {"tools"}


def test_rm_multi_arg_failure_leaves_manifest_atomic(tmp_path):
    """gf.toml is atomically replaced only after ALL selected children are
    unlinked: a failing selection keeps the manifest whole even though an
    earlier link was already removed (spec L273; row-adjacent)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)

    # register a foreign link the parent does not own
    foreign_dir = tmp_path / "elsewhere"
    foreign_dir.mkdir()
    (parent / "vendor" / "foreign").symlink_to(foreign_dir)
    manifest = (parent / "gf.toml").read_text()
    (parent / "gf.toml").write_text(
        manifest + '[[git_folder]]\nname = "zforeign"\n'
        f'url = "{foreign_dir}"\nref = "latest"\npath = "vendor/foreign"\n')

    r = gf("-C", str(parent), "rm", "vendor/api", "vendor/foreign",
           check=False)
    assert r.returncode != 0
    # manifest unchanged — the temp file is discarded on error
    assert _manifest_names(parent) == {"api", "tools", "zforeign"}


def test_rm_all_removes_links_keeps_gf_tree(tmp_path):
    """`gf rm --all` unlinks every subfolder binding and preserves the
    whole `.gf` tree (spec --all selection + L271; row-adjacent)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    before = _gf_tree(parent)
    gf("-C", str(parent), "rm", "--all")
    assert not os.path.lexists(parent / "vendor" / "api")
    assert not os.path.lexists(parent / "vendor" / "tools")
    assert _manifest_names(parent) == set()
    assert _gf_tree(parent) == before


def test_rm_refuses_manifest_path_spelling_gf_interior(tmp_path):
    """F1 corruption guard: a manifest binding whose `path` spells a
    `.gf/wt/...` interior path (the spelling the clone duplicate-path
    refusal exists to keep out) is refused by `gf rm` — and the refusal
    modifies NOTHING under `<root>/.gf` (constraints L19: rm removes
    only the consumer link + manifest entry; the checkout, its
    worktree record, and the store are gf storage, never a removable
    child). Without the guard, `remove_child`'s whole-repo branch would
    mistake the checkout's admin dir for a child `.gf/git` and
    move+delete it."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)

    # Corrupt the api entry: path spells the checkout-interior subdir.
    rk = next(
        p for p in (parent / ".gf" / "wt").iterdir() if p.is_dir())
    interior = f".gf/wt/{rk.name}/master/docs/api"
    manifest = (parent / "gf.toml").read_text()
    assert 'path = "vendor/api"' in manifest
    (parent / "gf.toml").write_text(
        manifest.replace('path = "vendor/api"', f'path = "{interior}"'))

    before = _gf_tree(parent)
    r = gf("-C", str(parent), "rm", "api", check=False)

    assert r.returncode != 0
    # refusal names the binding + the gf storage the path reached into:
    # manifest-read validation fails closed before rm ever dispatches
    # (remove_child's own guard stays as the in-depth layer)
    err = r.stderr + r.stdout
    assert "api" in err and "gf-managed storage" in err
    # constraints L19: nothing under <root>/.gf is modified — the
    # checkout (incl. the mapped subdir the corrupt path spelled), the
    # worktree record, and the repo store are byte-identical
    assert _gf_tree(parent) == before, ".gf tree changed on refused rm"
    # the refused entry stays in the manifest; the real link survives
    assert _manifest_names(parent) == {"api", "tools"}
    assert os.path.lexists(parent / "vendor" / "api")
