# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Non-UTF-8 bytes → envelope pins: UnicodeDecodeError never tracebacks.

`tomllib.load` decodes a file's bytes as UTF-8 BEFORE TOML parsing, so
a `gf.toml` / `gf.local.toml` / checkout `.state` file — or a
pyproject.toml the `--version` probe reads — carrying non-UTF-8 bytes
raises `UnicodeDecodeError`, a ValueError, NOT a TOMLDecodeError.
Before the fix under test only TOMLDecodeError was caught at every
read site, so a non-UTF-8 byte escaped each envelope as a raw
traceback:

- `manifest.read_manifest` / `read_local_overrides`: `gf status`,
  `gf pull`, `gf rm --all`, `gf ls` died with a UnicodeDecodeError
  traceback instead of the `corrupt git-folders manifest` envelope.
- `state.load_checkout`: `gf pull` on a non-UTF-8
  `.gf/wt/<repo-key>/.<key>.state` tracebacked mid-planning instead of
  the `pull failed for git-folder ...: corrupt git-folders state`
  envelope.
- `shelf.pull_shared_bindings`' `owning_folders` scan and
  `_vacate_checkout`'s owning-manifest re-read: a non-UTF-8
  OWNING-root `gf.toml` tracebacked a pull driven through a linked
  worktree — the invocation reads wt2's own manifest copy, so the
  pull itself is valid (the F3 stale-worktree harness, reused below).
- `cli._get_version`'s pyproject probe leaked it through its
  `(OSError, TOMLDecodeError)` catch.

The payload throughout is valid content plus a lone 0xe9 byte: 0xe9
opens a three-byte UTF-8 sequence the following bytes cannot complete,
so the decode fails before TOML sees a token — the same surface the
malformed-but-UTF-8 TOML pins already get, never a `Traceback`.

Pre-fix signatures (verified by stashing the worktree diff on base
85856af): the discriminating arms exit 1 with `Traceback` and
`UnicodeDecodeError` in stderr — the worktree pull dies at the
`owning_folders` read before any checkout work. The control arms pass
both ways: the TOMLDecodeError envelope predates this fix.
"""

import importlib.metadata
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from conftest import gf, git, push_branch
from gf import cli, layout, manifest as manifest_mod, state as state_mod
from gf.exceptions import GitFoldersError


# A lone 0xe9 byte: invalid UTF-8 in any position — appended to valid
# content so the payload proves even a well-formed prefix does not
# reach the parser.
_NON_UTF8 = b"x \xe9\n"

_VALID_MANIFEST = (
    '[[git_folder]]\nname = "lib"\nurl = "u"\n'
    'ref = "latest"\npath = "vendor/lib"\n')


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
    """A real parent git repo with one commit and no gf.toml."""
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path) -> Path:
    """Bare upstream with `docs/api` + `tools` subdirs on master —
    the subfolder-binding source the state/worktree pins clone."""
    up = tmp_path / "upstream"
    _git("init", "-q", "--bare", str(up))
    work = tmp_path / "_seed_upstream"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text("api on master")
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text("tool in upstream")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")
    return up


def _assert_corrupt_manifest_envelope(r, manifest_path: Path) -> None:
    """rc 1 + `gf: corrupt git-folders manifest at <path>: ...` — the
    GitFoldersError surface naming the file, no traceback leak."""
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert "UnicodeDecodeError" not in r.stderr
    assert "TOMLDecodeError" not in r.stderr
    assert r.stderr.startswith("gf: "), r.stderr
    assert "corrupt git-folders manifest at " in r.stderr
    m = re.search(r"git-folders manifest at (\S+?):", r.stderr)
    assert m, r.stderr
    # The printed path is `<resolved parent>/<name>` — resolve the
    # directory but keep the leaf literal so a symlinked file still
    # compares equal.
    assert Path(m.group(1)) == \
        manifest_path.parent.resolve() / manifest_path.name


# ---------------------------------------------------------------------------
# unit pins — each wrapped read site converts UnicodeDecodeError into
# the same GitFoldersError envelope a TOMLDecodeError gets


class TestUnitDecodeEnvelope:
    def test_read_manifest_non_utf8(self, tmp_path):
        path = tmp_path / "gf.toml"
        path.write_bytes(_VALID_MANIFEST.encode() + _NON_UTF8)
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_manifest(tmp_path)
        assert f"corrupt git-folders manifest at {path}" in str(ei.value)

    def test_read_local_overrides_non_utf8(self, tmp_path):
        path = tmp_path / "gf.local.toml"
        path.write_bytes(b'ref = "dev"\n' + _NON_UTF8)
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_local_overrides(tmp_path)
        assert f"corrupt git-folders manifest at {path}" in str(ei.value)

    def test_load_checkout_non_utf8(self, tmp_path):
        co = layout.whole_repo_checkout(tmp_path / "child")
        co.state.parent.mkdir(parents=True)
        co.state.write_bytes(b'resolved = "abc"\n' + _NON_UTF8)
        with pytest.raises(GitFoldersError) as ei:
            state_mod.load_checkout(co)
        assert f"corrupt git-folders state at {co.state}" in \
            str(ei.value)
        assert "remove it" in str(ei.value)


def _no_dist(name: str):
    raise importlib.metadata.PackageNotFoundError(name)


class TestVersionProbeDecode:
    """`_get_version`'s pyproject fallback probe runs before main()'s
    try — a non-UTF-8 pyproject.toml must fall through to the built-in
    default, not traceback `gf --version`."""

    def test_non_utf8_pyproject_falls_back(self, tmp_path, monkeypatch):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "pyproject.toml").write_bytes(
            b'[project]\nversion = "9.9.9"\n' + _NON_UTF8)
        monkeypatch.setattr(importlib.metadata, "version", _no_dist)
        monkeypatch.setattr(cli, "__file__", str(pkg / "cli.py"))

        assert cli._get_version() == "0.1.0"

    def test_valid_pyproject_is_read(self, tmp_path, monkeypatch):
        """Control: a valid pyproject's version IS returned — the
        fallback above is reached through the decode failure, not by
        the probe never reading the file."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "pyproject.toml").write_bytes(
            b'[project]\nversion = "9.9.9"\n')
        monkeypatch.setattr(importlib.metadata, "version", _no_dist)
        monkeypatch.setattr(cli, "__file__", str(pkg / "cli.py"))

        assert cli._get_version() == "9.9.9"


