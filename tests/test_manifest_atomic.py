# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""F4b — manifest writes are atomic and never follow a symlink.

`manifest.write_manifest` (used by `gf clone`/`gf init`/`gf rm`) writes
via ``tempfile.mkstemp(dir=parent_root)`` + ``tomli_w.dump`` +
``os.replace``, mirroring ``state.save_checkout``: a torn write never
reaches the manifest, and the replace swaps the ``gf.toml`` NAME rather
than writing through a committed symlink planted at that path.
``cmd_rm``'s inline atomic-write block was deleted and delegates to
``write_manifest`` with unchanged observable behavior.

Pins (mirroring the ``state.save_checkout`` atomic-write pins in
test_state.py):

- write-through: with a committed ``gf.toml -> <abs>/victim.txt``
  symlink, ``gf clone`` replaces the NAME — the victim stays
  byte-identical and ``gf.toml`` becomes a regular file holding a valid
  manifest. Real git only: MockGitBackend can't model a committed
  symlink.
- atomicity: the prior manifest is served while ``tomli_w.dump`` runs;
  an injected dump failure propagates, leaves the original
  byte-identical, and leaves zero ``*.tmp`` litter.
- delegation: ``gf rm <kid>`` on a multi-binding manifest writes a
  valid manifest without the removed entry; ``gf rm --all`` leaves
  ``git_folder = []`` (cmd_rm already wrote atomically inline — these
  pins hold pre- and post-fix).
