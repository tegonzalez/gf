# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import os
import re
import tomllib
from pathlib import Path

import pytest
import tomli_w

from conftest import git, push_commit, gf
from gf import layout, state as state_mod
from gf.exceptions import GitFoldersError


def _load_state(child: Path) -> dict:
    path = child / ".gf" / "state"
    with open(path, "rb") as f:
        return tomllib.load(f)


def test_state_recorded_on_clone(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"
    st = _load_state(child)

    assert st["ref"] == "latest"
    assert st["url"] == str(upstream)
    assert st["override"] is False
    assert len(st["resolved"]) == 40

    head = gf("-C", str(child), "sh", "-c", "git rev-parse HEAD").stdout.strip()
    assert st["resolved"] == head


def test_state_updated_after_pull(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"
    first = _load_state(child)["resolved"]

    push_commit(upstream, "update", "update")
    gf("-C", str(parent), "pull")

    second = _load_state(child)["resolved"]
    assert second != first
    head = gf("-C", str(child), "sh", "-c", "git rev-parse HEAD").stdout.strip()
    assert second == head


def test_state_records_override(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    (parent / "gf.local.toml").write_text('[[git_folder_override]]\nname = "lib"\nref = "master"\n')

    gf("-C", str(parent), "pull")
    child = parent / "vendor" / "lib"
    st = _load_state(child)
    assert st["override"] is True
    assert st["ref"] == "master"


def test_pull_dirty_exit_code(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"
    (child / "a.txt").write_text("dirty")

    push_commit(upstream, "update", "update")
    result = gf("-C", str(parent), "pull", check=False)
    assert result.returncode == 3
    assert "dirty" in (result.stdout + result.stderr).lower()


def test_url_override_fetches_new_shelf(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    fork = tmp_path / "fork"
    fork.mkdir()
    git("clone", "--bare", str(upstream), str(fork), cwd=tmp_path)
    fork_work = tmp_path / "_work_fork"
    git("clone", str(fork), str(fork_work), cwd=tmp_path)
    (fork_work / "fork.txt").write_text("fork content")
    git("add", "fork.txt", cwd=fork_work)
    git("commit", "-m", "fork content", cwd=fork_work)
    git("push", "origin", "master", cwd=fork_work)

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"
    assert not (child / "fork.txt").exists()

    (parent / "gf.local.toml").write_text(f'[[git_folder_override]]\nname = "lib"\nurl = "{fork}"\n')
    gf("-C", str(parent), "pull")
    assert (child / "fork.txt").read_text() == "fork content"

    st = _load_state(child)
    assert st["url"] == str(fork)
    assert st["override"] is True


def test_clone_recommends_parent_gitignore(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    result = gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    assert not (parent / ".gitignore").exists()
    assert 'add "vendor/lib/" to .gitignore' in result.stdout


def test_status_shows_porcelain(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"
    (child / "a.txt").write_text("dirty")

    result = gf("-C", str(parent), "status")
    assert re.search(r"lib\s+" + re.escape(str(upstream)) + r"\s+\[master\]", result.stdout)
    assert " M a.txt" in result.stdout


def test_pull_initializes_missing_child(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    (parent / "gf.toml").write_text(
        f'[[git_folder]]\nname = "lib"\nurl = "{upstream}"\nref = "latest"\npath = "vendor/lib"\n'
    )

    child = parent / "vendor" / "lib"
    assert not child.exists()
    result = gf("-C", str(parent), "pull")
    assert child.is_dir()
    assert (child / "a.txt").read_text() == "hello"
    assert not (parent / ".gitignore").exists()
    assert 'add "vendor/lib/" to .gitignore' in result.stdout


def test_pull_fails_on_non_git_folder_path(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    child = parent / "vendor" / "lib"
    child.mkdir(parents=True)
    (child / "existing.txt").write_text("I was here first")

    (parent / "gf.toml").write_text(
        f'[[git_folder]]\nname = "lib"\nurl = "{upstream}"\nref = "latest"\npath = "vendor/lib"\n'
    )

    result = gf("-C", str(parent), "pull", check=False)
    assert result.returncode == 1
    assert "not a git-folder" in (result.stdout + result.stderr).lower()


def test_pull_invalid_ref_exit_code(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")

    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)

    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    (parent / "gf.local.toml").write_text('[[git_folder_override]]\nname = "lib"\nref = "no-such-ref"\n')

    result = gf("-C", str(parent), "pull", check=False)
    assert result.returncode == 1
    assert "ref" in (result.stdout + result.stderr).lower()


# ---------------------------------------------------------------------------
# GF-10-7 — atomic state writes and the clean corrupt-state error
#
# `state.save_checkout` writes atomically: mkstemp inside the state
# file's own directory → tomli_w.dump → os.replace over `state`, with the
# temp file unlinked on failure. A reader therefore never observes a
# torn record, and a failed dump leaves the prior content untouched.
#
# `state.load_checkout` reports a malformed state file as a
# GitFoldersError naming the path (`corrupt git-folders state at ...`)
# instead of leaking tomllib.TOMLDecodeError as an uncaught traceback.
# On the `gf pull` path that surfaces as a clean
# `pull failed for git-folder '<name>' (<path>): corrupt ...` (rc 1).

_STATE = {
    "resolved": "a" * 40,
    "ref": "latest",
    "url": "https://example.test/repo.git",
    "override": False,
}


def _tmp_litter(dirpath: Path) -> list[str]:
    """mkstemp leftovers: save_checkout temps end in `.tmp`."""
    return sorted(p.name for p in dirpath.iterdir()
                  if p.name.endswith(".tmp"))


def test_save_checkout_writes_complete_parseable_state(tmp_path):
    """Unit pin: a save produces a complete parseable TOML record and
    leaves no temp litter — the only file under `.gf` is `state`."""
    co = layout.whole_repo_checkout(tmp_path / "child")
    state_mod.save_checkout(co, _STATE)

    with open(co.state, "rb") as f:
        assert tomllib.load(f) == _STATE
    assert sorted(p.name for p in co.state.parent.iterdir()) == ["state"]


def test_save_checkout_prior_state_stays_visible_during_dump(
        tmp_path, monkeypatch):
    """os.replace semantics: while tomli_w.dump is mid-write the state
    path still serves the PRIOR complete record — new bytes land only at
    the atomic replace (pre-fix this wrote in place: a reader at dump
    time observed the already-truncated file)."""
    co = layout.whole_repo_checkout(tmp_path / "child")
    state_mod.save_checkout(co, _STATE)
    old_bytes = co.state.read_bytes()

    observed: list[bytes] = []
    real_dump = tomli_w.dump

    def spy(data, f):
        observed.append(co.state.read_bytes())
        return real_dump(data, f)

    monkeypatch.setattr(tomli_w, "dump", spy)

    new = dict(_STATE, resolved="b" * 40)
    state_mod.save_checkout(co, new)

    assert observed == [old_bytes]
    with open(co.state, "rb") as f:
        assert tomllib.load(f) == new
    assert _tmp_litter(co.state.parent) == []


def test_save_checkout_failed_dump_preserves_state_no_tmp_litter(
        tmp_path, monkeypatch):
    """A dump that fails mid-write propagates, leaves the prior record
    byte-identical, and unlinks the temp file (pre-fix the write
    truncated `state` in place, leaving the torn partial content)."""
    co = layout.whole_repo_checkout(tmp_path / "child")
    state_mod.save_checkout(co, _STATE)
    old_bytes = co.state.read_bytes()

    def boom(data, f):
        f.write(b"torn partial write [[[")
        raise RuntimeError("dump exploded")

    monkeypatch.setattr(tomli_w, "dump", boom)

    with pytest.raises(RuntimeError, match="dump exploded"):
        state_mod.save_checkout(co, dict(_STATE, resolved="c" * 40))

    assert co.state.read_bytes() == old_bytes
    assert _tmp_litter(co.state.parent) == []


@pytest.mark.parametrize("payload", [
    b"not toml [[[ at all",
    b'ref = "latest"\nresolved =',      # truncated record
])
def test_load_checkout_corrupt_state_raises_git_folders_error(
        tmp_path, payload):
    """A malformed state file raises GitFoldersError naming the file —
    never a leaked tomllib.TOMLDecodeError."""
    co = layout.whole_repo_checkout(tmp_path / "child")
    co.state.parent.mkdir(parents=True)
    co.state.write_bytes(payload)

    with pytest.raises(GitFoldersError) as ei:
        state_mod.load_checkout(co)
    msg = str(ei.value)
    assert f"corrupt git-folders state at {co.state}" in msg
    assert "remove it" in msg


# ---------------------------------------------------------------------------
# pull-path integration over a real bare upstream


def _upstream_repo(tmp_path: Path) -> Path:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")
    return upstream


def _upstream_with_subdir(tmp_path: Path) -> Path:
    """Bare upstream with a `docs/api` subdir for subfolder bindings."""
    up = tmp_path / "upstream"
    up.mkdir()
    git("init", "--bare", cwd=up)
    work = tmp_path / "_seed"
    git("clone", str(up), str(work), cwd=tmp_path)
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text("api v1")
    git("add", "-A", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("push", "origin", "master", cwd=work)
    return up


def _parent_repo(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _assert_clean_corrupt_state_error(r, state_file: Path, name: str):
    """rc 1 + the GitFoldersError surface naming the file, no traceback."""
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert "TOMLDecodeError" not in r.stderr
    assert f"pull failed for git-folder '{name}'" in r.stderr
    m = re.search(r"corrupt git-folders state at (\S+?):", r.stderr)
    assert m, r.stderr
    assert Path(m.group(1)) == state_file.resolve()
    assert "remove it" in r.stderr


def test_pull_corrupt_child_state_reports_clean_error(tmp_path):
    """A whole-repo child's corrupt `.gf/state` makes `gf pull` fail
    with the clean corrupt-state error (rc 1, the file named, no
    traceback); deleting the file — the error's own prescribed remedy —
    lets the same pull recover and rewrite the record."""
    upstream = _upstream_repo(tmp_path)
    parent = _parent_repo(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    state_file = parent / "vendor" / "lib" / ".gf" / "state"
    assert state_file.is_file()

    state_file.write_bytes(b"resolved = [[[corrupt\n")
    r = gf("-C", str(parent), "pull", check=False)
    _assert_clean_corrupt_state_error(r, state_file, "lib")

    # Recovery per the error's instruction: remove the corrupt record.
    state_file.unlink()
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    st = _load_state(parent / "vendor" / "lib")
    head = gf("-C", str(parent / "vendor" / "lib"),
              "sh", "-c", "git rev-parse HEAD").stdout.strip()
    assert st["resolved"] == head
    assert _tmp_litter(state_file.parent) == []


def test_pull_corrupt_shared_checkout_state_reports_clean_error(tmp_path):
    """A subfolder binding's corrupt `.gf/wt/<rk>/.master.state` fails
    `gf pull` the same clean way — the pull consults the recorded
    `binding_urls` map while planning, before any fetch."""
    upstream = _upstream_with_subdir(tmp_path)
    parent = _parent_repo(tmp_path)
    gf("-C", str(parent), "clone", f"{upstream}/docs/api", "vendor/api")

    states = list((parent / ".gf" / "wt").glob("*/.*.state"))
    assert len(states) == 1, states
    state_file = states[0]

    state_file.write_bytes(b"binding_urls = { broken\n")
    r = gf("-C", str(parent), "pull", check=False)
    _assert_clean_corrupt_state_error(r, state_file, "api")

    state_file.unlink()
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    with open(state_file, "rb") as f:
        tomllib.load(f)                     # rewritten record parses
    assert _tmp_litter(state_file.parent) == []


def test_pull_rewrites_state_and_leaves_no_tmp_litter(tmp_path):
    """Happy-path control: `gf pull` rc 0 rewrites the state record in
    place (`resolved` advances to the new HEAD) with no temp litter in
    `.gf`, and status/ls still exit clean on the healthy tree."""
    upstream = _upstream_repo(tmp_path)
    parent = _parent_repo(tmp_path)
    gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
    child = parent / "vendor" / "lib"
    first = _load_state(child)["resolved"]

    push_commit(upstream, "update", "update")
    gf("-C", str(parent), "pull")

    st = _load_state(child)                  # parses — never torn
    assert st["resolved"] != first
    head = gf("-C", str(child), "sh", "-c",
              "git rev-parse HEAD").stdout.strip()
    assert st["resolved"] == head
    assert _tmp_litter(child / ".gf") == []

    assert gf("-C", str(parent), "status").returncode == 0
    assert gf("-C", str(parent), "ls").returncode == 0


@pytest.mark.parametrize("op", ["status", "ls"])
def test_listing_corrupt_checkout_scope_state_reports_clean_error(
        tmp_path, op):
    """`gf status`/`gf ls` read the checkout's `bindings` union from
    `.<key>.state` when the consumer link resolves to the checkout ROOT
    (`binding_scope`'s fallback arm — a link at the root carries no
    subdir position to scope by). With that state file corrupt, the
    command must fail inside its per-folder error envelope — rc 1,
    `<op> failed for git-folder '<name>' (<path>): corrupt git-folders
    state at <path>` — not an uncaught traceback."""
    upstream = _upstream_with_subdir(tmp_path)
    parent = _parent_repo(tmp_path)
    gf("-C", str(parent), "clone", f"{upstream}/docs/api", "vendor/api")

    states = list((parent / ".gf" / "wt").glob("*/.*.state"))
    assert len(states) == 1, states
    state_file = states[0]
    # The state file `.<key>.state` sits beside the checkout `<key>`.
    checkout_dir = state_file.parent / state_file.name.removeprefix(
        ".").removesuffix(".state")
    assert checkout_dir.is_dir()

    # Repoint the consumer link at the checkout ROOT so binding_scope
    # takes the recorded-`bindings` fallback. The target must be spelled
    # relative to the LINK's directory — a `.gf/...` or wrongly-anchored
    # spelling dangles into `vendor/.gf/wt/...`, which resolve_checkout
    # maps onto a phantom checkout whose absent state file reads `{}`:
    # no state is ever parsed and the test would pass vacuously.
    link = parent / "vendor" / "api"
    assert link.is_symlink()
    target = os.path.relpath(checkout_dir, link.parent)
    assert target == os.path.join("..", ".gf", "wt",
                                  checkout_dir.parent.name,
                                  checkout_dir.name)
    link.unlink()
    link.symlink_to(target)
    assert os.path.realpath(link) == str(checkout_dir.resolve())

    state_file.write_bytes(b"bindings = [ broken\n")
    r = gf("-C", str(parent), op, check=False)

    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert "TOMLDecodeError" not in r.stderr
    assert f"{op} failed for git-folder 'api' (vendor/api):" in r.stderr
    m = re.search(r"corrupt git-folders state at (\S+?):", r.stderr)
    assert m, r.stderr
    assert Path(m.group(1)) == state_file.resolve()
