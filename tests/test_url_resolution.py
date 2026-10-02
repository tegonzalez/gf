# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Receiving tests for slice P2.W1.1 — URL resolution.

Derived from the governing documents (plan DC-DOC-PLAN-005, P2.W1.1 row);
expectations come from the docs, not from any implementation:

- docs/gf-spec.md "URL resolution": a `url` is a plain path. A local path
  walks up to the nearest directory that is a repository (a `.git` entry,
  a bare repository, or a `gf` child with `.gf/git`) — that directory is
  the repo URL and the rest is the subdir. A remote URL with a segment
  ending in `.git` splits after the first such segment with no network.
  Any other remote URL is probed with `git ls-remote`: the full URL first,
  then one path segment shorter at a time; the longest prefix that
  answers is the repo URL and the rest is the subdir. Probes run with
  terminal prompts disabled (`GIT_TERMINAL_PROMPT=0`). When no prefix
  resolves, the error tells the user to mark the boundary by writing
  `.git` after the repository name. The subdir is a nonempty
  repository-relative POSIX path; `.`, `..`, and empty segments are
  rejected. A `url` that is the repository is a whole-repo binding.
- docs/gf-spec.md `gf pull`: "local and relative paths resolve against
  the parent repo root".
- docs/gf-arch.md ("`src/gf/shelf.py`", URL resolution contract): the
  resolver lives in `shelf.py`; "Repo URL / Subdir" (gf-spec.md
  terminology) is its result; `GF-D6` keeps the subfolder as a plain path
  in `url`.