# ---------------------------------------------------------------------------
# subprocess pins — the command envelope


class TestNonUtf8ManifestEnvelope:
    """A non-UTF-8 gf.toml fails every command that reaches the manifest
    read with the clean `gf: corrupt git-folders manifest` error — rc 1,
    the file named, no UnicodeDecodeError traceback."""

    @pytest.mark.parametrize("args", [
        ["status"], ["pull"], ["rm", "--all"], ["ls"],
    ])
    def test_non_utf8_manifest_dies_naming_file(self, tmp_path, args):
        parent = _parent(tmp_path)
        (parent / "gf.toml").write_bytes(
            _VALID_MANIFEST.encode() + _NON_UTF8)

        r = gf("-C", str(parent), *args, check=False)

        _assert_corrupt_manifest_envelope(r, parent / "gf.toml")


class TestNonUtf8LocalOverrideEnvelope:
    """gf.local.toml gets the same envelope from the commands that read
    it (status, pull). `gf ls` never reads it — the control pins that a
    non-UTF-8 override does not break ls."""

    @pytest.mark.parametrize("args", [["status"], ["pull"]])
    def test_non_utf8_local_dies_naming_file(self, tmp_path, args):
        parent = _parent(tmp_path)
        (parent / "gf.toml").write_text(_VALID_MANIFEST)
        (parent / "gf.local.toml").write_bytes(
            b'ref = "dev"\n' + _NON_UTF8)

        r = gf("-C", str(parent), *args, check=False)

        _assert_corrupt_manifest_envelope(r, parent / "gf.local.toml")

    def test_non_utf8_local_does_not_break_ls(self, tmp_path):
        """Control: `gf ls` never consults gf.local.toml."""
        parent = _parent(tmp_path)
        (parent / "gf.toml").write_text("git_folder = []\n")
        (parent / "gf.local.toml").write_bytes(
            b'ref = "dev"\n' + _NON_UTF8)

        r = gf("-C", str(parent), "ls", check=False)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "Traceback" not in r.stderr


