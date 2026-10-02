# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Regression pins for confirmed defects (hardening loop).

Each test pins the behavior the governing docs fix and drives the real
`gf` CLI over local bare upstreams (no network, fixture-isolated). The
pins ran as ``xfail(strict=True, raises=AssertionError)`` while the
defects stood; each marker was removed in the commit that landed its
fix. Only the claim assertions raise ``AssertionError``; setup and
scenario preconditions stop through ``pytest.fail`` so a broken fixture
reports as a real failure, never as an assertion failure.

Authorities (docs/gf-spec.md unless noted):

- Unborn `gf init` child: `gf init` creates an empty child; `gf ls` lists
  every manifest git-folder, one line per child, `*` on uncommitted
  changes; `gf status` prints one header per child followed by its
  porcelain lines; the drift algorithm raises a clear error with a
  deterministic non-zero exit when the effective ref cannot be resolved
  locally. Ruling U1 fixes the unborn child's rendering: `[]` for the
  branch bracket and `?` for the hash; when the effective ref does
  resolve locally, drift reports `behind` or `both` — an unborn HEAD
  can never match the resolved SHA.
- Subfolder-to-whole-repo url switch: Repo store / Checkout terminology,
  "A whole-repo binding and subfolder bindings of the same repository do
  not share object storage", "a changed repo URL selects a different repo
  store", and gf-arch.md GF-D5/GF-D7 store-and-checkout contract. Ruling
  U2 fixes that the switch is refused (exit 1, naming the binding and
  the rejected url); ruling U3 fixes that pull checks every selected
  binding before changing any.
- Pinned ref added upstream after the store exists / retry after a
  failed first store fetch: `gf clone` subfolder bullet (create or reuse
  the store, fetch, resolve the ref in the repo store, detached checkout
  for a tag), Failure cleanup, and Checkout integrity (worktree record
  at `<store>/worktrees/<checkout-key>`). Ruling R6 fixes that a joining
  clone appends-then-fetches unconditionally; R7 fixes that a failed
  clone removes the store it created.
- `--depth` on a store-creating subfolder clone: `gf clone --depth`
  bullet and gf-arch.md GF-D12.
