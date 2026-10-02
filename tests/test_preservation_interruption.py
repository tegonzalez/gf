# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""P3 interruption, recovery, and worktree-remove-ownership witnesses
(Change DC-DOC-PLAN-007, obligations O-recovery / O-worktree
remove-side; GF-D21, GF-D22, and the `gf worktree remove` owned-storage
refusal).

Expected behavior derives only from the admitted documents:

- gf-spec.md `gf worktree remove`: refuse when the target worktree owns
  `gf` storage the removal would carry away — a `.gf` directory
  anywhere inside the target holding any real content (repo stores,
  checkouts, binding state, a child's `.gf` gitdir) detected from the
  FILESYSTEM, not the manifest alone, so unmanifested residue and
  children registered only in the target's own manifest are caught the
  same way; a target `gf.toml` that exists but is a symlink or
  unparseable also refuses, since the declared bindings cannot be
  enumerated and ownership is ambiguous; `--force` never waives the
  refusal; a `.gf` left holding nothing — empty or residue skeleton
  dirs — does not block. A target that only links children OUT removes
  fine with the source storage preserved.
- gf-arch.md "Interruption and recovery" / GF-D21: partial effects are
  retained, not swept; git's own in-progress merge/rebase markers
  refuse a stacked operation; a stranded `gf autostash` entry stays
  named in the checkout's stash list; atomic writes leave no torn
  record.
- gf-arch.md GF-D22 + spec `gf worktree add`: a failing add removes
  ONLY the worktree this invocation registered — pre-existing sibling
  paths and links are untouched.
- gf-arch.md GF-D18/D19: retained artifacts re-materialize under
  recorded identity; ambiguous identity refuses; a reconciled
  registered-worktree record lets a re-run proceed to the preserved
  end state.

Stuck states are driven directly (a named `gf autostash` entry plus a
real `MERGE_HEAD` created by fixture git, a worktree record severed by
hand) — deterministic where a SIGKILL mid-operation would be timing-
dependent. Nothing in this file consults `src/gf/` to decide expected
behavior.
"""

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from conftest import checkout_snapshot, gf, git, hash_tree
from gf import layout


# ---------------------------------------------------------------------------
# helpers — real-git fixtures, same style as test_preservation_unmap


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
    """Push one commit changing x.txt and t.txt; return the tip sha."""
    work = tmp_path / f"_adv_{tag}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api" / "x.txt").write_text(f"api {tag}\n")
    (work / "tools" / "t.txt").write_text(f"tool {tag}\n")
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
    return parent, up, parent / "vendor" / "lib"


def _clone_pair(parent: Path, up: Path) -> None:
    """Two subfolder bindings of one upstream on one shared checkout."""
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")


def _co(parent: Path, up: Path, key: str = "master",
        subdir: str = "docs/api"):
    return layout.subfolder_checkout(parent, str(up), key, subdir)


def _child_git(child: Path, *args) -> subprocess.CompletedProcess:
    return _git("--git-dir", str(child / ".gf" / "git"),
                "--work-tree", str(child), *args)


def _co_git(co, *args) -> subprocess.CompletedProcess:
    """git addressed at a shared checkout's admin dir + worktree."""
    return _git("--git-dir", str(co.gitdir),
                "--work-tree", str(co.work_tree), *args)


def _manifest_names(root: Path) -> set[str]:
    data = tomllib.loads((root / "gf.toml").read_text())
    return {f["name"] for f in data.get("git_folder", [])}


def _gf_tree(root: Path) -> dict:
    """Every path under `<root>/.gf` → ('dir',)/('link',t)/('file',b)."""
    base = root / ".gf"
    snap = {}
    if not base.is_dir():
        return snap
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).as_posix()
        if p.is_symlink():
            snap[rel] = ("link", os.readlink(p))
        elif p.is_dir():
            snap[rel] = ("dir",)
        else:
            snap[rel] = ("file", p.read_bytes())
    return snap


