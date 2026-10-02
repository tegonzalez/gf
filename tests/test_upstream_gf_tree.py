# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""R15-F1 regression pins: hostile upstream `.gf` trees, planted child
hooks, and retargeted store origins.

Real-git arms drive `gf` through subprocesses (``conftest.gf``); the
mock arm keeps ``tests/mock_git.py``'s ``Repo.gf_entry`` poison flag and
its ``ls-tree``/``config --get remote.origin.url``/``fetch``
propagation verbs honest by exercising ``shelf.ensure_checkout`` and
``shelf.ensure_repo_store`` directly.

Mechanisms under test:

- ``shelf._assert_no_gf_root_entry`` — ``git ls-tree <sha> .gf``
  refuses ANY root ``.gf`` entry (blob, tree, symlink or gitlink)
  before the resolved tree materializes: at ``_apply_ref`` (whole-repo
  clone/pull) and at ``ensure_checkout`` (shared-store checkouts, plus
  a second ``_resolve_remote_branch`` probe so a stale
  ``refs/heads/<b>`` cannot shadow ``origin/<b>``).  ``ls-tree <sha>
  .gf`` lists only the ROOT entry, so a nested ``sub/.gf`` stays legal.
- ``backends.GitCliBackend.git`` — calls with ``git_dir`` set (every op
  gf itself runs on gf-managed gitdirs) pin ``core.hooksPath`` to the
  null device through the GIT_CONFIG_COUNT/GIT_CONFIG_KEY_0/
  GIT_CONFIG_VALUE_0 env channel, so a committed or planted hook never
  executes during gf's own clone/pull work; cwd-only passthroughs
  (``gf sh``/``gf git``/``gf diff``/``gf log``) keep the user's hooks.
