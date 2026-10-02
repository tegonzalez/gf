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
- Slash-named upstream default branch: spec "Reference model"
  (`latest` resolves to the remote's default branch; a floating
  checkout keys by `quote(resolved branch, safe='')`) and `gf clone`/
  `gf pull`. `_effective_branch` read `refs/remotes/origin/HEAD` and
  kept only its last `/` segment, so an upstream defaulting to
  `feature/main` mis-resolved `latest` to `main` — the clone died
  `could not resolve remote branch 'origin/main'` — while a symref
  spelling outside the prefix (e.g. `refs/heads/master`) truncated to
  `master` and pulled silently instead of failing on the unresolvable
  `origin/<target>` the symref actually names.
- `.git`-suffixed local repository alias: spec §Physical layout fixes
  `<repo-key>` = `<basename>-<first 8 hex of sha1(normalized repo URL)>`
  and URL resolution's local walk-up treats `…/repo` and `…/repo.git`
  as two real repository boundaries; gf-arch.md GF-D5's store contract
  makes the `.git` alias a remote-transport convention only. Ruling
  R14-F5 fixes the collapse: a `.git`-suffixed LOCAL binding derives a
  key distinct from its suffixless sibling, so each repository keeps
  its own store/checkout and serves its own bytes; an existing
  collapsed binding's next pull re-derives the new key into a fresh
  store, retargets the consumer link, and leaves the old store
  orphaned on disk.
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

import pytest