def _tree(root: Path) -> dict:
    """Byte-level snapshot of a whole directory (worktree incl. `.gf`)."""
    snap = {}
    if not root.is_dir():
        return snap
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            snap[rel] = ("link", os.readlink(p))
        elif p.is_dir():
            snap[rel] = ("dir",)
        else:
            snap[rel] = ("file", p.read_bytes())
    return snap


def _registered_worktrees(parent: Path) -> list[str]:
    out = _out("-C", parent, "worktree", "list", "--porcelain")
    return [line.split(" ", 1)[1] for line in out.splitlines()
            if line.startswith("worktree ")]


# ---------------------------------------------------------------------------
# refusal — worktree remove never carries away owned `.gf` storage


def test_worktree_remove_refuses_target_owning_subfolder_store(tmp_path):
    """refusal · worktree: `gf worktree remove` on a target whose `.gf`
    owns a repo store + checkout refuses BEFORE `git worktree remove`
    runs — nonzero, the message names the owned `.gf` storage, the
    target survives byte-identical including dirty checkout bytes, and
    `--force` does not waive it (spec `gf worktree remove` owned-
    storage bullet; "gf offers no discard mode")."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    # a binding in the source parent first — materializes gf.toml
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools")
    git("add", "gf.toml", cwd=parent)
    git("commit", "-qm", "manifest", cwd=parent)
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    # the TARGET owns gf storage: a subfolder binding created inside wt2
    gf("-C", str(wt2), "clone", str(up / "docs/api"), "vendor/api")
    co = _co(wt2, up)
    (co.work_tree / "docs" / "api" / "dirty.txt").write_text("dirty\n")
    before = _tree(wt2)

    for extra in ((), ("--force",)):
        r = gf("-C", str(parent), "worktree", "remove", str(wt2),
               *extra, check=False)
        assert r.returncode != 0, (
            f"remove{extra} rc=0 carried away owned .gf storage")
        err = r.stderr + r.stdout
        assert "gf:" in err, err
        assert ".gf" in err, (
            f"refusal did not name the owned storage:\n{err}")
        # nothing was removed — the whole target is byte-identical
        assert _tree(wt2) == before
        assert wt2.is_dir()


def test_worktree_remove_refuses_target_with_real_dir_child(tmp_path):
    """refusal · worktree: a manifest entry that is a REAL DIRECTORY
    inside the target carrying its own `.gf` gitdir (a whole-repo child
    created in that worktree) owns storage the removal would carry away
    — refused under `--force` too, child and manifest intact."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools")
    git("add", "gf.toml", cwd=parent)
    git("commit", "-qm", "manifest", cwd=parent)
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    gf("-C", str(wt2), "clone", str(up), "vendor/lib")
    child = wt2 / "vendor" / "lib"
    assert (child / ".gf" / "git" / "HEAD").is_file()
    (child / "dirty.txt").write_text("dirty\n")
    before = _tree(wt2)

    r = gf("-C", str(parent), "worktree", "remove", str(wt2),
           "--force", check=False)
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    err = r.stderr + r.stdout
    assert "gf:" in err, err

    assert _tree(wt2) == before
    assert (child / ".gf" / "git" / "HEAD").is_file()
    # wt2's manifest still lists its own child plus the tools binding
    # the worktree add inherited from the parent
    assert _manifest_names(wt2) == {"lib", "tools"}


