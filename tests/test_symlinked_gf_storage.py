# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Symlinked `.gf` storage regression pins (hardening loop, R14-F2).

Defect: a `.gf`-rooted storage path — `<root>/.gf` itself or a `.gf`-rooted
component (`repos`, `wt`, a repo-key or checkout-key dir) — could be a
symlink, committable in a checked-out tree or hand-placed. Pre-fix,
`gf clone <up>/<subdir> path` through `parent/.gf -> /outside` wrote the
repo store (`git init --bare`), the linked checkout (`worktree add`), the
state file, and the consumer link through the link into `/outside`, and
the failure teardowns `_remove_repo_store`/`_remove_checkout` would
`shutil.rmtree` through the link — deleting foreign content the spelling
only *seemed* to name.

Fix (this changeset): `layout.storage_is_real` (spelling == realpath)
guards every `.gf`-rooted write site — `ensure_repo_store`,
`ensure_checkout`, `_init_child_gitdir`, `init_git_folder`,
`pull_shared_bindings` — refusing `gf: ... resolves through a symlink`;
the two teardowns skip the recursive remove when the spelling is
non-real, leaving foreign content untouched. The predicate is
write-side only by design: `resolve_checkout`/`in_gf_wt` still resolve
through the link so `gf ls`/`gf status` keep working under a hostile
`.gf`. For the whole-repo child the check is the two-sided form
`realpath(gitdir) == realpath(child)/.gf/"git"` — the child's own leaf
may legitimately be a symlink.

R15-F4 follow-on (established-child swap): the resolve-to-self
predicate originally ran only at creation-time write sites, so a
whole-repo child whose `.gf` was swapped for a symlink AFTER the
binding was established — `kid/.gf -> donor/.gf` — still operated
through the link everywhere else: `update_child`'s dirty check,
origin write, fetch and checkout ran against the donor's gitdir,
`remove_child`'s `shutil.move` carried the donor's gitdir to
`kid/.git` before the rmtree died on the link itself, and the
`gf git`/`sh`/`diff`/`log` passthroughs ran with GIT_DIR pointed into
the donor's storage — while `ls`/`status`/`drift` answered with the
donor's HEAD/branch/porcelain as if they were the child's. The new
predicate `layout.whole_repo_gitdir_is_real` (the same two-sided
compare `_init_child_gitdir` used) now gates every operation on an
established whole-repo child: `update_child` refuses ahead of the
dirty check, `remove_child` ahead of the move, `_passthrough_target`
fails closed, and the read paths degrade to `missing`/`?`/empty
rather than answer through the swap.

Every test drives the real `gf` CLI over local bare upstreams (no
network, fixture-isolated). Setup/precondition failures stop through
``pytest.fail``; claim assertions raise ``AssertionError``.

Expected discrimination against the pre-fix code: the clone/pull
write-through arms fail by landing storage in the outside dir (or
exiting 0), the init arm fails on the missing "resolves through a
symlink" wording, the teardown arms fail by deleting foreign sentinel
files through the link, and the R15-F4 swap arms fail by operating on
the donor — `rm` relocates the donor's gitdir into `kid/.git`, `pull`
fetches/checks out through the link, `gf git` prints the donor's sha,
and ls/status render the donor's identity on the kid row. Controls
(occupancy refusal on an occupied child, the leaf-symlink child, real
`.gf` storage, `foo.gf`/`x.git` lookalike names, ls/status reads, the
`.gf`-points-outside-parent pull, and an ordinary established-child
pull/rm) are unchanged pre-fix — they pin the guard's precision and
the read-side contract.
"""

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from conftest import gf, git, push_commit
from gf import layout, shelf
from gf.backends import GitCliBackend


# ---------------------------------------------------------------------------
# helpers — setup failures use pytest.fail, never AssertionError

API = {"docs/api/reference.md": "api v1", "docs/guide/intro.md": "guide v1"}


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        pytest.fail(
            f"setup: git {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stderr}")
    return r


def _setup_gf(*args) -> subprocess.CompletedProcess:
    r = gf(*args, check=False)
    if r.returncode != 0:
        pytest.fail(
            f"setup: gf {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stdout}\n{r.stderr}")
    return r


def _parent(tmp_path: Path, name: str = "parent") -> Path:
    parent = tmp_path / name
    parent.mkdir(parents=True, exist_ok=True)
    git("init", "-q", str(parent), cwd=parent.parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-qm", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """Bare upstream with one commit on master holding `files`."""
    up = tmp_path / name
    _git("init", "-q", "--bare", up)
    work = tmp_path / f"_seed_{name}"
    _git("clone", "-q", up, work)
    for rel, text in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")
    return up


def _manifest_folders(parent: Path) -> list[dict]:
    """The `git_folder` entries in `parent`'s manifest ([] when absent)."""
    mf = parent / "gf.toml"
    if not mf.is_file():
        return []
    data = tomllib.loads(mf.read_text())
    return data.get("git_folder", [])


