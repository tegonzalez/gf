# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Receiving tests for slice P2R.E1 — the error-message contract.

Spec §Error handling (docs/gf-spec.md):

  - Exit codes are deterministic: 1 validation, 2 network/git failure,
    3 dirty worktree preventing update.
  - "Error messages include the git-folder name, path, and the
    operation that failed."

The admitted architecture ruling scopes that clause to errors raised
while handling an IDENTIFIED git-folder — a manifest entry, or args
that supply name+path (e.g. `clone <url> <path>`, `init <path> -n`).
Context-free failures with no folder identity are outside the clause
and are deliberately NOT pinned here: "not inside a git repo",
"not inside a git-folder child", `rm --all` usage errors, argparse
errors, and `gf sh`/`gf git` passthrough stderr (the command's own
output, not a gf message).

Baseline offenders these pins expose (locate by content, pre-E1):

  - `child <path> is dirty; commit or stash before pulling`
    (pull, rc 3 — path+operation, no name)
  - `could not resolve ref '<ref>' in <path>` /
    `could not resolve remote branch 'origin/<b>' in <path>`
    (pull/status --remote, rc 1 — path, no name)
  - `rebase of <path> onto origin/<b> failed` (pull --rebase, rc 2 —
    path+operation, no name)
  - `autostash pop for <path> conflicted` (pull --autostash, rc 2 —
    path+operation, no name)
  - `child path <path> already exists and is not empty` (clone, rc 1 —
    path, no name/operation)
  - `child path <path> exists and is not a git-folder` (pull, rc 1 —
    path, no name/operation)
  - `git-folder '<name>' already exists in manifest` (clone/init, rc 1 —
    name, no path/operation)
  - `path <rel> is already used by git-folder '<other>'` (clone/init,
    rc 1 — path+other name, no operand name/operation)
  - `<target> already has a .git directory` / `.gf directory`
    (init, rc 1 — path, no name/operation)
  - `<child> already contains a .git directory` (rm, rc 1 — path,
    no name/operation)
  - `<child> is a symlinked child; remove it from the owning worktree`
    (rm, rc 1 — path, no name)
  - `die(str(e))` propagations of backend text on fetch/resolve
    failures (clone/pull unreachable URL — no name/path/operation)
  - `child path must be inside the parent repo` (clone/init path arg,
    rc 1 — IN scope per arch ruling: args supply name+path; the die
    carries none of the three fields)
  - `could not resolve 'latest' for <path>: no local
    refs/remotes/origin/HEAD, main, or master` (status --remote at
    latest — path, no name; real-git pin removes the child's
    origin/HEAD symref on a dev-only upstream so `latest` has no
    local fallback)

Deferred: the worktree-add `child path already exists` die
(cli.py:542) is in the seam's fix scope but its receiving pin is
deferred to S10 per the arch ruling.

Field assertions are spec-visible only: the folder NAME must appear,
the manifest-relative PATH spelling must appear (absolute renderings
contain it as a suffix), and a token naming the failed operation must
appear. No message format, emitting-seam name, or exception-field
internals are pinned.