- ``shelf._assert_store_origin`` — an existing repo store's
  ``remote.origin.url`` must ``repo_key``-match the binding's recorded
  resolution before any refspec write or fetch (``ensure_repo_store``
  join arm and ``pull_shared_bindings``' existing-store arm).
"""

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from conftest import gf, git
from gf import layout, shelf
from gf.exceptions import ValidationError


_ROOT_ENTRY_REFUSAL = "root tree carries"    # _assert_no_gf_root_entry
_GF_OWNED_STORAGE = "gf-managed storage"
_STORE_ORIGIN_REFUSAL = "no longer matches"  # _assert_store_origin

_SHA1 = "1111111111111111111111111111111111111111"
_WILDCARD_FETCH = "+refs/heads/*:refs/remotes/origin/*"


# --- helpers -----------------------------------------------------------


def _real_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _bare_upstream(tmp_path: Path, name: str = "upstream") -> tuple[Path, Path]:
    """Bare upstream at ``tmp_path/<name>`` plus its seed work clone."""
    up = tmp_path / name
    up.mkdir()
    git("init", "--bare", cwd=up)
    work = tmp_path / f"_seed-{name}"
    git("clone", str(up), str(work), cwd=tmp_path)
    return up, work


def _push_all(work: Path, message: str = "seed") -> None:
    git("add", "-A", cwd=work)
    git("commit", "-m", message, cwd=work)
    git("push", "-q", "origin", "master", cwd=work)


def _plant_hook(path: Path, marker: Path) -> None:
    """Write an executable post-checkout hook that touches `marker`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf planted > '{marker}'\n")
    path.chmod(0o755)


def _ls_tree(repo: Path, spec: str) -> str:
    """`git ls-tree HEAD <spec>` output — a fixture-mode sanity probe."""
    r = subprocess.run(
        ["git", "--git-dir", str(repo), "ls-tree", "HEAD", spec],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _manifest_entries(parent: Path) -> list[dict]:
    gf_toml = parent / "gf.toml"
    if not gf_toml.is_file():
        return []
    return tomllib.loads(gf_toml.read_text()).get("git_folder", [])


# --- committed root `.gf` refusal --------------------------------------


def test_clone_refuses_upstream_root_gf_hook_tree(tmp_path):
    """Arm (a): upstream commits mode-100755 `.gf/git/hooks/post-checkout`.

    A whole-repo child anchors its gitdir at `child/.gf/git`, so the
    committed tree would plant an EXECUTABLE hook inside gf's gitdir —
    pre-fix the first `checkout -B` materialized it and git ran it (the
    marker file appeared, `Cloned` printed).  Post-fix the root-entry
    guard refuses before any materialization: rc!=0 naming `.gf`, no
    `Cloned`, no marker, and the new child is cleaned up.
    """
    marker = tmp_path / "hook-fired"
    up, work = _bare_upstream(tmp_path)
    _plant_hook(work / ".gf" / "git" / "hooks" / "post-checkout", marker)
    (work / "content.txt").write_text("upstream content")
    _push_all(work)
    # Fixture sanity: the hook really is an executable blob in the tree.
    assert _ls_tree(up, ".gf/git/hooks/post-checkout").startswith("100755")

    parent = _real_parent(tmp_path)
    r = gf("-C", str(parent), "clone", str(up), "child", check=False)
    assert r.returncode != 0
    assert _ROOT_ENTRY_REFUSAL in r.stderr
    assert _GF_OWNED_STORAGE in r.stderr
    assert "Cloned" not in r.stdout
    assert not marker.exists()
    child = parent / "child"
    assert not os.path.lexists(child) or (
        child.is_dir() and not any(child.iterdir())
    )
    assert _manifest_entries(parent) == []


def test_clone_refuses_upstream_root_gf_symlink(tmp_path):
    """Arm (b): a committed root `.gf` SYMLINK refuses identically —
    `ls-tree` reports `120000 blob`; entry kind is irrelevant.

    Pre-fix this died differently: `checkout` collided the `.gf` link
    with the child's just-created `.gf/` gitdir directory and failed
    with git's own untracked-overwrite error — the guard's message is
    the discriminating signal.
    """
    up, work = _bare_upstream(tmp_path, name="upstream-sym")
    (work / "vault").mkdir()
    (work / "vault" / "v.txt").write_text("link target")
    os.symlink("vault", work / ".gf")
    (work / "content.txt").write_text("content")
    _push_all(work)
    # Fixture sanity: `.gf` really is a committed symlink.
    assert _ls_tree(up, ".gf").startswith("120000")

    parent = _real_parent(tmp_path)
    r = gf("-C", str(parent), "clone", str(up), "child", check=False)
    assert r.returncode != 0
    assert _ROOT_ENTRY_REFUSAL in r.stderr
    assert _GF_OWNED_STORAGE in r.stderr
    assert "Cloned" not in r.stdout
    assert not os.path.lexists(parent / "child")
    assert _manifest_entries(parent) == []


def test_clone_subfolder_spelling_refuses_root_gf_entry(tmp_path):
    """Arm (c): `gf clone <up>/sub` on the poisoned upstream refuses too.

    The shared repo store fetches the hostile tree before
    `ensure_checkout` probes it — the refusal precedes any cone
    materialization.  Pre-fix this clone SUCCEEDED: the cone only ever
    materialized `sub/`, so the root `.gf` blob stayed inside the
    store's objects and `vendored` linked normally.
    """
    up, work = _bare_upstream(tmp_path, name="upstream-sub")
    (work / "sub").mkdir()
    (work / "sub" / "x.txt").write_text("sub content")
    (work / ".gf").write_text("a root .gf blob — any entry kind poisons")
    _push_all(work)
    # Fixture sanity: `.gf` is a committed root blob here.
    assert _ls_tree(up, ".gf").startswith("100644")

    parent = _real_parent(tmp_path)
    r = gf(
        "-C", str(parent), "clone", str(up / "sub"), "vendored",
        check=False,
    )
    assert r.returncode != 0
    assert _ROOT_ENTRY_REFUSAL in r.stderr
    assert _GF_OWNED_STORAGE in r.stderr
    assert "Cloned" not in r.stdout
    assert not os.path.lexists(parent / "vendored")
    # The store the failed clone created is rolled back too.
    repos = parent / ".gf" / "repos"
    assert not repos.exists() or not any(repos.iterdir())
    assert _manifest_entries(parent) == []


def test_nested_sub_gf_entry_is_legal(tmp_path):
    """Arm (d): control — a committed `sub/.gf/...` never collides with
    the gf-managed anchor (the probe lists only the ROOT `.gf` entry):
    the whole-repo clone materializes it and a later pull stays green.
    """
    up, work = _bare_upstream(tmp_path)
    (work / "sub" / ".gf").mkdir(parents=True)
    (work / "sub" / ".gf" / "notes.txt").write_text("nested .gf content")
    (work / "top.txt").write_text("top")
    _push_all(work)

    parent = _real_parent(tmp_path)
    r = gf("-C", str(parent), "clone", str(up), "child")
    assert "Cloned" in r.stdout
    assert (
        parent / "child" / "sub" / ".gf" / "notes.txt"
    ).read_text() == "nested .gf content"

    (work / "top.txt").write_text("top v2")
    _push_all(work, "second")
    r = gf("-C", str(parent), "pull")
    assert "Pulled" in r.stdout
    assert (parent / "child" / "top.txt").read_text() == "top v2"


# --- hook silencing -----------------------------------------------------


def test_pull_silences_planted_hook_but_passthrough_runs_it(tmp_path):
    """Arm (e): a hook planted into an ESTABLISHED child's gitdir cannot
    run inside gf's own ops — `gf pull` fetches and re-checks-out with
    `git_dir` set, so the hooksPath overlay silences it (marker absent)
    while the pull itself stays green — but the user's own git through
    `gf sh` keeps normal hook resolution (marker written).

    Pre-fix BOTH sides ran the hook: the pull's `checkout -B` executed
    `child/.gf/git/hooks/post-checkout`, so the marker exists after the
    pull — the `not marker.exists()` assertion is the discriminator.
    """
    up, work = _bare_upstream(tmp_path)
    (work / "f.txt").write_text("v1")
    _push_all(work)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "child")
    child = parent / "child"

    # Plant the hook AFTER the child is established — this is the
    # "someone wrote into .gf/git/hooks" posture, not a committed entry.
    marker = tmp_path / "hook-fired"
    _plant_hook(child / ".gf" / "git" / "hooks" / "post-checkout", marker)

    # Advance upstream so the pull really moves HEAD and checkout fires.
    (work / "f.txt").write_text("v2")
    _push_all(work, "second")

    r = gf("-C", str(parent), "pull")
    assert r.returncode == 0
    assert "Pulled" in r.stdout
    assert not marker.exists()

    # The passthrough keeps hooks: the SAME hook runs for user git.
    r = gf("sh", "-c", "git checkout -b probe", cwd=child)
    assert r.returncode == 0
    assert marker.exists()
    assert marker.read_text() == "planted"


# --- store origin binding ----------------------------------------------


def test_pull_refuses_retargeted_store_origin(tmp_path):
    """Arm (f): an existing repo store whose `remote.origin.url` was
    rewritten must not be fetched — `gf pull` refuses before the
    coverage loop writes refspecs or fetches the attacker URL.

    Pre-fix the pull silently fetched the rewritten origin and
    re-checked-out ITS tree: rc=0, `Pulled` printed, and the linked
    content changed to the attacker's bytes.
    """
    up, work = _bare_upstream(tmp_path)
    (work / "sub").mkdir()
    (work / "sub" / "x.txt").write_text("upstream sub")
    _push_all(work)
    parent = _real_parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "sub"), "vendored")
    assert (parent / "vendored" / "x.txt").read_text() == "upstream sub"

    # An attacker-controlled repository the store gets pointed at.
    evil = tmp_path / "evil"
    evil.mkdir()
    git("init", cwd=evil)
    (evil / "sub").mkdir()
    (evil / "sub" / "x.txt").write_text("evil payload")
    git("add", "-A", cwd=evil)
    git("commit", "-m", "evil", cwd=evil)

    stores = [p for p in (parent / ".gf" / "repos").iterdir() if p.is_dir()]
    assert len(stores) == 1, f"expected one repo store, found {stores}"
    store = stores[0] / "git"
    git(
        "--git-dir", str(store), "config", "remote.origin.url", str(evil),
        cwd=tmp_path,
    )

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode != 0
    assert "remote.origin.url" in r.stderr
    assert _STORE_ORIGIN_REFUSAL in r.stderr
    assert "Pulled" not in r.stdout
    # The refusal precedes the fetch AND the checkout: the linked
    # content is still the binding's own bytes.
    assert (parent / "vendored" / "x.txt").read_text() == "upstream sub"