def _tree(root: Path) -> list[str]:
    """`root`'s full relative entry list (dirs and files, sorted)."""
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*"))


def _single_glob(root: Path, pattern: str, what: str) -> Path:
    hits = sorted(root.glob(pattern))
    if len(hits) != 1:
        pytest.fail(f"setup: expected exactly one {what}, found {hits}")
    return hits[0]


def _checkout_key_dir(gf_dir: Path, key: str) -> str:
    """The single checkout dir name under `.gf/wt/<key>` (a directory —
    the sibling `.<key>.state` file is not a candidate)."""
    hits = sorted(p for p in (gf_dir / "wt" / key).iterdir() if p.is_dir())
    if len(hits) != 1:
        pytest.fail(
            f"setup: expected exactly one checkout dir, found {hits}")
    return hits[0].name


def _repo_key_dir(gf_dir: Path) -> str:
    """The single `<repo-key>` dir name under a seeded `.gf/repos`."""
    return _single_glob(gf_dir / "repos", "*", "repo-key dir").name


def _assert_clean_refusal(r: subprocess.CompletedProcess) -> None:
    """The shared refusal envelope: non-zero, `gf:` error, no traceback."""
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    assert re.search(r"^gf: ", r.stderr, re.M), (r.stdout, r.stderr)


# ---------------------------------------------------------------------------
# Arm 1 — clone a subfolder binding through a symlinked `parent/.gf`


def test_clone_subfolder_through_symlinked_dot_gf_writes_nothing(tmp_path):
    """`gf clone <up>/docs/api api` with `parent/.gf` a symlink to an
    outside dir must refuse before the store exists: non-zero exit, a
    clean `gf:` error naming "resolves through a symlink", the outside
    dir left EMPTY (no `repos`/`wt` written through the link), and no
    manifest binding recorded.

    Pre-fix signature (observed): `git init --bare`, the fetch and
    `worktree add` all wrote through `parent/.gf` into the outside dir;
    the clone then died late at the worktree-record check ("git worktree
    add did not create the expected worktree record ...") and the
    failure teardown rmtree'd the foreign store and checkout dirs back
    out through the link — `outside` was left holding `repos/`/`wt/<key>`
    skeletons where foreign content would have been destroyed.
    """
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, parent / ".gf")

    r = gf("-C", str(parent), "clone", f"{up}/docs/api", "api",
           check=False)

    _assert_clean_refusal(r)
    assert "resolves through a symlink" in r.stderr, r.stderr
    assert list(outside.iterdir()) == [], _tree(outside)
    # The consumer path was never created and no binding was recorded:
    # the manifest (materialized empty by cmd_clone) has no entries.
    assert not os.path.lexists(parent / "api"), "consumer link was placed"
    assert _manifest_folders(parent) == [], \
        (parent / "gf.toml").read_text()


# ---------------------------------------------------------------------------
# Arm 2 — `gf pull` must refuse before fetching/writing through the link


def test_pull_with_dot_gf_resolving_outside_parent_refuses(tmp_path):
    """`.gf` -> an outside dir makes every consumer link resolve outside
    the parent, so `gf pull` dies at the pre-existing consumer-path
    containment gate before any store write — a clean `gf:` error, the
    outside dir untouched. (This refusal precedes `pull_shared_bindings`;
    it is pinned as required behavior, not as the new guard — the
    containment check predates the fix.)
    """
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "api")
    manifest_before = (parent / "gf.toml").read_bytes()

    (parent / ".gf").rename(parent / ".gf-real")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, parent / ".gf")

    r = gf("-C", str(parent), "pull", check=False)

    _assert_clean_refusal(r)
    assert "outside" in r.stderr, r.stderr
    assert list(outside.iterdir()) == [], _tree(outside)
    assert (parent / "gf.toml").read_bytes() == manifest_before