def test_worktree_remove_refuses_target_with_unparseable_manifest(
        tmp_path):
    """refusal · worktree: the TARGET's own `gf.toml` left
    conflict-marked — unparseable, so its declared bindings cannot be
    enumerated and ownership is ambiguous — makes `gf worktree remove`
    refuse even under `--force`, while a real-dir child inside the
    target carrying `.gf/git` plus dirty work survives byte-identical
    (spec `gf worktree remove` owned-storage bullet: 'a target gf.toml
    that exists but is a symlink or unparseable also refuses')."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools")
    git("add", "gf.toml", cwd=parent)
    git("commit", "-qm", "manifest", cwd=parent)
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    # a real-dir child owning `.gf` storage registered ONLY in wt2's
    # own manifest — then the manifest is left conflict-marked
    gf("-C", str(wt2), "clone", str(up), "vendor/lib")
    child = wt2 / "vendor" / "lib"
    assert (child / ".gf" / "git" / "HEAD").is_file()
    (child / "dirty.txt").write_text("dirty\n")
    (wt2 / "gf.toml").write_text(
        "<<<<<<< HEAD\n"
        '[[git_folder]]\nname = "tools"\n'
        "=======\n"
        '[[git_folder]]\nname = "lib"\n'
        ">>>>>>> incoming\n")
    # commit wt2's whole tree — conflict-marked manifest, `.gf` carrier,
    # dirty bytes — so `git worktree remove` alone would take it
    # cleanly: any refusal is provably the owned-storage/manifest gate,
    # never git's own modified-or-untracked refusal
    git("add", "-A", cwd=wt2)
    git("commit", "-qm", "corrupt manifest", cwd=wt2)
    before = _tree(wt2)

    for extra in ((), ("--force",)):
        r = gf("-C", str(parent), "worktree", "remove", str(wt2),
               *extra, check=False)
        assert r.returncode != 0, (
            f"remove{extra} rc=0 over an unparseable target manifest")
        err = r.stderr + r.stdout
        assert "gf:" in err, err
        assert ".gf" in err or "gf.toml" in err, (
            f"refusal named neither the storage nor the manifest:\n"
            f"{err}")
        # target survives byte-identical: corrupt manifest, `.gf`
        # carrier, and dirty work all retained
        assert _tree(wt2) == before
        assert (child / ".gf" / "git" / "HEAD").is_file()


def test_worktree_remove_refuses_target_with_symlinked_manifest(
        tmp_path):
    """refusal · worktree: the target's `gf.toml` replaced by a symlink
    to a REAL manifest — the content still parses, so a refusal must
    come from the link form itself: a manifest that is not a real file
    cannot be trusted to enumerate the bindings, so ownership is
    ambiguous and `gf worktree remove` refuses under `--force` too,
    the target surviving byte-identical (same spec bullet)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools")
    git("add", "gf.toml", cwd=parent)
    git("commit", "-qm", "manifest", cwd=parent)
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    # symlink to a manifest whose content parses — fail-closed on the
    # form, not on the bytes
    os.unlink(wt2 / "gf.toml")
    os.symlink(str(parent / "gf.toml"), wt2 / "gf.toml")
    assert (wt2 / "gf.toml").is_symlink()
    # commit the link — a clean worktree means only the owned-storage /
    # manifest gate can refuse (git alone would remove it)
    git("add", "gf.toml", cwd=wt2)
    git("commit", "-qm", "linked manifest", cwd=wt2)
    before = _tree(wt2)

    for extra in ((), ("--force",)):
        r = gf("-C", str(parent), "worktree", "remove", str(wt2),
               *extra, check=False)
        assert r.returncode != 0, (
            f"remove{extra} rc=0 over a symlinked target manifest")
        err = r.stderr + r.stdout
        assert "gf:" in err, err
        assert _tree(wt2) == before


