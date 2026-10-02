# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""OSError → `gf:` envelope pins (no raw tracebacks from fs failures).

A filesystem OSError (EACCES on unlink/rename/open/mkstemp) escaping a
gf operation dies with a clean `gf:` error naming the operation and the
path — never a raw Python traceback. Each pin drives the real `gf` CLI
(`python -m gf` subprocess) over real-git fixtures and forces EACCES
with chmod, or drops to unit-level monkeypatch where a chmod cannot
isolate the arm.

Wrapped sites under test:

- ``shelf.ensure_consumer_link`` retarget ``link.unlink()`` — a `gf pull`
  whose binding's effective url moved to a different repo must retarget
  the consumer link; the unlink dies inside the ``folder_error``
  envelope naming the git-folder (spec §Error handling).
- ``shelf.remove_child`` — `gf rm` of a consumer link (unlink arm) and
  of a whole-repo child (``.gf/git`` → ``.git`` move arm), both inside
  the ``folder_error`` envelope.
- ``state.load_checkout`` — `gf pull` on an unreadable
  ``.gf/wt/<repo-key>/.<checkout-key>.state`` wraps the open() failure.
- ``manifest.write_manifest`` — `gf rm` with an unwritable parent root
  wraps the mkstemp failure (no per-folder envelope — the write happens
  after the removal loop).
- ``cli.main``'s trailing ``except OSError`` — the completeness net for
  fs sites outside the wrapped list: the ``os.unlink`` sweep in
  ``gf worktree remove`` is deliberately unwrapped and reaches it.
- Unit pins: ``state.save_checkout`` mkstemp and ``os.replace`` arms,
  ``write_manifest`` replace arm, ``rollback_consumer_link``, and the
  non-OSError passthrough (a non-OSError from the write block re-raises
  raw — the wrapper must not swallow it).

