# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""F4a pins: manifest reads produce a clean `gf:` error, never a traceback.

The spec's error-handling section: a `gf.toml`/`gf.local.toml` that is
unreadable, invalid TOML, or fails shape validation fails the command
with a clean `gf:` error naming the file and exit 1 — never a Python
traceback. A missing `gf.toml` is not an error: `gf clone`/`gf init`
create it and the other commands treat it as an empty manifest.

Pre-fix signatures these pins discriminate against:

- corrupt TOML: `tomllib.TOMLDecodeError` escaped `read_manifest` as an
  uncaught traceback (the `_resolve`/`_passthrough_target` `is_file()`
  guard passed for a regular file).
- wrong shape: the raw parsed value flowed into shelf/cli — a non-list
  `git_folder` raised TypeError on iteration, a string entry raised
  TypeError on `folder["path"]`, a missing field raised KeyError in
  `effective_url_ref`/`_select_for_path`, and a bad override entry
  raised AttributeError on `o.get(...)`.
- `gf.toml` as a directory (or other non-regular path): `is_file()`
  counted it as absent — `status`/`ls`/`pull`/`rm` exited 0 silently,
  `clone`/`init` created the child and then crashed at `write_manifest`
  with IsADirectoryError, and the passthrough commands died with the
  wrong "not inside" message.
- unit: `read_manifest` on an absent file raised FileNotFoundError
  instead of returning {}.

