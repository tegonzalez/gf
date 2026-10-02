# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Ambient GIT_* scrub: foreign repo env must not steer gf's git calls.

Fix under test (uncommitted): `backends.clean_environ()` drops the
repo-redirecting ambient `GIT_*` names (GIT_DIR, GIT_INDEX_FILE,
GIT_CONFIG_COUNT/PARAMETERS, GIT_CONFIG_KEY_*/GIT_CONFIG_VALUE_*,
the GIT_CONFIG/GLOBAL/SYSTEM file redirects, GIT_TEMPLATE_DIR,
GIT_DEFAULT_*, GIT_TEST_*, object/alternate dirs, pathspec modes, …)
before every `GitCliBackend.git` subprocess and inside
`cli._git_env_for_child`, while gf's deliberate overlays — the `env`
argument, `git_dir`, `work_tree`, `PWD` — are applied AFTER the scrub
and the kept channels (GIT_CONFIG_NOSYSTEM, GIT_AUTHOR_*/
GIT_COMMITTER_*, transport/identity knobs) still pass through.

Pins (each fails on the pre-fix code, except the overlay controls which
stay green both ways):

1. `GIT_DIR=<foreign>/.git` ambient: `gf -C parent worktree list` must
   enumerate ONLY the parent's worktrees — identical to the set the same
   `git worktree list --porcelain` reports when run directly in the
   parent (independent oracle). Pre-fix the spawned git resolves the
   foreign gitdir and lists ITS worktrees.
2. `GIT_INDEX_FILE=<sentinel>` ambient: `gf pull` must never let any
   git call create the sentinel — the checkout's own `$GIT_DIR/index`
   is the only index touched.
3. `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0` injection:
   a `git` inside gf sees the suite's real config — `gf sh -c 'git
   config protocol.file.allow'` prints `user` (the conftest
   $HOME-anchored gitconfig value), not the injected `never` — and a `gf
   clone` whose fetch the injected `protocol.file.allow=never` would
   refuse still succeeds.
4. Control: the deliberate overlays still land — `git_dir=` wins over
   ambient GIT_DIR in the spawned env (run_command-recording arm), and
   `gf sh -c 'echo $GIT_DIR'` prints the child gitdir even with an
   ambient GIT_DIR set.
5. The conftest contract survives the scrub under its new channel:
   inside `gf sh`, the env var GIT_CONFIG_GLOBAL itself is gone
   (scrubbed — fails on the pre-fix code, which passed it through),
   yet `git config` still resolves the suite's $HOME-anchored
   gitconfig and `git var` resolves the GF-test author/committer
   identity.
6. R15-F2/F3 blocklist additions: every newly blocked name —
   GIT_CONFIG/GLOBAL/SYSTEM, GIT_DEFAULT_HASH,
   GIT_DEFAULT_INITIAL_BRANCH_NAME, GIT_INDEX_VERSION,
   GIT_SHALLOW_FILE, GIT_NO_REPLACE_OBJECTS, GIT_TEMPLATE_DIR,
   GIT_SSH, GIT_EXTERNAL_DIFF, and the GIT_TEST_ prefix family —
   prints `unset` inside `gf sh` (pre-fix each passes through and
   prints its value).
7. Ambient GIT_TEMPLATE_DIR never seeds the child gitdir: a template
   fixture carrying an executable `hooks/post-checkout` leaves no file
   in `child/.gf/git/hooks/` after `gf clone` — only the stock
   `*.sample` set.
8. Ambient GIT_TEST_FSMONITOR never reaches a spawned `git status`:
   the hook script's marker file is absent after `gf status`.
9. Ambient GIT_DEFAULT_HASH=sha256 cannot reformat the child store:
   `gf clone` succeeds and the child reports `sha1`; pre-fix the fetch
   dies on `mismatched algorithms: client sha256; server sha1`.
