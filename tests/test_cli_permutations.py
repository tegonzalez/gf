"""Comprehensive in-process CLI permutation tests using a mock git backend."""
# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import os
import re
from pathlib import Path

from gf.exceptions import GitError


SHA1 = "1111111111111111111111111111111111111111"
SHA2 = "2222222222222222222222222222222222222222"


def test_normalize_and_name_from_url():
    from gf.cli import _normalize_url, _name_from_url

    assert _normalize_url("github.com/cursor/plugins") == "https://github.com/cursor/plugins"
    assert _normalize_url("github.com/cursor/plugins.git") == "https://github.com/cursor/plugins.git"
    assert _normalize_url("git@github.com:cursor/plugins.git") == "git@github.com:cursor/plugins.git"
    assert _normalize_url("/upstream") == "/upstream"

    assert _name_from_url("github.com/cursor/plugins") == "plugins"
    assert _name_from_url("https://github.com/cursor/plugins.git") == "plugins"
    assert _name_from_url("git@github.com:cursor/plugins.git") == "plugins"
    assert _name_from_url("/upstream") == "upstream"


def test_normalize_url_dotted_head_existing_at_base_stays_local(tmp_path):
    """gf-spec.md `gf clone`: bare host/path expansion applies only when
    neither the first segment nor the spelled path exists under the
    anchor — a spelled path existing at the anchor is a local path even
    when its first segment contains a dot (the `<repo>.git/<subdir>`
    spelling)."""
    from gf.cli import _normalize_url

    # `<base>/upx.git` exists (a bare repo carried inside the parent):
    # the spelling is a local path, never `https://upx.git/...`.
    (tmp_path / "upx.git").mkdir()
    assert _normalize_url("upx.git/docs/api", tmp_path) == "upx.git/docs/api"

    # "exists" is not "is a directory": a first segment that is an
    # ordinary file still anchors the spelling as a local path.
    (tmp_path / "v1.2").write_text("notes")
    assert _normalize_url("v1.2/changelog", tmp_path) == "v1.2/changelog"


def test_normalize_url_dotted_head_missing_at_base_expands(tmp_path):
    """The same dotted-first-segment spelling under an anchor that
    lacks it is host shorthand (gf-spec.md `gf clone`: expanded to
    `https://...`)."""
    from gf.cli import _normalize_url

    assert _normalize_url("upx.git/docs/api", tmp_path) == (
        "https://upx.git/docs/api")
    # Ordinary host shorthand is unchanged by the existence guard.
    assert _normalize_url("github.com/x", tmp_path) == (
        "https://github.com/x")


def test_normalize_url_scheme_spellings_passthrough(tmp_path):
    """Control: `https://`/`git@` spellings are returned unchanged
    whatever the anchor holds (gf-spec.md `gf clone`)."""
    from gf.cli import _normalize_url

    (tmp_path / "upx.git").mkdir()
    assert _normalize_url(
        "https://host.org/repo.git/docs/api", tmp_path
    ) == "https://host.org/repo.git/docs/api"
    assert _normalize_url(
        "git@host.org:repo.git/docs/api", tmp_path
    ) == "git@host.org:repo.git/docs/api"


def test_get_version_reads_pyproject():
    from gf.cli import _get_version
    # The version is sourced from pyproject.toml (or installed metadata).
    v = _get_version()
    assert v
    assert v != "unknown"


def test_version_flag_prints_version(fs, gf_inproc, mock_backend):
    fs.create_dir("/parent/.git")
    r = gf_inproc("--version", backend=mock_backend, check=True)
    assert r.returncode == 0
    assert r.stdout.strip()
    assert r.stdout.strip() != "unknown"


def test_version_short_flag_prints_version(fs, gf_inproc, mock_backend):
    fs.create_dir("/parent/.git")
    r = gf_inproc("-v", backend=mock_backend, check=True)
    assert r.returncode == 0
    assert r.stdout.strip()
    assert r.stdout.strip() != "unknown"


def test_version_flag_works_without_repo(fs, gf_inproc, mock_backend):
    fs.create_dir("/empty")
    r = gf_inproc("-C", "/empty", "--version", backend=mock_backend, check=True)
    assert r.returncode == 0
    assert r.stdout.strip() != "unknown"


def test_help_lists_version(fs, gf_inproc, mock_backend):
    """`gf --help` advertises the --version option."""
    fs.create_dir("/parent/.git")
    r = gf_inproc("--help", backend=mock_backend, check=False)
    # argparse prints help to stdout and exits 0.
    assert r.returncode == 0
    assert "--version" in r.stdout


def _parent(fs, root: str = "/parent") -> Path:
    """Create a fake parent git repo with an empty gf.toml."""
    parent = Path(root)
    parent.mkdir(parents=True, exist_ok=True)
    (parent / ".git").mkdir(parents=True, exist_ok=True)
    (parent / "gf.toml").write_text("git_folder = []\n")
    return parent


def _upstream(mock_backend, path: str = "/upstream", files: dict | None = None, refs: dict | None = None, head: str = "master") -> str:
    """Seed the mock with a bare upstream repo."""
    repo = mock_backend.seed(path, bare=True, mirror=False)
    content = files or {"a.txt": "hello"}
    commit = mock_backend.add_commit(repo, SHA1, content)
    for ref, sha in (refs or {f"refs/heads/{head}": SHA1}).items():
        repo.refs[ref] = sha
    repo.head_ref = f"refs/heads/{head}"
    return path