from conftest import deny_file_transport, gf, git


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
    which fails the fetch after the store is initialised. The refusal is
    injected by rewriting the suite's $HOME-anchored gitconfig — the
    file channel every spawned git still reads; the env vars
    (GIT_CONFIG_GLOBAL, GIT_CONFIG_KEY_*/VALUE_*) no longer reach child
    git."""
    up = _upstream(tmp_path, "upstream", API)
    parent = _parent(tmp_path)

    with deny_file_transport(tmp_path, monkeypatch):
        first = gf("-C", str(parent), "clone", f"{up}/docs/api",
                   "vendor/api", check=False)
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


# ---------------------------------------------------------------------------
# Defect 8 — a dangling consumer-path symlink is occupancy, not a crash
#
# Authorities: spec `gf clone` occupancy bullet ("a dangling symlink at
# `<path>` counts as occupied — a path that exists only as a link to nowhere
# is refused with a clean error rather than treated as empty space"), spec
# `gf init` ("a dangling symlink at `<path>` is refused rather than
# initialized through, and so is any non-directory occupant"), and the
# changelog entry ("a dangling symlink at a consumer path is occupancy, not
# a traceback ... the link is preserved rather than removed as gf-created
# state ... a link to a real directory still binds through the link").
#
# Pre-fix signatures (verified against the base sources):
# - `gf clone <up> <dangling-link>` and `gf init <dangling-link>` died with
#   `FileExistsError` tracebacks: `exists()` is False on a dangling link, so
#   the occupancy guard passed it through and `mkdir(exist_ok=True)` raised
#   `EEXIST` on the link itself (`init_child`'s `child.mkdir`; `cmd_init`'s
#   `target.mkdir`).
# - `gf clone <up> <file>` — a plain file or a link to one — died with
#   `NotADirectoryError` at `init_child`'s `any(child.iterdir())`; `gf init
#   <file>` reached `init_git_folder`'s `gitdir.mkdir` the same way.
# - `gf pull` on a whole-repo binding whose consumer path is a file or a
#   symlink loop died in `update_child` identically (`iterdir` /
#   `child.mkdir`).
#
# A dangling link whose target resolves OUTSIDE the parent still hits the
# earlier containment refusal, so the occupants below dangle at in-parent
# targets.


def test_clone_dangling_symlink_refused_link_preserved(tmp_path):
    """`gf clone <up> <path>` onto a consumer path that is a symlink to
    nowhere is refused with the spec'd occupancy error — `exists()` is
    False on a dangling link, but `lexists` counts it as occupied so the
    clone dies `is a dangling symlink` (rc 1, `gf:` envelope) instead of
    crashing `FileExistsError` in `child.mkdir`. The link — user content
    the command never created — is preserved exactly."""
    up = _upstream(tmp_path, "upstream", {"leaf.txt": "v1"})
    parent = _occupied_parent(tmp_path)
    os.symlink("gone", parent / "kid")  # `kid -> gone`, `gone` absent
    before = _tree_bytes(parent)

    r = gf("-C", str(parent), "clone", str(up), "kid", check=False)
    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "gf: clone failed for git-folder 'kid' (kid):" in err, err
    assert "is a dangling symlink" in err, err
    assert "Traceback" not in err, err

    link = parent / "kid"
    assert link.is_symlink() and os.readlink(link) == "gone", (
        "dangling link not preserved")
    assert not os.path.lexists(parent / "gone"), "link target created"
    assert _tree_bytes(parent) == before, (
        "parent tree changed by a refused clone")


def test_init_dangling_symlink_refused_link_preserved(tmp_path):
    """`gf init <dangling-link>` is refused the same way: `cmd_init` must
    not pass the link through to `target.mkdir` (`EEXIST`) or let
    `init_git_folder`'s gitdir mkdir write through it — the refusal names
    the dangling symlink and the link is preserved."""
    parent = _occupied_parent(tmp_path)
    os.symlink("gone", parent / "din")
    before = _tree_bytes(parent)

    r = gf("-C", str(parent), "init", "din", check=False)
    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "gf: init failed for git-folder 'din' (din):" in err, err
    assert "is a dangling symlink" in err, err
    assert "Traceback" not in err, err

    link = parent / "din"
    assert link.is_symlink() and os.readlink(link) == "gone", (
        "dangling link not preserved")
    assert not os.path.lexists(parent / "gone"), "link target created"
    assert _tree_bytes(parent) == before, (
        "parent tree changed by a refused init")


def test_clone_file_occupants_refused_cleanly(tmp_path):
    """A plain file — or a link to one — at `<path>` passes `exists()`
    but is not a directory: `gf clone` refuses `exists and is not a
    directory` instead of crashing `NotADirectoryError` in
    `any(child.iterdir())`. Both occupants survive untouched."""
    up = _upstream(tmp_path, "upstream", {"leaf.txt": "v1"})
    parent = _occupied_parent(tmp_path)
    (parent / "plainfile").write_text("occupied\n")
    os.symlink("plainfile", parent / "linkfile")
    before = _tree_bytes(parent)

    for path in ("plainfile", "linkfile"):
        r = gf("-C", str(parent), "clone", str(up), path, check=False)
        err = r.stderr + r.stdout
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert f"gf: clone failed for git-folder '{path}' ({path}):" in err, err
        assert "exists and is not a directory" in err, err
        assert "Traceback" not in err, err

    assert (parent / "plainfile").read_text() == "occupied\n"
    assert (parent / "linkfile").is_symlink()
    assert _tree_bytes(parent) == before, (
        "parent tree changed by a refused clone")


def test_init_file_occupants_refused_cleanly(tmp_path):
    """Same occupants through `gf init`: the non-directory refusal fires
    before `init_git_folder`'s `gitdir.mkdir` — where a file path
    previously surfaced only as `NotADirectoryError`."""
    parent = _occupied_parent(tmp_path)
    (parent / "plainfile").write_text("occupied\n")
    os.symlink("plainfile", parent / "linkfile")
    before = _tree_bytes(parent)

    for path in ("plainfile", "linkfile"):
        r = gf("-C", str(parent), "init", path, check=False)
        err = r.stderr + r.stdout
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert f"gf: init failed for git-folder '{path}' ({path}):" in err, err
        assert "exists and is not a directory" in err, err
        assert "Traceback" not in err, err

    assert (parent / "plainfile").read_text() == "occupied\n"
    assert (parent / "linkfile").is_symlink()
    assert _tree_bytes(parent) == before, (
        "parent tree changed by a refused init")


def test_pull_whole_repo_file_occupants_refused_cleanly(tmp_path):
    """`update_child` carries the same gates as `init_child`: `gf pull` on
    a whole-repo binding whose consumer path was replaced by a file — or
    a link to one — refuses `exists and is not a directory` instead of
    crashing `NotADirectoryError` in the non-empty check's `iterdir`."""
    up = _upstream(tmp_path, "upstream", {"leaf.txt": "v1"})
    parent = _occupied_parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up), "kid")

    # Plain file at the binding's path.
    shutil.rmtree(parent / "kid")
    (parent / "kid").write_text("occupied\n")
    r = gf("-C", str(parent), "pull", check=False)
    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "gf: pull failed for git-folder 'kid' (kid):" in err, err
    assert "exists and is not a directory" in err, err
    assert "Traceback" not in err, err
    assert (parent / "kid").read_text() == "occupied\n"

    # A link to a file — the error names the resolved target.
    (parent / "kid").unlink()
    (parent / "real.txt").write_text("occupied\n")
    os.symlink("real.txt", parent / "kid")
    r = gf("-C", str(parent), "pull", check=False)
    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "gf: pull failed for git-folder 'kid' (kid):" in err, err
    assert "exists and is not a directory" in err, err
    assert "Traceback" not in err, err
    assert (parent / "kid").is_symlink()
    assert (parent / "real.txt").read_text() == "occupied\n"