# --- mock-backend pins ---------------------------------------------------


def _seed_upstream_repo(mock_backend, path="/upstream", gf_entry=False):
    """Seed a bare upstream with one master commit; return the Repo."""
    repo = mock_backend.seed(path, bare=True, head="master")
    mock_backend.add_commit(
        repo, _SHA1, {"sub/x.txt": "x", "top.txt": "t"}, message="seed",
    )
    repo.refs["refs/heads/master"] = _SHA1
    repo.gf_entry = gf_entry
    return repo


def _create_store(mock_backend, parent: str, upstream: str) -> Path:
    """Create the shared repo store the way `ensure_repo_store` does."""
    store = layout.repo_store(Path(parent), upstream)
    mock_backend.git("init", "--bare", git_dir=store)
    mock_backend.git(
        "config", "remote.origin.url", upstream, git_dir=store,
    )
    mock_backend.git(
        "config", "remote.origin.fetch", _WILDCARD_FETCH, git_dir=store,
    )
    mock_backend.git("fetch", "origin", git_dir=store)
    return store


def test_mock_gf_entry_blocks_ensure_checkout(fs, mock_backend):
    """Arm (g): `Repo.gf_entry` fetched into the store makes
    `ensure_checkout` refuse before any materialization — the mock's
    `ls-tree <sha> .gf` verb answers the guard's probe, and `fetch`
    propagated the poison from the upstream onto the store repo."""
    _seed_upstream_repo(mock_backend, gf_entry=True)
    store = _create_store(mock_backend, "/parent", "/upstream")
    store_repo = mock_backend.repos[str(store)]
    assert store_repo.gf_entry is True  # fetch propagated the poison

    co = layout.subfolder_checkout("/parent", "/upstream", "master", "sub")
    with pytest.raises(ValidationError, match="root tree carries"):
        shelf.ensure_checkout(co, "master", backend=mock_backend)
    # The refusal really came from the guard's probe verb.
    assert any(
        call[0][0] == "ls-tree" and ".gf" in call[0]
        for call in mock_backend.calls
    )