def test_pull_non_utf8_checkout_state_reports_clean_error(tmp_path):
    """A subfolder binding's non-UTF-8 `.gf/wt/<rk>/.master.state` fails
    `gf pull` inside the folder envelope — `pull failed for git-folder
    'api' (vendor/api): corrupt git-folders state at <path>` (rc 1),
    not a UnicodeDecodeError traceback. The error's own prescribed
    remedy — remove the record — recovers the same pull."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up / "docs" / "api"),
              "vendor/api")

    states = list((parent / ".gf" / "wt").glob("*/.*.state"))
    if len(states) != 1:
        pytest.fail(f"setup: expected one checkout state, found {states}")
    state_file = states[0]
    state_file.write_bytes(state_file.read_bytes() + _NON_UTF8)

    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert "UnicodeDecodeError" not in r.stderr
    assert "TOMLDecodeError" not in r.stderr
    assert "pull failed for git-folder 'api' (vendor/api)" in r.stderr
    m = re.search(r"corrupt git-folders state at (\S+?):", r.stderr)
    assert m, r.stderr
    assert Path(m.group(1)) == state_file.resolve()
    assert "remove it" in r.stderr

    # Recovery per the error's instruction: remove the corrupt record.
    state_file.unlink()
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    with open(state_file, "rb") as f:
        tomllib.load(f)                     # rewritten record parses


# ---------------------------------------------------------------------------
# degrade arm — non-UTF-8 OWNING-root manifest during a pull driven
# through a linked worktree (the F3 harness from test_worktree_links):
# the `owning_folders` scan degrades to no served siblings and
# `_vacate_checkout` skips pruning rather than tracebacking the pull.


def _store_names(root: Path) -> list[str]:
    """Sorted repo-key dirs under `<root>/.gf/repos` (empty when absent)."""
    repos = root / ".gf" / "repos"
    if not repos.is_dir():
        return []
    return sorted(p.name for p in repos.iterdir() if p.is_dir())


def _checkout_state(root: Path, repo_key: str, key: str) -> dict:
    """The checkout record `.gf/wt/<repo-key>/.<key>.state` under `root`."""
    return tomllib.loads(
        (root / ".gf" / "wt" / repo_key / f".{key}.state").read_text())


def _push_lib_subdir(up: Path, tmp_path: Path) -> None:
    """Push `libs/lib/l.txt` to upstream master — the third subdir the
    post-snapshot `gf clone <up>/libs/lib vendor/lib` binds."""
    work = tmp_path / "_lib_seed"
    _git("clone", str(up), str(work))
    (work / "libs" / "lib").mkdir(parents=True)
    (work / "libs" / "lib" / "l.txt").write_text("lib payload\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "lib subdir")
    _git("-C", work, "push", "origin", "master")


def _stale_worktree_setup(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """`parent` serving api+tools+lib from ONE `master` checkout, where
    `vendor/lib` was cloned in AFTER `worktree add wt2` snapshotted the
    manifest — wt2's gf.toml copy lists api+tools only. Returns
    (upstream, parent, wt2, repo_key)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up / "docs" / "api"),
              "vendor/api")
    _setup_gf("-C", str(parent), "clone", str(up / "tools"),
              "vendor/tools", "-b", "master")
    wt2 = tmp_path / "wt2"
    _setup_gf("-C", str(parent), "worktree", "add", str(wt2))

    # the source manifest gains a third binding on the same checkout —
    # after wt2's snapshot was taken
    _push_lib_subdir(up, tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up / "libs" / "lib"),
              "vendor/lib")

    rk = _store_names(parent)
    if len(rk) != 1:
        pytest.fail(f"setup: expected one repo store, found {rk}")
    st = _checkout_state(parent, rk[0], "master")
    if set(st["bindings"]) != {"docs/api", "libs/lib", "tools"} or \
            set(st["binding_urls"]) != {
                "vendor/api", "vendor/tools", "vendor/lib"}:
        pytest.fail(f"setup: master record not fully bound: {st}")

    # the setup premise: wt2's copied manifest predates `vendor/lib`,
    # and no lib consumer link was ever placed inside wt2
    copied = tomllib.loads((wt2 / "gf.toml").read_text())
    if [f["path"] for f in copied["git_folder"]] != [
            "vendor/api", "vendor/tools"]:
        pytest.fail(f"setup: wt2 manifest snapshot not stale: {copied}")
    if os.path.lexists(wt2 / "vendor" / "lib"):
        pytest.fail("setup: wt2 unexpectedly has a lib consumer link")
    return up, parent, wt2, rk[0]


