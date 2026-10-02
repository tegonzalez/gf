# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`gf sh`/`git`/`diff`/`log` passthrough on resolved checkouts.

Pinned contract: `cli._git_env_for_child`/`_run_child_command`/
`cmd_git`/`cmd_sh`/`cmd_diff`/`cmd_log` — env derived from `co`
(GIT_DIR = the worktree admin dir `co.gitdir`, GIT_WORK_TREE =
`co.work_tree`); NO `os.chdir` in the run path; `-- .` appended to
diff/log only when `co.is_store_checkout` AND the user supplied no
pathspec — no `--` and no operand git itself would promote to one
(an existing path under cwd, or `* ? [`/`:` magic).

Scope: from a consumer link, `gf git commit` works; `gf git log` is
unscoped whole-repo history; `gf diff`/`gf log` append `-- .` iff the
user supplied no pathspec operand; relative paths resolve from the
mapped position;
absent-checkout failure exits via the gf error path (deterministic
code, named folder/path/operation), never a traceback.

Authorities: spec `gf sh` (L314-332), `gf git` (L336-349 — subfolder
runs with cwd at the PHYSICAL mapped subdir, no pathspec → whole-repo
history), `gf diff` (L351-369 — `-- .` appended only for subfolder
bindings with no user pathspec; user `--` passes untouched), `gf log`
(L371-388 — same `-- .` rule; an explicit `--` OR ANY PATHSPEC disables
the scope; `gf git log` for unscoped history),
process-replacement/streaming (L53); arch GF-D10; plan D6.

Scope notes: TTY-dependent exec paths are unverifiable in-process and
skipped; the pins cover the non-TTY streamed path (stdout pipes)
including child-exit-code propagation.
Real git over local bare upstreams via `gf clone`. Pathspecs that must
escape the mapped dir use `../../` — `../` resolves lexically on the
git prefix (`docs/api/../tools` == `docs/tools`).
"""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import gf, git
from gf import cli, layout


# ---------------------------------------------------------------------------
# helpers


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} rc={r.returncode}:\n{r.stderr}")
    return r


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path) -> Path:
    """Bare upstream whose history separates the mapped dir from the rest
    of the tree — scoped vs unscoped output is distinguishable:

      seed          docs/api/{x,y}.txt docs/api/sub/deep.txt tools/t.txt
      api change    docs/api/x.txt
      deep change   docs/api/sub/deep.txt
      tools change  tools/t.txt
    """
    up = tmp_path / "upstream"
    _git("init", "--bare", str(up))
    work = tmp_path / "_seed"
    _git("clone", str(up), str(work))
    (work / "docs" / "api" / "sub").mkdir(parents=True)
    (work / "tools").mkdir()
    for rel in ("docs/api/x.txt", "docs/api/y.txt",
                "docs/api/sub/deep.txt", "tools/t.txt"):
        (work / rel).write_text(rel)
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "seed")
    (work / "docs" / "api" / "x.txt").write_text("changed")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "api change")
    (work / "docs" / "api" / "sub" / "deep.txt").write_text("deeper")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "deep change")
    (work / "tools" / "t.txt").write_text("changed")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "tools change")
    _git("-C", work, "push", "-q", "origin", "master")
    return up


def _clone_pair(parent: Path, up: Path) -> None:
    """api -> docs/api and tools -> tools: two bindings sharing one
    store/checkout, so each link can dirty the other's half of the
    checkout."""
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")


def _checkout_wt(parent: Path) -> Path:
    """The shared checkout worktree root `<root>/.gf/wt/<rk>/master`."""
    return Path(parent / "vendor" / "api").resolve().parents[1]


# ---------------------------------------------------------------------------
# `gf git` — real git at the mapped position, unscoped


def test_git_commit_from_consumer_link(tmp_path):
    """`gf git` runs real git in the resolved checkout: writes through
    the consumer link, `git add` + `git commit` via gf succeed and the
    porcelain is clean afterward (spec L336-349; receiving item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)

    (parent / "vendor" / "api" / "wip.txt").write_text("uncommitted")
    gf("-C", str(parent / "vendor" / "api"), "git", "add", "-A")
    gf("-C", str(parent / "vendor" / "api"), "git", "commit", "-qm", "wip")
    r = gf("-C", str(parent / "vendor" / "api"), "git", "log",
           "--format=%s", "-1")
    assert r.stdout.strip() == "wip"