def test_mock_clean_tree_ensure_checkout_builds(fs, mock_backend):
    """Control for arm (g): without `gf_entry` the identical
    `ensure_checkout` runs its create arm to completion — the poison
    pin, not an adjacent refusal, is what the guard test exercises."""
    _seed_upstream_repo(mock_backend)
    _create_store(mock_backend, "/parent", "/upstream")

    co = layout.subfolder_checkout("/parent", "/upstream", "master", "sub")
    assert shelf.ensure_checkout(co, "master", backend=mock_backend) is True
    assert (co.work_tree / "sub" / "x.txt").read_text() == "x"


def test_mock_store_origin_mismatch_blocks_join(fs, mock_backend):
    """Mock pin for the store-origin check: `config --get
    remote.origin.url` answers the recorded remote wherever the model
    kept it, so `ensure_repo_store`'s join arm refuses a retargeted
    store and accepts the binding's own."""
    _seed_upstream_repo(mock_backend)
    store = _create_store(mock_backend, "/parent", "/upstream")

    # Retarget the store's origin — same verb sequence as the real-git
    # arm's `git --git-dir <store> config remote.origin.url <other>`.
    mock_backend.git(
        "config", "remote.origin.url", "/evil", git_dir=store,
    )
    with pytest.raises(ValidationError, match="no longer matches"):
        shelf.ensure_repo_store(store, "/upstream", backend=mock_backend)

    # Restore the binding's resolution: the join arm proceeds to fetch.
    mock_backend.git(
        "config", "remote.origin.url", "/upstream", git_dir=store,
    )
    shelf.ensure_repo_store(store, "/upstream", backend=mock_backend)
    fetches = [
        call[0] for call in mock_backend.calls
        if call[0][0] == "fetch" and call[0][-1] == "origin"
    ]
    assert len(fetches) == 2  # create fetch + join fetch
