# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""P2R.S6 — `gf pull` grouped composition + init-child first-pull conversion.

Receiving checks against the landed seam (S3-S5): the command surface is
live; the grouping behaviors are the designed-red surface.

Row scope (verbatim): ``cmd_pull``/``shelf`` partition selected bindings
by resolved form — whole-repo keeps existing per-child order
(dirty-check → fetch → apply); subfolder groups by ``co.common_dir`` →
one fetch per store (ensure-store + refspec coverage first), groups by
checkout key → one dirty check / --force / --autostash / apply per
checkout → per-binding link create/retarget + state + one output line
per binding served + "(moved with X)" marks; override repo-URL change →
new store; key change → retarget link; retarget drops the moved binding
from the vacated checkout's recorded bindings; init-child conversion:
``rev-parse --verify HEAD`` rc + files-other-than-``.gf`` test → empty
converts, else refuse deleting nothing.

Authorities: spec `gf pull` (L239-243) + "Update algorithm" (L485-499) +
`gf init` (L301-315) + "URL resolution" record rules + constraint L546;
arch GF-D10/D11/D12/D13; plan D10.

Call logs use a wrapped real GitCliBackend in-process (`cli.main(...,
backend=rec)`) on the real filesystem — the same observable contract as
the mock-backend call log, without the fake-fs divergences the mock
model shows for shared-store layouts (probe: `ref=latest` keying, strict
re-lock on re-add).
"""

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from conftest import gf, git
from gf import cli, layout
from gf.backends import GitCliBackend


# ---------------------------------------------------------------------------
# helpers


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} rc={r.returncode}:\n{r.stderr}")
    return r


def _out(*args) -> str:
    return _git(*args).stdout.strip()


class _LogBackend(GitCliBackend):
    """Real-git backend recording (argv, kwargs) of every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict]] = []

    def git(self, *args, **kwargs):
        self.calls.append((tuple(map(str, args)), kwargs))
        return super().git(*args, **kwargs)


def _run(parent: Path, capsys, *args, backend=None):
    """In-process `gf -C <parent> <args>` on the real fs.

    Returns (exit_code, stdout). `-C` chdirs inside cli.main; restore.
    """
    old = Path.cwd()
    try:
        try:
            code = cli.main(["-C", str(parent), *map(str, args)],
                            backend=backend)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    finally:
        os.chdir(old)
    return code or 0, capsys.readouterr().out