def test_git_log_is_unscoped_whole_repo_history(tmp_path):
    """`gf git log` gets NO pathspec — whole-repo history including
    commits outside the mapped dir (spec L343-ish; receiving item;
    discriminates against the `gf log` scope rule)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    r = gf("-C", str(parent / "vendor" / "api"), "git", "log",
           "--format=%s")
    subjects = r.stdout.split()
    assert subjects == ["tools", "change", "deep", "change", "api",
                        "change", "seed"]  # format=%s word-splits
    assert "tools change" in r.stdout


def test_git_diff_never_appends_pathspec(tmp_path):
    """`gf git diff` is raw passthrough — dirty in BOTH halves of the
    shared checkout shows both paths (negative arm of the `-- .` rule;
    spec L343: `gf git` is never scoped)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "vendor" / "api" / "x.txt").write_text("dirty")
    (parent / "vendor" / "tools" / "t.txt").write_text("dirty")
    r = gf("-C", str(parent / "vendor" / "api"), "git", "diff",
           "--name-only")
    assert sorted(r.stdout.split()) == ["docs/api/x.txt", "tools/t.txt"]


def test_relative_paths_resolve_from_mapped_position(tmp_path):
    """The subprocess cwd is the PHYSICAL mapped subdir: git reports the
    repo-relative prefix of the caller's position (spec L344 'relative
    paths resolve from the mapped position'; receiving item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    r = gf("-C", str(parent / "vendor" / "api"), "git", "rev-parse",
           "--show-prefix")
    assert r.stdout == "docs/api/\n"
    # deeper positions map to the corresponding checkout position
    r = gf("-C", str(parent / "vendor" / "api" / "sub"), "git",
           "rev-parse", "--show-prefix")
    assert r.stdout == "docs/api/sub/\n"


# ---------------------------------------------------------------------------
# `gf sh` — env + cwd at the mapped position


def test_sh_env_derived_from_checkout(tmp_path):
    """GIT_DIR is the worktree admin dir `co.gitdir`; GIT_WORK_TREE is
    `co.work_tree` (arch GF-D10)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    wt = _checkout_wt(parent)
    rk = wt.parent.name  # upstream-<hash>
    r = gf("-C", str(parent / "vendor" / "api"), "sh", "-c",
           'printf "%s|%s" "$GIT_DIR" "$GIT_WORK_TREE"')
    git_dir, work_tree = r.stdout.split("|")
    assert Path(work_tree) == wt
    assert Path(git_dir) == (
        parent / ".gf" / "repos" / rk / "git" / "worktrees" / "master")


def test_sh_cwd_is_physical_mapped_subdir(tmp_path):
    """`gf sh` runs at the checkout position, not the lexical link —
    `pwd -P` lands inside `.gf/wt`, and file writes land in the mapped
    subdir (visible back through the link) (spec L314-332 + L344)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    link = parent / "vendor" / "api"
    r = gf("-C", str(link), "sh", "-c", "pwd -P")
    assert Path(r.stdout.strip()) == link.resolve()
    assert ".gf/wt" in r.stdout

    gf("-C", str(link), "sh", "-c", "echo hi > wrote.txt")
    wrote = _checkout_wt(parent) / "docs" / "api" / "wrote.txt"
    assert wrote.read_text().strip() == "hi"
    assert (link / "wrote.txt").is_file()  # visible through the link


def test_sh_exit_code_propagates(tmp_path):
    """The streamed (non-TTY) run returns the child's exit code —
    spec L53 process-replacement/streaming semantics."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    r = gf("-C", str(parent / "vendor" / "api"), "sh", "-c", "exit 7",
           check=False)
    assert r.returncode == 7


# ---------------------------------------------------------------------------
# `gf diff`/`gf log` — `-- .` gating (both arms + negatives)


def test_log_appends_dot_pathspec_when_no_user_dashes(tmp_path):
    """With no user `--`, `gf log` appends `-- .`: history scoped to the
    mapped subdir — `tools change` (outside docs/api) is excluded
    (spec L371-388; receiving item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    r = gf("-C", str(parent / "vendor" / "api"), "log", "--format=%s")
    assert "api change" in r.stdout
    assert "deep change" in r.stdout
    assert "tools change" not in r.stdout


def test_log_scopes_to_deeper_cwd(tmp_path):
    """The appended `.` is the caller's prefix — from `vendor/api/sub`
    only commits touching `docs/api/sub` appear (`api change`, which
    touches the sibling x.txt, is excluded) (spec `-- .` = cwd)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    r = gf("-C", str(parent / "vendor" / "api" / "sub"), "log",
           "--format=%s")
    assert "deep change" in r.stdout
    assert "api change" not in r.stdout
    assert "tools change" not in r.stdout