Commands that never read `gf.local.toml` (`ls`, `rm`, `clone`, `init`)
are unaffected by a corrupt local override — `gf ls` pins that as a
control.
"""

import re
import tomllib
from pathlib import Path

import pytest

from conftest import git, push_commit, gf
from gf import cli, manifest as manifest_mod
from gf.exceptions import GitFoldersError


# ---------------------------------------------------------------------------
# fixtures


def _parent_repo(tmp_path: Path) -> Path:
    """A real parent git repo with one commit and no gf.toml."""
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream_repo(tmp_path: Path) -> Path:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    push_commit(upstream, "init", "hello")
    return upstream


# ---------------------------------------------------------------------------
# envelope assertions


def _assert_clean_manifest_error(r, manifest_path: Path, lead: str):
    """rc 1 + `gf: <lead> git-folders manifest at <path>: ...` — the
    GitFoldersError surface naming the file, no traceback leak."""
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert "TOMLDecodeError" not in r.stderr
    assert r.stderr.startswith("gf: "), r.stderr
    assert f"{lead} git-folders manifest at " in r.stderr
    m = re.search(r"git-folders manifest at (\S+?):", r.stderr)
    assert m, r.stderr
    # The printed path is `<resolved parent>/<name>` — resolve the
    # directory but keep the leaf literal so a symlinked manifest file
    # still compares equal (resolve() would follow it to the target).
    assert Path(m.group(1)) == \
        manifest_path.parent.resolve() / manifest_path.name


# ---------------------------------------------------------------------------
# unit pins — read_manifest / read_local_overrides


class TestReadManifestUnit:
    def test_absent_returns_empty_manifest(self, tmp_path):
        """Absent gf.toml reads as the empty manifest."""
        assert manifest_mod.read_manifest(tmp_path) == {}

    def test_valid_manifest_round_trips(self, tmp_path):
        (tmp_path / "gf.toml").write_text(
            '[[git_folder]]\nname = "lib"\nurl = "u"\n'
            'ref = "latest"\npath = "vendor/lib"\n')
        data = manifest_mod.read_manifest(tmp_path)
        assert data["git_folder"] == [{
            "name": "lib", "url": "u",
            "ref": "latest", "path": "vendor/lib"}]

    @pytest.mark.parametrize("payload", [
        b"not toml [[[ at all",
        b'name = "x"\npath =',               # truncated record
    ])
    def test_corrupt_manifest_raises_named_error(self, tmp_path, payload):
        path = tmp_path / "gf.toml"
        path.write_bytes(payload)
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_manifest(tmp_path)
        assert f"corrupt git-folders manifest at {path}" in str(ei.value)

    def test_manifest_directory_not_regular_file(self, tmp_path):
        (tmp_path / "gf.toml").mkdir()
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_manifest(tmp_path)
        assert f"cannot read git-folders manifest at {tmp_path / 'gf.toml'}" \
            in str(ei.value)
        assert "not a regular file" in str(ei.value)

    def test_manifest_dangling_symlink_not_regular_file(self, tmp_path):
        (tmp_path / "gf.toml").symlink_to(tmp_path / "missing-target")
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_manifest(tmp_path)
        assert "not a regular file" in str(ei.value)

    @pytest.mark.parametrize("value", ["5", '"oops"', '{ name = "x" }'])
    def test_git_folder_not_a_list(self, tmp_path, value):
        path = tmp_path / "gf.toml"
        path.write_text(f"git_folder = {value}\n")
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_manifest(tmp_path)
        assert "'git_folder' must be a list of tables" in str(ei.value)
        assert str(path) in str(ei.value)

    @pytest.mark.parametrize("entry", ['"oops"', "5", "2024-01-01"])
    def test_git_folder_entry_not_a_table(self, tmp_path, entry):
        path = tmp_path / "gf.toml"
        path.write_text(f"git_folder = [{entry}]\n")
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_manifest(tmp_path)
        assert "'git_folder[0]' must be a table" in str(ei.value)
        assert str(path) in str(ei.value)

    _FULL = {
        "name": '"lib"', "url": '"u"',
        "ref": '"latest"', "path": '"vendor/lib"',
    }

    @pytest.mark.parametrize("dropped", ["name", "url", "ref", "path"])
    def test_git_folder_missing_required_key(self, tmp_path, dropped):
        path = tmp_path / "gf.toml"
        fields = {k: v for k, v in self._FULL.items() if k != dropped}
        path.write_text("[[git_folder]]\n" +
                        "\n".join(f"{k} = {v}" for k, v in fields.items()) +
                        "\n")
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_manifest(tmp_path)
        assert (f"'git_folder[0]' is missing required key '{dropped}'"
                in str(ei.value))
        assert str(path) in str(ei.value)

    @pytest.mark.parametrize("field", ["name", "url", "ref", "path"])
    def test_git_folder_non_string_field(self, tmp_path, field):
        path = tmp_path / "gf.toml"
        fields = dict(self._FULL, **{field: "5"})
        path.write_text("[[git_folder]]\n" +
                        "\n".join(f"{k} = {v}" for k, v in fields.items()) +
                        "\n")
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_manifest(tmp_path)
        assert f"'git_folder[0].{field}' must be a string" in str(ei.value)
        assert str(path) in str(ei.value)


class TestReadLocalOverridesUnit:
    def test_absent_returns_empty_list(self, tmp_path):
        assert manifest_mod.read_local_overrides(tmp_path) == []

    def test_override_name_not_required(self, tmp_path):
        """A git_folder_override entry may carry only the fields it
        overrides — the four-key requirement applies to git_folder only."""
        (tmp_path / "gf.local.toml").write_text(
            '[[git_folder_override]]\nref = "feature"\n')
        assert manifest_mod.read_local_overrides(tmp_path) == [
            {"ref": "feature"}]

    @pytest.mark.parametrize("payload", [
        b"override [[[ broken",
        b'ref =',
    ])
    def test_corrupt_local_raises_named_error(self, tmp_path, payload):
        path = tmp_path / "gf.local.toml"
        path.write_bytes(payload)
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_local_overrides(tmp_path)
        assert f"corrupt git-folders manifest at {path}" in str(ei.value)

    def test_local_directory_not_regular_file(self, tmp_path):
        (tmp_path / "gf.local.toml").mkdir()
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_local_overrides(tmp_path)
        assert "not a regular file" in str(ei.value)
        assert str(tmp_path / "gf.local.toml") in str(ei.value)

    def test_local_dangling_symlink_not_regular_file(self, tmp_path):
        (tmp_path / "gf.local.toml").symlink_to(tmp_path / "missing-target")
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_local_overrides(tmp_path)
        assert "not a regular file" in str(ei.value)

    def test_git_folder_override_not_a_list(self, tmp_path):
        path = tmp_path / "gf.local.toml"
        path.write_text('git_folder_override = "oops"\n')
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_local_overrides(tmp_path)
        assert "'git_folder_override' must be a list of tables" \
            in str(ei.value)
        assert str(path) in str(ei.value)

    def test_git_folder_override_entry_not_a_table(self, tmp_path):
        path = tmp_path / "gf.local.toml"
        path.write_text('git_folder_override = ["oops"]\n')
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_local_overrides(tmp_path)
        assert "'git_folder_override[0]' must be a table" in str(ei.value)
        assert str(path) in str(ei.value)

    def test_git_folder_override_non_string_field(self, tmp_path):
        path = tmp_path / "gf.local.toml"
        path.write_text(
            '[[git_folder_override]]\nname = "lib"\nurl = 5\n')
        with pytest.raises(GitFoldersError) as ei:
            manifest_mod.read_local_overrides(tmp_path)
        assert "'git_folder_override[0].url' must be a string" \
            in str(ei.value)
        assert str(path) in str(ei.value)


# ---------------------------------------------------------------------------
# subprocess pins — the command envelope


_CORRUPT = "git_folder = [[[ broken\n"


class TestCorruptManifestEnvelope:
    """A malformed gf.toml fails every command that reaches `_resolve` —
    or the passthrough target — with the clean corrupt-manifest error."""

    @pytest.mark.parametrize("args", [
        ["status"], ["ls"], ["pull"], ["rm"],
        ["worktree", "list"],
        ["git", "status"], ["diff"], ["log"], ["sh", "-c", "true"],
        ["init", "child"], ["clone", "never-used-url", "vendor/lib"],
    ])
    def test_corrupt_manifest_dies_naming_file(self, tmp_path, args):
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text(_CORRUPT)

        r = gf("-C", str(parent), *args, check=False)

        _assert_clean_manifest_error(r, parent / "gf.toml", "corrupt")

    def test_corrupt_manifest_blocks_side_effects(self, tmp_path):
        """`gf init` dies before creating the child directory."""
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text(_CORRUPT)

        r = gf("-C", str(parent), "init", "child", check=False)

        _assert_clean_manifest_error(r, parent / "gf.toml", "corrupt")
        assert not (parent / "child").exists()


class TestManifestShapeEnvelope:
    """A parseable gf.toml whose git_folder shape is wrong fails with
    'invalid git-folders manifest' naming file and key — not the
    TypeError/KeyError the raw value used to raise inside shelf."""

    @pytest.mark.parametrize("args", [["status"], ["ls"]])
    def test_git_folder_not_a_list(self, tmp_path, args):
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text("git_folder = 5\n")

        r = gf("-C", str(parent), *args, check=False)

        _assert_clean_manifest_error(r, parent / "gf.toml", "invalid")
        assert "'git_folder' must be a list of tables" in r.stderr

    @pytest.mark.parametrize("args", [["status"], ["ls"]])
    def test_git_folder_entry_not_a_table(self, tmp_path, args):
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text('git_folder = ["oops"]\n')

        r = gf("-C", str(parent), *args, check=False)

        _assert_clean_manifest_error(r, parent / "gf.toml", "invalid")
        assert "'git_folder[0]' must be a table" in r.stderr

    @pytest.mark.parametrize("args", [["status"], ["ls"]])
    def test_git_folder_path_not_a_string(self, tmp_path, args):
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text(
            '[[git_folder]]\nname = "lib"\nurl = "u"\n'
            'ref = "latest"\npath = 5\n')

        r = gf("-C", str(parent), *args, check=False)

        _assert_clean_manifest_error(r, parent / "gf.toml", "invalid")
        assert "'git_folder[0].path' must be a string" in r.stderr

    @pytest.mark.parametrize("args", [["status"], ["ls"]])
    def test_git_folder_missing_path(self, tmp_path, args):
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text(
            '[[git_folder]]\nname = "lib"\nurl = "u"\nref = "latest"\n')

        r = gf("-C", str(parent), *args, check=False)

        _assert_clean_manifest_error(r, parent / "gf.toml", "invalid")
        assert "'git_folder[0]' is missing required key 'path'" in r.stderr

    def test_git_folder_missing_ref_fails_pull(self, tmp_path):
        """A manifest entry without `ref` used to KeyError inside
        `effective_url_ref` during pull planning."""
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text(
            '[[git_folder]]\nname = "lib"\nurl = "u"\n'
            'path = "vendor/lib"\n')

        r = gf("-C", str(parent), "pull", check=False)

        _assert_clean_manifest_error(r, parent / "gf.toml", "invalid")
        assert "'git_folder[0]' is missing required key 'ref'" in r.stderr


class TestNonRegularManifestEnvelope:
    """A gf.toml that exists but is not a regular file must reach the
    manifest error envelope — pre-fix `is_file()` treated it as absent:
    read commands exited 0 silently and clone/init acted on an empty
    manifest before crashing at the manifest write."""

    @pytest.mark.parametrize("args", [
        ["status"], ["ls"], ["pull"], ["rm"],
        ["worktree", "list"],
        ["git", "status"], ["diff"], ["sh", "-c", "true"],
    ])
    def test_manifest_directory_dies_naming_file(self, tmp_path, args):
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").mkdir()

        r = gf("-C", str(parent), *args, check=False)

        _assert_clean_manifest_error(r, parent / "gf.toml", "cannot read")
        assert "not a regular file" in r.stderr

    def test_manifest_directory_blocks_clone(self, tmp_path):
        """`gf clone` dies in `_resolve` — no child is created (pre-fix
        the clone completed and `write_manifest` crashed with
        IsADirectoryError, leaving the child behind)."""
        upstream = _upstream_repo(tmp_path)
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").mkdir()

        r = gf("-C", str(parent), "clone", str(upstream), "vendor/lib",
               check=False)

        _assert_clean_manifest_error(r, parent / "gf.toml", "cannot read")
        assert "not a regular file" in r.stderr
        assert not (parent / "vendor" / "lib").exists()

    def test_manifest_directory_blocks_init(self, tmp_path):
        """`gf init` dies in `_resolve` — no child directory is made."""
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").mkdir()

        r = gf("-C", str(parent), "init", "child", check=False)

        _assert_clean_manifest_error(r, parent / "gf.toml", "cannot read")
        assert "not a regular file" in r.stderr
        assert not (parent / "child").exists()

    @pytest.mark.parametrize("args", [["status"], ["ls"]])
    def test_manifest_dangling_symlink_dies_naming_file(
            self, tmp_path, args):
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").symlink_to(parent / "missing-target")

        r = gf("-C", str(parent), *args, check=False)

        _assert_clean_manifest_error(r, parent / "gf.toml", "cannot read")
        assert "not a regular file" in r.stderr


class TestLocalOverrideEnvelope:
    """gf.local.toml gets the same envelope from the commands that read
    it (status, pull). `gf ls` never reads it — the control below pins
    that a corrupt override does not break ls."""

    @pytest.mark.parametrize("args", [["status"], ["pull"]])
    def test_corrupt_local_dies_naming_file(self, tmp_path, args):
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text(
            '[[git_folder]]\nname = "lib"\nurl = "u"\n'
            'ref = "latest"\npath = "vendor/lib"\n')
        (parent / "gf.local.toml").write_text("ref = [[ broken\n")

        r = gf("-C", str(parent), *args, check=False)

        _assert_clean_manifest_error(
            r, parent / "gf.local.toml", "corrupt")

    def test_corrupt_local_does_not_break_ls(self, tmp_path):
        """Control: `gf ls` never consults gf.local.toml."""
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text("git_folder = []\n")
        (parent / "gf.local.toml").write_text("ref = [[ broken\n")

        r = gf("-C", str(parent), "ls", check=False)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    def test_override_entry_not_a_table(self, tmp_path):
        """`git_folder_override = ["oops"]` used to AttributeError on
        `o.get("name")` inside `effective_url_ref` during pull."""
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text(
            '[[git_folder]]\nname = "lib"\nurl = "u"\n'
            'ref = "latest"\npath = "vendor/lib"\n')
        (parent / "gf.local.toml").write_text(
            'git_folder_override = ["oops"]\n')

        r = gf("-C", str(parent), "pull", check=False)

        _assert_clean_manifest_error(
            r, parent / "gf.local.toml", "invalid")
        assert "'git_folder_override[0]' must be a table" in r.stderr

    def test_override_non_string_field(self, tmp_path):
        """An override `url = 5` for the selected folder used to
        TypeError inside `_normalize_url` during pull planning."""
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text(
            '[[git_folder]]\nname = "lib"\nurl = "u"\n'
            'ref = "latest"\npath = "vendor/lib"\n')
        (parent / "gf.local.toml").write_text(
            '[[git_folder_override]]\nname = "lib"\nurl = 5\n')

        r = gf("-C", str(parent), "pull", check=False)

        _assert_clean_manifest_error(
            r, parent / "gf.local.toml", "invalid")
        assert "'git_folder_override[0].url' must be a string" in r.stderr


# ---------------------------------------------------------------------------
# controls — absent manifests keep their quiet/empty behavior


class TestAbsentManifestControls:
    @pytest.mark.parametrize("args", [
        ["status"], ["ls"], ["pull"], ["rm", "--all"],
    ])
    def test_absent_manifest_is_quiet_success(self, tmp_path, args):
        """No gf.toml: read/pull/rm commands treat it as an empty
        manifest — rc 0, no output."""
        parent = _parent_repo(tmp_path)

        r = gf("-C", str(parent), *args)
        assert r.returncode == 0
        assert r.stdout == ""
        assert r.stderr == ""

    @pytest.mark.parametrize("contents", ["", "git_folder = []\n"])
    def test_empty_manifest_is_quiet_success(self, tmp_path, contents):
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text(contents)

        for args in (["status"], ["ls"]):
            r = gf("-C", str(parent), *args)
            assert r.returncode == 0

    def test_init_creates_manifest(self, tmp_path):
        parent = _parent_repo(tmp_path)

        r = gf("-C", str(parent), "init", "child")
        assert r.returncode == 0
        manifest_path = parent / "gf.toml"
        assert manifest_path.is_file()
        with open(manifest_path, "rb") as f:
            data = tomllib.load(f)
        assert data["git_folder"][0]["path"] == "child"

    def test_clone_status_pull_green(self, tmp_path):
        """Happy path with a real local upstream: clone writes the
        manifest, status/ls/pull exit 0 — and gf.local.toml absent is
        fine."""
        upstream = _upstream_repo(tmp_path)
        parent = _parent_repo(tmp_path)

        r = gf("-C", str(parent), "clone", str(upstream), "vendor/lib")
        assert "Cloned lib into vendor/lib" in r.stdout
        assert (parent / "gf.toml").is_file()
        assert not (parent / "gf.local.toml").exists()

        r = gf("-C", str(parent), "status")
        assert r.returncode == 0
        assert "lib" in r.stdout

        r = gf("-C", str(parent), "ls")
        assert r.returncode == 0
        assert "lib" in r.stdout

        r = gf("-C", str(parent), "pull")
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# die()/SystemExit preservation — the main() catch must not swallow
# non-GitFoldersError exits or re-code them


class TestDiePreservation:
    def test_rm_without_path_dies_with_its_message(self, tmp_path):
        """A plain die() inside dispatch still exits 1 with its `gf:`
        message — the GitFoldersError catch does not intercept it."""
        parent = _parent_repo(tmp_path)
        (parent / "gf.toml").write_text("")

        r = gf("-C", str(parent), "rm", check=False)
        assert r.returncode == 1
        assert r.stderr.startswith("gf: ")
        assert "rm requires an explicit path" in r.stderr
        assert "Traceback" not in r.stderr

    def test_clone_fetch_failure_keeps_git_error_code(self, tmp_path):
        """A clone whose fetch fails dies with GitError's code 2 — a
        non-1 exit passes through unchanged."""
        parent = _parent_repo(tmp_path)

        r = gf("-C", str(parent), "clone", "/nonexistent/deep/repo",
               "vendor/lib", check=False)
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        # The backend streams git's own stderr before the `gf:` line;
        # the folder name derives from the spelled child path.
        assert "gf: clone failed for git-folder 'lib' (vendor/lib):" \
            in r.stderr
        assert "Traceback" not in r.stderr

    def test_main_converts_manifest_error_to_die(
            self, tmp_path, monkeypatch, capsys):
        """In-process pin: a GitFoldersError escaping a command exits
        SystemExit(1) via die() — `gf:` prefixed, file named."""
        parent = tmp_path / "parent"
        (parent / ".git").mkdir(parents=True)
        (parent / "gf.toml").write_bytes(_CORRUPT.encode())
        monkeypatch.chdir(parent)

        with pytest.raises(SystemExit) as ei:
            cli.main(["status"])
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("gf: corrupt git-folders manifest at ")
        assert str(parent / "gf.toml") in err
        assert "Traceback" not in err

    def test_main_lets_systemexit_through(
            self, tmp_path, monkeypatch, capsys):
        """In-process pin: die()'s SystemExit propagates through the
        dispatch try/except untouched (it is not a GitFoldersError)."""
        parent = tmp_path / "parent"
        (parent / ".git").mkdir(parents=True)
        (parent / "gf.toml").write_text("")
        monkeypatch.chdir(parent)

        with pytest.raises(SystemExit) as ei:
            cli.main(["rm"])
        assert ei.value.code == 1
        assert "rm requires an explicit path" in capsys.readouterr().err