def _wt2_retarget_pull(wt2: Path) -> subprocess.CompletedProcess:
    """`gf -C wt2 pull` under wt2's own `api → dev` override — retargets
    `vendor/api` off the shared `master` checkout, vacating it."""
    (wt2 / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "dev"\n')
    return gf("-C", str(wt2), "pull", check=False)


def test_pull_via_worktree_non_utf8_owning_manifest_keeps_record(
        tmp_path):
    """Non-UTF-8 owning-manifest arm — same topology as the F3 corrupt
    arm, but the OWNING root's `gf.toml` fails the UTF-8 decode rather
    than the TOML parse. The pull must still complete rc 0: the owning
    manifest drives only the cosmetic `(moved with ...)` lines and the
    vacate prune, so its UnicodeDecodeError degrades to no-siblings /
    no-prune — it must not abort a pull whose inputs (wt2's manifest
    copy, the recorded state) are all readable.

    Pre-fix signature (verified): `read_manifest` on the non-UTF-8
    `gf.toml` escapes `pull_shared_bindings` — rc=1, `Traceback` ending
    in `UnicodeDecodeError`, raised at the `owning_folders` read before
    any checkout is touched."""
    up, parent, wt2, rk = _stale_worktree_setup(tmp_path)
    push_branch(up, "dev", "dev content")
    # valid manifest bytes stay a well-formed prefix — the appended
    # 0xe9 line is what fails the decode
    with open(parent / "gf.toml", "ab") as f:
        f.write(_NON_UTF8)

    r = _wt2_retarget_pull(wt2)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert "UnicodeDecodeError" not in r.stderr
    assert "Pulled api" in r.stdout
    assert "Pulled tools" in r.stdout
    # the destroyed owning manifest cannot name served siblings — the
    # degrade is cosmetic lines only, never a wipe
    assert "(moved with" not in r.stdout

    # the vacated `master` record kept every pre-pull entry — the moved
    # `api` included — rather than being emptied against no manifest
    st = _checkout_state(parent, rk, "master")
    assert set(st["bindings"]) == {"docs/api", "libs/lib", "tools"}
    assert set(st["binding_urls"]) == {
        "vendor/api", "vendor/tools", "vendor/lib"}

    # the pull itself still did its work: api retargeted into the new
    # `dev` checkout, reachable through wt2's chained link
    dev_wt = parent / ".gf" / "wt" / rk / "dev"
    assert (parent / "vendor" / "api").resolve() == (
        dev_wt / "docs" / "api").resolve()
    assert (wt2 / "vendor" / "api" / "x.txt").is_file()
    dev = _checkout_state(parent, rk, "dev")
    assert set(dev["bindings"]) == {"docs/api"}
    assert set(dev["binding_urls"]) == {"vendor/api"}

    # the pin: the still-linked sibling wt2's manifest never knew stays
    # materialized — the record the cone union reads was not wiped
    assert (parent / "vendor" / "lib" / "l.txt").read_text() == \
        "lib payload\n"
    assert (parent / "vendor" / "tools" / "t.txt").read_text().strip() == \
        "tool in upstream"


# ---------------------------------------------------------------------------
# controls — valid-UTF-8 malformed TOML keeps the corrupt envelope
# (the TOMLDecodeError arm predates this fix); valid files unaffected


class TestDecodeControls:
    @pytest.mark.parametrize("args", [
        ["status"], ["pull"], ["rm", "--all"], ["ls"],
    ])
    def test_valid_utf8_corrupt_manifest_dies_naming_file(
            self, tmp_path, args):
        """Malformed-but-UTF-8 TOML still gets `corrupt` — this arm is
        unchanged by the fix and must not regress."""
        parent = _parent(tmp_path)
        (parent / "gf.toml").write_text("git_folder = [[[ broken\n")

        r = gf("-C", str(parent), *args, check=False)

        _assert_corrupt_manifest_envelope(r, parent / "gf.toml")

    @pytest.mark.parametrize("args", [
        ["status"], ["pull"], ["rm", "--all"], ["ls"],
    ])
    def test_valid_files_quiet_success(self, tmp_path, args):
        """Valid manifest + valid override: read commands stay a quiet
        rc 0 over an empty binding set."""
        parent = _parent(tmp_path)
        (parent / "gf.toml").write_text("git_folder = []\n")
        (parent / "gf.local.toml").write_text(
            '[[git_folder_override]]\nname = "lib"\nref = "dev"\n')

        r = gf("-C", str(parent), *args)
        assert r.returncode == 0
        assert r.stderr == ""