Mock-backend cases run in-process via `gf_inproc`/`mock_backend`
(pyfakefs); failures that need real git semantics (rebase conflict,
stash-pop conflict, worktree-symlinked child) run through the real
`gf` subprocess. Every test here is expected RED at the pre-E1
baseline and green under the landed seam.
"""

import re
from pathlib import Path

from conftest import gf, git, push_branch, push_commit


# --- shared field assertion -----------------------------------------------


def _assert_error_fields(result, *, name, path, operation, rc):
    """Assert the E1 clause on a failed command result.

    `name` is the git-folder name; `path` is its manifest-relative
    spelling ("vendor/lib" — an absolute rendering contains it);
    `operation` is a regex naming the failed operation; `rc` is the
    deterministic exit code from spec §Error handling.
    """
    assert result.returncode == rc, (
        f"expected rc={rc}, got {result.returncode}:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert name in result.stderr, (
        f"git-folder name {name!r} missing from error:\n{result.stderr}"
    )
    assert path in result.stderr, (
        f"git-folder path {path!r} missing from error:\n{result.stderr}"
    )
    assert re.search(operation, result.stderr), (
        f"failed operation /{operation}/ missing from error:\n"
        f"{result.stderr}"
    )


# --- mock-backend fixtures (pyfakefs) -------------------------------------

SHA1 = "1111111111111111111111111111111111111111"


def _parent(fs, root: str = "/parent") -> Path:
    """Fake parent repo with an empty manifest (test_cli_permutations idiom)."""
    parent = Path(root)
    parent.mkdir(parents=True, exist_ok=True)
    (parent / ".git").mkdir(parents=True, exist_ok=True)
    (parent / "gf.toml").write_text("git_folder = []\n")
    return parent


def _upstream(mock_backend, path: str = "/upstream") -> str:
    """Seed the mock bare upstream (one commit, branch master)."""
    repo = mock_backend.seed(path, bare=True, mirror=False)
    commit = mock_backend.add_commit(repo, SHA1, {"a.txt": "hello"})
    repo.refs["refs/heads/master"] = commit.sha
    repo.head_ref = "refs/heads/master"
    return path


def _mock_clone(
    fs, gf_inproc, mock_backend, tag="parent", rel="vendor/lib",
    name="widgets",
):
    parent = _parent(fs, root=f"/{tag}")
    _upstream(mock_backend, path=f"/up-{tag}")
    gf_inproc(
        "-C", str(parent), "clone", f"/up-{tag}", rel, "-n", name,
        backend=mock_backend,
    )
    return parent


def _override(parent: Path, name: str, **fields: str) -> None:
    """Write a gf.local.toml override for one git-folder."""
    body = "".join(f'{k} = "{v}"\n' for k, v in fields.items())
    (parent / "gf.local.toml").write_text(
        f'[[git_folder_override]]\nname = "{name}"\n{body}'
    )


def _manifest_with(parent: Path, *, name, url, ref, path) -> None:
    (parent / "gf.toml").write_text(
        f'[[git_folder]]\nname = "{name}"\nurl = "{url}"\n'
        f'ref = "{ref}"\npath = "{path}"\n'
    )


# --- mock-backend scenario table -------------------------------------------
#
# Each scenario prepares fixture state and returns
# (argv, field expectations) for `gf_inproc(..., backend=mock_backend,
# check=False)`. `name` spellings are chosen so the name can never be a
# substring of the path — a `name` assertion cannot pass spuriously.


def _sc_pull_dirty(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _mock_clone(fs, gf_inproc, mock_backend, tag=tag)
    (parent / "vendor" / "lib" / "a.txt").write_text("dirty")
    return (["-C", str(parent), "pull"], dict(
        name="widgets", path="vendor/lib", operation=r"pull", rc=3))


def _sc_pull_unresolvable_ref(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _mock_clone(fs, gf_inproc, mock_backend, tag=tag)
    _override(parent, "widgets", ref="no-such")
    return (["-C", str(parent), "pull"], dict(
        name="widgets", path="vendor/lib", operation=r"pull|resolve", rc=1))


def _sc_pull_path_not_a_folder(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _parent(fs, root=f"/{tag}")
    _upstream(mock_backend, path=f"/up-{tag}")
    _manifest_with(
        parent, name="widgets", url=f"/up-{tag}", ref="latest",
        path="vendor/lib",
    )
    child = parent / "vendor" / "lib"
    child.mkdir(parents=True)
    (child / "a.txt").write_text("not a git-folder")
    return (["-C", str(parent), "pull"], dict(
        name="widgets", path="vendor/lib", operation=r"pull", rc=1))


def _sc_pull_unreachable_url(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _mock_clone(fs, gf_inproc, mock_backend, tag=tag)
    _override(parent, "widgets", url="/no-such-repo")
    return (["-C", str(parent), "pull"], dict(
        name="widgets", path="vendor/lib", operation=r"pull|fetch", rc=2))


def _sc_clone_duplicate_name(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _mock_clone(fs, gf_inproc, mock_backend, tag=tag)
    return (
        ["-C", str(parent), "clone", f"/up-{tag}", "vendor/lib2",
         "-n", "widgets"],
        dict(name="widgets", path="vendor/lib2", operation=r"clone", rc=1),
    )


def _sc_clone_path_in_use(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _mock_clone(fs, gf_inproc, mock_backend, tag=tag)
    return (
        ["-C", str(parent), "clone", f"/up-{tag}", "vendor/lib",
         "-n", "gadgets"],
        dict(name="gadgets", path="vendor/lib", operation=r"clone", rc=1),
    )


def _sc_clone_nonempty_path(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _parent(fs, root=f"/{tag}")
    _upstream(mock_backend, path=f"/up-{tag}")
    child = parent / "vendor" / "lib"
    child.mkdir(parents=True)
    (child / "a.txt").write_text("occupied")
    return (
        ["-C", str(parent), "clone", f"/up-{tag}", "vendor/lib",
         "-n", "widgets"],
        dict(name="widgets", path="vendor/lib", operation=r"clone", rc=1),
    )


def _sc_clone_unreachable_url(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _parent(fs, root=f"/{tag}")
    _upstream(mock_backend, path=f"/up-{tag}")
    return (
        ["-C", str(parent), "clone", "/no-such-repo", "vendor/lib",
         "-n", "widgets"],
        dict(name="widgets", path="vendor/lib", operation=r"clone", rc=2),
    )


def _sc_clone_path_outside_parent(fs, gf_inproc, mock_backend, tag="parent"):
    """cli's inside-the-parent-repo check (in-scope per ruling: the args
    supply name+path — validation of a named folder's path, not a
    context-free precondition)."""
    parent = _parent(fs, root=f"/{tag}")
    _upstream(mock_backend, path=f"/up-{tag}")
    return (
        ["-C", str(parent), "clone", f"/up-{tag}", "/elsewhere/x",
         "-n", "widgets"],
        dict(name="widgets", path="/elsewhere/x", operation=r"clone", rc=1),
    )


def _sc_init_duplicate_name(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _parent(fs, root=f"/{tag}")
    gf_inproc(
        "-C", str(parent), "init", "vendor/lib", "-n", "widgets",
        backend=mock_backend,
    )
    return (
        ["-C", str(parent), "init", "vendor/lib2", "-n", "widgets"],
        dict(name="widgets", path="vendor/lib2", operation=r"init", rc=1),
    )


def _sc_init_path_in_use(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _parent(fs, root=f"/{tag}")
    _manifest_with(
        parent, name="a", url=f"/up-{tag}", ref="latest", path="vendor/x",
    )
    return (
        ["-C", str(parent), "init", "vendor/x", "-n", "widgets"],
        dict(name="widgets", path="vendor/x", operation=r"init", rc=1),
    )


def _sc_init_existing_git_dir(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _parent(fs, root=f"/{tag}")
    (parent / "vendor" / "x" / ".git").mkdir(parents=True)
    return (
        ["-C", str(parent), "init", "vendor/x", "-n", "widgets"],
        dict(name="widgets", path="vendor/x", operation=r"init", rc=1),
    )


def _sc_init_path_outside_parent(fs, gf_inproc, mock_backend, tag="parent"):
    """Same inside-the-parent-repo validation on `gf init` — args supply
    name+path, so the identified-folder clause applies (ruling)."""
    parent = _parent(fs, root=f"/{tag}")
    return (
        ["-C", str(parent), "init", "/elsewhere/x", "-n", "widgets"],
        dict(name="widgets", path="/elsewhere/x", operation=r"init", rc=1),
    )


def _sc_init_existing_gf_dir(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _parent(fs, root=f"/{tag}")
    (parent / "vendor" / "x" / ".gf").mkdir(parents=True)
    return (
        ["-C", str(parent), "init", "vendor/x", "-n", "widgets"],
        dict(name="widgets", path="vendor/x", operation=r"init", rc=1),
    )


def _sc_rm_existing_dot_git(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _mock_clone(fs, gf_inproc, mock_backend, tag=tag)
    (parent / "vendor" / "lib" / ".git").mkdir()
    return (["-C", str(parent), "rm", "vendor/lib"], dict(
        name="widgets", path="vendor/lib", operation=r"rm|remove", rc=1))


def _sc_status_remote_unresolvable_ref(fs, gf_inproc, mock_backend, tag="parent"):
    parent = _mock_clone(fs, gf_inproc, mock_backend, tag=tag)
    _override(parent, "widgets", ref="no-such")
    return (["-C", str(parent), "status", "--remote"], dict(
        name="widgets", path="vendor/lib",
        operation=r"status|resolve", rc=1))


MOCK_SCENARIOS = [
    ("pull/dirty", _sc_pull_dirty),
    ("pull/unresolvable-ref", _sc_pull_unresolvable_ref),
    ("pull/path-not-a-folder", _sc_pull_path_not_a_folder),
    ("pull/unreachable-url", _sc_pull_unreachable_url),
    ("clone/duplicate-name", _sc_clone_duplicate_name),
    ("clone/path-in-use", _sc_clone_path_in_use),
    ("clone/nonempty-path", _sc_clone_nonempty_path),
    ("clone/unreachable-url", _sc_clone_unreachable_url),
    ("init/duplicate-name", _sc_init_duplicate_name),
    ("init/path-in-use", _sc_init_path_in_use),
    ("init/existing-git-dir", _sc_init_existing_git_dir),
    ("init/existing-gf-dir", _sc_init_existing_gf_dir),
    ("init/path-outside-parent", _sc_init_path_outside_parent),
    ("clone/path-outside-parent", _sc_clone_path_outside_parent),
    ("rm/existing-dot-git", _sc_rm_existing_dot_git),
    ("status-remote/unresolvable-ref", _sc_status_remote_unresolvable_ref),
]


# --- real-git fixtures ------------------------------------------------------


def _real_repo(tmp_path: Path):
    """Bare upstream (a.txt on master) + real parent repo."""
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
    return upstream, parent


def _run_real(scenario, tmp_path: Path):
    upstream, parent = _real_repo(tmp_path)
    argv, fields = scenario(upstream, parent)
    result = gf(*argv, check=False)
    _assert_error_fields(result, **fields)


def _sc_real_rebase_conflict(upstream, parent):
    gf(
        "-C", str(parent), "clone", str(upstream), "vendor/lib",
        "-n", "widgets",
    )
    child = parent / "vendor" / "lib"
    (child / "a.txt").write_text("local work")
    gf("-C", str(child), "git", "add", "a.txt")
    gf("-C", str(child), "git", "commit", "-m", "local")
    push_commit(upstream, "upstream", "upstream work")
    return (["-C", str(parent), "pull", "--rebase"], dict(
        name="widgets", path="vendor/lib", operation=r"rebase", rc=2))


def _sc_real_autostash_pop_conflict(upstream, parent):
    gf(
        "-C", str(parent), "clone", str(upstream), "vendor/lib",
        "-n", "widgets",
    )
    (parent / "vendor" / "lib" / "a.txt").write_text("local dirty")
    push_commit(upstream, "upstream", "upstream change")
    return (["-C", str(parent), "pull", "--autostash"], dict(
        name="widgets", path="vendor/lib", operation=r"stash|pull", rc=2))


def _sc_real_status_remote_latest_unresolvable(upstream, parent):
    """spec 'Reference model' latest-resolution + 'gf status --remote':
    `latest` resolves via refs/remotes/origin/HEAD, then origin/main,
    then origin/master. A dev-only upstream plus a removed origin/HEAD
    leaves none — `status --remote` fails ref resolution locally
    (`could not resolve 'latest' for <path>`) on the manifest folder."""
    # A second, dev-only upstream so the child never records
    # origin/main or origin/master; remote HEAD -> dev lets
    # `clone -b dev` complete.
    alt = parent.parent / "upstream-dev"
    alt.mkdir()
    git("init", "--bare", cwd=alt)
    push_branch(alt, "dev", "dev content")
    git("symbolic-ref", "HEAD", "refs/heads/dev", cwd=alt)

    gf(
        "-C", str(parent), "clone", str(alt), "vendor/lib",
        "-n", "widgets", "-b", "dev",
    )
    child = parent / "vendor" / "lib"
    # Remove the child's remote HEAD symref (gf git passthrough): local
    # refs now lack origin/HEAD, origin/main, and origin/master, so
    # `latest` has nothing to resolve against.
    gf(
        "-C", str(child), "git", "update-ref", "-d",
        "refs/remotes/origin/HEAD",
    )
    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "widgets"\nref = "latest"\n'
    )
    return (["-C", str(parent), "status", "--remote"], dict(
        name="widgets", path="vendor/lib",
        operation=r"status|resolve", rc=1))


def _sc_real_rm_symlinked_child(upstream, parent):
    gf(
        "-C", str(parent), "clone", str(upstream), "vendor/lib",
        "-n", "widgets",
    )
    wt2 = parent.parent / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2), "-b", "wt2")
    return (["-C", str(wt2), "rm", "vendor/lib"], dict(
        name="widgets", path="vendor/lib", operation=r"rm|remove", rc=1))


REAL_SCENARIOS = [
    ("pull/rebase-conflict", _sc_real_rebase_conflict),
    ("pull/autostash-pop-conflict", _sc_real_autostash_pop_conflict),
    ("rm/symlinked-child", _sc_real_rm_symlinked_child),
    ("status-remote/latest-unresolvable",
     _sc_real_status_remote_latest_unresolvable),
]


# --- representative per-command pins ---------------------------------------
#
# Each test anchors one offender class; the sweeps below assert the
# invariant over the whole enumerated surface.


class TestPullErrorFields:
    """spec 'gf pull' failures on an identified (manifest) git-folder;
    §Error handling — name + path + operation, deterministic rc."""

    def test_pull_dirty_error_carries_fields(self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_pull_dirty(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)

    def test_pull_unresolvable_ref_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_pull_unresolvable_ref(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)

    def test_pull_path_not_a_folder_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_pull_path_not_a_folder(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)

    def test_pull_unreachable_url_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_pull_unreachable_url(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)


class TestCloneErrorFields:
    """spec 'gf clone' failures — the folder is identified by the clone
    args (path, plus `-n` name). §Error handling."""

    def test_clone_duplicate_name_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_clone_duplicate_name(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)

    def test_clone_path_in_use_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_clone_path_in_use(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)

    def test_clone_nonempty_path_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_clone_nonempty_path(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)

    def test_clone_unreachable_url_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_clone_unreachable_url(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)

    def test_clone_path_outside_parent_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        """cli's 'child path must be inside the parent repo' die is
        IN-scope per the arch ruling: clone args supply name+path, so
        the error fires on an identified folder — name + the arg's
        spelling + 'clone' must appear."""
        argv, fields = _sc_clone_path_outside_parent(
            fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)


class TestInitErrorFields:
    """spec 'gf init [<path>]' failures — identified by init args."""

    def test_init_duplicate_name_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_init_duplicate_name(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)

    def test_init_path_in_use_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_init_path_in_use(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)

    def test_init_existing_git_dir_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_init_existing_git_dir(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)

    def test_init_path_outside_parent_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        """Same inside-the-parent-repo validation on `gf init` — in-scope
        per the ruling (args supply name+path)."""
        argv, fields = _sc_init_path_outside_parent(
            fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)


class TestRmErrorFields:
    """spec 'gf rm <path>' failures on an identified git-folder."""

    def test_rm_existing_dot_git_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_rm_existing_dot_git(fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)


class TestStatusErrorFields:
    """spec 'gf status --remote': unresolvable ref → clear error,
    deterministic non-zero exit (§status, §Error handling)."""

    def test_status_remote_unresolvable_ref_error_carries_fields(
            self, fs, gf_inproc, mock_backend):
        argv, fields = _sc_status_remote_unresolvable_ref(
            fs, gf_inproc, mock_backend)
        _assert_error_fields(
            gf_inproc(*argv, backend=mock_backend, check=False), **fields)


class TestRealGitErrorFields:
    """Failures whose trigger needs real git semantics (conflict,
    symlinked worktree child) — real `gf` subprocess, tmp_path fixtures."""

    def test_pull_rebase_conflict_error_carries_fields(self, tmp_path):
        _run_real(_sc_real_rebase_conflict, tmp_path)

    def test_pull_autostash_pop_conflict_error_carries_fields(self, tmp_path):
        _run_real(_sc_real_autostash_pop_conflict, tmp_path)

    def test_rm_symlinked_child_error_carries_fields(self, tmp_path):
        _run_real(_sc_real_rm_symlinked_child, tmp_path)

    def test_status_remote_latest_unresolvable_error_carries_fields(
            self, tmp_path):
        """spec 'Reference model': `latest` falls back origin/HEAD →
        origin/main → origin/master and errors clearly when none
        resolve (the `could not resolve 'latest'` offender site);
        spec 'gf status --remote' — deterministic non-zero exit."""
        _run_real(_sc_real_status_remote_latest_unresolvable, tmp_path)


# --- message-shape sweeps ----------------------------------------------------
#
# The invariant pin: over the enumerated identified-folder failure
# surface, every emitted message carries name + path + operation, and
# the exit code is the spec'd deterministic one for that failure class.


def test_mock_identified_folder_error_sweep(fs, gf_inproc, mock_backend):
    failures = []
    for i, (label, scenario) in enumerate(MOCK_SCENARIOS):
        argv, fields = scenario(fs, gf_inproc, mock_backend, tag=f"s{i}")
        result = gf_inproc(*argv, backend=mock_backend, check=False)
        try:
            _assert_error_fields(result, **fields)
        except AssertionError as e:
            failures.append(f"{label}: {e}")
    assert not failures, "\n\n".join(failures)


def test_real_git_identified_folder_error_sweep(tmp_path):
    failures = []
    for label, scenario in REAL_SCENARIOS:
        case_dir = tmp_path / label.replace("/", "-")
        case_dir.mkdir()
        try:
            _run_real(scenario, case_dir)
        except AssertionError as e:
            failures.append(f"{label}: {e}")
    assert not failures, "\n\n".join(failures)