def test_pull_whole_repo_symlink_loop_refused_as_dangling(tmp_path):
    """The `update_child` dangling gate on a path that stays a link after
    realpath: a self-referential `kid -> kid` cannot resolve (ELOOP), so
    `link_path.resolve()` returns the link itself — `lexists` True,
    `exists()` False — and `gf pull` dies `is a dangling symlink` (rc 1)
    instead of `FileExistsError` in `child.mkdir`. The loop link is
    preserved."""
    up = _upstream(tmp_path, "upstream", {"leaf.txt": "v1"})
    parent = _occupied_parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up), "kid")
    shutil.rmtree(parent / "kid")
    os.symlink("kid", parent / "kid")  # self-referential loop

    r = gf("-C", str(parent), "pull", check=False)
    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "gf: pull failed for git-folder 'kid' (kid):" in err, err
    assert "is a dangling symlink" in err, err
    assert "Traceback" not in err, err

    link = parent / "kid"
    assert link.is_symlink() and os.readlink(link) == "kid", (
        "loop link not preserved")


def test_clone_link_to_empty_dir_still_binds_through(tmp_path):
    """Control: a link to a real directory is not occupancy — `gf clone
    <up> <link-to-empty-dir>` still binds through the link per the spec's
    write-through semantics (`exists()`/`is_dir()` follow it, the
    non-empty check passes on the empty target, and `.gf/git` lands in
    the real directory while the manifest records the spelled path)."""
    up = _upstream(tmp_path, "upstream", {"leaf.txt": "v1"})
    parent = _occupied_parent(tmp_path)
    (parent / "emptytarget").mkdir()
    os.symlink("emptytarget", parent / "linkdir")

    r = gf("-C", str(parent), "clone", str(up), "linkdir", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    link = parent / "linkdir"
    assert link.is_symlink() and os.readlink(link) == "emptytarget"
    assert (link / "leaf.txt").read_text() == "v1"
    assert (parent / "emptytarget" / ".gf" / "git" / "HEAD").is_file()
    manifest = tomllib.loads((parent / "gf.toml").read_text())
    entry = _single(manifest["git_folder"], "git_folder entry")
    assert entry["name"] == "linkdir", manifest
    assert entry["path"] == "linkdir", manifest
    assert entry["url"] == str(up), manifest


def test_clone_and_init_fresh_paths_still_succeed(tmp_path):
    """Control: the new occupancy gates sit in front of the existing
    checks only — a clone onto an absent path and an init onto an absent
    path proceed exactly as before."""
    up = _upstream(tmp_path, "upstream", {"leaf.txt": "v1"})
    parent = _occupied_parent(tmp_path)

    r = gf("-C", str(parent), "clone", str(up), "kid", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (parent / "kid" / "leaf.txt").read_text() == "v1"
    assert (parent / "kid" / ".gf" / "git" / "HEAD").is_file()

    r = gf("-C", str(parent), "init", "fresh", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (parent / "fresh" / ".gf" / "git" / "HEAD").is_file()

    manifest = tomllib.loads((parent / "gf.toml").read_text())
    assert [(f["name"], f["path"]) for f in manifest["git_folder"]] == [
        ("kid", "kid"), ("fresh", "fresh")], manifest


def test_pull_retargets_dangling_consumer_link(tmp_path):
    """Heal pin (`ensure_consumer_link` is untouched): a consumer link
    left dangling — here retargeted by hand at a `.gf/wt` checkout path
    that was never created, so its realpath still names a store checkout
    and the binding keeps its subfolder routing — is retargeted back at
    the live checkout by `gf pull` (spec `gf pull` grouped update:
    consumer links are created or retargeted when the effective repo
    URL, subdir, or checkout key changes them out from under the link)."""
    up = _upstream(tmp_path, "upstream", API)
    parent = _occupied_parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api")
    link = parent / "vendor" / "api"
    rk = _single([p for p in (parent / ".gf" / "wt").iterdir()
                  if p.is_dir()], "repo-key dir")
    live = rk / "master" / "docs" / "api"
    if Path(os.path.realpath(link)) != live:
        pytest.fail("setup: consumer link does not map to the live checkout")

    link.unlink()
    os.symlink(f"../.gf/wt/{rk.name}/gone/docs/api", link)
    if link.exists() or not link.is_symlink():
        pytest.fail("setup: consumer link not left dangling")

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled api" in r.stdout, r.stdout
    assert link.is_symlink() and link.exists(), "link still dangling"
    assert Path(os.path.realpath(link)) == live
    assert (link / "reference.md").read_text() == "api v1"


def test_pull_recreated_checkout_heals_dangling_consumer_link(tmp_path):
    """Heal pin, second arm: deleting the shared checkout leaves the
    consumer link dangling; `gf pull --force` recreates the worktree
    (its admin record survives — `ensure_checkout`'s existing-checkout
    arm) so the untouched link resolves again and serves the mapping.
    `--force` answers the recreated checkout's transient ` D` porcelain
    — the fresh worktree reads deleted against the surviving index
    before the ref apply repopulates it."""
    up = _upstream(tmp_path, "upstream", API)
    parent = _occupied_parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api")
    link = parent / "vendor" / "api"
    rk = _single([p for p in (parent / ".gf" / "wt").iterdir()
                  if p.is_dir()], "repo-key dir")
    shutil.rmtree(rk / "master")
    if link.exists() or not link.is_symlink():
        pytest.fail("setup: consumer link not left dangling")

    r = gf("-C", str(parent), "pull", "--force", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled api" in r.stdout, r.stdout
    assert link.is_symlink() and link.exists(), "link still dangling"
    assert Path(os.path.realpath(link)) == rk / "master" / "docs" / "api"
    assert (link / "reference.md").read_text() == "api v1"


# ---------------------------------------------------------------------------
# Defect 9 — a slash-named upstream default branch resolved to its last
# `/` segment only (`origin/feature/main` misread as `origin/main`)


def _slash_default_upstream(tmp_path: Path) -> Path:
    """Bare upstream whose HEAD names a slash default branch — the
    `git init -b feature/main` shape (HEAD → refs/heads/feature/main) —
    carrying one commit with a root file and a docs/api tree."""
    up = tmp_path / "upstream"
    _git("init", "-q", "--bare", "-b", "feature/main", up)
    work = tmp_path / "_seed_slash"
    _git("clone", "-q", up, work)
    if _git("-C", work, "symbolic-ref", "HEAD").stdout.strip() != (
            "refs/heads/feature/main"):
        pytest.fail("setup: seed clone did not adopt feature/main")
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "reference.md").write_text(
        "api on feature/main")
    (work / "a.txt").write_text("hello")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "feature/main")
    return up


def _push_slash_update(tmp_path: Path, rel: str, text: str) -> None:
    """Commit `rel`=`text` on the slash upstream's feature/main."""
    work = tmp_path / "_seed_slash"
    (work / rel).write_text(text)
    _git("-C", work, "commit", "-qam", f"update {rel}")
    _git("-C", work, "push", "-q", "origin", "feature/main")


def test_clone_pull_upstream_slash_default_branch(tmp_path):
    """A whole-repo `gf clone` of an upstream defaulting to a slash-named
    branch checks out the FULL branch — `feature/main` tracking
    `origin/feature/main`: `latest` resolution must strip the whole
    `refs/remotes/origin/` prefix off the origin/HEAD symref, never
    keep only its last segment (spec "Reference model", `gf clone`).
    The same upstream's subfolder clone keys its shared checkout
    `feature%2Fmain` (`quote(resolved branch, safe='')`), and `gf pull`
    updates both bindings.

    Pre-fix signature: `head.split('/')[-1]` truncated the symref to
    `main`, so the clone died `gf: clone failed for git-folder 'lib'
    (vendor/lib): could not resolve remote branch 'origin/main' in
    <child>` — the fetched `origin/feature/main` was never tried."""
    up = _slash_default_upstream(tmp_path)
    parent = _parent(tmp_path)

    r = gf("-C", str(parent), "clone", str(up), "vendor/lib",
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    child = parent / "vendor" / "lib"
    assert (child / "a.txt").read_text() == "hello"

    # The checked-out branch is the full slash name tracking its origin
    # twin — never the truncated `main` segment.
    branch = gf("-C", str(child), "git", "rev-parse", "--abbrev-ref",
                "HEAD").stdout.strip()
    assert branch == "feature/main", branch
    upstream_ref = gf("-C", str(child), "git", "rev-parse",
                      "--abbrev-ref", "--symbolic-full-name",
                      "@{upstream}").stdout.strip()
    assert upstream_ref == "origin/feature/main", upstream_ref
    status = gf("-C", str(parent), "status").stdout
    assert re.search(
        rf"lib\s+{re.escape(str(up))}\s+\[feature/main\]", status), status

    # A subfolder clone of the same upstream resolves `latest` to the
    # same branch: the checkout key is quote('feature/main', safe='').
    r = gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api",
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    rk = _single([p for p in (parent / ".gf" / "wt").iterdir()
                  if p.is_dir()], "repo-key dir")
    checkout = _single([p for p in rk.iterdir() if p.is_dir()],
                       "checkout dir")
    assert checkout.name == "feature%2Fmain", [
        p.name for p in rk.iterdir()]
    assert not (rk / "feature").exists(), "slash key split a path level"
    assert (rk / ".feature%2Fmain.state").is_file()
    link = parent / "vendor" / "api"
    assert link.is_symlink()
    assert Path(os.path.realpath(link)) == checkout / "docs" / "api"
    assert (link / "reference.md").read_text() == "api on feature/main"
    # The shared checkout's worktree record is attached to the branch.
    admin = _checkout_admin(parent, link, "docs/api")
    assert _git("--git-dir", admin, "symbolic-ref",
                "HEAD").stdout.strip() == "refs/heads/feature/main"

    # `gf pull` advances both bindings on feature/main.
    _push_slash_update(tmp_path, "a.txt", "adv")
    _push_slash_update(tmp_path, "docs/api/reference.md", "api adv")
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (child / "a.txt").read_text() == "adv"
    assert (link / "reference.md").read_text() == "api adv"
    assert gf("-C", str(child), "git", "rev-parse", "--abbrev-ref",
              "HEAD").stdout.strip() == "feature/main"


def test_clone_pull_upstream_master_default_branch_control(tmp_path):
    """Control: a `master` default resolves unchanged — whole-repo clone
    checks out `master` tracking `origin/master`, a same-repo subfolder
    clone keys its checkout `master`, and `gf pull` updates. With no
    slash in the name the prefix strip and the retired last-segment
    truncation agree, so this arm must hold on either side of the fix."""
    up = _upstream(tmp_path, "upstream",
                   {"a.txt": "hello",
                    "docs/api/reference.md": "api on master"})
    parent = _parent(tmp_path)

    r = gf("-C", str(parent), "clone", str(up), "vendor/lib",
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    child = parent / "vendor" / "lib"
    assert (child / "a.txt").read_text() == "hello"
    assert gf("-C", str(child), "git", "rev-parse", "--abbrev-ref",
              "HEAD").stdout.strip() == "master"
    assert gf("-C", str(child), "git", "rev-parse", "--abbrev-ref",
              "--symbolic-full-name",
              "@{upstream}").stdout.strip() == "origin/master"

    r = gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api",
           check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    rk = _single([p for p in (parent / ".gf" / "wt").iterdir()
                  if p.is_dir()], "repo-key dir")
    assert _single([p for p in rk.iterdir() if p.is_dir()],
                   "checkout dir").name == "master"

    _push_update(tmp_path, up, "a.txt", "adv")
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (child / "a.txt").read_text() == "adv"
    assert (parent / "vendor" / "api" / "reference.md"
            ).read_text() == "api on master"


def test_pull_origin_head_symref_outside_prefix_fails_honestly(tmp_path):
    """An `origin/HEAD` symref spelling a target OUTSIDE
    `refs/remotes/origin/` — here `refs/heads/master` — is not a
    default-branch answer: `latest` must fail with a clean `gf:` error
    naming the spelled target, never silently truncate to its last
    segment (spec "Reference model" — `latest` resolves against the
    remote-tracking refs). The planted symref verifies against the
    child gitdir's local `master`, so `_ensure_origin_head` leaves it
    in place for resolution to read.

    Pre-fix signature: `split('/')[-1]` reduced `refs/heads/master` to
    `master`, `origin/master` resolved, and the pull printed `Pulled
    lib` — silent truncation with no indication the symref pointed
    outside the origin namespace."""
    up = _upstream(tmp_path, "upstream", {"a.txt": "hello"})
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", str(up), "vendor/lib")
    child = parent / "vendor" / "lib"
    gitdir = child / ".gf" / "git"

    # Plant the defect shape; it survives `_ensure_origin_head` only
    # because `refs/heads/master` verifies as an existing local ref.
    _git("--git-dir", gitdir, "symbolic-ref",
         "refs/remotes/origin/HEAD", "refs/heads/master")
    if _git("--git-dir", gitdir, "symbolic-ref",
            "refs/remotes/origin/HEAD").stdout.strip() != (
            "refs/heads/master"):
        pytest.fail("setup: planted origin/HEAD symref did not stick")
    if _git("--git-dir", gitdir, "show-ref", "--verify",
            "refs/heads/master", check=False).returncode != 0:
        pytest.fail("setup: child gitdir lacks local refs/heads/master")

    r = gf("-C", str(parent), "pull", "vendor/lib", check=False)

    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert r.stderr.startswith("gf: "), r.stderr
    assert "Traceback" not in r.stderr
    # The error names the spelled target — `origin/refs/heads/master`
    # is unresolvable — not a `main`/`master` segment guess.
    assert "origin/refs/heads/master" in r.stderr, r.stderr
    assert "Pulled" not in r.stdout
    # Nothing was applied or silently repaired: HEAD stayed on master
    # and the out-of-prefix symref is still what it spelled.
    assert gf("-C", str(child), "git", "rev-parse", "--abbrev-ref",
              "HEAD").stdout.strip() == "master"
    assert _git("--git-dir", gitdir, "symbolic-ref",
                "refs/remotes/origin/HEAD").stdout.strip() == (
        "refs/heads/master")
# Defect 10 — a `.git`-suffixed local repository aliased into its
# suffixless sibling's store (`repo_key` stripped `.git` for every
# spelling; R14-F5 keeps the alias for remote spellings only)


def _sha8(text: str) -> str:
    """First 8 hex of sha1(text) — the spec's repo-key suffix formula."""
    return hashlib.sha1(text.encode()).hexdigest()[:8]


def _collapsed_key(repo_url: str) -> str:
    """The PRE-fix repo key of a `.git`-suffixed local path: the alias
    applied to every spelling, so `…/lib.git` keyed exactly as `…/lib`.
    Computed from the defect's own rule, never from `layout.repo_key`
    (the code under test)."""
    stripped = repo_url.rstrip("/").removesuffix(".git")
    base = stripped.replace(":", "/").rsplit("/", 1)[-1] or "repo"
    return f"{base}-{_sha8(stripped)}"


def _collapsed_simulated_binding(
        parent: Path, link: Path, old_key: str, new_key: str,
        checkout_key: str, subdir: str) -> None:
    """Rewrite a live post-fix binding into its pre-fix collapsed state.

    A pre-fix `gf clone` cannot run in-slice (the fix is applied), so
    the real store, checkout, worktree record and consumer link are
    renamed onto the collapsed key's paths — byte-identical to what the
    old key derivation produced (the worktree record's `gitdir` file and
    the relative consumer link are the only absolute-path-bearing
    artifacts)."""
    repos = parent / ".gf" / "repos"
    wt = parent / ".gf" / "wt"
    (repos / new_key).rename(repos / old_key)
    (wt / new_key).rename(wt / old_key)
    gitdir = repos / old_key / "git" / "worktrees" / checkout_key / "gitdir"
    gitdir.write_text(f"{wt / old_key / checkout_key / '.git'}\n")
    link.unlink()
    os.symlink(
        os.path.relpath(wt / old_key / checkout_key / subdir,
                        link.parent),
        link)


def test_clone_local_dotgit_repo_keeps_its_own_store_and_bytes(tmp_path):
    """R14-F5 live defect: the local walk-up resolves `…/repo/docs` and
    `…/repo.git/docs` at two DISTINCT repository boundaries (spec URL
    resolution), so each derives its own `<repo-key>` and store — the
    `.git` alias is a remote-transport convention (`https://h/r` ≡
    `https://h/r.git`), never a local one. Collapsed into one key, the
    second binding joined the first store's checkout and served the
    FIRST repository's bytes."""
    plain = _upstream(tmp_path, "repo", {"docs/x.txt": "plain repo bytes"})
    dotted = _upstream(tmp_path, "repo.git", {
        "docs/x.txt": "dotted repo bytes",
        "docs/only-dotted.txt": "present only in repo.git",
    })
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{plain}/docs", "one")
    _setup_gf("-C", str(parent), "clone", f"{dotted}/docs", "two")

    # The spec formula, computed independently of the code under test:
    # each resolved local path keys by its own spelling, `.git` kept.
    plain_key = f"repo-{_sha8(os.path.realpath(plain))}"
    dotted_key = f"repo.git-{_sha8(os.path.realpath(dotted))}"
    assert {p.name for p in (parent / ".gf" / "repos").iterdir()} == {
        plain_key, dotted_key}
    assert {p.name for p in (parent / ".gf" / "wt").iterdir()} == {
        plain_key, dotted_key}
    # Each consumer link lands in ITS repository's checkout …
    assert Path(os.path.realpath(parent / "one")) == Path(
        os.path.realpath(
            parent / ".gf" / "wt" / plain_key / "master" / "docs"))
    assert Path(os.path.realpath(parent / "two")) == Path(
        os.path.realpath(
            parent / ".gf" / "wt" / dotted_key / "master" / "docs"))
    # … and serves that repository's own bytes — the wrong-result
    # witness the collapse produced (repo's `x.txt`, no `only-dotted`).
    assert (parent / "one" / "x.txt").read_text() == "plain repo bytes"
    assert (parent / "two" / "x.txt").read_text() == "dotted repo bytes"
    assert (parent / "two" / "only-dotted.txt").read_text() == (
        "present only in repo.git")


def test_pull_migrates_dotgit_local_binding_to_its_own_store(tmp_path):
    """R14-F5 migration: a binding recorded under the collapsed key has
    its effective url re-derived on the next `gf pull` — the recorded
    store's `remote.origin.url` (`…/lib.git`) no longer keys that store,
    so the resolution re-runs, builds the NEW key's store and checkout,
    retargets the consumer link, and leaves the old store orphaned on
    disk (never deleted — GF-TRB-3-class leftover; uncommitted work in
    its checkout survives)."""
    up = _upstream(tmp_path, "lib.git", {"docs/x.txt": "lib.git v1"})
    parent = _parent(tmp_path)
    _setup_gf("-C", str(parent), "clone", f"{up}/docs", "vend")

    up_real = os.path.realpath(up)
    new_key = f"lib.git-{_sha8(up_real)}"
    old_key = _collapsed_key(up_real)
    repos = parent / ".gf" / "repos"
    wt = parent / ".gf" / "wt"
    if sorted(p.name for p in repos.iterdir()) != [new_key]:
        pytest.fail(
            f"setup: clone created stores "
            f"{sorted(p.name for p in repos.iterdir())}, not [{new_key}]")
    if old_key == new_key:
        pytest.fail("setup: keys unexpectedly identical")

    # Rebuild the pre-fix collapsed state in place: same store, same
    # checkout, same link — spelled under the OLD key's paths.
    link = parent / "vend"
    _collapsed_simulated_binding(parent, link, old_key, new_key,
                                 "master", "docs")
    if Path(os.path.realpath(link)) != (
            wt / old_key / "master" / "docs"):
        pytest.fail("setup: collapsed-state link does not resolve into "
                    "the old-key checkout")
    if not (repos / old_key / "git" / "HEAD").is_file():
        pytest.fail("setup: collapsed-state store has no HEAD")
    # Uncommitted work inside the old checkout: it must survive.
    (wt / old_key / "master" / "docs" / "local.txt").write_text(
        "uncommitted work")

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled vend" in r.stdout, r.stdout

    # A NEW store under the re-derived key; the old one orphaned, not
    # deleted or rewritten into service.
    assert {p.name for p in repos.iterdir()} == {old_key, new_key}
    assert {p.name for p in wt.iterdir()} == {old_key, new_key}
    new_store = repos / new_key / "git"
    assert (new_store / "HEAD").is_file()
    origin = _git("--git-dir", new_store, "config",
                  "remote.origin.url").stdout.strip()
    assert origin == up_real

    # The consumer link retargeted to the new checkout and serves the
    # binding's own repository bytes.
    assert Path(os.path.realpath(link)) == Path(
        os.path.realpath(wt / new_key / "master" / "docs"))
    assert (link / "x.txt").read_text() == "lib.git v1"

    # The orphaned store keeps its content (a still-valid bare repo) and
    # the old checkout's uncommitted file survives on disk.
    old_store = repos / old_key / "git"
    assert (old_store / "HEAD").is_file()
    assert _git("--git-dir", old_store, "rev-parse", "--verify",
                "refs/remotes/origin/master",
                check=False).returncode == 0
    assert (wt / old_key / "master" / "docs" / "local.txt") \
        .read_text() == "uncommitted work"

    # The retargeted binding keeps working: a pushed update lands on the
    # next pull through the NEW store.
    _push_update(tmp_path, up, "docs/x.txt", "lib.git v2")
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (link / "x.txt").read_text() == "lib.git v2"