def test_pull_through_in_parent_dot_gf_link_refuses_at_store(tmp_path):
    """`.gf` -> `parent/gf-real` keeps consumer paths resolving inside the
    parent, so pull reaches `pull_shared_bindings` — where the non-real
    repo-store spelling must refuse BEFORE any `git init`/fetch/state
    write lands through the link. `gf-real` stays EMPTY and the real
    storage (moved aside) is untouched; the manifest is unchanged.

    Pre-fix signature (observed): the pull created
    `gf-real/repos/<key>/git`, fetched into it, and `worktree add`ed
    `gf-real/wt/<key>/<branch>` — all through the `.gf` link — then died
    late at the worktree-record check ("git worktree add did not create
    the expected worktree record ...") and the teardown rmtree'd the
    foreign store back out through the link.
    """
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "api")
    manifest_before = (parent / "gf.toml").read_bytes()
    real_gf = sorted(p.relative_to(parent / ".gf").as_posix()
                     for p in (parent / ".gf").rglob("*"))

    (parent / ".gf").rename(parent / ".gf-away")
    decoy = parent / "gf-real"
    decoy.mkdir()
    os.symlink(decoy, parent / ".gf")

    r = gf("-C", str(parent), "pull", check=False)

    _assert_clean_refusal(r)
    assert "resolves through a symlink" in r.stderr, r.stderr
    # Nothing was fetched or written through the link: the decoy dir is
    # still empty and the real storage tree is unchanged.
    assert list(decoy.iterdir()) == [], _tree(decoy)
    assert sorted(
        p.relative_to(parent / ".gf-away").as_posix()
        for p in (parent / ".gf-away").rglob("*")) == real_gf
    assert (parent / "gf.toml").read_bytes() == manifest_before


def test_pull_through_symlinked_repos_component_refuses(tmp_path):
    """A symlink at the `.gf/repos` component (`.gf` itself real) makes the
    repo-store spelling non-real while consumer links still resolve
    inside the parent. `gf pull` must refuse at `pull_shared_bindings`'
    storage gate before the coverage loop creates the store or fetches
    through the link: outside stays EMPTY.

    Pre-fix signature: `ensure_repo_store`'s create arm ran `git init
    --bare` and fetched into `outside/<key>/git`, then `ensure_checkout`
    died on the real checkout's missing record under the foreign store —
    the pull failed late, after writing the store through the link.
    """
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "api")
    manifest_before = (parent / "gf.toml").read_bytes()

    (parent / ".gf" / "repos").rename(parent / ".gf" / "repos-away")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, parent / ".gf" / "repos")

    r = gf("-C", str(parent), "pull", check=False)

    _assert_clean_refusal(r)
    assert "resolves through a symlink" in r.stderr, r.stderr
    assert list(outside.iterdir()) == [], _tree(outside)
    assert (parent / "gf.toml").read_bytes() == manifest_before
    # The pre-existing checkout and consumer link are untouched.
    assert (parent / "api").is_symlink()
    assert (parent / ".gf" / "wt").is_dir()


def test_pull_through_symlinked_wt_component_refuses_checkout(tmp_path):
    """A symlink at the `.gf/wt` component (store still real) makes the
    checkout work-tree/record spellings non-real: `ensure_checkout` must
    refuse with "checkout storage path ... resolves through a symlink"
    before `wt.mkdir`/`worktree add`/state writes land through the link.
    The link target stays EMPTY.

    Pre-fix signature (observed): the record-valid arm of
    `ensure_checkout` mkdir'd `wt-real/<key>/<branch>` through the link
    and ran `sparse-checkout` into it — writes landed outside the real
    `.gf` — before the pull died at the dirty check ("checkout ... is
    dirty; commit or stash before pulling") on the materialized foreign
    tree.
    """
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "api")
    manifest_before = (parent / "gf.toml").read_bytes()

    (parent / ".gf" / "wt").rename(parent / ".gf" / "wt-away")
    decoy = parent / "wt-real"          # inside the parent: containment passes
    decoy.mkdir()
    os.symlink(decoy, parent / ".gf" / "wt")

    r = gf("-C", str(parent), "pull", check=False)

    _assert_clean_refusal(r)
    assert "resolves through a symlink" in r.stderr, r.stderr
    assert list(decoy.iterdir()) == [], _tree(decoy)
    assert (parent / "gf.toml").read_bytes() == manifest_before