10. Kept-channel controls stay green both ways: GIT_CONFIG_NOSYSTEM
    and the identity vars pass through, and $HOME/.gitconfig content
    still reaches the spawned `git config`.

Real git over local fixtures via the `gf`/`git` subprocess helpers, plus
one in-process arm spying the `gf.backends.run_command` boundary.
"""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import gf, git
from gf import backends
from gf.backends import GitCliBackend


# ---------------------------------------------------------------------------
# helpers (same real-git fixture style as test_pull_recovery)


def _git(*args, check: bool = True, cwd: Path | None = None):
    """Real git with captured output (fixture helper)."""
    r = subprocess.run(
        ["git", *map(str, args)],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True)
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


def _upstream(tmp_path: Path, name: str = "upstream") -> Path:
    """Bare upstream with docs/api/x.txt + tools/t.txt on master."""
    up = tmp_path / name
    _git("init", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text(f"api on master in {name}")
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text(f"tool on master in {name}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "init")
    _git("-C", work, "push", "origin", "master")
    return up


def _worktree_lines(porcelain: str) -> set[str]:
    """`worktree <path>` entries of a `worktree list --porcelain` output."""
    return {
        line.split(" ", 1)[1]
        for line in porcelain.splitlines()
        if line.startswith("worktree ")
    }


# ---------------------------------------------------------------------------
# 1 — ambient GIT_DIR must not retarget `gf worktree list`


def test_worktree_list_ignores_ambient_foreign_git_dir(
        tmp_path, monkeypatch):
    """`GIT_DIR=<foreign>/.git` in gf's env points a bare `git worktree
    list` at the foreign repo — the spawned command gets cwd=parent and
    no git_dir overlay, so the inherited var is the ONLY repo pointer
    (pre-fix it wins). Post-fix the scrub drops it and discovery via cwd
    lands on the parent: the listed set equals the oracle that direct
    git reports for the parent — never the foreign repo's."""
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "child")  # materializes gf.toml
    parent_wt = tmp_path / "parent-wt"
    git("worktree", "add", str(parent_wt), "-b", "parentside", cwd=parent)

    foreign = tmp_path / "foreign"
    git("init", str(foreign), cwd=tmp_path)
    (foreign / "f.txt").write_text("foreign")
    git("add", "f.txt", cwd=foreign)
    git("commit", "-m", "f", cwd=foreign)
    foreign_wt = tmp_path / "foreign-wt"
    git("worktree", "add", str(foreign_wt), "-b", "outside", cwd=foreign)

    # Independent oracle taken BEFORE the env is poisoned (the fixture
    # helpers inherit os.environ).
    expected = _worktree_lines(
        _git("worktree", "list", "--porcelain", cwd=parent).stdout)
    assert expected == {
        str(parent.resolve()), str(parent_wt.resolve())}

    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    r = gf("-C", str(parent), "worktree", "list", "--porcelain")

    listed = _worktree_lines(r.stdout)
    assert listed == expected, (
        f"gf listed {listed}, parent owns {expected}")
    # the foreign repo's worktrees must not appear at all
    assert str(foreign.resolve()) not in listed
    assert str(foreign_wt.resolve()) not in listed


# ---------------------------------------------------------------------------
# 2 — ambient GIT_INDEX_FILE sentinel is never written by `gf pull`


def test_pull_never_writes_ambient_git_index_file(tmp_path, monkeypatch):
    """An ambient GIT_INDEX_FILE retargets every index-touching git the
    pull spawns (`status --porcelain` drift check, `checkout -B`) onto
    the sentinel path — pre-fix the pull's checkout CREATES it. Post-fix
    the pull runs against the checkout's own index and the sentinel
    path never appears.

    `--force` keeps the pull running past the drift gate: the leaked
    path reads as a missing (empty) index, so `status` reports the whole
    checkout dirty — a plain pull would stop at "is dirty" before ever
    reaching the index-writing checkout this pin watches."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api")

    sentinel = tmp_path / "ambient-index"
    monkeypatch.setenv("GIT_INDEX_FILE", str(sentinel))
    r = gf("-C", str(parent), "pull", "--force")
    assert "Pulled api" in r.stdout
    assert not os.path.lexists(sentinel), (
        f"ambient GIT_INDEX_FILE was created by a pull-spawned git: "
        f"{sentinel}")


# ---------------------------------------------------------------------------
# 3 — GIT_CONFIG_COUNT/GIT_CONFIG_KEY_*/GIT_CONFIG_VALUE_* injection


def test_child_git_sees_global_config_not_injected_pairs(
        tmp_path, monkeypatch):
    """A `git` run inside `gf sh` must read the suite's real config, not
    env-injected pairs: `protocol.file.allow` resolves to the conftest
    $HOME-anchored gitconfig value `user`, never the injected `never`.
    Pre-fix the KEY/VALUE pair rides the inherited env and prints
    `never`."""
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "child")
    child = parent / "child"

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")

    r = gf("sh", "-c", "git config protocol.file.allow", cwd=child)
    assert r.stdout.strip() == "user", (
        f"injected config leaked into the child (got {r.stdout!r})")


def test_clone_fetch_ignores_injected_protocol_file_deny(
        tmp_path, monkeypatch):
    """Same injection through `GitCliBackend.git` itself: an ambient
    `protocol.file.allow=never` would refuse the clone's store fetch at
    the transport layer (the old denial mechanism the suite used). Under
    the scrub the fetch sees only the suite $HOME gitconfig's `user`
    policy and the clone serves the binding."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")

    r = gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api",
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    link = parent / "vendor" / "api"
    assert (link / "x.txt").read_text().strip() == \
        "api on master in upstream"


# ---------------------------------------------------------------------------
# 4 — control: gf's deliberate overlays still land in the spawned env


def test_backend_git_dir_overlay_wins_over_ambient(
        tmp_path, monkeypatch):
    """Unit arm at the `run_command` boundary (the seam where the env
    crosses to the subprocess): with an ambient GIT_DIR set, the
    `git_dir=` kwarg still wins in the spawned env, a caller `env=` pair
    lands verbatim, and `cwd=` lands as PWD. Passes pre- and post-fix —
    the fix changes only the BASE the overlays merge onto."""
    repo = tmp_path / "repo"
    git("init", str(repo), cwd=tmp_path)

    recorded: list[tuple[list[str], dict]] = []
    orig = backends.run_command

    def _spy(cmd, env, *, mode, cwd=None):
        recorded.append((list(cmd), dict(env)))
        return orig(cmd, env, mode=mode, cwd=cwd)

    monkeypatch.setattr(backends, "run_command", _spy)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign" / ".git"))

    GitCliBackend().git(
        "rev-parse", "--git-dir", cwd=tmp_path,
        git_dir=repo / ".git", env={"GF_MARK": "yes"})

    cmd, env = recorded[-1]
    assert cmd == ["git", "rev-parse", "--git-dir"]
    assert env["GIT_DIR"] == str(repo / ".git")
    assert env["GF_MARK"] == "yes"
    assert env["PWD"] == str(tmp_path)


def test_gf_sh_sets_child_git_dir_over_ambient(tmp_path, monkeypatch):
    """`gf sh` contract: the child's git sees gf's OWN GIT_DIR — the
    checkout gitdir — even with a foreign ambient GIT_DIR present.
    Pre-fix `_git_env_for_child` also overwrote the inherited var, so
    this arm stays green both ways; it exists to prove the scrub does
    not eat the deliberate overlay."""
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "child")
    child = parent / "child"

    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign" / ".git"))
    r = gf("sh", "-c", 'printf %s "$GIT_DIR"', cwd=child)
    assert r.stdout.strip() == str((child / ".gf" / "git").resolve())


# ---------------------------------------------------------------------------
# 5 — the conftest contract survives the scrub under its new channel


def test_child_env_keeps_identity_and_home_config(tmp_path):
    """Inside `gf sh`: the GIT_CONFIG_* env vars are scrubbed (the
    file-redirect channel is closed — fails on the pre-fix code, which
    passed GIT_CONFIG_GLOBAL through), yet the suite's git config still
    reaches the child through $HOME/.gitconfig (`git config
    protocol.file.allow` → `user`) and `git var` resolves the propagated
    GF-test author/committer identity (the `_git_env` autouse env)."""
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "child")
    child = parent / "child"

    r = gf("sh", "-c",
           'echo "GCFG=${GIT_CONFIG_GLOBAL-unset}"; '
           "git config protocol.file.allow; "
           "git var GIT_AUTHOR_IDENT; "
           "git var GIT_COMMITTER_IDENT",
           cwd=child)
    lines = r.stdout.splitlines()
    assert "GCFG=unset" in lines, r.stdout
    assert "user" in lines, r.stdout
    assert sum(
        line.startswith("gf-test <test@git-folders>") for line in lines
    ) == 2, r.stdout


# ---------------------------------------------------------------------------
# 6 — R15-F2/F3 blocklist additions: each newly blocked name is gone
#     from the `gf sh` child env


@pytest.mark.parametrize("var", [
    # config-file redirects — the suite's old pinning channel, now
    # closed (the pins moved to $HOME/.gitconfig)
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    # init/object/ref semantics
    "GIT_DEFAULT_HASH",
    "GIT_DEFAULT_INITIAL_BRANCH_NAME",
    "GIT_INDEX_VERSION",
    "GIT_SHALLOW_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    # executable content planted through the init template copy
    "GIT_TEMPLATE_DIR",
    # caller-chosen programs
    "GIT_SSH",
    "GIT_EXTERNAL_DIFF",
    # the GIT_TEST_ prefix family: one real member, plus a name that
    # exists only to prove the match is by prefix, not a fixed list
    "GIT_TEST_FSMONITOR",
    "GIT_TEST_AMBIENT_PIN",
])
def test_newly_blocked_env_never_reaches_gf_sh(
        tmp_path, monkeypatch, var):
    """Every name the widened blocklist added must be absent from the
    env a `gf sh` child runs under: `printf %s "${VAR-unset}"` prints
    `unset` post-fix, while pre-fix the inherited var passed straight
    through `_git_env_for_child` and prints its value. The values are
    inert markers — `gf sh` resolves the child purely on the
    filesystem and never runs git itself, so even the config-file and
    program redirects cannot fault the spawn."""
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "child")
    child = parent / "child"

    monkeypatch.setenv(var, f"ambient:{var}")
    r = gf("sh", "-c", f'printf %s "${{{var}-unset}}"', cwd=child)
    assert r.stdout == "unset", (
        f"{var} reached the `gf sh` child env: {r.stdout!r}")


# ---------------------------------------------------------------------------
# 7 — ambient GIT_TEMPLATE_DIR never seeds the child gitdir's hooks


def test_clone_init_ignores_ambient_git_template_dir(
        tmp_path, monkeypatch):
    """`git init` copies `$GIT_TEMPLATE_DIR/hooks/*` into the fresh
    gitdir — an ambient template plants an EXECUTABLE post-checkout
    into every child `gf clone` creates (it even runs on the clone's
    own checkout). Post-fix the init sees no template override, so
    `child/.gf/git/hooks/` holds only the stock `*.sample` set — the
    pin asserts on the directory LISTING, not on whether the hook ran
    (a later-landing overlay would silence a ran-marker anyway).
    Pre-fix the listing is exactly the fixture's `post-checkout`."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)

    tmpl = tmp_path / "tmpl"
    hook = tmpl / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmpl))

    r = gf("-C", str(parent), "clone", str(up), "child", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    names = {
        p.name
        for p in (parent / "child" / ".gf" / "git" / "hooks").iterdir()
    }
    assert "post-checkout" not in names, (
        f"ambient GIT_TEMPLATE_DIR planted {sorted(names)}")
    assert names and all(n.endswith(".sample") for n in names), (
        f"child hooks dir holds non-sample files: {sorted(names)}")


# ---------------------------------------------------------------------------
# 8 — the GIT_TEST_ family: an ambient GIT_TEST_FSMONITOR hook never
#     fires inside `gf status`


def test_status_never_invokes_ambient_fsmonitor_hook(
        tmp_path, monkeypatch):
    """GIT_TEST_FSMONITOR is git's own test-only injection: when set,
    `git status` treats the named file as the fsmonitor hook and
    EXECUTES it (the exit status only decides whether git trusts the
    answer — the process has already run). `gf status` spawns
    `status --porcelain` through `GitCliBackend.git`; post-fix the
    scrub drops the var so the hook's marker never appears, pre-fix
    the leaked env runs the script. A `gf init` child suffices —
    fsmonitor fires even on an unborn HEAD."""
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "child")

    marker = tmp_path / "fsmonitor-ran"
    script = tmp_path / "fsmonitor-hook"
    script.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    script.chmod(0o755)
    monkeypatch.setenv("GIT_TEST_FSMONITOR", str(script))

    gf("-C", str(parent), "status")
    assert not marker.exists(), (
        "ambient GIT_TEST_FSMONITOR hook ran inside gf's status")


# ---------------------------------------------------------------------------
# 9 — ambient GIT_DEFAULT_HASH cannot reformat the child store


def test_clone_ignores_ambient_git_default_hash(tmp_path, monkeypatch):
    """`git init` honors GIT_DEFAULT_HASH: with `sha256` ambient, the
    child gitdir is created in the sha256 object format and the first
    `git fetch origin` against the sha1 fixture upstream dies on
    `mismatched algorithms: client sha256; server sha1` — the ambient
    var doesn't just leak, it breaks the clone. Post-fix init and
    fetch run sha1 end-to-end and the child reports the upstream's own
    format."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)

    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")
    r = gf("-C", str(parent), "clone", str(up), "child", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    fmt = gf("sh", "-c", "git rev-parse --show-object-format",
             cwd=parent / "child")
    assert fmt.stdout.strip() == "sha1", fmt.stdout


# ---------------------------------------------------------------------------
# 10 — kept-channel controls: GIT_CONFIG_NOSYSTEM, identity, and the
#      $HOME/.gitconfig file channel still reach the spawned git


def test_kept_channels_still_reach_gf_sh(tmp_path):
    """Green both ways — the control that the scrub strips only the
    blocklist: GIT_CONFIG_NOSYSTEM (a removal-only switch, never a
    redirect) and the GF-test identity vars pass through verbatim, and
    the suite's git config still reaches the child through the new
    $HOME/.gitconfig channel now that the GIT_CONFIG_* file redirects
    are closed. A bespoke key appended to that file must resolve in
    `git config` inside `gf sh`."""
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "child")
    child = parent / "child"

    gitconfig = Path(os.environ["HOME"]) / ".gitconfig"
    gitconfig.write_text(
        gitconfig.read_text() + "[gf-pin]\n\tchannel = home-gitconfig\n")

    r = gf("sh", "-c",
           'echo "NOSYSTEM=${GIT_CONFIG_NOSYSTEM-unset}"; '
           'echo "AUTHOR=${GIT_AUTHOR_NAME-unset}"; '
           "git config gf-pin.channel",
           cwd=child)
    lines = r.stdout.splitlines()
    assert "NOSYSTEM=1" in lines, r.stdout
    assert "AUTHOR=gf-test" in lines, r.stdout
    assert "home-gitconfig" in lines, r.stdout
