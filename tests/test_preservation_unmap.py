# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""P2 preservation witnesses for `gf rm` and reconnection — unmap,
remap, linked-worktree provenance, and provably-created-only cleanup
(Change DC-DOC-PLAN-007, obligations O-unmap / O-reconnect).

Oracle: docs/gf-testing.md "Preservation witness oracle". Every witness
captures the protected surface through real `git` before the stimulus,
drives the operation through the real `python -m gf` entry, then
re-observes. Fixture-minted SHAs and bytes are the exact-comparison
source.

Expected behavior derives only from the admitted documents:

- gf-spec.md `gf rm`: a whole-repo child is converted to an ordinary
  repository — `child/.gf/git` moves to `child/.git` with `HEAD`,
  index, refs, and config intact and `origin` still pointing at the
  real URL; registered `git worktree` entries keep resolving after the
  move because `gf` rewrites their path references, or `gf` refuses
  with instructions when a record cannot be reconciled — before the
  move, never leaving a `.git` that misdirects a registered worktree.
  `.gf/state` is provably gf-owned bookkeeping and is removed; `.gf`
  itself is removed only when left empty — foreign content is retained
  and reported. A subfolder `gf rm` removes only the consumer link and
  the manifest entry: the checkout including uncommitted work, its
  sparse cone, its per-checkout state record, and the repo store are
  not touched, and a binding whose consumer link is dangling or absent
  still unregisters cleanly without touching storage.
- gf-spec.md `gf pull` update algorithm: joining an existing checkout
  re-links only — the sparse cone widens to materialize the new
  binding's subdir, but no ref is applied — `HEAD` and the index are
  never reset — so a binding removed with `gf rm` and added back finds
  its uncommitted work in place. A checkout directory without its
  worktree record stops the operation with an error; ambiguous identity
  refuses, nothing is rebuilt over retained work.
- gf-arch.md GF-D18: binding identity is verified from recorded
  evidence before a join; ambiguous identity refuses.
- gf-arch.md GF-D19: `_convert_whole_repo_gitdir` /
  `_rewrite_linked_gitfiles`; foreign `.gf` content retained and
  reported; `gf clone` onto a `.git`-carrying path stays refused as
  occupied.
- gf-arch.md GF-D21/GF-D22: interruption retains and reports; a
  rollback or recovery step deletes only what this invocation provably
  created.
- gf-troubleshooting.md GF-TRB-13: the reconcile pass runs before the
  move and refuses when a registered gitfile cannot be reconciled.

Nothing in this file consults `src/gf/` to decide expected behavior.
"""

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from conftest import checkout_snapshot, gf, git, git_out, hash_tree
from gf import layout


# ---------------------------------------------------------------------------
# helpers — real-git fixtures, same style as test_preservation_update


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
    (`ignored.txt`) on master."""
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


def _co(parent: Path, up: Path, key: str = "master",
        subdir: str = "docs/api"):
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


def _cap_converted(child: Path) -> dict:
    """Same snapshot after conversion, addressed at `child/.git`."""
    return checkout_snapshot(child / ".git", child)


def _cap_co(co) -> dict:
    return checkout_snapshot(co.gitdir, co.work_tree)


def _manifest_names(parent: Path) -> set[str]:
    data = tomllib.loads((parent / "gf.toml").read_text())
    return {f["name"] for f in data.get("git_folder", [])}


def _gf_tree(root: Path) -> dict:
    """Every path under `<root>/.gf` mapped to ('dir',) / ('link', target)
    / ('file', bytes) — the byte-identical storage snapshot."""
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


def _refs_of(gitdir: Path, work_tree: Path) -> dict:
    r = _git("--git-dir", gitdir, "--work-tree", work_tree,
             "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split(" ", 1)
                for line in r.stdout.splitlines() if " " in line)