# ---------------------------------------------------------------------------
# Arm 3 — whole-repo child gitdir under a linked `child/.gf`
#
# `gf clone`/`gf pull` never reach `_init_child_gitdir`'s check for this
# shape: any `child/.gf` entry (link or dir) is occupancy — "already
# exists and is not empty" / "exists and is not a git-folder" — refused
# upstream of the gitdir mkdir, identically before and after the fix.
# The only CLI route to the whole-repo storage check is `gf init`, whose
# occupancy gate `(target/".gf").exists()` does not see a *dangling*
# `.gf` link — the symlink check then owns the refusal.


def test_init_refuses_when_child_dot_gf_is_a_dangling_symlink(tmp_path):
    """`gf init kid` onto a child holding only `.gf -> <missing>` must
    refuse in `init_git_folder` with "child gitdir path ... resolves
    through a symlink", leaving the link as found and recording no
    manifest entry.

    Pre-fix signature: `gitdir.mkdir(parents=True)` raised
    FileExistsError on the link itself, escaping to the `gf:` OSError
    envelope (`[Errno 17] File exists`) — rc 1 without the refusal
    wording.
    """
    parent = _parent(tmp_path)
    child = parent / "kid"
    child.mkdir()
    missing = tmp_path / "nowhere"
    os.symlink(missing, child / ".gf")

    r = gf("-C", str(parent), "init", "kid", check=False)

    _assert_clean_refusal(r)
    assert "resolves through a symlink" in r.stderr, r.stderr
    # Nothing was created: the child holds only the planted link and the
    # link's (missing) target was never made.
    assert sorted(p.name for p in child.iterdir()) == [".gf"]
    assert (child / ".gf").is_symlink()
    assert os.readlink(child / ".gf") == str(missing)
    assert not missing.exists()
    assert _manifest_folders(parent) == []


def test_clone_whole_repo_with_dot_gf_symlink_fails_as_occupied(tmp_path):
    """The packet's literal shape: `gf clone <up> kid` where `kid/.gf` is
    a symlink to an existing dir. The refusal is the occupancy gate's —
    "child path ... already exists and is not empty" — not the symlink
    check, because `init_child` refuses non-empty children before
    `_init_child_gitdir` runs. Identical pre- and post-fix; pinned so the
    surface stays a clean `gf:` refusal, never a traceback, and nothing
    is written through the link.
    """
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    child = parent / "kid"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, child / ".gf")

    r = gf("-C", str(parent), "clone", str(up), "kid", check=False)

    _assert_clean_refusal(r)
    assert "already exists and is not empty" in r.stderr, r.stderr
    assert list(outside.iterdir()) == [], _tree(outside)
    assert (child / ".gf").is_symlink()
    assert _manifest_folders(parent) == []