def test_log_user_dashes_pass_untouched(tmp_path):
    """A user-supplied `--` suppresses the append: `gf log -- <spec>`
    forwards the pathspec verbatim — `-- ../../tools` escapes the
    mapped dir and finds the tools commits (spec L365; receiving
    item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    r = gf("-C", str(parent / "vendor" / "api"), "log", "--format=%s",
           "--", "../../tools")
    assert "tools change" in r.stdout


def test_diff_appends_dot_pathspec_when_no_user_dashes(tmp_path):
    """`gf diff` with no user `--` appends `-- .`: dirty in both halves
    of the shared checkout, only the mapped-dir path is reported
    (spec L351-369; receiving item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "vendor" / "api" / "x.txt").write_text("dirty")
    (parent / "vendor" / "tools" / "t.txt").write_text("dirty")
    r = gf("-C", str(parent / "vendor" / "api"), "diff", "--name-only")
    assert r.stdout.split() == ["docs/api/x.txt"]


def test_diff_user_dashes_pass_untouched(tmp_path):
    """`gf diff -- x.txt` forwards only the user's pathspec — the
    sibling dirty file y.txt stays out (`-- .` would union it in or
    corrupt the spec) (spec L365; receiving item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "vendor" / "api" / "x.txt").write_text("dirty")
    (parent / "vendor" / "api" / "y.txt").write_text("dirty")
    r = gf("-C", str(parent / "vendor" / "api"), "diff", "--name-only",
           "--", "x.txt")
    assert r.stdout.split() == ["docs/api/x.txt"]


def test_whole_repo_diff_and_log_never_append(tmp_path):
    """Whole-repo bindings are not subfolder bindings: `gf diff`/`gf log`
    inside one never get `-- .` — the checkout IS the repo root
    (`co.is_store_checkout` gate; spec L351-388; negative arm)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "libs/up")
    child = parent / "libs" / "up"
    (child / "docs" / "api" / "x.txt").write_text("dirty")
    (child / "tools" / "t.txt").write_text("dirty")
    r = gf("-C", str(child), "diff", "--name-only")
    assert sorted(r.stdout.split()) == ["docs/api/x.txt", "tools/t.txt"]
    r = gf("-C", str(child), "log", "--format=%s")
    assert "tools change" in r.stdout


# ---------------------------------------------------------------------------
# user pathspec OPERANDS suppress the scope (spec L385 "or any pathspec")


def test_log_positional_pathspec_disables_scope(tmp_path):
    """`gf log --format=%s x.txt` takes `x.txt` as the user's pathspec:
    rc 0 and exactly the commits that touch it (`api change`, `seed`).
    Appending `-- .` would strand the token on the rev side —
    `fatal: bad revision 'x.txt'` (spec L385 'any pathspec disables the
    scope'; the discriminating arm for the R4-B defect)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    r = gf("-C", str(parent / "vendor" / "api"), "log", "--format=%s",
           "x.txt")
    assert r.returncode == 0
    assert r.stdout.splitlines() == ["api change", "seed"]


def test_diff_positional_pathspec_disables_scope(tmp_path):
    """`gf diff --name-only x.txt` forwards only the user's pathspec:
    with `x.txt` dirty in the mapping AND `tools/t.txt` dirty outside
    it, just the named mapped path reports (spec L367 + L385; pre-fix
    the token hit the rev side of the appended `--` and git died
    rc=128 `fatal: bad revision 'x.txt'`)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "vendor" / "api" / "x.txt").write_text("dirty")
    (parent / "vendor" / "tools" / "t.txt").write_text("dirty")
    r = gf("-C", str(parent / "vendor" / "api"), "diff", "--name-only",
           "x.txt")
    assert r.returncode == 0
    assert r.stdout.split() == ["docs/api/x.txt"]