def _dirty_child_surface(child: Path) -> str:
    """Arm every protected-work category in the whole-repo child:
    local commit on the branch, a second local branch, a tag, a stash
    entry, a staged file, an unstaged modification, an untracked file,
    an ignored file, and the `refs/worktree/gf-retained` retention ref
    (minted directly — the ref IS gf's retention mechanism; the witness
    only asserts it survives the move)."""
    (child / "local.txt").write_text("local commit\n")
    _child_git(child, "add", "local.txt")
    _child_git(child, "commit", "-qm", "local work")
    local_sha = _out("--git-dir", child / ".gf" / "git",
                     "rev-parse", "HEAD")
    _child_git(child, "branch", "side")
    _child_git(child, "tag", "mytag")
    _child_git(child, "update-ref", "refs/worktree/gf-retained",
               local_sha)
    # stash first so the staged/unstaged partition survives untouched
    (child / "docs" / "api" / "x.txt").write_text("stashed mod\n")
    _child_git(child, "stash", "push", "-u", "-m", "probe stash")
    # armed surface: staged + unstaged + untracked + ignored
    (child / "docs" / "api" / "x.txt").write_text("api on master\n")
    (child / "staged.txt").write_text("staged\n")
    _child_git(child, "add", "staged.txt")
    (child / "docs" / "api" / "x.txt").write_text("unstaged mod\n")
    (child / "dirty.txt").write_text("untracked\n")
    (child / "ignored.txt").write_text("ignored bytes\n")
    return local_sha


def _envelope(r, name: str, path: str, op: str) -> str:
    """Assert the `gf:` refusal envelope names folder name, path, op;
    return combined output for further checks."""
    err = r.stderr + r.stdout
    assert "gf:" in err, err
    assert f"{op} failed for git-folder '{name}'" in err, err
    assert f"({path})" in err, err
    return err


# ---------------------------------------------------------------------------
# completion — whole-repo `gf rm` converts to an ordinary usable repo