- control: a normal clone writes a parseable ``gf.toml``.
"""

import tomllib
from pathlib import Path

import pytest
import tomli_w

from conftest import gf, git, push_commit
from gf import manifest as manifest_mod

MANIFEST = "gf.toml"

_FOLDER_A = {
    "name": "a",
    "url": "https://example.test/a.git",
    "ref": "latest",
    "path": "a",
}
_FOLDER_B = {
    "name": "b",
    "url": "https://example.test/b.git",
    "ref": "latest",
    "path": "b",
}


def _tmp_litter(dirpath: Path) -> list[str]:
    """mkstemp leftovers: write_manifest temps end in `.tmp`."""
    return sorted(p.name for p in dirpath.iterdir()
                  if p.name.endswith(".tmp"))


def _read_manifest(parent: Path) -> dict:
    with open(parent / MANIFEST, "rb") as f:
        return tomllib.load(f)


def _upstream(tmp_path: Path, name: str = "upstream") -> Path:
    up = tmp_path / name
    up.mkdir()
    git("init", "--bare", cwd=up)
    push_commit(up, "init", "hello")
    return up


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


# ---------------------------------------------------------------------------
# unit pins — write_manifest itself


def test_write_manifest_writes_parseable_manifest_no_litter(tmp_path):
    """Unit pin: a write produces a complete parseable manifest and
    leaves no temp litter in the parent root."""
    parent = tmp_path / "parent"
    parent.mkdir()
    manifest_mod.write_manifest(parent, {"git_folder": [_FOLDER_A]})

    assert _read_manifest(parent) == {"git_folder": [_FOLDER_A]}
    assert _tmp_litter(parent) == []


def test_write_manifest_prior_content_stays_visible_during_dump(
        tmp_path, monkeypatch):
    """os.replace semantics: while tomli_w.dump is mid-write the
    manifest path still serves the PRIOR complete manifest — new bytes
    land only at the atomic replace (pre-fix this wrote in place: a
    reader at dump time observed the already-truncated file)."""
    parent = tmp_path / "parent"
    parent.mkdir()
    manifest_mod.write_manifest(parent, {"git_folder": [_FOLDER_A]})
    old_bytes = (parent / MANIFEST).read_bytes()

    observed: list[bytes] = []
    real_dump = tomli_w.dump

    def spy(data, f):
        observed.append((parent / MANIFEST).read_bytes())
        return real_dump(data, f)

    monkeypatch.setattr(tomli_w, "dump", spy)

    new = {"git_folder": [_FOLDER_A, _FOLDER_B]}
    manifest_mod.write_manifest(parent, new)

    assert observed == [old_bytes]
    assert _read_manifest(parent) == new
    assert _tmp_litter(parent) == []


def test_write_manifest_failed_dump_preserves_manifest_no_tmp_litter(
        tmp_path, monkeypatch):
    """A dump that fails mid-write propagates, leaves the prior manifest
    byte-identical, and unlinks the temp file (pre-fix the write
    truncated `gf.toml` in place, leaving the torn partial content)."""
    parent = tmp_path / "parent"
    parent.mkdir()
    manifest_mod.write_manifest(parent, {"git_folder": [_FOLDER_A]})
    old_bytes = (parent / MANIFEST).read_bytes()

    def boom(data, f):
        f.write(b"torn partial write [[[")
        raise RuntimeError("dump exploded")

    monkeypatch.setattr(tomli_w, "dump", boom)

    with pytest.raises(RuntimeError, match="dump exploded"):
        manifest_mod.write_manifest(parent, {"git_folder": [_FOLDER_B]})

    assert (parent / MANIFEST).read_bytes() == old_bytes
    assert _tmp_litter(parent) == []


# ---------------------------------------------------------------------------
# write-through pin — real git (committed symlink)


def test_clone_replaces_gf_toml_symlink_instead_of_writing_through(
        tmp_path):
    """A committed `gf.toml` symlink must never be written through:
    `gf clone` replaces the NAME via os.replace, so the symlink target
    keeps its bytes and `gf.toml` becomes a regular file holding the
    new manifest. Pre-fix, `write_manifest` opened `gf.toml` directly
    and the write followed the link into the victim file.

    The victim holds comment-only TOML so the pre-write manifest read
    still succeeds — isolating the claim to the write itself."""
    upstream = _upstream(tmp_path)
    parent = _parent(tmp_path)

    victim = tmp_path / "victim.txt"
    victim_bytes = (
        b"# victim bytes -- gf must never write through a gf.toml "
        b"symlink\n"
    )
    victim.write_bytes(victim_bytes)
    (parent / MANIFEST).symlink_to(victim)
    git("add", MANIFEST, cwd=parent)
    git("commit", "-m", "plant gf.toml symlink", cwd=parent)
    assert (parent / MANIFEST).is_symlink()

    r = gf("-C", str(parent), "clone", str(upstream), "kid")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    assert victim.read_bytes() == victim_bytes
    link = parent / MANIFEST
    assert link.is_file() and not link.is_symlink()
    [entry] = _read_manifest(parent)["git_folder"]
    assert entry["name"] == "kid"
    assert entry["url"] == str(upstream)
    assert entry["ref"] == "latest"
    assert entry["path"] == "kid"
    assert _tmp_litter(parent) == []


# ---------------------------------------------------------------------------
# delegation pins — cmd_rm routes through write_manifest


def test_rm_writes_valid_manifest_without_removed_binding(tmp_path):
    """`gf rm <kid>` on a two-binding manifest leaves a parseable
    manifest carrying only the surviving entry (behavior unchanged by
    the delegation — cmd_rm already wrote atomically inline)."""
    up_a = _upstream(tmp_path, "up_a")
    up_b = _upstream(tmp_path, "up_b")
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up_a), "kid_a")
    gf("-C", str(parent), "clone", str(up_b), "kid_b")

    gf("-C", str(parent), "rm", "kid_a")

    [surviving] = _read_manifest(parent)["git_folder"]
    assert surviving["name"] == "kid_b"
    assert surviving["url"] == str(up_b)
    assert surviving["ref"] == "latest"
    assert surviving["path"] == "kid_b"
    assert _tmp_litter(parent) == []
    # rm unregisters — the removed child keeps its files.
    assert (parent / "kid_a" / ".git" / "HEAD").is_file()
    assert not (parent / "kid_a" / ".gf").exists()


def test_rm_all_leaves_empty_git_folder(tmp_path):
    """`gf rm --all` removes every binding and writes a valid manifest
    whose git_folder table is empty."""
    up_a = _upstream(tmp_path, "up_a")
    up_b = _upstream(tmp_path, "up_b")
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up_a), "kid_a")
    gf("-C", str(parent), "clone", str(up_b), "kid_b")

    gf("-C", str(parent), "rm", "--all")

    assert _read_manifest(parent)["git_folder"] == []
    assert _tmp_litter(parent) == []


# ---------------------------------------------------------------------------
# control — normal clone


def test_clone_writes_parseable_manifest(tmp_path):
    """Control: an ordinary clone writes a `gf.toml` that parses and
    carries the new binding."""
    upstream = _upstream(tmp_path)
    parent = _parent(tmp_path)

    gf("-C", str(parent), "clone", str(upstream), "kid")

    [entry] = _read_manifest(parent)["git_folder"]
    assert entry["name"] == "kid"
    assert entry["url"] == str(upstream)
    assert entry["ref"] == "latest"
    assert entry["path"] == "kid"
    assert _tmp_litter(parent) == []