- Dot-segment `<path>` leaf (`missing/..`): `gf clone`'s occupancy
  refusal ("Fail if the path exists, is not empty, and is not a
  git-folder or a consumer link"), `gf init`'s refusal of a directory
  that already contains `.git`/`.gf`, the child-path containment
  refusal, and the failure-cleanup contract ("remove the child
  directory ... so no partial state is left behind" — the parent
  worktree is never the child directory). Ruling R4-A fixes that a
  `.`/`..` leaf is normalized through realpath before the guards, so a
  leaf that resolves to the parent root hits the occupancy refusal
  before anything is created — unnormalized, `child.exists()` stat'd
  through the nonexistent `missing/` intermediate and the failed-fetch
  cleanup `rmtree`d `missing/..`, which is the parent root itself.
- Manifest-collision `gf init` refusals: spec `gf init` ("Refuse to
  initialize a directory that already contains a `.git` or `.gf`
  directory ..."), §Error handling (`git-folder '<name>' already exists
  in manifest` / `path <rel> is already used by git-folder '<other>'`,
  rc 1), and the failure-cleanup contract's invariant that a refused
  command leaves no partial state behind. The collision dies ran only
  after `target.mkdir`/`init_git_folder`, so `gf init kid2 -n dup`
  created `kid2/` and `kid2/.gf/git` before refusing — and the
  corrected retry died `kid2 already has a .gf directory` on the
  half-made child.
"""

import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

import pytest

from conftest import gf, git


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


def _push_update(tmp_path: Path, up: Path, rel: str, text: str,
                 tag: str | None = None) -> str:
    """Commit `rel`=`text` on upstream master (optionally tagged); return SHA."""
    work = tmp_path / f"_seed_{up.name}"
    (work / rel).write_text(text)
    _git("-C", work, "commit", "-qam", f"update {rel}")
    _git("-C", work, "push", "-q", "origin", "master")
    if tag:
        _git("-C", work, "tag", tag)
        _git("-C", work, "push", "-q", "origin", tag)
    return _git("-C", work, "rev-parse", "HEAD").stdout.strip()


def _single(paths: list[Path], what: str) -> Path:
    if len(paths) != 1:
        pytest.fail(f"setup: expected exactly one {what}, found {paths}")
    return paths[0]


def _store(parent: Path) -> Path:
    return _single(sorted((parent / ".gf" / "repos").glob("*/git")),
                   "repo store")


def _checkout_admin(parent: Path, link: Path, subdir: str) -> Path:
    """The worktree record of the checkout serving `link` (spec L103)."""
    checkout = Path(os.path.realpath(link))
    for _ in Path(subdir).parts:
        checkout = checkout.parent
    return _store(parent) / "worktrees" / checkout.name


API = {"docs/api/reference.md": "api v1", "docs/guide/intro.md": "guide v1"}


# ---------------------------------------------------------------------------
# Defect 1 — a `gf init` child with no commits breaks ls and status


def _unborn_child_parent(tmp_path: Path) -> tuple[Path, Path]:
    """Parent with a whole-repo binding `lib`, a subfolder binding `api`,
    and a `gf init` child `scratch` with no commits, one staged file and
    one untracked file."""
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up), "vendor/lib")
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api")
    _setup_gf("-C", str(parent), "init", "scratch")
    scratch = parent / "scratch"
    gitdir = scratch / ".gf" / "git"
    if _git("--git-dir", gitdir, "rev-parse", "--verify", "HEAD",
            check=False).returncode == 0:
        pytest.fail("setup: `gf init` child unexpectedly has a commit")
    (scratch / "staged.txt").write_text("staged")
    _git("--git-dir", gitdir, "--work-tree", scratch, "add", "staged.txt")
    (scratch / "unstaged.txt").write_text("unstaged")
    return parent, up


def test_ls_lists_unborn_init_child_and_every_other_binding(tmp_path):
    """`gf ls` lists all git-folders in the manifest, one line per child,
    with `*` appended when the child has uncommitted changes (spec
    `gf ls`). A `gf init` child with no commits is a state `gf init`
    itself produces (spec `gf init`), so it neither fails the command
    nor suppresses the other bindings' rows. Ruling U1 fixes the unborn
    child's rendering: `[]` for the branch bracket and `?` for the hash.
    """
    parent, up = _unborn_child_parent(tmp_path)

    r = gf("-C", str(parent), "ls", check=False)
    out = r.stdout
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert re.search(rf"^lib\s+{re.escape(str(up))}\s+\[", out, re.M), out
    assert re.search(
        rf"^api\s+{re.escape(f'{up}/docs/api')}\s+\[", out, re.M), out
    row = re.search(r"^scratch\s+scratch\s+\[\]\s+\?\*?$", out, re.M)
    assert row, out
    assert row.group(0).endswith("*"), row.group(0)


def test_status_reports_unborn_init_child_porcelain_and_every_other_binding(
        tmp_path):
    """`gf status` prints one `name url [branch]` header per child
    followed by `git status --porcelain` lines for its dirty files (spec
    `gf status`). The unborn child's staged and untracked files are
    reported under its header and the other bindings keep their headers.
    Ruling U1 fixes the unborn child's header bracket to `[]`."""
    parent, up = _unborn_child_parent(tmp_path)

    r = gf("-C", str(parent), "status", check=False)
    lines = r.stdout.splitlines()
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    headers = {
        "lib": rf"^lib\s+{re.escape(str(up))}\s+\[[^\]]*\]$",
        "api": rf"^api\s+{re.escape(f'{up}/docs/api')}\s+\[[^\]]*\]$",
        "scratch": r"^scratch\s+scratch\s+\[\]$",
    }
    at = {}
    for name, pattern in headers.items():
        idx = [i for i, ln in enumerate(lines) if re.match(pattern, ln)]
        assert len(idx) == 1, (name, r.stdout)
        at[name] = idx[0]
    following = [i for i in at.values() if i > at["scratch"]]
    block = lines[at["scratch"] + 1:min(following, default=len(lines))]
    assert "A  staged.txt" in block, r.stdout
    assert "?? unstaged.txt" in block, r.stdout


def test_status_remote_unborn_init_child_reports_unresolvable_ref(tmp_path):
    """The drift algorithm resolves the effective ref before reading HEAD;
    when it cannot be resolved locally, `gf status --remote` raises a
    clear error and exits with a deterministic non-zero code instead of a
    drift state (spec `gf status`, Drift algorithm steps 2-5). The unborn
    `gf init` child has a HEAD file (not `missing`) and no remote-tracking
    refs, so its `latest` is unresolvable: the exit code must equal the one
    for a committed child whose `latest` is unresolvable, and the error
    must name the ref and the folder."""
    parent, _ = _unborn_child_parent(tmp_path)

    # Reference: a committed whole-repo child whose `latest` has no local
    # remote-tracking ref to resolve against.
    lib_git = parent / "vendor" / "lib" / ".gf" / "git"
    _git("--git-dir", lib_git, "symbolic-ref", "-d",
         "refs/remotes/origin/HEAD", check=False)
    for ref in _git("--git-dir", lib_git, "for-each-ref", "--format=%(refname)",
                    "refs/remotes/origin").stdout.split():
        _git("--git-dir", lib_git, "update-ref", "-d", ref)
    ref_run = gf("-C", str(parent), "status", "--remote", "vendor/lib",
                 check=False)
    if ref_run.returncode == 0:
        pytest.fail(f"setup: reference unresolvable-ref case exited 0:\n"
                    f"{ref_run.stdout}")

    r = gf("-C", str(parent), "status", "--remote", "scratch", check=False)
    err = r.stderr + r.stdout
    assert r.returncode == ref_run.returncode, (
        r.returncode, ref_run.returncode, err)
    assert "latest" in err, err
    assert "scratch" in err, err
    assert not re.search(
        r"^scratch\s.*\b(clean|behind|local-dirty|both|missing)$",
        r.stdout, re.M), r.stdout


def test_status_remote_unborn_init_child_with_resolvable_ref_reports_behind(
        tmp_path):
    """F3 witness: an unborn `gf init` child whose effective ref DOES
    resolve locally reports a drift state instead of a git error. After
    `latest` resolves (remote configured, upstream fetched into the
    child's gitdir) the unborn HEAD can never match the resolved SHA, so
    `gf status --remote` reports `behind` on a clean worktree and `both`
    once the child has local changes (spec `gf status`, Drift
    algorithm)."""
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "init", "scratch")
    scratch = parent / "scratch"
    gitdir = scratch / ".gf" / "git"
    if _git("--git-dir", gitdir, "rev-parse", "--verify", "HEAD",
            check=False).returncode == 0:
        pytest.fail("setup: `gf init` child unexpectedly has a commit")

    # Resolve `latest` without touching HEAD: configure the upstream as
    # the child's origin and fetch so refs/remotes/origin/master exists.
    _git("--git-dir", gitdir, "--work-tree", scratch,
         "remote", "add", "origin", str(up))
    _git("--git-dir", gitdir, "--work-tree", scratch,
         "fetch", "-q", "origin")
    if _git("--git-dir", gitdir, "show-ref", "--verify",
            "refs/remotes/origin/master",
            check=False).returncode != 0:
        pytest.fail("setup: fetch did not create "
                    "refs/remotes/origin/master")

    scratch_row = r"^scratch\s+scratch\s+\[\]\s+(\S+)$"
    r = gf("-C", str(parent), "status", "--remote", "scratch",
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    row = re.search(scratch_row, r.stdout, re.M)
    assert row, r.stdout
    assert row.group(1) == "behind", r.stdout

    (scratch / "local.txt").write_text("uncommitted")
    r = gf("-C", str(parent), "status", "--remote", "scratch",
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    row = re.search(scratch_row, r.stdout, re.M)
    assert row, r.stdout
    assert row.group(1) == "both", r.stdout


# ---------------------------------------------------------------------------
# Defect 2 — a subfolder→whole-repo url switch mutates the shared store


def test_url_switch_subfolder_to_whole_repo_leaves_shared_store_intact(
        tmp_path):
    """Ruling U2: switching a subfolder binding to a whole repository is
    refused — `gf pull` exits 1 and the error names the binding and says
    the switch is not supported. Ruling U3: pull checks every selected
    binding before changing any, so the refusal leaves the selected
    sibling's pending update unapplied — no fetch, no apply. The shared
    repo store, its checkout, both consumer links and the manifest
    entry stay exactly as cloned (spec Terminology, Subfolder-binding
    layout, URL resolution; gf-arch.md GF-D5/GF-D7)."""
    up = _upstream(tmp_path, "upstream", API)
    other = _upstream(tmp_path, "other", {"other.md": "other repo"})
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api")
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/guide", "vendor/guide")

    store = _store(parent)
    admin = _checkout_admin(parent, parent / "vendor" / "guide", "docs/guide")
    origin_before = _git("--git-dir", store, "config",
                         "remote.origin.url").stdout.strip()
    head_before = _git("--git-dir", admin, "rev-parse", "HEAD").stdout.strip()
    branch_before = _git("--git-dir", admin, "symbolic-ref",
                         "HEAD").stdout.strip()
    remote_tip_before = _git("--git-dir", store, "rev-parse",
                             "refs/remotes/origin/master").stdout.strip()
    if origin_before != str(up) or branch_before != "refs/heads/master":
        pytest.fail(f"setup: unexpected store state {origin_before!r} "
                    f"{branch_before!r}")

    # Give the sibling a pending upstream update: under U3's
    # check-all-then-apply the refusal must leave it unapplied.
    _push_update(tmp_path, up, "docs/guide/intro.md", "guide v2")

    (parent / "gf.local.toml").write_text(
        f'[[git_folder_override]]\nname = "api"\nurl = "{other}"\n')
    r = gf("-C", str(parent), "pull", check=False)

    # U2: the switch is refused — exit 1, message names the binding
    # and the rule, and identifies the rejected url.
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    err = r.stderr + r.stdout
    assert "api" in err, err
    assert "vendor/api" in err, err
    assert ("switching a subfolder binding to a whole repository "
            "is not supported") in err, err
    assert str(other) in err, err

    # The shared store and its checkout are provably untouched — no
    # fetch and no checkout change happened before the refusal.
    assert _git("--git-dir", store, "config",
                "remote.origin.url").stdout.strip() == origin_before
    assert _git("--git-dir", store, "rev-parse",
                "refs/remotes/origin/master").stdout.strip() == \
        remote_tip_before
    assert _git("--git-dir", admin, "rev-parse",
                "HEAD").stdout.strip() == head_before
    assert _git("--git-dir", admin, "symbolic-ref",
                "HEAD").stdout.strip() == branch_before

    # U3: the sibling's pending update was never applied, and both
    # consumer links keep serving their recorded content.
    guide = parent / "vendor" / "guide"
    assert guide.is_symlink(), "sibling consumer link removed"
    assert sorted(os.listdir(guide)) == ["intro.md"]
    assert (guide / "intro.md").read_text() == "guide v1"
    api = parent / "vendor" / "api"
    assert api.is_symlink(), "consumer link removed"
    assert sorted(os.listdir(api)) == ["reference.md"]
    assert (api / "reference.md").read_text() == "api v1"

    # The manifest entry still records the binding's subfolder url.
    manifest = tomllib.loads((parent / "gf.toml").read_text())
    entry = next(e for e in manifest["git_folder"] if e["name"] == "api")
    assert entry["url"] == f"{up}/docs/api", manifest


# ---------------------------------------------------------------------------
# Defect 3 — a joining clone resolves its ref without refreshing the store


def test_clone_subfolder_tag_created_after_store_exists(tmp_path):
    """`gf clone` creates or reuses the repo store, fetches, resolves the
    effective ref in the repo store, and checks a tag out detached (spec
    `gf clone` subfolder bullet and ref bullets). A tag that exists
    upstream when the second clone runs is therefore checked out, even
    though it was pushed after the first clone created the store."""
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api")
    tag_sha = _push_update(tmp_path, up, "docs/api/reference.md", "api v2",
                           tag="v2")

    r = gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api-v2",
           "-b", "v2", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    link = parent / "vendor" / "api-v2"
    assert link.is_symlink()
    assert (link / "reference.md").read_text() == "api v2"
    admin = _checkout_admin(parent, link, "docs/api")
    assert _git("--git-dir", admin, "rev-parse",
                "HEAD").stdout.strip() == tag_sha
    assert _git("--git-dir", admin, "symbolic-ref", "-q", "HEAD",
                check=False).returncode != 0, "tag checkout is not detached"


def test_clone_subfolder_retry_after_failed_initial_store_fetch(
        tmp_path, monkeypatch):
    """A subfolder clone whose first store fetch fails, retried once the
    upstream is reachable, succeeds: the failed attempt's cleanup removes
    the store it created (ruling R7, spec Failure cleanup), so the retry
    creates the store fresh, fetches, and resolves the ref in it (spec
    `gf clone` subfolder bullet). The first attempt's transport is
    refused through git's own config (`protocol.file.allow=never`),
    which fails the fetch after the store is initialised."""
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")
    first = gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api",
               check=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT")
    monkeypatch.delenv("GIT_CONFIG_KEY_0")
    monkeypatch.delenv("GIT_CONFIG_VALUE_0")
    if first.returncode == 0 or "fetch" not in first.stderr:
        pytest.fail(f"setup: first clone did not fail at the fetch:\n"
                    f"rc={first.returncode}\n{first.stderr}")

    r = gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api",
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    link = parent / "vendor" / "api"
    assert link.is_symlink()
    assert (link / "reference.md").read_text() == "api v1"


# ---------------------------------------------------------------------------
# Defect 4 — --depth is not applied when a subfolder clone creates the store


def test_clone_subfolder_depth_creates_shallow_store(tmp_path):
    """`--depth <n>` is passed to the initial fetch as `--depth=<n>`; for
    a subfolder binding it applies when this clone creates the repo store
    (spec `gf clone --depth`; gf-arch.md GF-D12). A store created by
    `gf clone <repo>/<subdir> --depth 1` is shallow with one commit of
    history behind the checkout."""
    up = _upstream(tmp_path, "upstream", API)
    _push_update(tmp_path, up, "docs/api/reference.md", "api v2")
    parent = _parent(tmp_path)

    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api",
              "--depth", "1")
    store = _store(parent)
    admin = _checkout_admin(parent, parent / "vendor" / "api", "docs/api")
    if (parent / "vendor" / "api" / "reference.md").read_text() != "api v2":
        pytest.fail("setup: clone did not check out the upstream tip")

    shallow = _git("--git-dir", store, "rev-parse",
                   "--is-shallow-repository").stdout.strip()
    depth = _git("--git-dir", admin, "rev-list", "--count",
                 "HEAD").stdout.strip()
    assert shallow == "true", shallow
    assert depth == "1", depth


# ---------------------------------------------------------------------------
# Defect 5 — a `.`/`..` <path> leaf stat's through a missing intermediate
# (ruling R4-A)


def _occupied_parent(tmp_path: Path, dirname: str = "parent") -> Path:
    """Parent repo with one committed tracked file and a committed
    (empty) `gf.toml`, so a refused `gf clone`/`init` must leave every
    byte — the tracked file, `.git`, and the manifest — untouched.
    `dirname` may carry intermediate directories."""
    parent = tmp_path / dirname
    parent.parent.mkdir(parents=True, exist_ok=True)
    git("init", "-q", str(parent), cwd=tmp_path)
    (parent / "tracked.txt").write_text("precious\n")
    (parent / "gf.toml").write_text("# git-folders manifest\n")
    git("add", "tracked.txt", "gf.toml", cwd=parent)
    git("commit", "-qm", "root", cwd=parent)
    return parent


def _unresolvable_url(tmp_path: Path) -> str:
    """A local URL that resolves to no repository: outside any parent
    worktree and absent, so a clone that reaches URL handling fails its
    fetch — the pre-fix arm that ran the destructive failure cleanup.
    A URL under `tmp_path` would instead walk up to the enclosing
    repository and take the subfolder-binding path."""
    return str(Path(tempfile.gettempdir()) / f"gf-gone-{tmp_path.name}")


def _tree_bytes(root: Path) -> dict[str, tuple[str, object]]:
    """Every entry under `root` as {relative path: (kind, payload)} —
    files carry their bytes, links their target, dirs a marker — so an
    equal dict means a byte-identical tree."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for entry in dirnames + filenames:
            p = Path(dirpath) / entry
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                snap[rel] = ("link", os.readlink(p))
            elif p.is_dir():
                snap[rel] = ("dir", "")
            else:
                snap[rel] = ("file", p.read_bytes())
    return snap


def test_clone_dotdot_leaf_through_missing_dir_refused_preserves_parent(
        tmp_path):
    """`gf clone <url> <path>` fails if the path exists, is not empty,
    and is not a git-folder (spec `gf clone`); the failure cleanup may
    only remove a child the command itself created (spec Failure
    cleanup). Ruling R4-A: a `missing/..` leaf normalizes through
    realpath to the parent root, so the clone hits the occupancy refusal
    before creating anything — unnormalized, `child.exists()` stat'd
    through the nonexistent `missing/` and the failed-fetch cleanup
    `rmtree`d `missing/..`, which is the parent root. The URL resolves
    to no repository, so the refusal must arrive before URL handling:
    the error is the occupancy refusal, not a fetch failure.
    """
    parent = _occupied_parent(tmp_path)
    before = _tree_bytes(parent)
    bad_url = _unresolvable_url(tmp_path)

    r = gf("-C", str(parent), "clone", str(bad_url), "missing/..",
           check=False)
    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "already exists and is not empty" in err, err
    assert "'parent'" in err, err  # the resolved leaf's real name

    assert not (parent / "missing").exists(), "missing/ was created"
    assert not (parent / ".gf").exists(), ".gf leaked into parent"
    assert _tree_bytes(parent) == before, (
        "parent tree changed by a refused clone")


def test_clone_dotdot_leaf_valid_upstream_refused_identically(tmp_path):
    """Same leaf against a reachable local upstream: the occupancy
    refusal precedes URL resolution, so the valid upstream is refused
    exactly as the unresolvable one is — pinning that the normalized
    path hits the guard rather than corrupting the parent through the
    clone's success path."""
    up = _upstream(tmp_path, "upstream", {"leaf.txt": "v1"})
    # Two same-named parents so the refusal envelopes differ only in
    # the absolute path they embed.
    parent_bad = _occupied_parent(tmp_path, "bad/parent")
    parent_ok = _occupied_parent(tmp_path, "ok/parent")
    before = _tree_bytes(parent_ok)

    bad = gf("-C", str(parent_bad), "clone", _unresolvable_url(tmp_path),
             "missing/..", check=False)
    if bad.returncode == 0:
        pytest.fail(f"setup: bad-url clone unexpectedly succeeded:\n"
                    f"{bad.stdout}\n{bad.stderr}")

    r = gf("-C", str(parent_ok), "clone", str(up), "missing/..",
           check=False)
    assert r.returncode == bad.returncode, (
        r.returncode, bad.returncode, r.stderr, bad.stderr)
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert r.stderr.replace(str(parent_ok), "<P>") == \
        bad.stderr.replace(str(parent_bad), "<P>"), (
            f"valid upstream refused differently:\n{r.stderr}\nvs\n"
            f"{bad.stderr}")
    assert "already exists and is not empty" in r.stderr, r.stderr

    assert not (parent_ok / "missing").exists(), "missing/ was created"
    assert not (parent_ok / ".gf").exists(), ".gf leaked into parent"
    assert _tree_bytes(parent_ok) == before, (
        "parent tree changed by a refused clone")


def test_init_dotdot_leaf_through_missing_dir_refused_no_side_effects(
        tmp_path):
    """`gf init` refuses to initialize a directory that already contains
    `.git` (spec `gf init`); after R4-A's realpath normalization,
    `missing/..` resolves to the parent root and refuses before the
    directory is created. The refusal is a pure validation: nothing —
    no `missing/`, no `.gf`, no manifest or `.git` change — is written.
    """
    parent = _occupied_parent(tmp_path)
    before = _tree_bytes(parent)

    r = gf("-C", str(parent), "init", "missing/..", check=False)
    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "already has a .git directory" in err, err

    assert not (parent / "missing").exists(), "missing/ was created"
    assert not (parent / ".gf").exists(), ".gf leaked into parent"
    assert _tree_bytes(parent) == before, (
        "parent tree changed by a refused init")


def test_init_dotdot_leaf_into_existing_empty_subdir_initializes_it(
        tmp_path):
    """`gf -C parent/sub init missing/..` resolves the leaf to `sub`
    itself: an existing empty directory, so `init` proceeds exactly as
    `gf -C parent init sub` does (spec `gf init` — "if `<path>` is
    given, create the directory if it does not exist and initialize
    `.gf` inside it", name from the directory name, url the
    root-relative path placeholder). No `missing/` is created."""
    parent = _occupied_parent(tmp_path)
    (parent / "sub").mkdir()

    r = gf("-C", str(parent / "sub"), "init", "missing/..", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    assert not (parent / "sub" / "missing").exists(), "missing/ created"
    assert (parent / "sub" / ".gf" / "git" / "HEAD").is_file()
    manifest = tomllib.loads((parent / "gf.toml").read_text())
    entry = _single(manifest["git_folder"], "git_folder entry")
    assert entry["name"] == "sub", manifest
    assert entry["path"] == "sub", manifest
    assert entry["url"] == "sub", manifest  # local placeholder per spec

    # Identical to `gf init sub` on an equivalent parent.
    parent2 = _occupied_parent(tmp_path, "parent2")
    (parent2 / "sub").mkdir()
    ref = gf("-C", str(parent2), "init", "sub", check=False)
    if ref.returncode != 0:
        pytest.fail(f"setup: reference `gf init sub` failed:\n"
                    f"{ref.stdout}\n{ref.stderr}")
    ref_manifest = tomllib.loads((parent2 / "gf.toml").read_text())
    assert manifest["git_folder"] == ref_manifest["git_folder"], (
        manifest, ref_manifest)


def test_deeper_dotdot_leaf_spellings_refused(tmp_path):
    """Ruling R4-A's deeper spellings on both commands. `a/b/../..`
    normalizes to the parent root — `clone` reports the spec'd
    occupancy refusal and `init` the `.git` refusal. `missing/../..`
    normalizes to the directory above the parent — both commands hit
    the containment refusal ("child path must be inside the parent
    repo") and create nothing."""
    parent = _occupied_parent(tmp_path)
    before = _tree_bytes(parent)
    bad_url = _unresolvable_url(tmp_path)

    r = gf("-C", str(parent), "clone", bad_url, "a/b/../..", check=False)
    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "already exists and is not empty" in err, err

    r = gf("-C", str(parent), "init", "a/b/../..", check=False)
    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "already has a .git directory" in err, err

    r = gf("-C", str(parent), "clone", bad_url, "missing/../..",
           check=False)
    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "must be inside the parent repo" in err, err

    r = gf("-C", str(parent), "init", "missing/../..", check=False)
    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "must be inside the parent repo" in err, err

    assert not (parent / "a").exists(), "a/ was created"
    assert not (parent / "missing").exists(), "missing/ was created"
    assert not (parent / ".gf").exists(), ".gf leaked into parent"
    assert _tree_bytes(parent) == before, (
        "parent tree changed by a refused command")


def test_clone_dot_leaf_into_existing_empty_dir_still_clones(tmp_path):
    """Positive arm: `gf -C parent/empty clone <up> .` keeps working —
    the `.` leaf resolves to the empty directory itself, which passes
    the occupancy guard and clones in place. The recorded binding is
    `path="empty"` under the directory's real name (spec `gf clone`:
    name derives from the basename of `<path>`)."""
    up = _upstream(tmp_path, "upstream", {"leaf.txt": "v1"})
    parent = _occupied_parent(tmp_path)
    (parent / "empty").mkdir()

    r = gf("-C", str(parent / "empty"), "clone", str(up), ".",
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    assert (parent / "empty" / "leaf.txt").read_text() == "v1"
    assert (parent / "empty" / ".gf" / "git" / "HEAD").is_file()
    manifest = tomllib.loads((parent / "gf.toml").read_text())
    entry = _single(manifest["git_folder"], "git_folder entry")
    assert entry["name"] == "empty", manifest
    assert entry["path"] == "empty", manifest
    assert entry["url"] == str(up), manifest


# ---------------------------------------------------------------------------
# Defect 6 — a manifest-collision `gf init` refusal left the half-created
# child behind


def test_init_duplicate_name_refused_leaves_no_child_and_retries(tmp_path):
    """`gf init <path> -n <name>` fails when `<name>` is already bound in
    the manifest (`git-folder '<name>' already exists in manifest`,
    rc 1 — spec §Error handling). The refusal is pure validation per the
    failure-cleanup contract (no partial state left behind): the
    colliding init must not create the child at all, so a corrected
    retry initializes it normally."""
    parent = _occupied_parent(tmp_path)
    _setup_gf("-C", str(parent), "init", "kid1", "-n", "dup")
    before = _tree_bytes(parent)

    r = gf("-C", str(parent), "init", "kid2", "-n", "dup", check=False)
    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "git-folder 'dup' already exists in manifest" in err, err

    # The discriminating assertion: a refused init leaves nothing —
    # no `kid2/` directory and no `.gf` inside it.
    assert not (parent / "kid2").exists(), (
        "refused init left the half-created child behind: "
        f"{sorted(p.as_posix() for p in (parent / 'kid2').rglob('*'))}")
    assert _tree_bytes(parent) == before, (
        "parent tree changed by a refused init")

    r = gf("-C", str(parent), "init", "kid2", "-n", "unique",
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (parent / "kid2" / ".gf" / "git" / "HEAD").is_file()
    manifest = tomllib.loads((parent / "gf.toml").read_text())
    assert [(f["name"], f["path"]) for f in manifest["git_folder"]] == [
        ("dup", "kid1"), ("unique", "kid2")], manifest


def test_init_path_in_use_refused_leaves_no_child(tmp_path):
    """`gf init <path>` fails when `<path>` is already bound in the
    manifest (`path <rel> is already used by git-folder '<other>'`,
    rc 1 — spec §Error handling): a distinct arm from the `.git`/`.gf`
    occupancy refusals, reached when the recorded child directory was
    removed from the worktree while its binding stays in `gf.toml`.
    Same contract — the refused init creates nothing."""
    parent = _occupied_parent(tmp_path)
    _setup_gf("-C", str(parent), "init", "kid1")
    shutil.rmtree(parent / "kid1")  # directory deleted; binding remains
    before = _tree_bytes(parent)

    r = gf("-C", str(parent), "init", "kid1", "-n", "other", check=False)
    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "path kid1 is already used by git-folder 'kid1'" in err, err

    assert not (parent / "kid1").exists(), (
        "refused init recreated the child directory")
    assert _tree_bytes(parent) == before, (
        "parent tree changed by a refused init")


def test_init_occupancy_refusals_still_fire_without_side_effects(tmp_path):
    """Control: the `.git`/`.gf` occupancy refusals (spec `gf init`) keep
    firing under the moved validation order, still before any side
    effect on the existing directory."""
    parent = _occupied_parent(tmp_path)
    (parent / "hasgit" / ".git").mkdir(parents=True)
    (parent / "hasgf" / ".gf").mkdir(parents=True)
    before = _tree_bytes(parent)

    r = gf("-C", str(parent), "init", "hasgit", check=False)
    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "already has a .git directory" in err, err

    r = gf("-C", str(parent), "init", "hasgf", check=False)
    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "already has a .gf directory" in err, err

    assert _tree_bytes(parent) == before, (
        "parent tree changed by a refused init")


def test_init_second_entry_still_appends_to_manifest(tmp_path):
    """Control: with validation folded ahead of creation and the stray
    mid-function `gf.toml` write folded into the single trailing
    `write_manifest`, a second `gf init` on an existing manifest still
    appends — both entries persist with their own children."""
    parent = _occupied_parent(tmp_path)
    _setup_gf("-C", str(parent), "init", "kid1")
    _setup_gf("-C", str(parent), "init", "kid2")

    assert (parent / "kid1" / ".gf" / "git" / "HEAD").is_file()
    assert (parent / "kid2" / ".gf" / "git" / "HEAD").is_file()
    manifest = tomllib.loads((parent / "gf.toml").read_text())
    assert [(f["name"], f["path"]) for f in manifest["git_folder"]] == [
        ("kid1", "kid1"), ("kid2", "kid2")], manifest


# ---------------------------------------------------------------------------
# Defect 7 — a `gf clone` invoked from a subdirectory anchored its relative
# local url at the invocation cwd instead of the parent repo root


def test_clone_relative_url_from_subdirectory_ignores_cwd_decoy(tmp_path):
    """gf-spec.md `gf pull`'s URL-resolution anchor, applied to `gf
    clone`: "a relative local `url` resolves at the parent repo root
    receiving the manifest, and the anchored repo URL feeds the child's
    `origin` and fetch — while the manifest records the spelled `url`".

    The real upstream is a bare sibling of the parent repo; a decoy bare
    repository sits at `parent/upstream.git` — exactly where the spelled
    `../upstream.git` lands when resolved from the invocation cwd
    `parent/sub`. Anchored at the parent root the clone fetches the real
    sibling and records its absolute path as `origin`; anchored at the
    cwd it would clone the decoy."""
    up = _upstream(tmp_path, "upstream.git", {"real.txt": "real"})
    parent = _parent(tmp_path)
    decoy = _upstream(tmp_path, "decoy.git", {"decoy.txt": "decoy"})
    decoy.rename(parent / "upstream.git")
    sub = parent / "sub"
    sub.mkdir()

    r = gf("clone", "../upstream.git", "kid", cwd=sub, check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    kid = sub / "kid"
    assert not (kid / "decoy.txt").exists(), "clone fetched the decoy"
    assert (kid / "real.txt").is_file()
    assert (kid / "real.txt").read_text() == "real"

    # `origin` is the anchored repo URL — the real upstream's absolute
    # resolved path — not the cwd-relative spelling nor the decoy.
    origin = _git("--git-dir", kid / ".gf" / "git", "config",
                  "remote.origin.url").stdout.strip()
    assert Path(origin).is_absolute(), origin
    assert Path(origin) == up.resolve(), origin

    manifest = tomllib.loads((parent / "gf.toml").read_text())
    entry = _single(manifest["git_folder"], "git_folder entry")
    assert entry["url"] == "../upstream.git", manifest
    assert entry["path"] == "sub/kid", manifest
    assert entry["name"] == "kid", manifest


def test_clone_relative_url_from_subdirectory_without_decoy(tmp_path):
    """Same layout without the decoy: the spelled `../upstream.git`
    resolved from the invocation cwd lands on `parent/upstream.git`,
    which does not exist — the cwd-anchored fetch died `does not appear
    to be a git repository`. Anchored at the parent repo root the clone
    still finds the real sibling upstream and records it as `origin`."""
    up = _upstream(tmp_path, "upstream.git", {"real.txt": "real"})
    parent = _parent(tmp_path)
    sub = parent / "sub"
    sub.mkdir()

    r = gf("clone", "../upstream.git", "kid", cwd=sub, check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    kid = sub / "kid"
    assert (kid / "real.txt").is_file()
    assert (kid / "real.txt").read_text() == "real"
    origin = _git("--git-dir", kid / ".gf" / "git", "config",
                  "remote.origin.url").stdout.strip()
    assert Path(origin) == up.resolve(), origin
    manifest = tomllib.loads((parent / "gf.toml").read_text())
    entry = _single(manifest["git_folder"], "git_folder entry")
    assert entry["url"] == "../upstream.git", manifest


def test_clone_relative_url_from_parent_root_unchanged(tmp_path):
    """Control: the same spelled url invoked from the parent root itself
    — there the invocation cwd IS the anchor, so the clone fetched the
    real sibling before the fix and keeps doing so (the decoy at
    `parent/upstream.git` is bypassed by `..`, which steps above the
    parent)."""
    up = _upstream(tmp_path, "upstream.git", {"real.txt": "real"})
    parent = _parent(tmp_path)
    decoy = _upstream(tmp_path, "decoy.git", {"decoy.txt": "decoy"})
    decoy.rename(parent / "upstream.git")

    r = gf("-C", str(parent), "clone", "../upstream.git", "kid",
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    kid = parent / "kid"
    assert (kid / "real.txt").is_file()
    assert (kid / "real.txt").read_text() == "real"
    assert not (kid / "decoy.txt").exists(), "clone fetched the decoy"
    manifest = tomllib.loads((parent / "gf.toml").read_text())
    entry = _single(manifest["git_folder"], "git_folder entry")
    assert entry["url"] == "../upstream.git", manifest
    assert entry["path"] == "kid", manifest