def test_rm_whole_repo_converts_to_usable_git(tmp_path):
    """completion · whole-repo: `gf rm` moves `.gf/git` to `.git`; the
    converted child is immediately an ordinary repository with every
    protected-work category intact (spec `gf rm` conversion clause;
    GF-D19): HEAD and its branch, the staged/unstaged index partition,
    local commits reachable through `git log`, the stash list, `origin`
    at the real URL, the `refs/worktree/gf-retained` retention ref,
    every other ref, and dirty/untracked/ignored bytes byte-identical.
    """
    parent, up, child = _clone_lib(tmp_path)
    local_sha = _dirty_child_surface(child)
    before = _cap_child(child)
    assert before["stash"], "setup: no stash entry armed"

    r = gf("-C", str(parent), "rm", "vendor/lib", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    # binding unregistered; gitdir moved, not copied or rebuilt —
    # `.gf` carried only provably gf-owned content, so it is gone
    assert _manifest_names(parent) == set()
    assert not (child / ".gf").exists()
    gitdir = child / ".git"
    assert gitdir.is_dir() and not gitdir.is_symlink()
    assert (gitdir / "HEAD").is_file()

    # provenance usable — plain git against the converted child
    assert _out("-C", child, "symbolic-ref", "HEAD") \
        == "refs/heads/master"
    assert _out("-C", child, "rev-parse", "HEAD") == before["head"]
    assert _out("-C", child, "remote", "get-url", "origin") == str(up)
    assert _git("-C", child, "log", "--oneline").returncode == 0
    assert _out("-C", child, "merge-base", "--is-ancestor",
                local_sha, "HEAD") == ""  # local commit reachable
    # index partition, porcelain, stash — the captured state verbatim
    assert _out("-C", child, "diff", "--cached", "--name-only") \
        == before["staged"].strip()
    assert _out("-C", child, "stash", "list") \
        == before["stash"].strip()
    assert _git("-C", child, "status", "--porcelain"
                ).stdout == before["porcelain"]
    # refs — every ref including refs/worktree/gf-retained survived
    assert _refs_of(gitdir, child) == before["refs"]
    assert before["refs"].get("refs/worktree/gf-retained") == local_sha
    # protected bytes — dirty + untracked + ignored, byte-identical
    assert hash_tree(child) == before["bytes"]


def test_rm_whole_repo_linked_worktree_gitfile_rewritten(tmp_path):
    """completion · whole-repo: a `git worktree add`-linked worktree
    registered in the child's gitdir keeps resolving after the move —
    its `.git` gitfile names the NEW `child/.git/worktrees/<n>` (the
    old `.gf/git` spelling would dangle), and `git status` inside the
    external worktree still works (spec `gf rm` conversion clause;
    GF-D19 `_rewrite_linked_gitfiles`; GF-TRB-13)."""
    parent, _up, child = _clone_lib(tmp_path)
    ext = tmp_path / "ext-wt"
    gf("-C", str(child), "git", "worktree", "add", str(ext), "-b",
       "side")
    (ext / "ext.txt").write_text("worktree dirt\n")
    ext_head = _out("-C", ext, "rev-parse", "HEAD")

    r = gf("-C", str(parent), "rm", "vendor/lib", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    # the gitfile names the post-move record, never the old .gf path
    gitfile = (ext / ".git").read_text()
    assert ".gf/git" not in gitfile, gitfile
    assert "worktrees/" in gitfile
    # the registered worktree resolves against the converted child
    assert _git("-C", ext, "status", check=False).returncode == 0
    assert _out("-C", ext, "rev-parse", "HEAD") == ext_head
    assert _out("-C", ext, "symbolic-ref", "HEAD") == "refs/heads/side"
    assert (ext / "ext.txt").read_text() == "worktree dirt\n"
    listed = _out("-C", child, "worktree", "list", "--porcelain")
    assert f"worktree {ext.resolve()}" in listed


def test_rm_whole_repo_foreign_gf_content_retained(tmp_path):
    """completion · whole-repo: `.gf` carrying foreign content is not
    swept — `gf rm` removes only the provably gf-owned pieces (`git`
    moved, `state` removed), retains and reports the foreign files
    (GF-D19/GF-D22: `.gf` is removed only when left empty)."""
    parent, _up, child = _clone_lib(tmp_path)
    (child / ".gf" / "foreign.txt").write_text("foreign\n")
    (child / ".gf" / "wt" / "keep").mkdir(parents=True)
    (child / ".gf" / "wt" / "keep" / "x.txt").write_text("keep\n")

    r = gf("-C", str(parent), "rm", "vendor/lib", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    # the conversion still happened; the manifest entry is gone
    assert (child / ".git" / "HEAD").is_file()
    assert _manifest_names(parent) == set()
    # foreign content retained byte-identical and reported — the
    # output names the retained location
    assert (child / ".gf" / "foreign.txt").read_text() == "foreign\n"
    assert (child / ".gf" / "wt" / "keep" / "x.txt"
            ).read_text() == "keep\n"
    assert ".gf" in (r.stdout + r.stderr), (
        f"retained .gf content was not reported:\n{r.stdout}\n{r.stderr}")
    # gf-owned bookkeeping was still removed
    assert not (child / ".gf" / "state").exists()
    assert _git("-C", child, "status", check=False).returncode == 0


# ---------------------------------------------------------------------------
# refusal — an unreconcilable linked-worktree record stops the move


def test_rm_whole_repo_unreconcilable_gitfile_refuses_before_move(
        tmp_path):
    """refusal · whole-repo: a registered linked-worktree gitfile that
    cannot be reconciled (the file itself is missing) refuses `gf rm`
    BEFORE the `.gf/git` → `.git` move — the binding stays registered,
    `.gf/git` stays intact, no `.git` appears, and every protected
    byte and ref is untouched (spec `gf rm` "refuses with instructions";
    GF-D19 reconcile pass; GF-TRB-13)."""
    parent, _up, child = _clone_lib(tmp_path)
    local_sha = _dirty_child_surface(child)
    ext = tmp_path / "ext-wt"
    gf("-C", str(child), "git", "worktree", "add", str(ext), "-b",
       "extside")
    # unreconcilable: the external gitfile gf would rewrite is gone
    os.unlink(ext / ".git")
    before = _cap_child(child)

    r = gf("-C", str(parent), "rm", "vendor/lib", check=False)
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    _envelope(r, "lib", "vendor/lib", "rm")

    # refusal is BEFORE the move — the child is still a gf git-folder
    assert (child / ".gf" / "git" / "HEAD").is_file()
    assert not (child / ".git").exists()
    assert _manifest_names(parent) == {"lib"}
    # nothing protected was touched
    assert _cap_child(child) == before
    assert _out("--git-dir", child / ".gf" / "git",
                "rev-parse", "HEAD") == local_sha
    # the external worktree's own files are likewise untouched
    assert (ext / "x.txt").is_file() or (ext / "docs").is_dir()


def test_clone_onto_converted_child_refused_occupied(tmp_path):
    """refusal · whole-repo: after conversion the child is an ordinary
    repository — `gf clone` onto its path stays refused as occupied,
    the `.git` is untouched, and the manifest is unchanged
    (spec occupancy rule; GF-D19 "adopting a `.git` directory on clone
    is unprovable identity — refused")."""
    parent, up, child = _clone_lib(tmp_path)
    _dirty_child_surface(child)
    gf("-C", str(parent), "rm", "vendor/lib")
    assert (child / ".git" / "HEAD").is_file()
    before_bytes = hash_tree(child)
    before_head = _out("-C", child, "rev-parse", "HEAD")

    r = gf("-C", str(parent), "clone", str(up), "vendor/lib",
           check=False)
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    err = _envelope(r, "lib", "vendor/lib", "clone")

    assert (child / ".git" / "HEAD").is_file()
    assert not (child / ".gf").exists() or not (
        child / ".gf" / "git").exists()
    assert _out("-C", child, "rev-parse", "HEAD") == before_head
    assert hash_tree(child) == before_bytes
    assert _manifest_names(parent) == set()
    assert "lib" in err and "vendor/lib" in err


# ---------------------------------------------------------------------------
# completion — subfolder `gf rm` is link-only


def test_rm_subfolder_sibling_keeps_checkout_state_and_pulls(tmp_path):
    """completion · subfolder: removing one of two sibling bindings
    removes only its link and manifest entry — the shared checkout's
    work bytes (uncommitted, untracked, staged), index partition, stash
    entries, refs and per-checkout state record are byte-identical, the
    repo store is untouched, and the sibling still resolves and pulls
    (under `--autostash`, since the retained work makes the shared
    checkout dirty) (spec `gf rm` link-only clause; GF-D19)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    co = _co(parent, up)
    # a stash entry first (it must not swallow the armed surface)
    (co.work_tree / "docs" / "api" / "x.txt").write_text("stashed\n")
    _co_git(co, "stash", "push", "-u", "-m", "probe stash")
    # the removed binding's retained surface inside the shared checkout
    # — armed on paths upstream's advance does NOT touch, so the
    # autostash restore is clean (x.txt/t.txt would legitimately
    # conflict on pop)
    (co.work_tree / "docs" / "api" / "staged.txt").write_text("staged\n")
    _co_git(co, "add", "docs/api/staged.txt")
    (co.work_tree / ".gitignore").write_text("ignored.txt\nlocal.*\n")
    (co.work_tree / "docs" / "api" / "dirty.txt").write_text("dirty\n")
    before = _cap_co(co)
    state_file = co.state
    state_bytes = state_file.read_bytes() if state_file.is_file() \
        else None
    gf_store_tree = _gf_tree(parent)

    r = gf("-C", str(parent), "rm", "vendor/api", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    link = parent / "vendor" / "api"
    assert not os.path.lexists(link)
    assert _manifest_names(parent) == {"tools"}
    # checkout + store + state record: byte-identical
    assert _cap_co(co) == before
    assert _gf_tree(parent) == gf_store_tree
    assert state_file.is_file() \
        and state_file.read_bytes() == state_bytes
    # the sibling resolves and serves
    assert (parent / "vendor" / "tools" / "t.txt").is_file()

    # the sibling still pulls — the retained dirt makes the shared
    # checkout dirty, so the autostash contract applies
    _advance(up, tmp_path)
    r = gf("-C", str(parent), "pull", "--autostash", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled tools" in r.stdout, r.stdout
    assert (parent / "vendor" / "tools" / "t.txt"
            ).read_text() == "tool adv\n"
    # autostash restored the whole retained surface; the probe stash
    # still lists
    assert (co.work_tree / "docs" / "api" / "dirty.txt"
            ).read_text() == "dirty\n"
    assert (co.work_tree / ".gitignore"
            ).read_text() == "ignored.txt\nlocal.*\n"
    assert "staged.txt" in _co_git(
        co, "diff", "--cached", "--name-only").stdout
    stash = _co_git(co, "stash", "list").stdout
    assert "probe stash" in stash


def test_rm_dangling_consumer_link_unregisters_storage_untouched(
        tmp_path):
    """completion · subfolder: a binding whose consumer link dangles
    still unregisters cleanly — the manifest entry goes, the link is
    unlinked, and every store/checkout/record byte under `.gf` is
    untouched (spec `gf rm` "missing linkage is never evidence to touch
    a store, a checkout, or a record")."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    wt_root = parent / ".gf" / "wt"
    rk = next(p for p in wt_root.iterdir() if p.is_dir())
    shutil.rmtree(rk / "master")  # checkout gone; consumer links dangle
    link = parent / "vendor" / "api"
    assert link.is_symlink() and not link.exists()
    storage = _gf_tree(parent)

    r = gf("-C", str(parent), "rm", "vendor/api", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    assert not os.path.lexists(link)
    assert _manifest_names(parent) == {"tools"}
    assert _gf_tree(parent) == storage, ".gf storage changed on rm"


# ---------------------------------------------------------------------------
# completion — reconnection joins the retained checkout


@pytest.mark.parametrize("ref", ["latest", "dev"])
def test_rm_subfolder_reclone_rejoins_same_checkout(tmp_path, ref):
    """completion · reconnect: `gf rm` then `gf clone` of the same
    url+subdir+ref re-links the SAME checkout — uncommitted staged/
    unstaged/untracked work is present in place and visible through the
    restored link; `HEAD` and the index are never reset (spec `gf pull`
    "Joining an existing checkout re-links only"; GF-D18 identity from
    recorded evidence)."""
    up = _upstream(tmp_path)
    if ref == "dev":
        _git("--git-dir", up, "branch", "dev", "master")
    parent = _parent(tmp_path)
    ref_args = ("-b", ref) if ref != "latest" else ()
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api",
       *ref_args)
    co = _co(parent, up, "master" if ref == "latest" else ref)
    if ref == "dev":
        # Keep a default-branch checkout available before taking the snapshot.
        gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/default")
        gf("-C", str(parent), "rm", "vendor/default")
    (co.work_tree / "docs" / "api" / "staged.txt").write_text("staged\n")
    _co_git(co, "add", "docs/api/staged.txt")
    (co.work_tree / "docs" / "api" / "x.txt").write_text("unstaged\n")
    (co.work_tree / "docs" / "api" / "dirty.txt").write_text("dirty\n")
    before = _cap_co(co)

    before_index = _co_git(co, "diff", "--cached", "--binary").stdout
    before_branch = _co_git(co, "symbolic-ref", "HEAD").stdout
    gf("-C", str(parent), "rm", "vendor/api")
    assert _manifest_names(parent) == set()
    if ref == "dev":
        # Same URL alone selects the default checkout, not retained dev work.
        gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
        default_link = parent / "vendor" / "api"
        assert default_link.resolve() == _co(parent, up).work_tree / "docs" / "api"
        assert not (default_link / "dirty.txt").exists()
        assert _cap_co(co) == before
        gf("-C", str(parent), "rm", "vendor/api")
    r = gf("-C", str(parent), "clone", str(up / "docs/api"),
           "vendor/api", *ref_args, check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    link = parent / "vendor" / "api"
    assert link.is_symlink() and link.exists()
    assert link.resolve() == co.work_tree / "docs" / "api"
    # the SAME checkout — every captured category identical, no reset
    after = _cap_co(co)
    assert after["head"] == before["head"]
    assert after["refs"] == before["refs"]
    assert after["staged"] == before["staged"]
    assert after["porcelain"] == before["porcelain"]
    assert after["stash"] == before["stash"]
    assert after["bytes"] == before["bytes"]
    assert _co_git(co, "diff", "--cached", "--binary").stdout == before_index
    assert _co_git(co, "symbolic-ref", "HEAD").stdout == before_branch
    # the retained work is served through the consumer link again
    assert (link / "dirty.txt").read_text() == "dirty\n"
    assert (link / "x.txt").read_text() == "unstaged\n"
    assert _manifest_names(parent) == {"api"}


@pytest.mark.parametrize("ignore_consumer", [False, True])
def test_targeted_clean_dry_run_keeps_both_binding_forms(tmp_path, ignore_consumer):
    """Cleanup is scoped outside stores and consumers, even when a whole
    child is not ignored. Excluding only root .gf is an unsafe control.
    All cleanup commands are dry runs confined to the fixture.
    """
    parent, up, child = _clone_lib(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    co = _co(parent, up)
    (child / "local-only.txt").write_text("whole local work\n")
    (child / "ignored.txt").write_text("whole ignored work\n")
    (parent / "vendor/api/ignored.txt").write_text("subfolder ignored work\n")
    if ignore_consumer:
        (parent / ".gitignore").write_text(".gf/\nvendor/lib/\n")
    scratch = parent / "scratch"
    scratch.mkdir()
    (scratch / "generated.txt").write_text("disposable output\n")
    before_storage = _gf_tree(parent)
    before_child, before_co = _cap_child(child), _cap_co(co)

    unsafe = git_out("-C", parent, "clean", "-ndx", "-e", ".gf/",
                     "--", "vendor/lib/")
    assert "Would remove vendor/lib/" in unsafe, unsafe
    ancestor = git_out("-C", parent, "clean", "-ndx", "-e", ".gf/", "--", "vendor/")
    assert "Would remove vendor/" in ancestor, ancestor
    non_ignored_only = git_out("-C", parent, "clean", "-nd", "--", "vendor/lib/")
    assert bool(non_ignored_only) is not ignore_consumer

    safe = git_out("-C", parent, "clean", "-ndx", "--", "scratch/")
    assert safe.splitlines() == ["Would remove scratch/"]
    assert (scratch / "generated.txt").is_file()
    assert _gf_tree(parent) == before_storage
    assert _cap_child(child) == before_child
    assert _cap_co(co) == before_co


def test_rm_subfolder_reclone_other_subdir_widens_cone(tmp_path):
    """completion · reconnect: re-adding a different subdir of the same
    repo+ref joins the same checkout and widens its sparse cone to
    materialize the new binding — the retained subdir's work survives
    physically in the checkout (spec `gf pull` "the sparse cone widens
    to materialize the new binding's subdir")."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    co = _co(parent, up, subdir="docs/api")
    (co.work_tree / "docs" / "api" / "dirty.txt").write_text("dirty\n")
    (co.work_tree / "docs" / "api" / "x.txt").write_text("unstaged\n")
    before = _cap_co(co)
    assert not (co.work_tree / "tools").exists(), (
        "setup: tools already materialized")

    gf("-C", str(parent), "rm", "vendor/api")
    r = gf("-C", str(parent), "clone", str(up / "tools"),
           "vendor/api", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    # same checkout: HEAD/index/refs/porcelain/stash identical —
    # only the cone widened (tracked tools bytes materialize)
    after = _cap_co(co)
    for key in ("head", "refs", "staged", "porcelain", "stash"):
        assert after[key] == before[key], key
    assert (co.work_tree / "tools" / "t.txt").is_file()
    assert (co.work_tree / "docs" / "api" / "dirty.txt"
            ).read_text() == "dirty\n"
    # the link serves the NEW subdir
    link = parent / "vendor" / "api"
    assert (link / "t.txt").read_text() == "tool on master\n"


def test_clone_ambiguous_checkout_identity_refuses(tmp_path):
    """refusal · reconnect: a checkout directory at the expected path
    WITHOUT its worktree record is ambiguous identity — the re-add
    refuses rather than rebuilding over retained work: the directory,
    its uncommitted bytes, and the state record survive, no link is
    created (spec update algorithm "a checkout directory without its
    worktree record stops this checkout with an error"; GF-D18)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    co = _co(parent, up)
    (co.work_tree / "docs" / "api" / "dirty.txt").write_text("dirty\n")
    state_bytes = co.state.read_bytes()
    gf("-C", str(parent), "rm", "vendor/api")
    # sever the record — the checkout dir stands without it
    shutil.rmtree(co.gitdir)
    before = _cap_co(co)  # work-tree bytes still observable

    r = gf("-C", str(parent), "clone", str(up / "docs/api"),
           "vendor/api", check=False)
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    err = r.stderr + r.stdout
    assert "gf:" in err, err

    # nothing rebuilt over retained work; nothing linked
    assert not os.path.lexists(parent / "vendor" / "api")
    assert _cap_co(co)["bytes"] == before["bytes"]
    assert (co.work_tree / "docs" / "api" / "dirty.txt"
            ).read_text() == "dirty\n"
    assert co.state.is_file() and co.state.read_bytes() == state_bytes
    assert not (co.common_dir / "worktrees" / "master").exists(), (
        "a new worktree record was built over the ambiguous dir")
    assert _manifest_names(parent) == set()


# ---------------------------------------------------------------------------
# cleanup ownership — failure removes only provably-created paths


def test_clone_failure_keeps_preexisting_files(tmp_path):
    """recovery · cleanup ownership (GF-D22): a failed `gf clone`
    removes only what this invocation provably created — a pre-existing
    directory stays, foreign files beside the failing operation
    survive, no manifest entry is left."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)

    # whole-repo arm: pre-existing empty child dir + failing ref
    (parent / "vendor").mkdir()
    (parent / "vendor" / "pre").mkdir()
    r = gf("-C", str(parent), "clone", str(up), "vendor/pre",
           "-b", "nosuch", check=False)
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert (parent / "vendor" / "pre").is_dir(), (
        "pre-existing directory removed by clone cleanup")

    # subfolder arm: foreign file beside a failing clone survives
    (parent / "newdir").mkdir()
    (parent / "newdir" / "foreign.txt").write_text("foreign\n")
    r = gf("-C", str(parent), "clone", str(up / "docs/api"),
           "newdir/api", "-b", "nosuch", check=False)
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert (parent / "newdir" / "foreign.txt"
            ).read_text() == "foreign\n"
    assert not os.path.lexists(parent / "newdir" / "api")

    assert _manifest_names(parent) == set()
