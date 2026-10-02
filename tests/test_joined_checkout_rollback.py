# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`ensure_shared_binding` rollback after a JOINED checkout (R14-F4).

Defect pinned: `ensure_shared_binding` probed `_checkout_record_valid(co)`
BEFORE calling `ensure_checkout` and, on a later step's failure, tore the
checkout down whenever that snapshot had seen nothing. A concurrent
process's checkout that landed between the probe and this call's own
ensure — the join arm — was then rmtree'd by OUR unrelated consumer-link
failure: worktree record, `.state` file, and the checkout tree including
its untracked files.

Fix under test: `ensure_checkout` returns True only when its own create
arm completed and False on the join arm; `ensure_shared_binding` rolls
back only `if created:` — the return flag, never a filesystem snapshot,
decides.

Concurrency timing is too flaky for a subprocess pin (ruling), so the
race is pinned at the in-process seam: the monkeypatched
`shelf.ensure_checkout` materializes the racing process's checkout with
the REAL create arm (worktree add + record + sparse cone + lock +
`.state`), plants that process's untracked work, then reports the arm
verdict. Establishing the checkout BEFORE the call cannot discriminate —
the pre-fix probe would then see the record and skip teardown anyway;
the defect lives exactly in the stale window (absent at probe, present
at ensure), which only this interleaving reproduces. The post-checkout
failure is likewise real: `ensure_consumer_link` refuses a pre-occupied
non-empty consumer path. Arms 1 and 2 differ ONLY in the stubbed return
flag, isolating it as the rollback trigger; arm 3 is the green control.

Real-git fixtures, same style as test_store_checkout.py /
test_clone_subfolder.py. Authorities: spec clone mechanics failure
cleanup ("only the checkout this call created is torn down"; GF-D5/D8);
GF-TRB-11 owns the documented residual (a still-later joiner racing OUR
own created checkout).
"""

from pathlib import Path

import pytest

from conftest import git
from gf import layout, shelf
from gf.backends import GitCliBackend
from gf.exceptions import GitFoldersError


# ---------------------------------------------------------------------------
# fixtures and helpers


def _upstream(tmp_path: Path) -> Path:
    """Bare upstream whose master carries docs/api/x.txt, tools/y.txt and
    root.txt — two real cone-mappable subdirs."""
    up = tmp_path / "upstream"
    up.mkdir()
    git("init", "--bare", cwd=up)
    work = tmp_path / "_seed"
    git("clone", str(up), str(work), cwd=tmp_path)
    (work / "docs/api").mkdir(parents=True)
    (work / "docs/api/x.txt").write_text("api\n")
    (work / "tools").mkdir()
    (work / "tools/y.txt").write_text("tool\n")
    (work / "root.txt").write_text("root\n")
    git("add", "-A", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("push", "-q", "origin", "master", cwd=work)
    return up


def _real_parent(tmp_path: Path) -> Path:
    """Parent root that is itself a plain git repository."""
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """{relpath: bytes} for every regular file under `root` — the
    byte-identical survival witness."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _co(parent: Path, repo_url: str, subdir: str) -> layout.Checkout:
    return layout.subfolder_checkout(
        parent, repo_url, layout.checkout_key_for_branch("master"), subdir)


def _bind(child: Path, parent: Path, repo_url: str, subdir: str) -> None:
    """One `ensure_shared_binding` call with production-shaped args
    (`url` is the spelled repo/subdir spelling, `repo_url` the resolved
    repository)."""
    shelf.ensure_shared_binding(
        child, parent, repo_url, subdir, f"{repo_url}/{subdir}", "master",
        override=False, backend=GitCliBackend())


@pytest.fixture
def remove_checkout_calls(monkeypatch):
    """Spy on `shelf._remove_checkout`: records each torn-down checkout's
    work_tree while delegating to the real teardown."""
    calls: list[Path] = []
    real = shelf._remove_checkout

    def _spy(co, backend):
        calls.append(co.work_tree)
        return real(co, backend)

    monkeypatch.setattr(shelf, "_remove_checkout", _spy)
    return calls


def _racing_create(verdict: bool, seen: dict):
    """The `ensure_checkout` seam stub.

    Runs the REAL create arm — the racing process's checkout lands between
    this call's stale record probe and its own ensure — plants that
    process's untracked work, snapshots the witnesses, then reports
    `verdict`: False = join arm (foreign checkout), True = own create.
    """
    real = shelf.ensure_checkout

    def _stub(co, ref, *, backend=None):
        real(co, ref, backend=backend)
        (co.work_tree / "untracked.txt").write_text(
            "the creator's uncommitted work\n")
        seen["wt"] = _tree_bytes(co.work_tree)
        seen["record"] = _tree_bytes(co.gitdir)
        seen["state"] = co.state.read_bytes()
        return verdict

    return _stub