def test_clone_whole_repo_child_reached_through_leaf_symlink(tmp_path):
    """Control for the two-sided compare in `_init_child_gitdir`: the
    child's own leaf may legitimately be a symlink — `realpath(gitdir)`
    is compared against `realpath(child)/.gf/"git"`, never the lexical
    child — so `gf clone <up> kid` with `kid -> real/` inside the parent
    still binds normally. Pre-fix identical (the check is additive).
    """
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    real = parent / "real"
    real.mkdir()
    os.symlink("real", parent / "kid")

    r = gf("-C", str(parent), "clone", str(up), "kid", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (parent / "kid").is_symlink()
    assert (real / ".gf" / "git" / "HEAD").is_file()
    assert (real / "docs" / "api" / "reference.md").read_text() == "api v1"
    entry = [f for f in _manifest_folders(parent) if f["name"] == "kid"]
    assert len(entry) == 1 and entry[0]["path"] == "kid"


# ---------------------------------------------------------------------------
# Arm 4 — teardown must not rmtree through a non-real storage spelling


def test_clone_rollback_leaves_foreign_content_under_dot_gf_link(tmp_path):
    """`.gf` swapped for a symlink AFTER a successful binding, then a
    failing clone: the rollback `_remove_repo_store` must skip the
    non-real store spelling instead of rmtree'ing the foreign dir it
    resolves to. Post-fix the clone refuses at `ensure_repo_store` and
    every seeded foreign file survives; the link's tree is unchanged.

    Fixture: `.gf -> parent/gf-real` (inside the parent, so consumer
    paths still resolve under the anchor) holding a record-less foreign
    `<repo-key>/git` dir and a foreign `<checkout-key>` worktree dir.

    Pre-fix signature: `ensure_repo_store` populated the foreign store
    through the link (`git init --bare` + fetch), `ensure_checkout` then
    refused the occupied foreign worktree dir, and the store-phase
    teardown `shutil.rmtree(store)` deleted `gf-real/repos/<key>/git` —
    including the seeded foreign file.
    """
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "api")
    manifest_before = (parent / "gf.toml").read_bytes()
    key = _repo_key_dir(parent / ".gf")
    ck = _checkout_key_dir(parent / ".gf", key)

    (parent / ".gf").rename(parent / ".gf-away")
    decoy = parent / "gf-real"
    (decoy / "repos" / key / "git").mkdir(parents=True)
    (decoy / "repos" / key / "git" / "foreign.txt").write_text("foreign")
    (decoy / "wt" / key / ck).mkdir(parents=True)
    (decoy / "wt" / key / ck / "foreign-wt.txt").write_text("foreign")
    seeded = _tree(decoy)
    os.symlink(decoy, parent / ".gf")

    r = gf("-C", str(parent), "clone", f"{up}/docs/guide", "guide",
           check=False)

    _assert_clean_refusal(r)
    assert "resolves through a symlink" in r.stderr, r.stderr
    # Rollback skipped the non-real spelling: the seeded foreign tree is
    # byte-for-byte what was planted — nothing rmtree'd through the link,
    # nothing written through it either.
    assert _tree(decoy) == seeded
    assert (decoy / "repos" / key / "git" / "foreign.txt").is_file()
    assert (decoy / "wt" / key / ck / "foreign-wt.txt").is_file()
    assert not os.path.lexists(parent / "guide")
    assert (parent / "gf.toml").read_bytes() == manifest_before


def test_teardown_helpers_skip_nonreal_storage_spellings(tmp_path):
    """Unit-level pin for the two teardown skips, which deterministic CLI
    flows cannot reach post-fix (every path to `_remove_checkout`
    requires `ensure_checkout` to have succeeded at a real spelling;
    `_remove_repo_store`'s skip is exercised end-to-end by the rollback
    pin above). With `.gf` a symlink, both must return without deleting:
    the foreign checkout dir and the foreign store dir keep their
    planted files.

    Pre-fix signature: `_remove_checkout` fell through `worktree
    remove` (ignored failure) into `shutil.rmtree(co.work_tree)`, and
    `_remove_repo_store` ran `shutil.rmtree(store)` — each deleting the
    foreign sentinel through the `.gf` link.
    """
    parent = _parent(tmp_path)
    rk = layout.repo_key("https://github.com/foo/libfoo")
    outside = tmp_path / "outside"
    wt = outside / "wt" / rk / "master"
    wt.mkdir(parents=True)
    (wt / "foreign.txt").write_text("foreign checkout")
    store_dir = outside / "repos" / rk / "git"
    store_dir.mkdir(parents=True)
    (store_dir / "foreign.txt").write_text("foreign store")
    os.symlink(outside, parent / ".gf")

    co = layout.subfolder_checkout(parent, "https://github.com/foo/libfoo",
                                   "master", "docs/api")
    store = layout.repo_store(parent, "https://github.com/foo/libfoo")

    shelf._remove_checkout(co, GitCliBackend())
    shelf._remove_repo_store(store)

    assert (wt / "foreign.txt").is_file()
    assert (store_dir / "foreign.txt").is_file()
    assert _tree(outside) == [
        "repos", f"repos/{rk}", f"repos/{rk}/git",
        f"repos/{rk}/git/foreign.txt",
        "wt", f"wt/{rk}", f"wt/{rk}/master",
        f"wt/{rk}/master/foreign.txt",
    ]


# ---------------------------------------------------------------------------
# Arm 5 — controls


def test_clone_and_pull_on_real_dot_gf_stay_green(tmp_path):
    """Control: ordinary clone/pull on a real `.gf` dir — the storage
    predicate never fires on real spellings."""
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "api")

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (parent / ".gf" / "repos").is_dir()
    assert (parent / ".gf" / "wt").is_dir()
    assert (parent / "api" / "reference.md").read_text() == "api v1"