class TestLs:
    def test_ls_no_git_repo(self, fs, gf_inproc, mock_backend):
        fs.create_dir("/nope")
        r = gf_inproc("-C", "/nope", "ls", backend=mock_backend, check=True)
        assert r.returncode == 0
        assert r.stdout == ""
        assert r.stderr == ""
        assert mock_backend.calls == []

    def test_ls_no_manifest(self, fs, gf_inproc, mock_backend):
        fs.create_dir("/parent/.git")
        r = gf_inproc("-C", "/parent", "ls", backend=mock_backend, check=True)
        assert r.returncode == 0
        assert r.stdout == ""
        assert r.stderr == ""
        assert mock_backend.calls == []

    def test_ls_empty_manifest(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        r = gf_inproc("-C", str(parent), "ls", backend=mock_backend, check=True)
        assert r.returncode == 0
        assert r.stdout == ""
        assert r.stderr == ""
        assert mock_backend.calls == []

    def test_ls_does_not_call_remote(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        before = len(mock_backend.calls)
        r = gf_inproc("-C", str(parent), "ls", backend=mock_backend)
        assert r.returncode == 0
        new_calls = [c[0][0] for c in mock_backend.calls[before:]]
        assert "fetch" not in new_calls
        assert "remote" not in new_calls


class TestClone:
    def test_clone_default_path_and_ref(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        upstream = _upstream(mock_backend, files={"a.txt": "hello"})

        r = gf_inproc(
            "-C", str(parent), "clone", upstream, "lib",
            backend=mock_backend,
        )

        assert r.returncode == 0
        assert "Cloned lib" in r.stdout

        child = parent / "lib"
        assert child.is_dir()
        assert (child / "a.txt").read_text() == "hello"
        assert (child / ".gf" / "git" / "HEAD").is_file()
        assert (child / ".gf" / "state").is_file()
        assert not (parent / ".gitignore").exists()
        assert 'add "lib/" to .gitignore' in r.stdout

        manifest_text = (parent / "gf.toml").read_text()
        assert 'name = "lib"' in manifest_text
        assert 'url = "/upstream"' in manifest_text
        assert 'ref = "latest"' in manifest_text
        assert 'path = "lib"' in manifest_text

    def test_clone_with_path_and_branch(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        upstream = _upstream(mock_backend, files={"a.txt": "from-master"})

        r = gf_inproc(
            "-C", str(parent), "clone", upstream, "vendor/lib",
            "-b", "master",
            backend=mock_backend,
        )

        assert r.returncode == 0
        child = parent / "vendor" / "lib"
        assert (child / "a.txt").read_text() == "from-master"
        manifest = (parent / "gf.toml").read_text()
        assert 'path = "vendor/lib"' in manifest
        assert 'ref = "master"' in manifest

    def test_clone_stores_and_lists_repo_relative_paths(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc(
            "-C", str(parent), "clone", "/upstream", "vendor/lib",
            backend=mock_backend,
        )

        manifest = (parent / "gf.toml").read_text()
        assert 'path = "vendor/lib"' in manifest

        r = gf_inproc("-C", str(parent), "ls", backend=mock_backend)
        assert r.returncode == 0
        assert re.search(r"lib\s+/upstream\s+\[", r.stdout)
        assert str(parent) not in r.stdout

    def test_ls_inside_child_lists_all(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})

        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib2", backend=mock_backend)

        child = parent / "lib"
        r = gf_inproc("-C", str(child), "ls", backend=mock_backend)
        assert r.returncode == 0
        assert re.search(r"lib\s+/upstream\s+\[master\]", r.stdout)
        assert re.search(r"lib2\s+/upstream\s+\[master\]", r.stdout)

    def test_ls_shows_branch_and_dirty_marker(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        child = parent / "lib"
        child.mkdir()

        # init creates an unborn master branch
        gf_inproc("-C", str(child), "init", "-b", "master", backend=mock_backend)

        r = gf_inproc("-C", str(parent), "ls", backend=mock_backend)
        assert r.returncode == 0
        assert re.search(r"lib\s+lib\s+\[\]\s+\?", r.stdout)

        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib2", backend=mock_backend)

        child2 = parent / "lib2"
        (child2 / "a.txt").write_text("dirty")

        r = gf_inproc("-C", str(parent), "ls", backend=mock_backend)
        assert r.returncode == 0
        assert re.search(r"lib2\s+/upstream\s+\[master\]", r.stdout)
        assert "*" in r.stdout

    def test_clone_url_only_derives_name_and_path(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})

        r = gf_inproc("-C", str(parent), "clone", "/upstream", backend=mock_backend)
        assert r.returncode == 0
        assert (parent / "upstream" / "a.txt").read_text() == "hello"
        manifest_text = (parent / "gf.toml").read_text()
        assert 'name = "upstream"' in manifest_text
        assert 'url = "/upstream"' in manifest_text

    def test_clone_duplicate_name(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        upstream = _upstream(mock_backend)
        gf_inproc("-C", str(parent), "clone", upstream, "lib", backend=mock_backend)
        r = gf_inproc(
            "-C", str(parent), "clone", upstream, "lib",
            backend=mock_backend, check=False,
        )
        assert r.returncode == 1
        assert "already exists" in r.stderr

    def test_clone_bad_url_cleans_up_partial_child(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        r = gf_inproc(
            "-C", str(parent), "clone", "/no-such-repo", "lib",
            backend=mock_backend, check=False,
        )
        assert r.returncode == 2
        assert not (parent / "lib").exists()
        assert not (parent / "lib" / ".gf").exists()

    def test_clone_depth_passes_depth_to_fetch(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})

        gf_inproc(
            "-C", str(parent), "clone", "/upstream", "lib",
            "--depth", "1", backend=mock_backend,
        )
        fetch_calls = [c for c in mock_backend.calls if c[0][0] == "fetch"]
        assert fetch_calls
        assert "--depth=1" in fetch_calls[0][0]

    def test_clone_single_branch_narrows_refspec(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})

        gf_inproc(
            "-C", str(parent), "clone", "/upstream", "lib",
            "--single-branch", backend=mock_backend,
        )
        config_calls = [c for c in mock_backend.calls if c[0][0] == "config" and c[0][1] == "remote.origin.fetch"]
        # The last remote.origin.fetch config should narrow to the master branch.
        assert config_calls
        last = config_calls[-1][0]
        assert last[2] == "+refs/heads/master:refs/remotes/origin/master"

    def test_clone_single_branch_with_explicit_branch(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        upstream_repo = mock_backend.seed("/upstream", bare=True, mirror=False)
        mock_backend.add_commit(upstream_repo, SHA1, {"a.txt": "hello"})
        upstream_repo.refs["refs/heads/master"] = SHA1
        upstream_repo.refs["refs/heads/feature"] = SHA1
        upstream_repo.head_ref = "refs/heads/master"

        gf_inproc(
            "-C", str(parent), "clone", "/upstream", "lib",
            "-b", "feature", "--single-branch", backend=mock_backend,
        )
        # refspec VALUES written — the write may be a plain `config`
        # narrowing set or a `config --add` coverage append; both are
        # admitted spellings. `config --get-all` reads carry no value.
        writes = [c[0][-1] for c in mock_backend.calls
                  if c[0][0] == "config" and "remote.origin.fetch" in c[0][1:]
                  and c[0][-1].startswith(("+refs/", "refs/"))]
        assert writes
        # narrowed to the resolved branch; `+` stays inside the
        # refs/remotes/origin/* mirror namespace (gf-spec Fetch refspecs)
        assert writes[-1] == \
            "+refs/heads/feature:refs/remotes/origin/feature"
        # `feature` is a branch upstream — no tag coverage is needed and
        # no forced line may land outside the mirror namespace
        assert not any(w.startswith("+refs/tags/") for w in writes)


class TestPull:
    def test_pull_no_manifest_is_quiet_success(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        r = gf_inproc("-C", str(parent), "pull", backend=mock_backend)
        assert r.returncode == 0
        assert r.stdout == ""

    def test_pull_local_placeholder_succeeds(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        child = parent / "lib"
        child.mkdir()
        gf_inproc("-C", str(child), "init", backend=mock_backend)

        r = gf_inproc("-C", str(parent), "pull", backend=mock_backend)
        assert r.returncode == 0

    def test_pull_missing_placeholder_does_not_create_child(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        (parent / "gf.toml").write_text(
            '[[git_folder]]\nname = "lib"\nurl = "lib"\nref = "latest"\npath = "lib"\n'
        )

        r = gf_inproc("-C", str(parent), "pull", backend=mock_backend)
        assert r.returncode == 0
        assert not (parent / "lib").exists()

    def test_pull_updates_to_latest(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        upstream_repo = mock_backend.seed("/upstream", bare=True, mirror=False)
        mock_backend.add_commit(upstream_repo, SHA1, {"a.txt": "hello"})
        upstream_repo.refs["refs/heads/master"] = SHA1
        upstream_repo.head_ref = "refs/heads/master"

        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        # Push a new commit
        mock_backend.add_commit(upstream_repo, SHA2, {"a.txt": "update"}, parents=[SHA1])
        upstream_repo.refs["refs/heads/master"] = SHA2

        r = gf_inproc("-C", str(parent), "pull", backend=mock_backend)
        assert r.returncode == 0

        child = parent / "lib"
        assert (child / "a.txt").read_text() == "update"

    def test_pull_branch_updates_to_remote_tip(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        upstream_repo = mock_backend.seed("/upstream", bare=True, mirror=False)
        mock_backend.add_commit(upstream_repo, SHA1, {"a.txt": "hello"})
        upstream_repo.refs["refs/heads/master"] = SHA1
        upstream_repo.head_ref = "refs/heads/master"

        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", "-b", "master", backend=mock_backend)

        # Remote advances; child has a local master still at SHA1.
        mock_backend.add_commit(upstream_repo, SHA2, {"a.txt": "update"}, parents=[SHA1])
        upstream_repo.refs["refs/heads/master"] = SHA2

        r = gf_inproc("-C", str(parent), "pull", backend=mock_backend)
        assert r.returncode == 0
        assert (parent / "lib" / "a.txt").read_text() == "update"

    def test_pull_aborts_dirty(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        child = parent / "lib"
        (child / "a.txt").write_text("dirty")

        r = gf_inproc("-C", str(parent), "pull", backend=mock_backend, check=False)
        assert r.returncode == 3
        assert "dirty" in r.stderr

    def test_pull_invalid_ref(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        (parent / "gf.local.toml").write_text('[[git_folder_override]]\nname = "lib"\nref = "no-such"\n')
        r = gf_inproc("-C", str(parent), "pull", backend=mock_backend, check=False)
        assert r.returncode == 1
        assert "could not resolve ref" in r.stderr

    def test_pull_initializes_missing_child(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})

        # Write manifest entry manually
        (parent / "gf.toml").write_text(
            '[[git_folder]]\nname = "lib"\nurl = "/upstream"\nref = "latest"\npath = "vendor/lib"\n'
        )

        r = gf_inproc("-C", str(parent), "pull", backend=mock_backend)
        assert r.returncode == 0
        child = parent / "vendor" / "lib"
        assert (child / "a.txt").read_text() == "hello"
        assert not (parent / ".gitignore").exists()
        assert 'add "vendor/lib/" to .gitignore' in r.stdout

    def test_pull_rebase_updates_branch(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        upstream_repo = mock_backend.seed("/upstream", bare=True, mirror=False)
        mock_backend.add_commit(upstream_repo, SHA1, {"a.txt": "hello"})
        upstream_repo.refs["refs/heads/master"] = SHA1
        upstream_repo.head_ref = "refs/heads/master"

        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        # Remote advances
        mock_backend.add_commit(upstream_repo, SHA2, {"a.txt": "update"}, parents=[SHA1])
        upstream_repo.refs["refs/heads/master"] = SHA2

        r = gf_inproc("-C", str(parent), "pull", "--rebase", backend=mock_backend)
        assert r.returncode == 0

        child = parent / "lib"
        assert (child / "a.txt").read_text() == "update"

    def test_pull_force_flag_is_rejected(self, fs, gf_inproc, mock_backend):
        """`gf pull --force` does not exist (gf-spec `gf pull`): updating
        over uncommitted work is never offered — the flag is rejected
        with a nonzero exit before any git work, and the dirty child's
        bytes stay untouched."""
        parent = _parent(fs)
        upstream_repo = mock_backend.seed("/upstream", bare=True, mirror=False)
        mock_backend.add_commit(upstream_repo, SHA1, {"a.txt": "hello"})
        upstream_repo.refs["refs/heads/master"] = SHA1
        upstream_repo.head_ref = "refs/heads/master"

        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        (parent / "lib" / "a.txt").write_text("dirty")

        before = len(mock_backend.calls)
        r = gf_inproc("-C", str(parent), "pull", "--force",
                      backend=mock_backend, check=False)
        assert r.returncode != 0
        assert "--force" in r.stderr or "force" in r.stderr
        # rejected before any git call could touch the child
        assert mock_backend.calls[before:] == []
        assert (parent / "lib" / "a.txt").read_text() == "dirty"

    def test_pull_autostash_restores_dirty_changes(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        upstream_repo = mock_backend.seed("/upstream", bare=True, mirror=False)
        mock_backend.add_commit(upstream_repo, SHA1, {"a.txt": "hello"})
        upstream_repo.refs["refs/heads/master"] = SHA1
        upstream_repo.head_ref = "refs/heads/master"

        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        # Dirty the child with a new untracked file and advance the remote.
        (parent / "lib" / "new.txt").write_text("local work")
        mock_backend.add_commit(upstream_repo, SHA2, {"a.txt": "update"}, parents=[SHA1])
        upstream_repo.refs["refs/heads/master"] = SHA2

        r = gf_inproc("-C", str(parent), "pull", "--autostash", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        # The remote update applied.
        assert (parent / "lib" / "a.txt").read_text() == "update"
        # The stashed local work was restored.
        assert (parent / "lib" / "new.txt").read_text() == "local work"

    def test_pull_autostash_uses_stash_push_and_pop(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        (parent / "lib" / "new.txt").write_text("local work")

        before = len(mock_backend.calls)
        gf_inproc("-C", str(parent), "pull", "--autostash", backend=mock_backend)
        new_calls = [c[0] for c in mock_backend.calls[before:]]
        assert ("stash", "push", "-u", "-m", "gf autostash") in new_calls
        # `pop --index`: the staged/unstaged partition is part of the
        # work being preserved (gf-spec `gf pull`)
        assert ("stash", "pop", "--index") in new_calls

    def test_pull_without_force_or_autostash_aborts_dirty(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        (parent / "lib" / "a.txt").write_text("dirty")

        r = gf_inproc("-C", str(parent), "pull", backend=mock_backend, check=False)
        assert r.returncode == 3
        assert "dirty" in r.stderr


class TestStatus:
    def test_status_clean(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        r = gf_inproc("-C", str(parent), "status", backend=mock_backend)
        assert r.returncode == 0
        assert re.search(r"lib\s+/upstream\s+\[", r.stdout)

    def test_status_dirty(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        child = parent / "lib"
        (child / "a.txt").write_text("dirty")

        r = gf_inproc("-C", str(parent), "status", backend=mock_backend)
        assert r.returncode == 0
        assert re.search(r"lib\s+/upstream\s+\[", r.stdout)
        assert "M a.txt" in r.stdout

    def test_status_discovers_manifest_from_child_subdir(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "vendor/lib", backend=mock_backend)

        nested = parent / "vendor" / "lib" / "src"
        nested.mkdir(parents=True)

        r = gf_inproc("-C", str(nested), "status", backend=mock_backend)
        assert r.returncode == 0
        assert re.search(r"lib\s+/upstream\s+\[", r.stdout)

    def test_status_no_manifest_is_quiet_success(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        r = gf_inproc("-C", str(parent), "status", backend=mock_backend)
        assert r.returncode == 0
        assert r.stdout == ""

    def test_status_does_not_call_remote(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        before = len(mock_backend.calls)
        r = gf_inproc("-C", str(parent), "status", backend=mock_backend)
        assert r.returncode == 0
        new_calls = [c[0][0] for c in mock_backend.calls[before:]]
        assert "fetch" not in new_calls
        assert "remote" not in new_calls

    def test_status_branch(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        child = parent / "lib"
        child.mkdir()

        gf_inproc("-C", str(child), "init", "-b", "master", backend=mock_backend)

        r = gf_inproc("-C", str(parent), "status", backend=mock_backend)
        assert r.returncode == 0
        assert re.search(r"lib\s+lib\s+\[\]", r.stdout)

    def test_status_remote_clean(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        r = gf_inproc("-C", str(parent), "status", "--remote", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        assert re.search(r"lib\s+/upstream\s+\[master\]\s+clean", r.stdout)

    def test_status_remote_behind(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        upstream_repo = mock_backend.seed("/upstream", bare=True, mirror=False)
        mock_backend.add_commit(upstream_repo, SHA1, {"a.txt": "hello"})
        upstream_repo.refs["refs/heads/master"] = SHA1
        upstream_repo.head_ref = "refs/heads/master"

        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        # Remote advances; child still at SHA1. `gf status --remote` is
        # local-only, so simulate the state `git status` would show after
        # a `git fetch`: advance the local remote-tracking ref to SHA2
        # while leaving the child HEAD at SHA1.
        mock_backend.add_commit(upstream_repo, SHA2, {"a.txt": "update"}, parents=[SHA1])
        upstream_repo.refs["refs/heads/master"] = SHA2
        child = parent / "lib"
        child_repo = mock_backend._repo(str((child / ".gf" / "git").resolve()))
        child_repo.refs["refs/remotes/origin/master"] = SHA2
        child_repo.commits[SHA2] = upstream_repo.commits[SHA2]

        r = gf_inproc("-C", str(parent), "status", "--remote", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        assert re.search(r"lib\s+/upstream\s+\[master\]\s+behind", r.stdout)

    def test_status_remote_local_dirty(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        # Dirty the worktree without advancing the remote.
        (parent / "lib" / "a.txt").write_text("dirty")

        r = gf_inproc("-C", str(parent), "status", "--remote", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        assert re.search(r"lib\s+/upstream\s+\[master\]\s+local-dirty", r.stdout)

    def test_status_remote_behind_dirty(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        upstream_repo = mock_backend.seed("/upstream", bare=True, mirror=False)
        mock_backend.add_commit(upstream_repo, SHA1, {"a.txt": "hello"})
        upstream_repo.refs["refs/heads/master"] = SHA1
        upstream_repo.head_ref = "refs/heads/master"

        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        # Remote advances AND child is dirty. `gf status --remote` is
        # local-only, so simulate the state `git status` would show after
        # a `git fetch`: advance the local remote-tracking ref to SHA2
        # while leaving the child HEAD at SHA1, then dirty the worktree.
        mock_backend.add_commit(upstream_repo, SHA2, {"a.txt": "update"}, parents=[SHA1])
        upstream_repo.refs["refs/heads/master"] = SHA2
        child = parent / "lib"
        child_repo = mock_backend._repo(str((child / ".gf" / "git").resolve()))
        child_repo.refs["refs/remotes/origin/master"] = SHA2
        child_repo.commits[SHA2] = upstream_repo.commits[SHA2]
        (child / "a.txt").write_text("dirty")

        r = gf_inproc("-C", str(parent), "status", "--remote", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        # `behind-dirty`: the rev-list relation is behind (the remote
        # tip has commits HEAD lacks) and the worktree is dirty —
        # gf-spec.md Drift algorithm vocabulary.
        assert re.search(r"lib\s+/upstream\s+\[master\]\s+behind-dirty",
                         r.stdout)

    def test_status_remote_does_not_fetch(self, fs, gf_inproc, mock_backend):
        """`gf status --remote` is local-only: no git fetch/remote/ls-remote."""
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        before = len(mock_backend.calls)
        r = gf_inproc("-C", str(parent), "status", "--remote", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        new_calls = [c[0][0] for c in mock_backend.calls[before:]]
        assert "fetch" not in new_calls
        assert "remote" not in new_calls
        assert "ls-remote" not in new_calls

    def test_status_remote_missing_child(self, fs, gf_inproc, mock_backend):
        """`gf status --remote` reports `missing` for a child with no .gf/git/HEAD."""
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        # Declare a git-folder whose child path does not exist yet.
        (parent / "gf.toml").write_text(
            '[[git_folder]]\nname = "lib"\nurl = "/upstream"\nref = "latest"\npath = "lib"\n'
        )

        r = gf_inproc("-C", str(parent), "status", "--remote", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        assert re.search(r"lib\s+/upstream\s+\[\]\s+missing", r.stdout)

    def test_status_remote_unresolvable_ref_dies(self, fs, gf_inproc, mock_backend):
        """`gf status --remote` dies with a deterministic code on an unresolvable ref."""
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)
        # Override the ref to one that does not exist locally.
        (parent / "gf.local.toml").write_text(
            '[[git_folder_override]]\nname = "lib"\nref = "no-such-branch"\n'
        )

        r = gf_inproc(
            "-C", str(parent), "status", "--remote",
            backend=mock_backend, check=False,
        )
        assert r.returncode != 0
        assert r.returncode != 1 or "could not resolve" in r.stderr
        # No drift state column is printed on failure.
        assert "clean" not in r.stdout
        assert "behind" not in r.stdout

    def test_status_remote_git_error_uses_error_code(self, fs, gf_inproc, mock_backend, monkeypatch):
        """`cmd_status` propagates the GitFoldersError code for git failures."""
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        def boom(*args, **kwargs):
            raise GitError("git rev-parse failed")

        monkeypatch.setattr("gf.shelf.drift", boom)

        r = gf_inproc(
            "-C", str(parent), "status", "--remote",
            backend=mock_backend, check=False,
        )
        assert r.returncode == 2
        assert "git rev-parse failed" in r.stderr
        # No drift state column is printed on failure.
        assert "clean" not in r.stdout
        assert "behind" not in r.stdout

    def test_status_without_remote_does_not_fetch(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        before = len(mock_backend.calls)
        r = gf_inproc("-C", str(parent), "status", backend=mock_backend)
        assert r.returncode == 0
        new_calls = [c[0][0] for c in mock_backend.calls[before:]]
        assert "fetch" not in new_calls
        # Non-remote output format is unchanged: no drift state column.
        assert not re.search(
            r"\[(master|HEAD)\]\s+(clean|ahead|behind|diverged|missing|"
            r"local-dirty|ahead-dirty|behind-dirty|diverged-dirty)",
            r.stdout)


class TestInit:
    def test_init_named_child_creates_directory(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        r = gf_inproc(
            "-C", str(parent), "init", "abc",
            backend=mock_backend,
        )
        assert r.returncode == 0
        assert "Initialized abc" in r.stdout
        child = parent / "abc"
        assert (child / ".gf" / "git" / "HEAD").is_file()
        assert ".gf" in (child / ".gf" / "git" / "info" / "exclude").read_text()
        manifest_text = (parent / "gf.toml").read_text()
        assert 'name = "abc"' in manifest_text
        assert 'path = "abc"' in manifest_text
        assert 'url = "abc"' in manifest_text
        assert not (parent / ".gitignore").exists()
        assert 'add "abc/" to .gitignore' in r.stdout

    def test_init_in_cwd(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        child = parent / "lib"
        child.mkdir()
        r = gf_inproc(
            "-C", str(child), "init",
            backend=mock_backend,
        )
        assert r.returncode == 0
        assert "Initialized lib" in r.stdout
        assert (child / ".gf" / "git" / "HEAD").is_file()
        manifest_text = (parent / "gf.toml").read_text()
        assert 'name = "lib"' in manifest_text
        assert 'path = "lib"' in manifest_text

    def test_init_creates_manifest_if_missing(self, fs, gf_inproc, mock_backend):
        parent = Path("/parent")
        parent.mkdir()
        (parent / ".git").mkdir()
        child = parent / "lib"
        child.mkdir()
        r = gf_inproc(
            "-C", str(child), "init",
            backend=mock_backend,
        )
        assert r.returncode == 0
        assert (parent / "gf.toml").is_file()

    def test_init_does_not_create_manifest_on_failure(self, fs, gf_inproc, mock_backend):
        parent = Path("/parent")
        parent.mkdir()
        (parent / ".git").mkdir()
        r = gf_inproc(
            "-C", str(parent), "init",
            backend=mock_backend, check=False,
        )
        assert r.returncode == 1
        assert not (parent / "gf.toml").exists()

    def test_init_denies_existing_git(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        child = parent / "lib"
        (child / ".git").mkdir(parents=True)
        r = gf_inproc(
            "-C", str(parent), "init", "lib",
            backend=mock_backend, check=False,
        )
        assert r.returncode == 1
        assert ".git directory" in r.stderr

    def test_init_denies_existing_gf(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        child = parent / "lib"
        fs.create_file(str(child / ".gf" / "git" / "HEAD"), contents="ref: refs/heads/master\n")
        r = gf_inproc(
            "-C", str(parent), "init", "lib",
            backend=mock_backend, check=False,
        )
        assert r.returncode == 1
        assert ".gf directory" in r.stderr

    def test_init_with_name_override(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        r = gf_inproc(
            "-C", str(parent), "init", "abc", "-n", "custom-name",
            backend=mock_backend,
        )
        assert r.returncode == 0
        assert "Initialized custom-name" in r.stdout
        manifest_text = (parent / "gf.toml").read_text()
        assert 'name = "custom-name"' in manifest_text
        assert 'path = "abc"' in manifest_text

    def test_init_with_url_override(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        r = gf_inproc(
            "-C", str(parent), "init", "abc", "--url", "https://example.com/repo.git",
            backend=mock_backend,
        )
        assert r.returncode == 0
        manifest_text = (parent / "gf.toml").read_text()
        assert 'url = "https://example.com/repo.git"' in manifest_text
        # The default placeholder (url == path) is not used.
        assert 'url = "abc"' not in manifest_text


class TestRm:
    def test_rm_unregisters_child_preserving_worktree(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        r = gf_inproc("-C", str(parent), "rm", "lib", backend=mock_backend)
        assert r.returncode == 0
        child = parent / "lib"
        assert child.is_dir()
        assert (child / "a.txt").is_file()
        assert (child / ".git" / "HEAD").is_file()
        assert not (child / ".gf").exists()
        assert 'git_folder = []' in (parent / "gf.toml").read_text()

    def test_rm_preserves_user_created_worktree(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        child = parent / "lib"
        child.mkdir()
        (child / "user.txt").write_text("important work")

        gf_inproc("-C", str(child), "init", backend=mock_backend)

        r = gf_inproc("-C", str(parent), "rm", "lib", backend=mock_backend)
        assert r.returncode == 0
        assert (child / "user.txt").read_text() == "important work"
        assert (child / ".git" / "HEAD").is_file()
        assert not (child / ".gf").exists()

    def test_rm_requires_path(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        r = gf_inproc("-C", str(parent), "rm", backend=mock_backend, check=False)
        assert r.returncode == 1
        assert "rm requires an explicit path" in r.stderr

    def test_rm_all_unregisters_every_git_folder(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib2", backend=mock_backend)

        r = gf_inproc("-C", str(parent), "rm", "--all", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        # Both children are converted back to .git and the manifest is empty.
        for child in (parent / "lib", parent / "lib2"):
            assert (child / ".git" / "HEAD").is_file()
            assert not (child / ".gf").exists()
            assert (child / "a.txt").is_file()
        assert 'git_folder = []' in (parent / "gf.toml").read_text()
        assert "Removed lib" in r.stdout
        assert "Removed lib2" in r.stdout

    def test_rm_all_requires_parent_root(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "vendor/lib", backend=mock_backend)

        # Run from inside a child subdir, not the parent root.
        nested = parent / "vendor" / "lib"
        r = gf_inproc("-C", str(nested), "rm", "--all", backend=mock_backend, check=False)
        assert r.returncode == 1
        assert "parent repo root" in r.stderr
        # Nothing was removed.
        assert (nested / ".gf" / "git" / "HEAD").is_file()

    def test_rm_all_rejects_path_arguments(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        r = gf_inproc("-C", str(parent), "rm", "--all", "lib", backend=mock_backend, check=False)
        assert r.returncode == 1
        assert "does not accept path arguments" in r.stderr
        # Nothing was removed.
        assert (parent / "lib" / ".gf" / "git" / "HEAD").is_file()

    def test_rm_all_empty_manifest_is_quiet_success(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        r = gf_inproc("-C", str(parent), "rm", "--all", backend=mock_backend)
        assert r.returncode == 0
        assert r.stdout == ""


class TestGit:
    def test_git_fails_in_parent(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        r = gf_inproc(
            "-C", str(parent), "git", "status",
            backend=mock_backend, check=False,
        )
        assert r.returncode == 1
        assert "not inside a git-folder child" in r.stderr


class TestWorktreeList:
    def _seed_worktrees(self, mock_backend, parent: Path):
        """Seed the parent repo with two worktrees in the mock backend."""
        repo = mock_backend._repo(str(parent.resolve()))
        repo.worktrees = []
        mock_backend.add_worktree(repo, parent, head=SHA1, branch="master")
        mock_backend.add_worktree(
            mock_backend._repo(str(parent.resolve())),
            parent.parent / "feature",
            head=SHA2,
            branch="feature",
        )
        return repo

    def test_worktree_list_human(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        repo = self._seed_worktrees(mock_backend, parent)

        # Create a second worktree directory and symlink the git-folder into it.
        feature = parent.parent / "feature"
        feature.mkdir(parents=True, exist_ok=True)
        (feature / "gf.toml").write_text((parent / "gf.toml").read_text())
        new_child = feature / "lib"
        source_child = (parent / "lib").resolve()
        rel = os.path.relpath(source_child, new_child.parent)
        os.symlink(rel, new_child, target_is_directory=True)

        r = gf_inproc("-C", str(parent), "worktree", "list", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        assert str(parent) in r.stdout
        assert str(feature) in r.stdout
        # The source worktree has no linked git-folders (lib is a real child).
        # The feature worktree has lib linked.
        assert "lib -> " in r.stdout

    def test_worktree_list_verbose_includes_head(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)
        self._seed_worktrees(mock_backend, parent)

        r = gf_inproc("-C", str(parent), "worktree", "list", "--verbose", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        assert SHA1 in r.stdout
        assert SHA2 in r.stdout

    def test_worktree_list_porcelain(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)
        self._seed_worktrees(mock_backend, parent)

        feature = parent.parent / "feature"
        feature.mkdir(parents=True, exist_ok=True)
        (feature / "gf.toml").write_text((parent / "gf.toml").read_text())
        new_child = feature / "lib"
        source_child = (parent / "lib").resolve()
        rel = os.path.relpath(source_child, new_child.parent)
        os.symlink(rel, new_child, target_is_directory=True)

        r = gf_inproc("-C", str(parent), "worktree", "list", "--porcelain", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        assert f"worktree {parent}" in r.stdout
        assert f"worktree {feature}" in r.stdout
        assert f"HEAD {SHA1}" in r.stdout
        assert "branch refs/heads/master" in r.stdout
        assert "branch refs/heads/feature" in r.stdout
        assert f"git-folder lib {rel}" in r.stdout

    def test_worktree_list_no_manifest(self, fs, gf_inproc, mock_backend):
        fs.create_dir("/parent/.git")
        r = gf_inproc("-C", "/parent", "worktree", "list", backend=mock_backend, check=False)
        assert r.returncode == 1
        assert "not inside a git repo with gf.toml" in r.stderr


class TestWorktreeRemove:
    def _seed_main_and_feature(self, mock_backend, parent):
        """Seed the parent repo with the main worktree plus a feature worktree."""
        repo = mock_backend._repo(str(parent.resolve()))
        repo.worktrees = []
        mock_backend.add_worktree(repo, parent, head=SHA1, branch="master")
        feature = parent.parent / "feature"
        mock_backend.add_worktree(repo, feature, head=SHA2, branch="feature")
        return repo, feature

    def test_worktree_remove_unlinks_symlinks_and_calls_git(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        # Seed the main worktree plus a feature worktree, and create the
        # symlink on disk.
        repo, feature = self._seed_main_and_feature(mock_backend, parent)
        feature.mkdir(parents=True, exist_ok=True)
        (feature / "gf.toml").write_text((parent / "gf.toml").read_text())
        new_child = feature / "lib"
        source_child = (parent / "lib").resolve()
        os.symlink(os.path.relpath(source_child, new_child.parent), new_child, target_is_directory=True)

        r = gf_inproc("-C", str(parent), "worktree", "remove", str(feature), backend=mock_backend)
        assert r.returncode == 0, r.stderr
        # The symlink was removed before git worktree remove.
        assert not new_child.exists()
        # The source child is untouched.
        assert (parent / "lib" / ".gf" / "git" / "HEAD").is_file()
        # The worktree was removed from the mock.
        assert all(w.path != str(feature.resolve()) for w in repo.worktrees)

    def test_worktree_remove_force(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        repo, feature = self._seed_main_and_feature(mock_backend, parent)
        feature.mkdir(parents=True, exist_ok=True)
        (feature / "gf.toml").write_text((parent / "gf.toml").read_text())

        before = len(mock_backend.calls)
        r = gf_inproc("-C", str(parent), "worktree", "remove", str(feature), "--force", backend=mock_backend)
        assert r.returncode == 0, r.stderr
        # Verify --force was forwarded to git worktree remove.
        remove_calls = [c for c in mock_backend.calls[before:] if c[0][:2] == ("worktree", "remove")]
        assert remove_calls
        assert "--force" in remove_calls[0][0]

    def test_worktree_remove_refuses_unlisted_path(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)
        self._seed_main_and_feature(mock_backend, parent)

        r = gf_inproc(
            "-C", str(parent), "worktree", "remove", "/not-a-worktree",
            backend=mock_backend, check=False,
        )
        assert r.returncode == 1
        assert "not a listed worktree" in r.stderr

    def test_worktree_remove_refuses_main_worktree(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)
        self._seed_main_and_feature(mock_backend, parent)

        r = gf_inproc(
            "-C", str(parent), "worktree", "remove", str(parent),
            backend=mock_backend, check=False,
        )
        assert r.returncode == 1
        assert "main worktree" in r.stderr
        # No git worktree remove was attempted.
        remove_calls = [c for c in mock_backend.calls if c[0][:2] == ("worktree", "remove")]
        assert not remove_calls

    def test_worktree_remove_restores_symlinks_on_failure(self, fs, gf_inproc, mock_backend):
        """If `git worktree remove` fails, unlinked symlinks are restored."""
        parent = _parent(fs)
        _upstream(mock_backend, files={"a.txt": "hello"})
        gf_inproc("-C", str(parent), "clone", "/upstream", "lib", backend=mock_backend)

        repo, feature = self._seed_main_and_feature(mock_backend, parent)
        feature.mkdir(parents=True, exist_ok=True)
        (feature / "gf.toml").write_text((parent / "gf.toml").read_text())
        new_child = feature / "lib"
        source_child = (parent / "lib").resolve()
        rel = os.path.relpath(source_child, new_child.parent)
        os.symlink(rel, new_child, target_is_directory=True)

        # Patch the mock so `git worktree remove` fails for the feature
        # worktree, simulating a dirty or locked worktree that git refuses
        # to remove.
        original_dispatch = mock_backend._dispatch

        def failing_dispatch(args, path, cwd, git_dir, work_tree):
            if args[:2] == ("worktree", "remove") and str(feature.resolve()) in args:
                from mock_git import GitResult
                from gf.exceptions import GitError
                raise GitError(f"cannot remove {feature}: dirty worktree")
            return original_dispatch(args, path, cwd, git_dir, work_tree)

        mock_backend._dispatch = failing_dispatch

        r = gf_inproc(
            "-C", str(parent), "worktree", "remove", str(feature),
            backend=mock_backend, check=False,
        )
        assert r.returncode != 0
        assert "cannot remove" in r.stderr
        # The symlink was restored after the failure.
        assert new_child.is_symlink()
        assert os.readlink(new_child) == rel
        # The source child is untouched.
        assert (parent / "lib" / ".gf" / "git" / "HEAD").is_file()
        # The worktree was not removed from the mock.
        assert any(w.path == str(feature.resolve()) for w in repo.worktrees)


class TestSh:
    def test_sh_fails_in_parent(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        r = gf_inproc(
            "-C", str(parent), "sh", "-c", 'echo hi',
            backend=mock_backend, check=False,
        )
        assert r.returncode == 1
        assert "not inside a git-folder child" in r.stderr

    def test_sh_fails_in_non_child_path(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        (parent / "not-a-child").mkdir()
        r = gf_inproc(
            "-C", str(parent / "not-a-child"), "sh", "-c", 'echo hi',
            backend=mock_backend, check=False,
        )
        assert r.returncode == 1
        assert "not inside a git-folder child" in r.stderr

    def test_sh_rejects_mixing_c_and_positional(self, fs, gf_inproc, mock_backend):
        parent = _parent(fs)
        child = parent / "lib"
        child.mkdir()
        gf_inproc("-C", str(child), "init", "-b", "master", backend=mock_backend)
        r = gf_inproc(
            "-C", str(child), "sh", "-c", "echo hi", "echo", "again",
            backend=mock_backend, check=False,
        )
        assert r.returncode == 1
        assert "cannot use -c with a positional command" in r.stderr


class TestPassthroughArgParsing:
    """Verify that diff/log/git subparsers capture passthrough args correctly.

    These tests check the argparse layer only — they build the parser via
    ``cli.main``'s internal parser and inspect the parsed ``git_args``.  The
    actual subprocess execution is covered by the real-git integration tests.
    """

    def _parse(self, argv):
        """Build the same parser used by cli.main and parse the given argv."""
        import argparse
        from gf.cli import _apply_chdir
        argv = _apply_chdir(list(argv))
        parser = argparse.ArgumentParser(prog="gf")
        sub = parser.add_subparsers(dest="command", required=True)

        p_diff = sub.add_parser("diff")
        p_diff.add_argument("git_args", nargs=argparse.REMAINDER, default=[])
        p_log = sub.add_parser("log")
        p_log.add_argument("git_args", nargs=argparse.REMAINDER, default=[])
        p_git = sub.add_parser("git")
        p_git.add_argument("git_args", nargs=argparse.REMAINDER, default=[])

        args, unknown = parser.parse_known_args(argv)
        if args.command in ("git", "diff", "log"):
            # Mirror cli.main: slice passthrough args verbatim from argv to
            # preserve token order (REMAINDER reorders flags after positionals).
            cmd_idx = argv.index(args.command)
            args.git_args = list(argv[cmd_idx + 1:])
        elif unknown:
            parser.error(f"unrecognized arguments: {' '.join(unknown)}")
        return args

    def test_diff_captures_remainder(self):
        args = self._parse(["diff", "--stat", "HEAD~1..HEAD"])
        assert args.command == "diff"
        assert "--stat" in args.git_args
        assert "HEAD~1..HEAD" in args.git_args

    def test_log_captures_remainder(self):
        args = self._parse(["log", "--oneline", "-n", "1"])
        assert args.command == "log"
        assert args.git_args == ["--oneline", "-n", "1"]

    def test_git_captures_subcommand_and_args(self):
        args = self._parse(["git", "show", "--stat", "HEAD"])
        assert args.command == "git"
        assert args.git_args == ["show", "--stat", "HEAD"]

    def test_diff_no_args(self):
        args = self._parse(["diff"])
        assert args.command == "diff"
        assert args.git_args == []

    def test_git_no_args(self):
        args = self._parse(["git"])
        assert args.command == "git"
        assert args.git_args == []