- docs/gf-constraints.md ("Do not fetch the network in `ls` or
  `status`"): `ls-remote` runs only inside resolution; local-path and
  `.git`-boundary resolution are local-only and never probe.
- Plan row P2.W1.1: `GitBackend.git` gains an `env` override so probes
  set `GIT_TERMINAL_PROMPT=0`; the mock mirrors it.

Pinned surface (the docs name no function; this is the contract these
tests define and the implementation owner must satisfy):

    gf.shelf.resolve_repo_url(
        url: str,
        parent_root: Path | None = None,   # anchors relative local URLs
        backend: GitBackend | None = None,
    ) -> tuple[str, str]                   # (repo_url, subdir); "" = whole-repo

    GitBackend.git(..., env: dict | None = None)   # merged over os.environ

Remote probes go through `MockGitBackend` (seeded repos answer
`ls-remote` for their own URL and fail below it); local walk-up tests use
real directories under `tmp_path`. No network and no `fs` fixture: the
mock's `ls-remote` dispatch is a pure dict lookup keyed on the resolved
URL spelling.
"""

import os
from pathlib import Path

import pytest

from gf import cli, shelf
from gf.backends import GitCliBackend, GitResult
from gf.exceptions import GitError, GitFoldersError
from gf.runner import RunResult

from mock_git import MockGitBackend

SHA = "1111111111111111111111111111111111111111"


def _seed_remote(backend: MockGitBackend, url: str, head: str = "master"):
    """Seed a repository at `url`; the mock answers `ls-remote` for exactly
    that URL and fails for longer or unseeded prefixes."""
    repo = backend.seed(url, bare=True, head=head)
    backend.add_commit(repo, SHA, {"a.txt": "x"})
    repo.refs[f"refs/heads/{head}"] = SHA
    return repo


def _probed_urls(backend: MockGitBackend) -> list[str]:
    """`git ls-remote` target URLs in probe order, from the mock call log."""
    urls = []
    for entry in backend.calls:
        args = entry[0]
        if args and args[0] == "ls-remote":
            urls.append(next(a for a in args[1:] if not a.startswith("-")))
    return urls


def _mk_git_repo(path: Path) -> Path:
    """A directory that is a repository by `.git` entry (spec rule 1)."""
    (path / ".git").mkdir(parents=True)
    return path


def _mk_gf_child(path: Path) -> Path:
    """A `gf` whole-repo child: `.gf/git` with a HEAD (spec rule 1)."""
    gitdir = path / ".gf" / "git"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/master\n")
    return path


def _same_dir(a: str | Path, b: Path) -> bool:
    """Directory-identity comparison per gf-spec.md path comparison."""
    return Path(a).resolve() == b.resolve()


class _RecordingRemote:
    """A `GitBackend` that answers `ls-remote` for a fixed set of repo URLs.

    Records each probe's `env` kwarg so tests can pin
    `GIT_TERMINAL_PROMPT=0` (gf-spec.md "URL resolution": "Probes run with
    terminal prompts disabled"). Any non-`ls-remote` call fails the test:
    remote resolution probes and does nothing else.
    """

    def __init__(self, repo_urls):
        self._repo_urls = frozenset(repo_urls)
        self.probe_urls: list[str] = []
        self.probe_envs: list[dict | None] = []

    def git(
        self,
        *args,
        cwd=None,
        git_dir=None,
        work_tree=None,
        check=True,
        stream=False,
        env=None,
    ) -> GitResult:
        assert args and args[0] == "ls-remote", f"unexpected git call: {args}"
        url = next(a for a in args[1:] if not a.startswith("-"))
        self.probe_urls.append(url)
        self.probe_envs.append(env)
        if url in self._repo_urls:
            return GitResult(0, f"{SHA}\tHEAD\n", "")
        if check:
            raise GitError(f"no repo at {url}")
        return GitResult(1, "", f"no repo at {url}")

    def git_capture(self, *args, **kwargs) -> str:
        return self.git(*args, **kwargs).stdout


class TestRemoteUrls:
    """gf-spec.md "URL resolution" rule 3: longest-prefix `ls-remote` probe."""

    def test_whole_repo_url_is_a_whole_repo_binding(self, mock_backend):
        """`https://host/org/repo` answers `ls-remote` itself, so it resolves
        to a whole-repo binding: repo URL unchanged, empty subdir, and only
        the full-URL probe was needed (spec: "first the full URL")."""
        _seed_remote(mock_backend, "https://host/org/repo")
        repo_url, subdir = shelf.resolve_repo_url(
            "https://host/org/repo", backend=mock_backend,
        )
        assert repo_url == "https://host/org/repo"
        assert subdir == ""
        assert _probed_urls(mock_backend) == ["https://host/org/repo"]

    def test_subfolder_url_splits_at_longest_answering_prefix(
        self, mock_backend
    ):
        """`https://host/org/repo/docs/api`: probes walk down one segment at a
        time until `https://host/org/repo` answers; the remainder is the
        subdir `docs/api`."""
        _seed_remote(mock_backend, "https://host/org/repo")
        repo_url, subdir = shelf.resolve_repo_url(
            "https://host/org/repo/docs/api", backend=mock_backend,
        )
        assert repo_url == "https://host/org/repo"
        assert subdir == "docs/api"
        assert _probed_urls(mock_backend) == [
            "https://host/org/repo/docs/api",
            "https://host/org/repo/docs",
            "https://host/org/repo",
        ]

    def test_full_url_wins_when_it_itself_answers(self, mock_backend):
        """The longest answering prefix is the repo URL; when the full `url`
        answers there is no subdir, even if a shorter prefix is also a
        repository."""
        _seed_remote(mock_backend, "https://host/org/repo")
        _seed_remote(mock_backend, "https://host/org/repo/docs/api")
        repo_url, subdir = shelf.resolve_repo_url(
            "https://host/org/repo/docs/api", backend=mock_backend,
        )
        assert repo_url == "https://host/org/repo/docs/api"
        assert subdir == ""
        assert _probed_urls(mock_backend) == ["https://host/org/repo/docs/api"]

    def test_repo_url_may_end_above_the_last_url_segment(self, mock_backend):
        """The answering prefix need not be the `url`'s parent's name: seeding
        `https://host/org` makes `repo/docs/api` the subdir."""
        _seed_remote(mock_backend, "https://host/org")
        repo_url, subdir = shelf.resolve_repo_url(
            "https://host/org/repo/docs/api", backend=mock_backend,
        )
        assert repo_url == "https://host/org"
        assert subdir == "repo/docs/api"
        assert _probed_urls(mock_backend) == [
            "https://host/org/repo/docs/api",
            "https://host/org/repo/docs",
            "https://host/org/repo",
            "https://host/org",
        ]

    def test_ssh_style_url_splits_the_same_way(self, mock_backend):
        """`git@host:org/repo/sub/dir` probes the same prefix chain; the
        `git@host:` authority is kept while path segments are dropped."""
        _seed_remote(mock_backend, "git@host:org/repo")
        repo_url, subdir = shelf.resolve_repo_url(
            "git@host:org/repo/sub/dir", backend=mock_backend,
        )
        assert repo_url == "git@host:org/repo"
        assert subdir == "sub/dir"
        assert _probed_urls(mock_backend) == [
            "git@host:org/repo/sub/dir",
            "git@host:org/repo/sub",
            "git@host:org/repo",
        ]

    def test_bare_host_shorthand_is_normalized_then_split(self, mock_backend):
        """`host.org/repo/sub` goes through `cli._normalize_url` first
        (clone/pull call sites: cli.py `_normalize_url(url)`), giving
        `https://host.org/repo/sub`, which then splits by probing."""
        _seed_remote(mock_backend, "https://host.org/repo")
        url = cli._normalize_url("host.org/repo/sub")
        assert url == "https://host.org/repo/sub"
        repo_url, subdir = shelf.resolve_repo_url(url, backend=mock_backend)
        assert repo_url == "https://host.org/repo"
        assert subdir == "sub"
        assert _probed_urls(mock_backend) == [
            "https://host.org/repo/sub",
            "https://host.org/repo",
        ]


class TestDotGitBoundary:
    """gf-spec.md "URL resolution" rule 2: a segment ending in `.git` ends the
    repository after the first such segment, with no `ls-remote` probe."""

    def test_dot_git_segment_marks_the_boundary(self, mock_backend):
        repo_url, subdir = shelf.resolve_repo_url(
            "https://host/org/repo.git/docs/api", backend=mock_backend,
        )
        assert repo_url == "https://host/org/repo.git"
        assert subdir == "docs/api"
        assert _probed_urls(mock_backend) == []

    def test_trailing_dot_git_is_a_whole_repo_url(self, mock_backend):
        repo_url, subdir = shelf.resolve_repo_url(
            "https://host/org/repo.git", backend=mock_backend,
        )
        assert repo_url == "https://host/org/repo.git"
        assert subdir == ""
        assert _probed_urls(mock_backend) == []

    def test_first_dot_git_segment_wins(self, mock_backend):
        repo_url, subdir = shelf.resolve_repo_url(
            "https://host/org/a.git/b.git/c", backend=mock_backend,
        )
        assert repo_url == "https://host/org/a.git"
        assert subdir == "b.git/c"
        assert _probed_urls(mock_backend) == []

    def test_scp_style_dot_git_segment_marks_the_boundary(self, mock_backend):
        repo_url, subdir = shelf.resolve_repo_url(
            "git@host:org/repo.git/sub/dir", backend=mock_backend,
        )
        assert repo_url == "git@host:org/repo.git"
        assert subdir == "sub/dir"
        assert _probed_urls(mock_backend) == []


class TestLocalPaths:
    """gf-spec.md "URL resolution" rule 1: walk up to the nearest repository
    directory. Local resolution is filesystem-only — no `ls-remote`."""

    def test_subdir_walks_up_to_the_repo_boundary(self, tmp_path, mock_backend):
        repo = _mk_git_repo(tmp_path / "repo")
        (repo / "sub" / "dir").mkdir(parents=True)
        repo_url, subdir = shelf.resolve_repo_url(
            str(repo / "sub" / "dir"), parent_root=tmp_path, backend=mock_backend,
        )
        assert _same_dir(repo_url, repo)
        assert subdir == "sub/dir"
        assert _probed_urls(mock_backend) == []

    def test_path_that_is_a_repo_is_a_whole_repo_binding(
        self, tmp_path, mock_backend
    ):
        repo = _mk_git_repo(tmp_path / "repo")
        repo_url, subdir = shelf.resolve_repo_url(
            str(repo), parent_root=tmp_path, backend=mock_backend,
        )
        assert _same_dir(repo_url, repo)
        assert subdir == ""
        assert _probed_urls(mock_backend) == []

    def test_relative_local_path_resolves_against_parent_root(
        self, tmp_path, mock_backend
    ):
        """`../other/docs/api` anchors at the parent repo root (gf-spec.md
        `gf pull`: "local and relative paths resolve against the parent
        repo root"), then walks up to `other`."""
        parent = _mk_git_repo(tmp_path / "parent")
        other = _mk_git_repo(tmp_path / "other")
        (other / "docs" / "api").mkdir(parents=True)
        repo_url, subdir = shelf.resolve_repo_url(
            "../other/docs/api", parent_root=parent, backend=mock_backend,
        )
        assert _same_dir(repo_url, other)
        assert subdir == "docs/api"
        assert _probed_urls(mock_backend) == []

    def test_dot_git_file_entry_marks_the_boundary(self, tmp_path, mock_backend):
        """A `.git` *file* (linked worktree gitfile) is still "a `.git`
        entry" and marks the repository boundary."""
        repo = tmp_path / "repo"
        (repo / "sub").mkdir(parents=True)
        (repo / ".git").write_text("gitdir: /elsewhere/gitdir\n")
        repo_url, subdir = shelf.resolve_repo_url(
            str(repo / "sub"), parent_root=tmp_path, backend=mock_backend,
        )
        assert _same_dir(repo_url, repo)
        assert subdir == "sub"
        assert _probed_urls(mock_backend) == []

    def test_bare_repository_marks_the_boundary(self, tmp_path, mock_backend):
        repo = tmp_path / "bare-repo"
        (repo / "sub").mkdir(parents=True)
        (repo / "HEAD").write_text("ref: refs/heads/master\n")
        repo_url, subdir = shelf.resolve_repo_url(
            str(repo / "sub"), parent_root=tmp_path, backend=mock_backend,
        )
        assert _same_dir(repo_url, repo)
        assert subdir == "sub"
        assert _probed_urls(mock_backend) == []

    def test_gf_child_marks_the_boundary(self, tmp_path, mock_backend):
        """A `gf` child (`child/.gf/git`) is a repository boundary; the child
        directory is the repo URL."""
        child = _mk_gf_child(tmp_path / "child")
        (child / "docs").mkdir(parents=True)
        repo_url, subdir = shelf.resolve_repo_url(
            str(child / "docs"), parent_root=tmp_path, backend=mock_backend,
        )
        assert _same_dir(repo_url, child)
        assert subdir == "docs"
        assert _probed_urls(mock_backend) == []

    def test_existing_whole_repo_child_needs_no_probe(
        self, tmp_path, mock_backend
    ):
        """An existing whole-repo child URL is local-only: resolution finds
        `.gf/git` on disk and must not touch `ls-remote` (gf-constraints.md:
        local-only paths need no network; gf-spec.md: an existing
        whole-repo child is never re-resolved over the network)."""
        child = _mk_gf_child(tmp_path / "vendor" / "lib")
        repo_url, subdir = shelf.resolve_repo_url(
            str(child), parent_root=tmp_path, backend=mock_backend,
        )
        assert _same_dir(repo_url, child)
        assert subdir == ""
        assert _probed_urls(mock_backend) == []

    def test_nearest_repository_ancestor_wins(self, tmp_path, mock_backend):
        _mk_git_repo(tmp_path / "outer")
        inner = _mk_git_repo(tmp_path / "outer" / "inner")
        (inner / "deep").mkdir(parents=True)
        repo_url, subdir = shelf.resolve_repo_url(
            str(inner / "deep"), parent_root=tmp_path, backend=mock_backend,
        )
        assert _same_dir(repo_url, inner)
        assert subdir == "deep"
        assert _probed_urls(mock_backend) == []

    def test_leaf_directory_need_not_exist(self, tmp_path, mock_backend):
        """Walk-up starts from the path spelling: `repo/sub/dir` resolves even
        when `sub/dir` does not exist yet (it arrives on fetch)."""
        repo = _mk_git_repo(tmp_path / "repo")
        repo_url, subdir = shelf.resolve_repo_url(
            str(repo / "sub" / "dir"), parent_root=tmp_path, backend=mock_backend,
        )
        assert _same_dir(repo_url, repo)
        assert subdir == "sub/dir"
        assert _probed_urls(mock_backend) == []

    def test_dot_git_in_a_local_name_is_not_a_remote_boundary(
        self, tmp_path, mock_backend
    ):
        """Rule 2 is for remote URLs: a local directory *named* `repo.git`
        is a repository only if the filesystem says so."""
        repo = _mk_git_repo(tmp_path / "repo.git")
        (repo / "sub").mkdir(parents=True)
        repo_url, subdir = shelf.resolve_repo_url(
            str(repo / "sub"), parent_root=tmp_path, backend=mock_backend,
        )
        assert _same_dir(repo_url, repo)
        assert subdir == "sub"
        assert _probed_urls(mock_backend) == []


class TestSubdirValidation:
    """gf-spec.md: "The subdir is a nonempty repository-relative path in
    POSIX form; `.`, `..`, and empty segments are rejected."""

    def test_dotdot_segment_is_rejected(self, mock_backend):
        with pytest.raises(GitFoldersError):
            shelf.resolve_repo_url(
                "https://host/repo.git/../x", backend=mock_backend,
            )

    def test_dot_segment_is_rejected(self, mock_backend):
        with pytest.raises(GitFoldersError):
            shelf.resolve_repo_url(
                "https://host/repo.git/sub/./dir", backend=mock_backend,
            )

    def test_empty_segment_is_rejected(self, mock_backend):
        with pytest.raises(GitFoldersError):
            shelf.resolve_repo_url(
                "https://host/repo.git/sub//dir", backend=mock_backend,
            )


class TestUnresolvableUrl:
    """gf-spec.md: "If no prefix resolves … `gf` stops with an error that
    tells the user to mark the boundary by writing `.git` after the
    repository name." (gf-troubleshooting.md GF-TRB-7.)"""

    def test_unresolvable_url_errors_naming_the_dot_git_hint(
        self, mock_backend
    ):
        with pytest.raises(GitFoldersError) as excinfo:
            shelf.resolve_repo_url(
                "https://private.test/team/repo/sub", backend=mock_backend,
            )
        assert ".git" in str(excinfo.value)
        # Probing started at the full URL (spec: "first the full URL").
        assert _probed_urls(mock_backend)[0] == "https://private.test/team/repo/sub"


class TestProbeEnvironment:
    """gf-spec.md: "Probes run with terminal prompts disabled
    (`GIT_TERMINAL_PROMPT=0`)". Plan row P2.W1.1: `GitBackend.git` gains an
    `env` override, merged over `os.environ`; the mock mirrors it."""

    def test_every_ls_remote_probe_carries_terminal_prompt_0(self):
        remote = _RecordingRemote({"https://host/org/repo"})
        repo_url, subdir = shelf.resolve_repo_url(
            "https://host/org/repo/docs/api", backend=remote,
        )
        assert (repo_url, subdir) == ("https://host/org/repo", "docs/api")
        assert remote.probe_urls == [
            "https://host/org/repo/docs/api",
            "https://host/org/repo/docs",
            "https://host/org/repo",
        ]
        # Failed prefixes must not stop for a password either — every
        # probe carries the override, not just the answering one.
        assert len(remote.probe_envs) == 3
        for env in remote.probe_envs:
            assert env is not None
            assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_mock_backend_git_accepts_an_env_kwarg(self, mock_backend):
        """The mock mirrors the `GitBackend.git(..., env=...)` signature."""
        _seed_remote(mock_backend, "https://host/org/repo")
        r = mock_backend.git(
            "ls-remote", "https://host/org/repo",
            env={"GIT_TERMINAL_PROMPT": "0"}, check=False,
        )
        assert r.returncode == 0

    def test_cli_backend_env_is_merged_over_os_environ(self, monkeypatch):
        """`GitCliBackend.git(env=...)` merges the mapping over a copy of
        `os.environ`: override entries win, inherited entries survive."""
        captured = {}

        def fake_run_command(cmd, env, *, mode, cwd=None):
            captured["cmd"] = cmd
            captured["env"] = env
            return RunResult(0, "", "")

        monkeypatch.setattr("gf.backends.run_command", fake_run_command)
        monkeypatch.setenv("GF_TEST_SENTINEL", "present")
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

        GitCliBackend().git(
            "ls-remote", "https://host/org/repo",
            env={"GIT_TERMINAL_PROMPT": "0"},
        )

        env = captured["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GF_TEST_SENTINEL"] == "present"