def _blocked_consumer(parent: Path) -> Path:
    """A consumer path pre-occupied by a real non-empty directory — the
    post-checkout step then fails inside the real `ensure_consumer_link`,
    never destroying it."""
    child = parent / "vendor" / "tools"
    child.mkdir(parents=True)
    (child / "occupied.txt").write_text("consumer path taken\n")
    return child


# ---------------------------------------------------------------------------
# arms


def test_joined_checkout_survives_joiner_failure(
        tmp_path, monkeypatch, remove_checkout_calls):
    """Arm 1 — `ensure_checkout` reports the join arm (False): the
    checkout belongs to whoever created it, so this call's consumer-link
    failure leaves it — files, worktree record and `.state` — byte-
    identical, and `_remove_checkout` never runs.

    Pre-fix this goes red: the stale `checkout_existed` probe ran before
    the racing create landed (inside the ensure seam), saw no record, and
    `not checkout_existed` fired `_remove_checkout` — rmtree'ing the
    foreign checkout incl. `untracked.txt`; the outer store-created
    rollback then removed the whole repo store too."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    child = _blocked_consumer(parent)
    repo_url = str(upstream)
    seen: dict = {}
    monkeypatch.setattr(
        shelf, "ensure_checkout", _racing_create(False, seen))

    with pytest.raises(GitFoldersError, match="exists and is not"):
        _bind(child, parent, repo_url, "tools")

    co = _co(parent, repo_url, "tools")
    # The destructive rollback never fired.
    assert remove_checkout_calls == []
    # The racing process's checkout survives byte-identical: tree
    # (incl. its untracked work), worktree record, `.state`.
    assert shelf._checkout_record_valid(co)
    assert _tree_bytes(co.work_tree) == seen["wt"]
    assert _tree_bytes(co.gitdir) == seen["record"]
    assert co.state.read_bytes() == seen["state"]
    # ...and its foreign record guarded the repo store this call created
    # from the outer `_remove_repo_store` rollback.
    assert co.common_dir.is_dir()
    # The link-step failure itself was clean and non-destructive.
    assert (child / "occupied.txt").read_bytes() == b"consumer path taken\n"


def test_created_checkout_is_torn_down_on_failure(
        tmp_path, monkeypatch, remove_checkout_calls):
    """Arm 2 — counter-arm: identical stimulus, verdict True (this call's
    create arm completed): the rollback still fires — `_remove_checkout`
    drops the worktree record, the checkout tree including untracked
    files, and `.state`, and the just-created repo store goes too.

    Green pre- and post-fix: proves arm 1 discriminates the FLAG, not a
    rollback that simply never runs — a fix that deleted the rollback
    outright would fail here."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    child = _blocked_consumer(parent)
    repo_url = str(upstream)
    seen: dict = {}
    monkeypatch.setattr(
        shelf, "ensure_checkout", _racing_create(True, seen))

    with pytest.raises(GitFoldersError, match="exists and is not"):
        _bind(child, parent, repo_url, "tools")

    co = _co(parent, repo_url, "tools")
    # Real teardown ran exactly once, on this call's own checkout.
    assert remove_checkout_calls == [co.work_tree]
    assert not co.work_tree.exists()
    assert not co.gitdir.exists()
    assert not co.state.exists()
    # The store this failing call itself created is removed as well —
    # no foreign record guards it now.
    assert not co.common_dir.exists()
    # The blocking consumer path was never ours to touch.
    assert (child / "occupied.txt").read_bytes() == b"consumer path taken\n"


def test_ordinary_binding_and_join_never_roll_back(
        tmp_path, remove_checkout_calls):
    """Arm 3 — control: two ordinary bindings through the REAL functions;
    the second takes the real join arm (False) and succeeds — no
    rollback fires on the happy path. End-to-end clone/pull green is
    covered by the suite (test_clone_subfolder.py,
    test_clone_and_pull.py)."""
    upstream = _upstream(tmp_path)
    parent = _real_parent(tmp_path)
    repo_url = str(upstream)

    _bind(parent / "vendor/api", parent, repo_url, "docs/api")
    _bind(parent / "vendor/tools", parent, repo_url, "tools")

    co = _co(parent, repo_url, "tools")
    # One shared checkout serves both links; the join widened its cone.
    assert (parent / "vendor/api").resolve() == \
        (co.work_tree / "docs/api").resolve()
    assert (parent / "vendor/tools").resolve() == \
        (co.work_tree / "tools").resolve()
    assert (co.work_tree / "docs/api/x.txt").read_bytes() == b"api\n"
    assert (co.work_tree / "tools/y.txt").read_bytes() == b"tool\n"
    assert remove_checkout_calls == []