def test_worktree_remove_refuses_unmanifested_gf_carrier(tmp_path):
    """refusal · worktree: a `.gf` directory with real content
    registered NOWHERE — not in the parent's manifest, not in the
    target's — is still owned storage the removal would carry away:
    the check is filesystem detection, not manifest enumeration
    (GF-D21 crash-residue shape; spec `gf worktree remove`
    owned-storage bullet). `--force` never waives it and the carrier
    plus its dirty bytes survive byte-identical."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools")
    git("add", "gf.toml", cwd=parent)
    git("commit", "-qm", "manifest", cwd=parent)
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    # hand-plant a `.gf` carrier — a killed or hand-moved operation's
    # residue that no manifest knows
    carrier = wt2 / "mystery" / ".gf" / "git" / "objects" / "ab"
    carrier.mkdir(parents=True)
    (wt2 / "mystery" / ".gf" / "git" / "HEAD").write_text(
        "ref: refs/heads/master\n")
    (carrier / "cd1234").write_bytes(b"\xde\xad\xbe\xef")
    (wt2 / "mystery" / "dirty.txt").write_text("dirty\n")
    assert _manifest_names(wt2) == {"tools"}  # mystery is unmanifested
    # commit the carrier — a clean worktree isolates the refusal to
    # the owned-storage check, never git's untracked-files refusal
    git("add", "-A", cwd=wt2)
    git("commit", "-qm", "unmanifested carrier", cwd=wt2)
    before = _tree(wt2)

    for extra in ((), ("--force",)):
        r = gf("-C", str(parent), "worktree", "remove", str(wt2),
               *extra, check=False)
        assert r.returncode != 0, (
            f"remove{extra} rc=0 carried away an unmanifested .gf")
        err = r.stderr + r.stdout
        assert "gf:" in err, err
        assert ".gf" in err, (
            f"refusal did not name the owned storage:\n{err}")
        assert _tree(wt2) == before


def test_worktree_remove_empty_gf_residue_and_linkout_pair(tmp_path):
    """completion · worktree: a `.gf` holding no stores/checkouts/state
    is empty residue and does not block removal; a target that only
    links children OUT removes fine with the SOURCE storage and its
    dirty checkout preserved byte-identical (spec `gf worktree remove`
    owned-storage bullet, both halves)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    git("add", "gf.toml", cwd=parent)
    git("commit", "-qm", "manifest", cwd=parent)
    co = _co(parent, up)
    (co.work_tree / "docs" / "api" / "dirty.txt").write_text("dirty\n")
    source_storage = _gf_tree(parent)

    # arm A — empty `.gf` residue does not block; git's own untracked-
    # files refusal is not the owned-storage refusal, so the forwarded
    # `--force` is the legitimate removal vehicle here
    wt_a = tmp_path / "wt_a"
    gf("-C", str(parent), "worktree", "add", str(wt_a))
    (wt_a / ".gf").mkdir()  # residue only — no repos/wt/state
    r = gf("-C", str(parent), "worktree", "remove", str(wt_a),
           "--force", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert not wt_a.exists()

    # arm B — link-out target removes; source storage untouched
    wt_b = tmp_path / "wt_b"
    gf("-C", str(parent), "worktree", "add", str(wt_b))
    assert (wt_b / "vendor" / "api").is_symlink()
    r = gf("-C", str(parent), "worktree", "remove", str(wt_b),
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert not wt_b.exists()
    assert _gf_tree(parent) == source_storage
    # the source binding still serves its retained work
    assert (parent / "vendor" / "api" / "dirty.txt"
            ).read_text() == "dirty\n"


def test_worktree_add_rollback_removes_only_what_it_created(tmp_path):
    """recovery · rollback (GF-D22): a `gf worktree add` that fails in
    the link step removes only the worktree it just registered — the
    pre-existing sibling binding, its link, its dirty checkout, and the
    source store are byte-identical; no orphan registration remains."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    co = _co(parent, up)
    (co.work_tree / "docs" / "api" / "dirty.txt").write_text("dirty\n")
    # a tracked real-dir child whose path collides at the destination:
    # `git worktree add` materializes vendor/zz before the link step
    child = parent / "vendor" / "zz"
    child.mkdir()
    (child / "t.txt").write_text("tracked\n")
    git("add", "vendor/zz/t.txt", cwd=parent)
    git("commit", "-qm", "track zz", cwd=parent)
    gf("-C", str(parent), "init", "vendor/zz")
    storage_before = _gf_tree(parent)
    api_link = parent / "vendor" / "api"

    wt = tmp_path / "wt"
    r = gf("-C", str(parent), "worktree", "add", str(wt), check=False)
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)

    # only the provably-created worktree is gone — nothing else moved
    assert not wt.exists()
    assert str(wt.resolve()) not in _registered_worktrees(parent)
    assert _gf_tree(parent) == storage_before
    assert api_link.is_symlink()
    assert (co.work_tree / "docs" / "api" / "dirty.txt"
            ).read_text() == "dirty\n"
    assert (child / ".gf" / "git" / "HEAD").is_file()


# ---------------------------------------------------------------------------
# refusal + recovery — a stranded autostash and in-progress merge
# classify truthfully; resolving them reaches the preserved end state


def test_pull_refuses_on_in_progress_merge_keeps_stranded_autostash(
        tmp_path):
    """refusal·recovery · interruption residue: a checkout left holding
    a real `MERGE_HEAD` and a named `gf autostash` stash entry — the
    state a killed `pull --autostash` leaves — must never be stacked on
    or swept: `gf pull` refuses truthfully, the stash entry stays
    named, `MERGE_HEAD` stays, and every byte/ref is unchanged
    (gf-arch "Interruption and recovery"). Resolving the markers by the
    user's own git then lets the next `gf pull` reach the preserved end
    state."""
    parent, up, child = _clone_lib(tmp_path)
    # diverge so a real merge conflicts — local commit touches x.txt
    (child / "docs" / "api" / "x.txt").write_text("local\n")
    _child_git(child, "add", "docs/api/x.txt")
    _child_git(child, "commit", "-qm", "local x")
    adv = _advance(up, tmp_path)
    # the stranded stash entry, exactly as pull --autostash names it
    (child / "dirty.txt").write_text("dirty\n")
    _child_git(child, "stash", "push", "-u", "-m", "gf autostash")
    # then the in-progress merge the kill left behind
    _child_git(child, "fetch", "origin")
    m = _git("--git-dir", child / ".gf" / "git",
             "--work-tree", child, "merge", "origin/master",
             check=False)
    assert m.returncode != 0, "setup: merge did not conflict"
    gitdir = child / ".gf" / "git"
    assert (gitdir / "MERGE_HEAD").is_file()
    before = checkout_snapshot(gitdir, child)
    head_before = _out("--git-dir", gitdir, "rev-parse", "HEAD")

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    err = r.stderr + r.stdout
    assert "gf:" in err, err

    # nothing swept, nothing stacked: markers, stash, bytes, HEAD all
    # preserved
    assert (gitdir / "MERGE_HEAD").is_file()
    stash = _child_git(child, "stash", "list").stdout
    assert "gf autostash" in stash
    assert _out("--git-dir", gitdir, "rev-parse", "HEAD") == head_before
    after = checkout_snapshot(gitdir, child)
    assert after["bytes"] == before["bytes"]
    assert after["refs"] == before["refs"]

    # user's own git resolves the stuck state — merge commit heals the
    # divergence, stash pop restores the stranded work; the re-run then
    # reaches the preserved end state rather than refusing forever
    (child / "docs" / "api" / "x.txt").write_text("resolved\n")
    _child_git(child, "add", "docs/api/x.txt")
    _child_git(child, "commit", "-qm", "merge resolution")
    _child_git(child, "stash", "pop", "--index")
    assert (child / "dirty.txt").read_text() == "dirty\n"
    r = gf("-C", str(parent), "pull", "--autostash", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    # the merge commit (containing adv) stands; the stranded dirt is
    # restored in place; no gf autostash entry leaked
    head = _out("--git-dir", gitdir, "rev-parse", "HEAD")
    assert _out("--git-dir", gitdir, "merge-base", "--is-ancestor",
                adv, head) == ""
    assert (child / "dirty.txt").read_text() == "dirty\n"
    assert "gf autostash" not in _child_git(
        child, "stash", "list").stdout


def test_rm_refusal_rerun_after_reconcile_converts_cleanly(tmp_path):
    """recovery · idempotence: a `gf rm` refused on an unreconcilable
    linked-worktree gitfile is a stable state — re-running refuses
    identically (no drift, no partial move); once the user reconciles
    the gitfile the SAME `gf rm` completes the conversion and the
    linked worktree resolves at the new `.git` location (GF-D21
    "the next run classifies truthfully"; GF-D19 reconcile)."""
    parent, _up, child = _clone_lib(tmp_path)
    ext = tmp_path / "ext-wt"
    gf("-C", str(child), "git", "worktree", "add", str(ext), "-b",
       "side")
    gitfile_text = (ext / ".git").read_text()
    os.unlink(ext / ".git")
    before = checkout_snapshot(child / ".gf" / "git", child)

    r1 = gf("-C", str(parent), "rm", "vendor/lib", check=False)
    r2 = gf("-C", str(parent), "rm", "vendor/lib", check=False)
    for r in (r1, r2):
        assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
        assert "rm failed for git-folder 'lib'" in (
            r.stderr + r.stdout), r.stderr
    # identical classification — still a gf-managed child, unmoved
    assert (child / ".gf" / "git" / "HEAD").is_file()
    assert not (child / ".git").exists()
    assert _manifest_names(parent) == {"lib"}
    assert checkout_snapshot(child / ".gf" / "git", child) == before

    # reconcile the record (the user rewrites the gitfile to the old
    # spelling — what the registered record still names); the re-run
    # then completes and the linked worktree resolves at `.git`
    (ext / ".git").write_text(gitfile_text)
    r = gf("-C", str(parent), "rm", "vendor/lib", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (child / ".git" / "HEAD").is_file()
    assert _manifest_names(parent) == set()
    after_move = (ext / ".git").read_text()
    assert ".gf/git" not in after_move
    assert _git("-C", ext, "status", check=False).returncode == 0


def test_clone_rerun_after_interrupted_create_rematerializes(tmp_path):
    """recovery · idempotence: a binding whose consumer link vanished
    after creation (the partial effect a killed `gf clone`/`pull`
    leaves) is re-materialized by `gf pull` under its recorded identity
    — the retained checkout, its state record, and its bytes are
    reused, never rebuilt over (gf-arch "Interruption and recovery";
    GF-D18 recorded-evidence identity)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    co = _co(parent, up)
    # retained work: a local commit (ahead, clean-ish) plus untracked
    # dirt — the link a killed op never recreated
    (co.work_tree / "docs" / "api" / "keep.txt").write_text("keep\n")
    _co_git(co, "add", "docs/api/keep.txt")
    _co_git(co, "commit", "-qm", "local work")
    (co.work_tree / "docs" / "api" / "dirty.txt").write_text("dirty\n")
    before = checkout_snapshot(co.gitdir, co.work_tree)
    os.unlink(parent / "vendor" / "api")  # the killed op's leftover

    r = gf("-C", str(parent), "pull", "--autostash", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    # re-materialized under recorded identity — same checkout object,
    # same state record bytes, work in place
    link = parent / "vendor" / "api"
    assert link.is_symlink() and link.exists()
    assert (link / "dirty.txt").read_text() == "dirty\n"
    assert (link / "keep.txt").read_text() == "keep\n"
    after = checkout_snapshot(co.gitdir, co.work_tree)
    for key in ("bytes", "porcelain", "staged", "stash", "head"):
        assert after[key] == before[key], key
    # The retained local commit still leads the branch, and its
    # immutable per-checkout retention ref durably names the same tip.
    assert after["refs"]["refs/heads/master"] == \
        before["refs"]["refs/heads/master"] == after["head"]
    assert after["refs"][f"refs/worktree/gf-retained-commits/{after['head']}"] == after["head"]
    # the per-checkout state record still carries the same binding
    # identity — `resolved` is legitimately rewritten by the apply
    state_after = co.state.read_bytes()
    for field in (b'ref = ', b'url = ', b'"docs/api"',
                  b'"vendor/api"'):
        assert field in state_after, state_after