def test_lookalike_gf_and_git_names_do_not_trip_the_check(tmp_path):
    """Control: the refusal keys on realpath-vs-spelling of `.gf`-rooted
    paths, not on names — a `foo.gf`-named dir in the parent path and a
    `x.git`-named upstream dir neither trip it nor confuse resolution."""
    outer = tmp_path / "foo.gf"
    parent = _parent(outer, "parent")
    up = tmp_path / "x.git"
    _git("init", "-q", "--bare", up)
    work = tmp_path / "_seed_x"
    _git("clone", "-q", up, work)
    (work / "f.txt").write_text("x")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")

    r = gf("-C", str(parent), "clone", str(up), "api", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (parent / "api" / ".gf" / "git" / "HEAD").is_file()
    assert (parent / "api" / "f.txt").read_text() == "x"


def test_ls_and_status_read_through_symlinked_dot_gf(tmp_path):
    """Read-side control: `storage_is_real` is a write-only predicate —
    `resolve_checkout` keeps resolving through the link, so `gf ls` and
    `gf status` still answer under a hostile `.gf` instead of dying.
    The binding's checkout resolves into the outside dir, sees no gitdir,
    and renders the pre-clone fallbacks — `[]` branch and `?` HEAD on
    `ls`, `missing` on `status --remote`. No traceback.
    """
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "api")

    (parent / ".gf").rename(parent / ".gf-away")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, parent / ".gf")

    url = f"{up}/docs/api"
    r = gf("-C", str(parent), "ls", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert re.search(
        rf"^api\s+{re.escape(url)}\s+\[\]\s+\?\*?$", r.stdout, re.M), \
        r.stdout

    r = gf("-C", str(parent), "status", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert re.search(
        rf"^api\s+{re.escape(url)}\s+\[\]$", r.stdout, re.M), r.stdout

    r = gf("-C", str(parent), "status", "--remote", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert re.search(
        rf"^api\s+{re.escape(url)}\s+\[\]\s+missing$", r.stdout, re.M), \
        r.stdout


# ---------------------------------------------------------------------------
# Arm 6 — `.gf` swapped for a symlink on an ESTABLISHED whole-repo child
# (R15-F4). `kid/.gf -> donor/.gf` makes the spelled `kid/.gf/git` resolve
# into the sibling's storage: `whole_repo_gitdir_is_real`'s two-sided
# compare fails, so writes refuse and reads degrade. The swap moves the
# child's own `.gf` wholly OUTSIDE its worktree — a `.gf`-sibling left
# inside `kid` would be untracked content the through-the-link status
# would report, changing the shape under test.


def _whole_repo_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Two whole-repo children cloned from one upstream — `kid` and
    `donor` — inside one parent; returns (parent, kid, donor). Same
    upstream keeps kid's worktree byte-identical to the donor's index,
    so a through-the-link `status --porcelain` reports clean and a
    pre-fix pull proceeds past the dirty check into the donor."""
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up), "kid")
    _setup_gf("-C", str(parent), "clone", str(up), "donor")
    return parent, parent / "kid", parent / "donor"


def _swap_dot_gf(tmp_path: Path, kid: Path, donor: Path) -> Path:
    """Replace `kid/.gf` with a symlink to `donor/.gf`, moving the
    child's real `.gf` aside to `tmp_path` (outside the worktree).
    Returns the moved-aside dir."""
    away = tmp_path / f"{kid.name}-gf-away"
    (kid / ".gf").rename(away)
    os.symlink(donor / ".gf", kid / ".gf")
    return away


def _rev_parse(gitdir: Path, *args: str) -> str:
    return _git(f"--git-dir={gitdir}", "rev-parse", *args).stdout.strip()


def test_rm_with_swapped_dot_gf_refuses_without_moving_donor_gitdir(
    tmp_path,
):
    """`gf rm kid` with `kid/.gf` swapped to `donor/.gf` must refuse in
    `remove_child` BEFORE the gitdir move: a clean `gf:` refusal naming
    the `.gf` resolution, the donor's gitdir still in place and
    untouched, `kid/.git` never created, the `kid` binding still in the
    manifest (the die precedes `write_manifest`), and the link left as
    planted.

    Pre-fix signature (observed): `git_dir.is_dir()` resolved through
    the link and `shutil.move(kid/.gf/git -> kid/.git)` RELOCATED the
    donor's gitdir into `kid/.git` — the donor's `.gf/git` was gone —
    and only the follow-up `shutil.rmtree(kid/.gf)` failed: rc 1 as
    `cannot remove <kid>/.gf: [Errno None] None: PosixPath(...)`, the
    wrong error for damage already done.
    """
    parent, kid, donor = _whole_repo_pair(tmp_path)
    manifest_before = (parent / "gf.toml").read_bytes()
    donor_gitdir = donor / ".gf" / "git"
    donor_head = _rev_parse(donor_gitdir, "HEAD")
    donor_state = (donor / ".gf" / "state").read_bytes()
    away = _swap_dot_gf(tmp_path, kid, donor)

    r = gf("-C", str(parent), "rm", "kid", check=False)

    _assert_clean_refusal(r)
    assert "resolves through a symlink" in r.stderr, r.stderr
    assert "'kid'" in r.stderr and "(kid)" in r.stderr, r.stderr
    # The donor's gitdir was never carried across the link.
    assert (donor_gitdir / "HEAD").is_file()
    assert _rev_parse(donor_gitdir, "HEAD") == donor_head
    assert (donor / ".gf" / "state").read_bytes() == donor_state
    # No move landed and the swap stands as planted.
    assert not os.path.lexists(kid / ".git")
    assert (kid / ".gf").is_symlink()
    assert os.readlink(kid / ".gf") == str(donor / ".gf")
    assert (away / "git" / "HEAD").is_file()
    # The refusal precedes the manifest rewrite: kid is still bound.
    assert (parent / "gf.toml").read_bytes() == manifest_before


def test_pull_with_swapped_dot_gf_refuses_before_touching_donor(
    tmp_path,
):
    """`gf pull kid` on the swap must refuse in `update_child` BEFORE
    the dirty check, so nothing — not the status, the origin write, the
    fetch, the checkout, or the state record — reaches the donor's
    gitdir. With the upstream advanced past both clones, a through-
    the-link pull is visible: post-fix the donor's HEAD and `.gf/state`
    bytes are unchanged and kid's worktree never receives the new
    commit.

    Pre-fix signature: `git status` on the donor's index vs kid's
    identical worktree reported clean, then the fetch advanced the
    DONOR's refs/HEAD to the new upstream commit while the checkout
    wrote the new files into kid's worktree, and `save_checkout`
    rewrote `kid/.gf/state` — i.e. the donor's record — through the
    link: rc 0 "Pulled kid", donor HEAD moved, `kid/b.txt` present.
    """
    parent, kid, donor = _whole_repo_pair(tmp_path)
    manifest_before = (parent / "gf.toml").read_bytes()
    donor_gitdir = donor / ".gf" / "git"
    donor_head = _rev_parse(donor_gitdir, "HEAD")
    donor_state = (donor / ".gf" / "state").read_bytes()
    _swap_dot_gf(tmp_path, kid, donor)
    # The upstream advances after the swap: a through-the-link pull
    # moves the donor's refs and lands new files in kid's worktree.
    push_commit(tmp_path / "upstream", "update", "update")

    r = gf("-C", str(parent), "pull", "kid", check=False)

    _assert_clean_refusal(r)
    assert "resolves through a symlink" in r.stderr, r.stderr
    # The donor never observed the pull: same HEAD, same recorded state.
    assert _rev_parse(donor_gitdir, "HEAD") == donor_head
    assert (donor / ".gf" / "state").read_bytes() == donor_state
    # The new upstream commit never landed in kid's worktree either.
    assert not (kid / "a.txt").exists()
    assert (parent / "gf.toml").read_bytes() == manifest_before


def test_pull_with_swapped_dot_gf_refuses_ahead_of_dirty_check(
    tmp_path,
):
    """Ordering pin: the swap refusal precedes the dirty check, so a
    dirty kid still gets the `.gf`-resolution refusal — never the
    DirtyError a through-the-link status would raise. (Separates "gate
    added" from "gate added after the dirty check".)

    Pre-fix signature: rc 3 — `child <kid> is dirty; commit or stash
    before pulling`, raised on the donor's index vs kid's modified
    worktree.
    """
    parent, kid, donor = _whole_repo_pair(tmp_path)
    _swap_dot_gf(tmp_path, kid, donor)
    (kid / "docs" / "api" / "reference.md").write_text("dirty")

    r = gf("-C", str(parent), "pull", "kid", check=False)

    _assert_clean_refusal(r)
    assert "resolves through a symlink" in r.stderr, r.stderr
    assert "is dirty" not in r.stderr, r.stderr


@pytest.mark.parametrize("op_args", [
    ("git", "rev-parse", "HEAD"),
    ("sh", "-c", "true"),
    ("diff", "--name-only"),
    ("log", "--format=%s"),
])
def test_passthrough_ops_refuse_when_dot_gf_resolves_to_donor(
    tmp_path, op_args,
):
    """`gf git`/`sh`/`diff`/`log` inside a swapped child share
    `_passthrough_target`'s fail-closed gate: each refuses with a clean
    `gf:` error naming the `.gf` resolution and produces NO output —
    the subprocess would otherwise run with GIT_DIR inside the donor's
    storage.

    Pre-fix signature: every op reached `_run_child_command` with
    GIT_DIR resolved through the link — `git rev-parse HEAD` printed
    the DONOR's sha (rc 0), `sh -c true` exited 0, `log` printed the
    donor's history, `diff` answered rc 0 on the donor's index.
    """
    parent, kid, donor = _whole_repo_pair(tmp_path)
    donor_head = _rev_parse(donor / ".gf" / "git", "HEAD")
    _swap_dot_gf(tmp_path, kid, donor)

    r = gf("-C", str(kid), *op_args, check=False)

    _assert_clean_refusal(r)
    assert "resolves through a symlink" in r.stderr, r.stderr
    assert "'kid'" in r.stderr, r.stderr
    assert r.stdout == "", (op_args, r.stdout)
    assert donor_head not in r.stdout


def test_status_and_ls_degrade_swapped_child_but_siblings_render(
    tmp_path,
):
    """Read-side degradation: `ls`/`status`/`status --remote` still
    exit 0 and render every row — the swapped kid degrades to the
    missing-gitdir fallbacks (`[]` branch, `?` HEAD, `missing` drift,
    empty porcelain) while the sibling donor row renders its real
    `[master]`/sha/`clean`. The donor doubles as the link target, so a
    read THROUGH the link would render the donor's identity on kid's
    row — exactly what pre-fix does.

    Pre-fix signature: kid's row carried the donor's branch and short
    sha (`[master] <sha>`), identical to the donor's own row, and
    `status --remote` reported `clean` for kid.
    """
    parent, kid, donor = _whole_repo_pair(tmp_path)
    url = str(tmp_path / "upstream")
    donor_head = _rev_parse(donor / ".gf" / "git", "--short", "HEAD")
    _swap_dot_gf(tmp_path, kid, donor)

    r = gf("-C", str(parent), "ls", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert re.search(
        rf"^kid\s+{re.escape(url)}\s+\[\]\s+\?$", r.stdout, re.M), \
        r.stdout
    assert re.search(
        rf"^donor\s+{re.escape(url)}\s+\[master\]\s+"
        rf"{re.escape(donor_head)}$", r.stdout, re.M), r.stdout

    r = gf("-C", str(parent), "status", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert re.search(
        rf"^kid\s+{re.escape(url)}\s+\[\]$", r.stdout, re.M), r.stdout
    assert re.search(
        rf"^donor\s+{re.escape(url)}\s+\[master\]$", r.stdout, re.M), \
        r.stdout

    r = gf("-C", str(parent), "status", "--remote", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert re.search(
        rf"^kid\s+{re.escape(url)}\s+\[\]\s+missing$", r.stdout, re.M), \
        r.stdout
    assert re.search(
        rf"^donor\s+{re.escape(url)}\s+\[master\]\s+clean$", r.stdout,
        re.M), r.stdout


def test_established_whole_repo_child_pull_and_rm_stay_green(tmp_path):
    """Control: the new gate never fires on a real `.gf` — an ordinary
    established whole-repo child pulls and removes normally, with rm
    moving `.gf/git` to `.git` and dropping the binding."""
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up), "kid")
    kid = parent / "kid"
    head_before = _rev_parse(kid / ".gf" / "git", "HEAD")

    r = gf("-C", str(parent), "pull", "kid", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert _rev_parse(kid / ".gf" / "git", "HEAD") == head_before

    r = gf("-C", str(parent), "rm", "kid", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert kid.is_dir()
    assert (kid / ".git" / "HEAD").is_file()
    assert _rev_parse(kid / ".git", "HEAD") == head_before
    assert not os.path.lexists(kid / ".gf")
    assert (kid / "docs" / "api" / "reference.md").read_text() == "api v1"
    assert _manifest_folders(parent) == []