def _calls_matching(rec: _LogBackend, *verbs) -> list:
    return [c for c in rec.calls if any(v in c[0] for v in verbs)]


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str = "upstream",
              marker: str = "api on master") -> Path:
    """Bare upstream with docs/api/x.txt + tools/t.txt, dev branch, tag v1."""
    up = tmp_path / name
    _git("init", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", str(up), str(work))
    (work / "docs/api").mkdir(parents=True)
    (work / "docs/api/x.txt").write_text(marker)
    (work / "tools").mkdir()
    (work / "tools/t.txt").write_text(f"tool in {name}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "init")
    _git("-C", work, "push", "origin", "master")
    _git("-C", work, "checkout", "-q", "-b", "dev")
    (work / "docs/api/dev.txt").write_text(f"dev in {name}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "dev")
    _git("-C", work, "push", "origin", "dev")
    _git("-C", work, "tag", "v1", "master")
    _git("-C", work, "push", "origin", "v1")
    return up


def _advance(up: Path, tmp_path: Path, tag: str) -> None:
    """Push one more commit to upstream master."""
    work = tmp_path / f"_adv_{tag}"
    _git("clone", str(up), str(work))
    (work / "docs/api/x.txt").write_text(f"api {tag}")
    (work / "tools/t.txt").write_text(f"tool {tag}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", tag)
    _git("-C", work, "push", "origin", "master")


def _co(parent: Path, up: Path, key: str, subdir: str) -> layout.Checkout:
    return layout.subfolder_checkout(parent, str(up), key, subdir)


def _state(parent: Path, up: Path, key: str) -> dict:
    co = _co(parent, up, key, "docs/api")
    return tomllib.loads(co.state.read_text())


def _clone_pair(parent: Path, up: Path) -> None:
    """Two bindings on one upstream sharing the `master` checkout."""
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")


# ---------------------------------------------------------------------------
# grouped pull


def test_pull_one_fetch_per_store_one_apply_per_checkout(tmp_path, capsys):
    """One `fetch` per repo store and one apply per shared checkout across
    N bindings (spec Update algorithm L490-495; row items)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    _advance(up, tmp_path, "adv")

    rec = _LogBackend()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code == 0

    store = layout.repo_store(parent, str(up))
    fetches = [c for c in rec.calls
               if "fetch" in c[0] and c[1].get("git_dir") == store]
    assert len(fetches) == 1, f"fetches: {[c[0] for c in fetches]}"

    admin = store / "worktrees" / "master"
    applies = [c for c in rec.calls
               if "checkout" in c[0] and c[1].get("git_dir") == admin]
    assert len(applies) == 1, f"applies: {[c[0] for c in applies]}"

    # one dirty check over the shared checkout
    stats = [c for c in rec.calls
             if "status" in c[0] and c[1].get("git_dir") == admin]
    assert len(stats) == 1

    assert (parent / "vendor/api/x.txt").read_text().strip() == "api adv"
    assert (parent / "vendor/tools/t.txt").read_text().strip() == "tool adv"


def test_pull_selected_binding_marks_comoved_sibling(tmp_path, capsys):
    """A shared checkout serves every binding linked into it: selecting
    one binding updates the whole checkout and the unselected sibling is
    printed and marked `(moved with <selected>)` (spec L240; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    _advance(up, tmp_path, "adv")

    code, out = _run(parent, capsys, "pull", "vendor/api")
    assert code == 0
    assert "Pulled api" in out
    sibling = next(
        (ln for ln in out.splitlines() if "tools" in ln), None)
    assert sibling is not None, f"no sibling line in {out!r}"
    assert "moved with api" in sibling
    # the whole checkout moved: the sibling's files updated too
    assert (parent / "vendor/tools/t.txt").read_text().strip() == "tool adv"


def test_pull_whole_repo_dirty_check_before_fetch_order(tmp_path, capsys):
    """Whole-repo order is preserved: dirty-check → fetch → apply
    (spec Update algorithm; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "lib")
    _advance(up, tmp_path, "adv")

    rec = _LogBackend()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code == 0
    child_gitdir = parent / "lib" / ".gf" / "git"
    child_calls = [c for c in rec.calls
                   if c[1].get("git_dir") == child_gitdir]
    order = {v: next(i for i, c in enumerate(child_calls)
                    if v in c[0])
             for v in ("status", "fetch", "checkout")}
    assert order["status"] < order["fetch"] < order["checkout"]


def test_pull_dirty_whole_repo_aborts_before_fetch(tmp_path, capsys):
    """The dirty check precedes the fetch: a dirty whole-repo child aborts
    without fetching (spec Update algorithm order; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "lib")
    _advance(up, tmp_path, "adv")
    (parent / "lib" / "dirty.txt").write_text("uncommitted\n")

    rec = _LogBackend()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code != 0
    child_gitdir = parent / "lib" / ".gf" / "git"
    assert not [c for c in rec.calls
                if "fetch" in c[0] and c[1].get("git_dir") == child_gitdir]
    assert (parent / "lib" / "dirty.txt").read_text() == "uncommitted\n"


def test_pull_dirty_shared_checkout_blocks_both_bindings(tmp_path, capsys):
    """One dirty check per checkout: a dirty sibling blocks the shared
    checkout — with no --force/--autostash both bindings abort and the
    checkout stays put (spec L496; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    old_sha = _out("--git-dir",
                   layout.repo_store(parent, str(up))
                   / "worktrees" / "master", "rev-parse", "HEAD")
    _advance(up, tmp_path, "adv")
    (parent / "vendor" / "api" / "dirty.txt").write_text("uncommitted\n")

    code, out = _run(parent, capsys, "pull")
    assert code != 0
    assert "Pulled" not in out
    assert _out("--git-dir", layout.repo_store(parent, str(up))
                / "worktrees" / "master", "rev-parse", "HEAD") == old_sha
    assert (parent / "vendor" / "api" / "dirty.txt").read_text() == (
        "uncommitted\n")


def test_pull_uses_recorded_resolution_no_remote_probe(tmp_path, capsys):
    """Pull takes repo URL/subdir from the recorded resolution — no
    `ls-remote`/`remote set-head` re-probe of the remote (spec Update
    algorithm step 1 + Drift-algorithm local-only spirit; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)

    rec = _LogBackend()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code == 0
    rec.calls.clear()
    code, _ = _run(parent, capsys, "pull", backend=rec)
    assert code == 0
    assert not [c for c in rec.calls if "ls-remote" in c[0]]
    assert not [c for c in rec.calls
                if c[0][:2] == ("remote", "set-head")]


# ---------------------------------------------------------------------------
# override moves


def test_override_repo_url_moves_binding_and_vacates(tmp_path, capsys):
    """An override that changes the repo URL moves the binding to another
    store: the link retargets, the new store's record gains the binding,
    the vacated checkout's recorded bindings drop it, and the vacated
    checkout's files — incl. uncommitted work — are untouched
    (spec Update algorithm + L179 record rules; row item)."""
    up_a = _upstream(tmp_path, "up_a", "api in A")
    up_b = _upstream(tmp_path, "up_b", "api in B")
    parent = _parent(tmp_path)
    _clone_pair(parent, up_a)
    dirty = (_co(parent, up_a, "master", "docs/api").work_tree
             / "docs" / "api" / "dirty.txt")
    dirty.write_text("uncommitted in A\n")

    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\n'
        f'url = "{up_b}/docs/api"\n')
    code, _ = _run(parent, capsys, "pull")
    assert code == 0

    co_b = _co(parent, up_b, "master", "docs/api")
    assert (parent / "vendor" / "api").resolve() == (
        co_b.work_tree / "docs/api").resolve()
    assert (parent / "vendor" / "api" / "x.txt").read_text().strip() == (
        "api in B")

    # records: vacated checkout drops the binding, new store gains it
    recs_a = str(_state(parent, up_a, "master")["bindings"])
    assert "docs/api" not in recs_a and "tools" in recs_a
    assert "docs/api" in str(_state(parent, up_b, "master")["bindings"])

    # the vacated checkout's work survives — no tracked-file change
    assert dirty.read_text() == "uncommitted in A\n"


def test_override_ref_retargets_link_and_vacates_record(tmp_path, capsys):
    """An override that changes the effective checkout key retargets the
    consumer link to the other checkout and drops the moved binding from
    the vacated checkout's recorded bindings (spec Update algorithm; row
    item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)

    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "dev"\n')
    code, _ = _run(parent, capsys, "pull")
    assert code == 0

    co_dev = _co(parent, up, "dev", "docs/api")
    assert (parent / "vendor" / "api").resolve() == (
        co_dev.work_tree / "docs/api").resolve()
    assert (parent / "vendor" / "api" / "dev.txt").read_text().strip() == (
        f"dev in upstream")

    recs = str(_state(parent, up, "master")["bindings"])
    assert "docs/api" not in recs and "tools" in recs
    assert "docs/api" in str(_state(parent, up, "dev")["bindings"])


# ---------------------------------------------------------------------------
# init-child first-pull conversion


def test_init_empty_child_converts_to_consumer_link(tmp_path):
    """`gf init` + first `gf pull` with a subfolder URL converts an empty
    child (no commits, no files other than `.gf`) into a consumer link
    (spec L241 + constraint L546; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "vendor/api",
       "--url", str(up / "docs/api"), "-b", "master")
    child = parent / "vendor" / "api"
    assert child.is_dir() and not child.is_symlink()

    gf("-C", str(parent), "pull")
    assert child.is_symlink()
    co = _co(parent, up, "master", "docs/api")
    assert child.resolve() == (co.work_tree / "docs/api").resolve()
    assert (child / "x.txt").read_text().strip() == "api on master"


def test_init_child_with_files_refuses_and_deletes_nothing(tmp_path):
    """A child that gained files is refused — the pull stops with an
    error and deletes nothing (spec L241 + constraint L546; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "vendor/api",
       "--url", str(up / "docs/api"), "-b", "master")
    child = parent / "vendor" / "api"
    (child / "user.txt").write_text("user work\n")

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode != 0
    assert child.is_dir() and not child.is_symlink()
    assert (child / "user.txt").read_text() == "user work\n"
    assert (child / ".gf").exists()