def test_log_option_value_keeps_scope(tmp_path):
    """An option VALUE is not a pathspec: `-n 1` must not unscope — the
    newest mapped-dir commit `deep change` reports, not the newer
    out-of-mapping `tools change` (spec L385; negative arm against
    treating every non-dash token as a pathspec)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    r = gf("-C", str(parent / "vendor" / "api"), "log", "--format=%s",
           "-n", "1")
    assert r.stdout.strip() == "deep change"


def test_log_rev_operand_keeps_scope(tmp_path):
    """A rev operand is not a pathspec: `gf log --format=%s master`
    keeps the `.` scope — out-of-mapping `tools change` stays excluded
    while the mapped-dir commits still report (spec L385; negative arm:
    a rev rides along with the scope, it does not disable it)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    r = gf("-C", str(parent / "vendor" / "api"), "log", "--format=%s",
           "master")
    assert "api change" in r.stdout
    assert "deep change" in r.stdout
    assert "tools change" not in r.stdout


# ---------------------------------------------------------------------------
# `_scoped_dot` unit pins — pure operand classification against a cwd


def _store_checkout(base: Path) -> layout.Checkout:
    """A store-checkout-shaped Checkout: `is_store_checkout` reads only
    `gitdir != common_dir`, the single `co` fact `_scoped_dot` consumes.
    Paths need not exist."""
    return layout.Checkout(
        gitdir=base / ".gf" / "repos" / "rk" / "git" / "worktrees" / "master",
        work_tree=base,
        common_dir=base / ".gf" / "repos" / "rk" / "git",
        subdir="docs/api",
        state=base / ".gf" / "repos" / "rk" / "state",
    )


@pytest.mark.parametrize("operand", ["x.txt", "*.c", ":(literal)x", "."])
def test_scoped_dot_user_pathspec_operand_returns_empty(tmp_path, operand):
    """Each operand git's own no-`--` promotion would take as a pathspec
    — a path existing under cwd, glob magic `* ? [`, `:`-magic, a literal
    `.` — suppresses the append entirely (spec L385 'any pathspec
    disables the scope')."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "x.txt").write_text("seed")  # the existing-path operand
    co = _store_checkout(tmp_path)
    assert cli._scoped_dot(["--format=%s", operand], co, cwd) == []


def test_scoped_dot_user_dashes_return_empty(tmp_path):
    """An explicit `--` suppresses the append however the pathspecs
    trail it (spec L367/L385 — the pre-existing suppression arm)."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    co = _store_checkout(tmp_path)
    assert cli._scoped_dot(["--format=%s", "--", "x.txt"], co, cwd) == []


@pytest.mark.parametrize("git_args", [
    ["-n"], ["1"], ["-n", "1"], ["no-such-name"], ["HEAD~1..HEAD"],
])
def test_scoped_dot_non_pathspec_tokens_still_append(tmp_path, git_args):
    """Flags, their values, names absent under cwd and rev expressions
    are NOT pathspecs — none exists on disk and none carries magic, so
    the `.` scope still appends (spec L385 negative arm)."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    co = _store_checkout(tmp_path)
    assert cli._scoped_dot(git_args, co, cwd) == ["--", "."]


# ---------------------------------------------------------------------------
# absent checkout — gf error path, never a traceback


def test_git_absent_checkout_error_carries_fields(tmp_path):
    """Checkout admin gone while the consumer link still resolves:
    `gf git` exits via the gf error path — deterministic code, the
    identified folder's name+path+operation in the message, no
    traceback (spec §Error handling envelope)."""
    import shutil
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    rk = _checkout_wt(parent).parent.name
    shutil.rmtree(
        parent / ".gf" / "repos" / rk / "git" / "worktrees" / "master")

    r = gf("-C", str(parent / "vendor" / "api"), "git", "status",
           check=False)
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    err = r.stderr + r.stdout
    assert "api" in err and "vendor/api" in err and "git" in err


def test_sh_absent_checkout_error_carries_fields(tmp_path):
    """Same absent-checkout envelope through the `gf sh` seam (op `sh`;
    spec §Error handling envelope)."""
    import shutil
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    rk = _checkout_wt(parent).parent.name
    shutil.rmtree(
        parent / ".gf" / "repos" / rk / "git" / "worktrees" / "master")

    r = gf("-C", str(parent / "vendor" / "api"), "sh", "-c", "true",
           check=False)
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    err = r.stderr + r.stdout
    assert "api" in err and "vendor/api" in err and "sh" in err