Pre-fix signatures these pins discriminate against: every site above
propagated the raw ``PermissionError`` — `python -m gf` printed
``Traceback (most recent call last):`` and the failing call line
(``link.unlink()``, ``shutil.move(...)``, ``open(path, "rb")``,
``tempfile.mkstemp(...)``, ``os.unlink(child)``), exiting 1 without the
``gf:`` prefix or any git-folder context.
"""

import contextlib
import os
import subprocess
import tomllib
from pathlib import Path

import pytest
import tomli_w

from conftest import gf, git
from gf import layout, manifest as manifest_mod, shelf, state
from gf.exceptions import GitFoldersError, ValidationError


# ---------------------------------------------------------------------------
# helpers — setup failures use pytest.fail, never AssertionError


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
            f"setup: gf {' '.join(args)} rc={r.returncode}:\n"
            f"{r.stdout}\n{r.stderr}")
    return r


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", "-q", str(parent), cwd=tmp_path)
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


def _repo_key_dir(parent: Path) -> Path:
    wt = parent / ".gf" / "wt"
    entries = [p for p in wt.iterdir() if p.is_dir()]
    if len(entries) != 1:
        pytest.fail(f"setup: expected one repo store, found {entries}")
    return entries[0]


@contextlib.contextmanager
def _locked(path: Path, mode: int):
    """Hold `path` at `mode` for the block, restoring its mode after."""
    old = path.stat().st_mode & 0o777
    path.chmod(mode)
    try:
        yield
    finally:
        path.chmod(old)


def _assert_gf_error(r, *needles: str) -> None:
    """The wrapped-error envelope: nonzero rc, a `gf:`-prefixed error
    line, no traceback, and every operation/path needle present.

    Commands that stream git output (pull's fetch/checkout) may emit
    warning/progress lines before the error — the `gf:` line is
    asserted per-line, not at offset 0."""
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert any(line.startswith("gf: ") for line in r.stderr.splitlines()), \
        r.stderr
    assert "Traceback" not in r.stderr
    for needle in needles:
        assert needle in r.stderr, (needle, r.stderr)


# ---------------------------------------------------------------------------
# shelf.ensure_consumer_link — pull retarget unlink arm


def test_pull_retarget_link_unlink_eacces_names_folder(tmp_path):
    """A binding whose effective url moved to a different repo retargets
    its consumer link on `gf pull`; an EACCES on the retarget unlink
    dies inside the folder envelope — pre-fix it was a raw
    PermissionError traceback at `link.unlink()`."""
    up_a = _upstream(tmp_path, "up_a", {"docs/api/x.txt": "a"})
    up_b = _upstream(tmp_path, "up_b", {"docs/api/x.txt": "b"})
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone",
              str(up_a / "docs" / "api"), "vendor/api")
    link = parent / "vendor" / "api"
    if not link.is_symlink():
        pytest.fail(f"setup: {link} is not the consumer link")

    # Move the binding's effective url to a different repo: the pull
    # must create the new store checkout and retarget the link to it —
    # the retarget unlink is the wrapped site.
    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\n'
        f'url = "{up_b}/docs/api"\n')

    with _locked(parent / "vendor", 0o555):
        r = gf("-C", str(parent), "pull", check=False)

    _assert_gf_error(
        r,
        "pull failed for git-folder 'api' (vendor/api)",
        "consumer path",
        str(parent.resolve() / "vendor" / "api"))


# ---------------------------------------------------------------------------
# shelf.remove_child — unlink arm and move arm via `gf rm`


def test_rm_consumer_link_unlink_eacces_names_folder(tmp_path):
    """`gf rm` of a consumer link whose containing directory is
    unwritable dies inside the folder envelope — pre-fix a raw
    PermissionError traceback at `child.unlink()`."""
    up = _upstream(tmp_path, "up", {"docs/api/x.txt": "x"})
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone",
              str(up / "docs" / "api"), "vendor/api")
    if not (parent / "vendor" / "api").is_symlink():
        pytest.fail("setup: vendor/api is not the consumer link")

    with _locked(parent / "vendor", 0o555):
        r = gf("-C", str(parent), "rm", "vendor/api", check=False)

    _assert_gf_error(
        r,
        "rm failed for git-folder 'api' (vendor/api)",
        "cannot remove consumer link",
        str(parent.resolve() / "vendor" / "api"))


def test_rm_whole_repo_move_eacces_names_folder(tmp_path):
    """`gf rm` of a whole-repo child moves `.gf/git` to `.git`; an
    EACCES on the rename (unwritable child dir) dies inside the folder
    envelope — pre-fix a raw PermissionError traceback at
    `shutil.move(...)`."""
    up = _upstream(tmp_path, "up", {"x.txt": "x"})
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up), "kid")
    child = parent / "kid"
    if not (child / ".gf" / "git").is_dir():
        pytest.fail("setup: kid/.gf/git missing")

    with _locked(child, 0o555):
        r = gf("-C", str(parent), "rm", "kid", check=False)

    resolved = child.resolve()
    _assert_gf_error(
        r,
        "rm failed for git-folder 'kid' (kid)",
        "cannot move",
        str(resolved / ".gf" / "git"),
        str(resolved / ".git"))


# ---------------------------------------------------------------------------
# state.load_checkout — unreadable `.gf/wt/<key>/.<checkout>.state`


def test_pull_unreadable_checkout_state_names_folder(tmp_path):
    """`gf pull` reads the shared checkout's state during planning; an
    unreadable `.gf/wt/<key>/.master.state` dies inside the folder
    envelope — pre-fix a raw PermissionError traceback at
    `open(path, "rb")`."""
    up = _upstream(tmp_path, "up", {"docs/api/x.txt": "x"})
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone",
              str(up / "docs" / "api"), "vendor/api")
    state_file = _repo_key_dir(parent) / ".master.state"
    if not state_file.is_file():
        pytest.fail(f"setup: expected checkout state at {state_file}")

    with _locked(state_file, 0o000):
        r = gf("-C", str(parent), "pull", check=False)

    _assert_gf_error(
        r,
        "pull failed for git-folder 'api' (vendor/api)",
        "cannot read git-folders state at",
        str(state_file.resolve()))


# ---------------------------------------------------------------------------
# manifest.write_manifest — mkstemp arm via `gf rm` on a locked root


def test_rm_unwritable_root_manifest_write_names_file(tmp_path):
    """`gf rm` rewrites gf.toml after removing the child; an unwritable
    parent root fails the manifest mkstemp — post-fix a clean `gf:`
    error naming the manifest, pre-fix a raw PermissionError traceback
    at `tempfile.mkstemp(...)`."""
    up = _upstream(tmp_path, "up", {"x.txt": "x"})
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up), "kid")

    # The parent root is the mkstemp dir; `kid`'s own subtree stays
    # writable so remove_child completes and the failure isolates at
    # write_manifest.
    with _locked(parent, 0o555):
        r = gf("-C", str(parent), "rm", "kid", check=False)

    _assert_gf_error(
        r,
        "cannot write git-folders manifest at",
        str(parent.resolve() / "gf.toml"))
    # rm's removal still landed — only the manifest write failed.
    assert (parent / "kid" / ".git" / "HEAD").is_file()
    assert not (parent / "kid" / ".gf").exists()


# ---------------------------------------------------------------------------
# cli.main trailing `except OSError` — the completeness net


def test_worktree_remove_sweep_unlink_dies_via_main_net(tmp_path):
    """`gf worktree remove` unlinks the worktree's git-folder links
    before `git worktree remove` — that `os.unlink` sweep is outside the
    wrapped sites, so its EACCES reaches only main()'s trailing OSError
    catch: a bare `gf:` message (no folder envelope), rc 1, no
    traceback. Pre-fix this printed a raw PermissionError traceback."""
    up = _upstream(tmp_path, "up", {"x.txt": "x"})
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up), "vendor/lib")
    # This witness needs a clean target to reach the unlink failure;
    # copied uncommitted manifest edits are protected earlier by gf.
    git("add", "gf.toml", cwd=parent)
    git("commit", "-qm", "record git-folder", cwd=parent)
    wt2 = tmp_path / "wt2"
    _setup_gf("-C", str(parent), "worktree", "add", str(wt2),
              "-b", "feature")
    link = wt2 / "vendor" / "lib"
    if not link.is_symlink():
        pytest.fail(f"setup: {link} is not the worktree link")

    with _locked(wt2 / "vendor", 0o555):
        r = gf("-C", str(parent), "worktree", "remove", str(wt2),
               check=False)

    _assert_gf_error(r, str(wt2.resolve() / "vendor" / "lib"))
    # The sweep aborts before `git worktree remove`: the worktree and
    # the guarded link are untouched.
    assert link.is_symlink()


# ---------------------------------------------------------------------------
# unit pins — save_checkout / write_manifest arms chmod cannot isolate


class TestSaveCheckoutUnit:
    def test_mkstemp_eacces_wraps_as_state_error(self, tmp_path):
        """save_checkout's create-block (mkdir/mkstemp) wraps OSError;
        a locked state dir forces the mkstemp arm."""
        child = tmp_path / "child"
        (child / ".gf").mkdir(parents=True)
        co = layout.whole_repo_checkout(child)

        with _locked(child / ".gf", 0o555):
            with pytest.raises(GitFoldersError) as ei:
                state.save_checkout(co, {})

        assert "cannot write git-folders state at" in str(ei.value)
        assert str(co.state) in str(ei.value)

    def test_replace_eacces_wraps_as_state_error(
            self, tmp_path, monkeypatch):
        """save_checkout's inner os.replace arm wraps OSError through
        the cleanup path (the temp file is still unlinked)."""
        child = tmp_path / "child"
        child.mkdir()
        co = layout.whole_repo_checkout(child)

        def boom(src, dst):
            raise PermissionError(13, "Permission denied", dst)

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(GitFoldersError) as ei:
            state.save_checkout(co, {})

        assert "cannot write git-folders state at" in str(ei.value)
        assert str(co.state) in str(ei.value)
        assert [p for p in co.state.parent.iterdir()
                if p.name.endswith(".tmp")] == []

    def test_non_oserror_reraises_raw(self, tmp_path, monkeypatch):
        """A non-OSError from the write block is NOT wrapped — it
        re-raises raw so only fs failures get the envelope."""
        child = tmp_path / "child"
        child.mkdir()
        co = layout.whole_repo_checkout(child)

        def boom(data, f):
            raise RuntimeError("dump exploded")

        monkeypatch.setattr(tomli_w, "dump", boom)
        with pytest.raises(RuntimeError, match="dump exploded"):
            state.save_checkout(co, {})


class TestWriteManifestUnit:
    def test_replace_eacces_wraps_as_manifest_error(
            self, tmp_path, monkeypatch):
        """write_manifest's inner os.replace arm wraps OSError through
        the cleanup path."""
        parent = tmp_path / "parent"
        parent.mkdir()

        def boom(src, dst):
            raise PermissionError(13, "Permission denied", dst)

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.write_manifest(parent, {"git_folder": []})

        assert "cannot write git-folders manifest at" in str(ei.value)
        assert str(parent / "gf.toml") in str(ei.value)

    def test_non_oserror_reraises_raw(self, tmp_path, monkeypatch):
        """A non-OSError from the dump is NOT wrapped — it re-raises
        raw (pre- and post-fix behavior alike)."""
        parent = tmp_path / "parent"
        parent.mkdir()

        def boom(data, f):
            raise RuntimeError("dump exploded")

        monkeypatch.setattr(tomli_w, "dump", boom)
        with pytest.raises(RuntimeError, match="dump exploded"):
            manifest_mod.write_manifest(parent, {"git_folder": []})


def test_rollback_consumer_link_unlink_eacces(tmp_path):
    """Unit pin: rollback_consumer_link wraps its unlink/symlink/mkdir
    OSError as `cannot restore consumer path <link>` — pre-fix the raw
    PermissionError escaped."""
    link_dir = tmp_path / "vendor"
    link_dir.mkdir()
    link = link_dir / "api"
    link.symlink_to(tmp_path, target_is_directory=True)

    with _locked(link_dir, 0o555):
        with pytest.raises(ValidationError) as ei:
            shelf.rollback_consumer_link(link, ("retargeted", "old"))

    assert "cannot restore consumer path" in str(ei.value)
    assert str(link) in str(ei.value)


# ---------------------------------------------------------------------------
# controls — unaffected behavior


def test_rm_success_unchanged(tmp_path):
    """Control: an ordinary `gf rm` of a whole-repo child still
    unregisters it, moves `.gf/git` to `.git`, and rewrites the
    manifest."""
    up = _upstream(tmp_path, "up", {"x.txt": "x"})
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up), "kid")

    r = gf("-C", str(parent), "rm", "kid")

    assert r.returncode == 0
    assert "Removed kid" in r.stdout
    assert r.stderr == ""
    assert (parent / "kid" / ".git" / "HEAD").is_file()
    assert not (parent / "kid" / ".gf").exists()
    with open(parent / "gf.toml", "rb") as f:
        assert tomllib.load(f)["git_folder"] == []
