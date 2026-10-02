# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""F1 — consumer-path containment: an untrusted manifest `path` must
never aim a gf write outside the root that owns the binding, nor into
gf's `.gf` storage.

Two layers are pinned here:

1. Load-time lexical validation (`manifest._validate_consumer_path`,
   applied by `read_manifest` and `read_local_overrides`): a `path`
   must be a non-empty relative string whose normpath is not `.`/`..`,
   does not begin `..`, and carries no `.gf` segment — else the file is
   refused on read with an error naming the binding. On this slice
   `main()` does not yet intercept GitFoldersError (that lands with the
   F4a commit), so the refusal surfaces as an uncaught ValidationError
   traceback, rc=1 — the pins hold the REFUSAL and the no-writes
   invariant, never the stderr envelope.

2. Realpath containment at the write sites: `ensure_consumer_link`
   (callers pass the owning root), `_worktree_link_folders`,
   `linked_git_folder_symlinks_in_worktree`, `cmd_pull`'s plan loop and
   `update_child`'s top-level guard. A mid-path symlink the checkout
   materialized — a committed `vendor -> /abs`, mode 120000 — must not
   redirect an unlink/mkdir/symlink outside the owning root. Real git
   fixtures are required for those arms: MockGitBackend cannot express
   a committed symlink.

Authorities: docs/gf-spec.md — the `path` validation rule under
Manifest, the `gf pull` consumer-path / consumer-link containment
bullets, the `gf worktree add` placement-refusal bullet, the
`gf worktree remove` mid-path-skip bullet, and the Security bullet on
trusted manifest paths.

Layer 2d pins the sibling anchor fix: an in-root mid-path symlink WITH
a depth delta (`sub -> deep/nested/a`) lands the consumer leaf at
`deep/nested/a/x`, so the link's relative target must anchor at the
REALPATH'd parent — pre-fix it anchored at the spelled parent and the
leaf dangled.

Layer 3 (r14-f1) extends the same containment to the parent's own
`.git` metadata tree: a consumer path resolving under `.git` plants
content where git reads it — under `hooks/` the parent's next
`git checkout` EXECUTES it. The read gate, the link-layer realpath
guard, and the CLI operand parts-checks pin the refusal at every
layer, and the controls pin exact-segment matching (`x.git`/`x.gf`
substrings stay legal).

Pre-fix signatures verified against HEAD d45b79a are recorded per test.
"""

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from conftest import gf, git, push_branch
from gf import layout, shelf
from gf.exceptions import ValidationError


# ---------------------------------------------------------------------------
# helpers — setup failures use pytest.fail, never AssertionError
#   (tests/test_hardening_regressions.py convention)


def _git(*args, check: bool = True, env=None, input: str | None = None
         ) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True,
        env=env, input=input)
    if check and r.returncode != 0:
        pytest.fail(
            f"setup: git {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stderr}")
    return r


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", "-q", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root\n")
    git("add", "README", cwd=parent)
    git("commit", "-qm", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str = "upstream",
              marker: str = "api on master") -> Path:
    """Bare upstream with docs/api/x.txt + tools/t.txt on master."""
    up = tmp_path / name
    _git("init", "-q", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text(f"{marker}\n")
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text(f"tool in {name}\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")
    return up


def _advance(up: Path, tmp_path: Path, tag: str) -> None:
    """Push one more commit to upstream master (docs/api + tools)."""
    work = tmp_path / f"_adv_{tag}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api" / "x.txt").write_text(f"api {tag}\n")
    (work / "tools" / "t.txt").write_text(f"tool {tag}\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", tag)
    _git("-C", work, "push", "-q", "origin", "master")


def _tree_bytes(root: Path) -> dict[str, tuple[str, object]]:
    """Every entry under `root` as {relpath: (kind, payload)} — files
    carry bytes, links their target, dirs a marker — so an equal dict
    means a byte-identical tree."""
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


def _gf_tree(root: Path) -> dict:
    """Every path under `<root>/.gf` → ('dir',)/('link', target)/
    ('file', bytes) — byte-identical snapshot."""
    base = root / ".gf"
    snap = {}
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).as_posix()
        if p.is_symlink():
            snap[rel] = ("link", os.readlink(p))
        elif p.is_dir():
            snap[rel] = ("dir",)
        else:
            snap[rel] = ("file", p.read_bytes())
    return snap


def _worktree_paths(repo: Path) -> set[str]:
    """Real paths of every worktree `git worktree list` reports."""
    out = _git("-C", str(repo), "worktree", "list", "--porcelain").stdout
    return {
        os.path.realpath(line.split(" ", 1)[1])
        for line in out.splitlines() if line.startswith("worktree ")
    }


def _toml_value(v) -> str:
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return repr(v).lower() if isinstance(v, bool) else repr(v)


def _write_manifest(parent: Path, entries: list[dict]) -> None:
    """Hand-spell `gf.toml` — hostile `path` values are untrusted input
    the CLI itself never produces, so the fixtures write the file."""
    lines = ["git_folder = [\n"]
    for e in entries:
        fields = ", ".join(f"{k} = {_toml_value(v)}"
                           for k, v in e.items())
        lines.append(f"    {{ {fields} }},\n")
    lines.append("]\n")
    (parent / "gf.toml").write_text("".join(lines))


def _commit_branch_with(parent: Path, branch: str,
                        entries: dict[str, tuple[str, str]]) -> None:
    """Create `branch` as HEAD's tree plus `entries`, by pure plumbing —
    parent's worktree (with its live consumer links) never moves.

    `("link", target)` commits a mode-120000 symlink; `("file", text)`
    a regular file. This is how a hostile tree ships a mid-path symlink
    (`vendor -> /abs`) that `git worktree add` later materializes — the
    fixture real git provides and the mock backend cannot express.
    """
    idx = parent / ".git" / f"gf-test-index-{branch}"
    env = {**os.environ, "GIT_INDEX_FILE": str(idx)}

    def g(*args, **kw):
        return _git("-C", str(parent), *args, env=env, **kw)

    g("read-tree", "HEAD")
    try:
        for rel, (kind, payload) in entries.items():
            blob = g("hash-object", "-w", "--stdin",
                     input=payload).stdout.strip()
            mode = "120000" if kind == "link" else "100644"
            g("update-index", "--add", "--cacheinfo",
              f"{mode},{blob},{rel}")
        tree = g("write-tree").stdout.strip()
        commit = g("commit-tree", tree, "-p", "HEAD",
                   "-m", f"{branch} tree").stdout.strip()
        g("update-ref", f"refs/heads/{branch}", commit)
    finally:
        idx.unlink(missing_ok=True)


def _parent_with_binding(tmp_path: Path) -> tuple[Path, Path]:
    """(parent, upstream) — parent serves a `vendor/api` subfolder
    binding cloned from upstream."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    r = gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api",
           check=False)
    if r.returncode != 0:
        pytest.fail(f"setup: clone rc={r.returncode}:\n"
                    f"{r.stdout}\n{r.stderr}")
    return parent, up


def _victim_setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(parent, upstream, victim): parent has the live `vendor/api`
    binding; sibling `victim/` is an empty dir; branch `sym` commits
    `vendor` -> victim (absolute) so `git worktree add <wt> sym`
    materializes the mid-path symlink inside the new worktree."""
    parent, up = _parent_with_binding(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    _commit_branch_with(parent, "sym", {"vendor": ("link", str(victim))})
    return parent, up, victim


# ---------------------------------------------------------------------------
# Layer 1 — lexical validation at manifest read
#
# On this slice the refusal is an uncaught ValidationError (rc=1
# traceback); F4a's clean `gf:` envelope lands separately. Pin: rc != 0,
# the refusal names the binding + `gf.toml`, and nothing was written —
# inside the parent (`_tree_bytes` covers `.gf` leakage too) or outside.


@pytest.mark.parametrize(
    "spelling",
    ["../escaped", ".gf/x", "a/../../escaped", ".", "", 5],
    ids=["dotdot", "dotgf", "nested-dotdot", "dot", "empty", "int"],
)
def test_pull_refuses_hostile_manifest_path(tmp_path, spelling):
    """A committed `gf.toml` is untrusted input: `path` spellings that
    escape the parent (`..`-leading, `.`, empty, non-string) or reach
    into `.gf` refuse on read — `gf pull` exits non-zero naming the
    binding, and nothing is written inside the parent or outside it.
    """
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _write_manifest(parent, [{
        "name": "evil", "url": f"{up}/docs/api", "ref": "latest",
        "path": spelling}])
    before = _tree_bytes(parent)

    r = gf("-C", str(parent), "pull", check=False)

    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "evil" in err, err       # the refusal names the binding
    assert "gf.toml" in err, err    # and the file it was read from

    assert _tree_bytes(parent) == before, (
        "a refused pull wrote inside the parent")
    assert not os.path.lexists(tmp_path / "escaped"), (
        "a refused pull wrote outside the parent")


def test_pull_refuses_absolute_manifest_path(tmp_path):
    """`path = "<absolute>"` refuses the same way. The absolute target
    lives inside tmp_path so the (pre-fix) write is hermetic and
    observable: the operand is spelled explicitly because the no-arg
    selector never descends outside the parent."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    escape = tmp_path / "abs-escape"
    _write_manifest(parent, [{
        "name": "evil", "url": f"{up}/docs/api", "ref": "latest",
        "path": str(escape)}])
    before = _tree_bytes(parent)

    r = gf("-C", str(parent), "pull", str(escape), check=False)

    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "evil" in err, err
    assert not os.path.lexists(escape), (
        "a refused pull wrote outside the parent")
    assert _tree_bytes(parent) == before


def test_hostile_manifest_refuses_every_command(tmp_path):
    """Validation happens on read — every command resolving the
    manifest refuses the hostile `path` (spec: 'every `gf` command
    fails ... `gf rm` included'), not just pull."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _write_manifest(parent, [{
        "name": "evil", "url": f"{up}/docs/api", "ref": "latest",
        "path": "../escaped"}])
    before = _tree_bytes(parent)

    for cmd in (
        ("pull",), ("ls",), ("status",), ("rm", "evil"),
        ("worktree", "list"),
        ("worktree", "add", str(tmp_path / "wt2")),
        ("clone", f"{up}/docs/api", "kid"),
        ("init", "kid"),
    ):
        r = gf("-C", str(parent), *cmd, check=False)
        assert r.returncode != 0, (cmd, r.returncode, r.stdout, r.stderr)

    assert _tree_bytes(parent) == before
    assert not os.path.lexists(tmp_path / "escaped")
    assert not os.path.lexists(tmp_path / "wt2")


def test_pull_refuses_path_carried_in_local_override(tmp_path):
    """Overrides match bindings by `name` and carry only url/ref — but a
    `path` key an override happens to carry is validated identically on
    read: consistent refusal rather than an inert hostile spelling."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _write_manifest(parent, [{
        "name": "api", "url": f"{up}/docs/api", "ref": "latest",
        "path": "vendor/api"}])
    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\npath = "../escaped"\n')
    before = _tree_bytes(parent)

    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert not os.path.lexists(tmp_path / "escaped")
    assert _tree_bytes(parent) == before


def test_pull_refuses_malformed_git_folder_shapes(tmp_path):
    """The same read gate refuses a non-list `git_folder` and non-dict
    entries — not only hostile `path` spellings."""
    parent = _parent(tmp_path)

    for manifest in ('git_folder = 5\n',
                     'git_folder = [\n    "oops",\n]\n'):
        (parent / "gf.toml").write_text(manifest)
        before = _tree_bytes(parent)

        r = gf("-C", str(parent), "pull", check=False)
        assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
        assert "gf.toml" in (r.stderr + r.stdout)
        assert _tree_bytes(parent) == before


# ---------------------------------------------------------------------------
# Layer 2a — `gf worktree add` must not write through a committed
# mid-path symlink (the P1 core: real-git mode-120000 `vendor -> /abs`)


def test_worktree_add_force_never_replaces_outside_file(tmp_path):
    """P1 signature: `victim/api` is a REAL FILE the checked-out
    `vendor -> victim` link exposes at `<wt>/vendor/api`. Under `-f`
    the link step takes over existing leaf content — so pre-fix it
    unlinked `victim/api` and replaced it with the consumer link. The
    containment refusal must fire before any write: rc=1, the worktree
    rolled back, the outside file byte-identical."""
    parent, _up, victim = _victim_setup(tmp_path)
    sentinel = b"SENTINEL - gf must never touch this\n"
    (victim / "api").write_bytes(sentinel)
    wt2 = tmp_path / "wt2"

    r = gf("-C", str(parent), "worktree", "add", "-f", str(wt2), "sym",
           check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "resolves outside" in err, err
    # rolled back per the failure contract: unregistered, dir gone
    assert not os.path.lexists(wt2), "failed add left the worktree"
    assert os.path.realpath(wt2) not in _worktree_paths(parent)
    # the outside file is byte-identical — never unlinked/replaced
    assert not (victim / "api").is_symlink()
    assert (victim / "api").read_bytes() == sentinel


def test_worktree_add_refuses_escape_before_takeover_check(tmp_path):
    """Same shape without `-f`: the outside file is also an occupancy
    collision, but the CONTAINMENT refusal must be the one that fires —
    it runs before the takeover detail so no write outside is ever
    attempted (pre-fix the takeover refusal fired instead: the message
    named 'already exists', never 'resolves outside')."""
    parent, _up, victim = _victim_setup(tmp_path)
    sentinel = b"SENTINEL - gf must never touch this\n"
    (victim / "api").write_bytes(sentinel)
    wt2 = tmp_path / "wt2"

    r = gf("-C", str(parent), "worktree", "add", str(wt2), "sym",
           check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "resolves outside" in err, err
    assert "already exists" not in err, err
    assert not os.path.lexists(wt2)
    assert os.path.realpath(wt2) not in _worktree_paths(parent)
    assert not (victim / "api").is_symlink()
    assert (victim / "api").read_bytes() == sentinel


def test_worktree_add_escape_writes_nothing_without_force(tmp_path):
    """The leaf-absent case: `victim/` is empty, so nothing collides
    and pre-fix `worktree add` SUCCEEDED — planting `victim/api` as a
    consumer-link symlink outside the tree it just made (rc=0,
    `Added worktree`). Post-fix the link step refuses first and the
    worktree is rolled back."""
    parent, _up, victim = _victim_setup(tmp_path)
    wt2 = tmp_path / "wt2"

    r = gf("-C", str(parent), "worktree", "add", str(wt2), "sym",
           check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "resolves outside" in err, err
    assert not os.path.lexists(victim / "api"), (
        "the link step planted a consumer link outside the worktree")
    assert not os.path.lexists(wt2)
    assert os.path.realpath(wt2) not in _worktree_paths(parent)


def test_worktree_add_midpath_symlink_inside_tree_still_links(tmp_path):
    """Control: the guard bounds by the new worktree's real root — a
    committed `vendor -> inner` mid-path symlink that stays INSIDE the
    tree passes, and the child link lands through it as usual."""
    parent, _up = _parent_with_binding(tmp_path)
    _commit_branch_with(parent, "sym", {
        "vendor": ("link", "inner"),
        "inner/keep.txt": ("file", "k\n")})
    wt2 = tmp_path / "wt2"

    r = gf("-C", str(parent), "worktree", "add", str(wt2), "sym",
           check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert os.path.realpath(wt2) in _worktree_paths(parent)
    # the link landed through the in-root mid-path symlink
    leaf = wt2 / "inner" / "api"
    assert leaf.is_symlink()
    assert (wt2 / "vendor" / "api" / "x.txt").read_text() == \
        "api on master\n"


def test_worktree_add_refuses_midpath_symlink_into_gf_tree(tmp_path):
    """A committed mid-path symlink that stays inside the worktree but
    lands in a `.gf` subtree is refused too — the parent realpath must
    be inside the root AND outside `.gf`."""
    parent, _up = _parent_with_binding(tmp_path)
    _commit_branch_with(parent, "sym", {
        "vendor": ("link", ".gf/parking"),
        ".gf/parking/keep.txt": ("file", "k\n")})
    wt2 = tmp_path / "wt2"

    r = gf("-C", str(parent), "worktree", "add", str(wt2), "sym",
           check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "gf-managed storage" in err, err
    assert not os.path.lexists(wt2)
    assert os.path.realpath(wt2) not in _worktree_paths(parent)


# ---------------------------------------------------------------------------
# Layer 2b — `gf worktree remove` never unlinks an outside leaf


def test_worktree_remove_skips_leaf_whose_parent_escapes(tmp_path):
    """A worktree whose `vendor` is a committed symlink to `victim/`
    puts the manifest child path's PARENT outside the worktree:
    `vendor/api` there is `victim/api` — itself a symlink into the live
    source checkout (the r17 shape). Remove must skip it, not unlink
    it: the worktree is removed and the outside leaf survives with its
    target byte-identical."""
    parent, _up, victim = _victim_setup(tmp_path)
    # the outside leaf — a relative symlink into the real consumer path
    leaf_target = os.path.relpath(parent / "vendor" / "api", victim)
    os.symlink(leaf_target, victim / "api")
    wt2 = tmp_path / "wt2"
    # plain `git worktree add`: gf add would (correctly) refuse to link
    # into this tree; the checkout alone materializes the mid-path link
    _git("-C", str(parent), "worktree", "add", "-q", str(wt2), "sym")
    if not (wt2 / "vendor" / "api").is_symlink():
        pytest.fail("setup: worktree did not materialize vendor -> "
                    "victim with the victim/api leaf")
    before = _gf_tree(parent)

    r = gf("-C", str(parent), "worktree", "remove", str(wt2),
           check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert not os.path.lexists(wt2)
    assert os.path.realpath(wt2) not in _worktree_paths(parent)
    # the outside leaf was never unlinked
    assert (victim / "api").is_symlink()
    assert os.readlink(victim / "api") == leaf_target
    # the source binding and all of .gf are untouched
    assert _gf_tree(parent) == before
    assert (parent / "vendor" / "api" / "x.txt").read_text() == \
        "api on master\n"


# ---------------------------------------------------------------------------
# Layer 2c — the pull write sites: `ensure_consumer_link(root=)` in
# `pull_shared_bindings`, `cmd_pull`'s plan loop, `update_child`'s
# shelf-layer repeat, and `strip_placeholder_child` on the spelled path


def test_pull_never_creates_consumer_link_outside_parent(tmp_path):
    """Create-side of the pull link step: `parent/vendor` is a planted
    symlink to an EMPTY `victim/`, so the spelled link's parent resolves
    outside — the pull must refuse rather than materialize
    `victim/api`. (Pre-fix: rc=0 `Pulled api` and the link planted
    outside the parent.)"""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _write_manifest(parent, [{
        "name": "api", "url": f"{up}/docs/api", "ref": "latest",
        "path": "vendor/api"}])
    victim = tmp_path / "victim"
    victim.mkdir()
    os.symlink(str(victim), parent / "vendor")

    r = gf("-C", str(parent), "pull", check=False)

    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "api" in err, err     # folder_error names the binding
    assert not os.path.lexists(victim / "api"), (
        "the pull planted a consumer link outside the parent")


def test_pull_retarget_never_writes_link_outside_parent(tmp_path):
    """Retarget-side of the pull link step: `victim/api` is pre-pointed
    at the live `master` checkout (the binding resolves as established),
    and a `ref = "dev"` override retargets it. The link step must refuse
    — pre-fix it unlinked `victim/api` and wrote the retargeted consumer
    link there, outside the parent."""
    parent, up = _parent_with_binding(tmp_path)
    push_branch(up, "dev", "dev content")
    victim = tmp_path / "victim"
    victim.mkdir()
    # victim/api -> the real master checkout subdir (absolute target,
    # never routed back through parent/vendor — that would be a loop)
    target = (parent / "vendor" / "api").resolve()
    os.symlink(str(target), victim / "api")
    # plant the mid-path symlink in the consumer root
    (parent / "vendor" / "api").unlink()
    (parent / "vendor").rmdir()
    os.symlink(str(victim), parent / "vendor")
    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "dev"\n')

    r = gf("-C", str(parent), "pull", check=False)

    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "api" in err and "vendor/api" in err, err
    # the outside leaf was never unlinked or retargeted
    assert (victim / "api").is_symlink()
    assert os.readlink(victim / "api") == str(target)
    # the spelled consumer path still serves the master checkout
    assert (parent / "vendor" / "api" / "x.txt").read_text() == \
        "api on master\n"


def test_pull_refuses_resolved_child_outside_anchor(tmp_path):
    """cmd_pull's plan-loop containment (the update_child guard is the
    shelf-layer repeat): a whole-repo binding whose spelled `vendor/wr`
    resolves outside the parent dies during routing — before any fetch
    — and the outside child is never pulled into. Pre-fix the pull
    fetched upstream and checked the advanced commit out INSIDE
    `victim/wr` (rc=0, `Pulled wr`)."""
    up = _upstream(tmp_path)
    # `victim` hosts the real whole-repo child — it is its own little
    # parent repo only so `gf clone` can create the child there.
    victim = tmp_path / "victim"
    git("init", "-q", str(victim), cwd=tmp_path)
    r = gf("-C", str(victim), "clone", str(up), "wr", check=False)
    if r.returncode != 0:
        pytest.fail(f"setup: victim clone rc={r.returncode}:\n"
                    f"{r.stdout}\n{r.stderr}")
    if (victim / "wr" / "docs" / "api" / "x.txt").read_text() != \
            "api on master\n":
        pytest.fail("setup: victim child did not check out master")

    parent = _parent(tmp_path)
    _write_manifest(parent, [{
        "name": "wr", "url": str(up), "ref": "latest",
        "path": "vendor/wr"}])
    os.symlink(str(victim), parent / "vendor")
    _advance(up, tmp_path, "adv")

    r = gf("-C", str(parent), "pull", check=False)

    err = r.stderr + r.stdout
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "wr" in err and "vendor/wr" in err, err
    # the outside child was never fetched into or checked out
    assert (victim / "wr" / "docs" / "api" / "x.txt").read_text() == \
        "api on master\n"
    # and the pull died before building `.gf` storage at the parent
    assert not (parent / ".gf").exists()


def test_update_child_refuses_resolved_child_outside_parent(tmp_path):
    """The shelf-layer invariant directly: `update_child` raises
    ValidationError when `co.work_tree`'s realpath escapes the owning
    root — the guard holds even when reached without cmd_pull's
    plan-loop check. Pre-fix it proceeded and materialized the child
    INSIDE `victim/wr` (`.gf/` gitdir + upstream checkout)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    victim = tmp_path / "victim"
    (victim / "wr").mkdir(parents=True)
    os.symlink(str(victim), parent / "vendor")
    co = layout.resolve_checkout(parent / "vendor" / "wr")

    with pytest.raises(ValidationError):
        shelf.update_child(co, str(up), "latest", parent)

    # nothing was written through the mid-path symlink
    assert sorted(p.name for p in victim.iterdir()) == ["wr"]
    assert list((victim / "wr").iterdir()) == []


def test_pull_does_not_strip_placeholder_through_leaf_symlink(tmp_path):
    """`strip_placeholder_child` now receives the SPELLED link path: a
    consumer path that is itself a leaf symlink to an in-root `gf init`
    placeholder is retargeted as a link — the placeholder directory it
    points at is never rmtree'd. (Pre-fix the RESOLVED placeholder was
    stripped, destroying a child through the link.)"""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    r = gf("-C", str(parent), "init", "ph", check=False)
    if r.returncode != 0:
        pytest.fail(f"setup: init rc={r.returncode}:\n"
                    f"{r.stdout}\n{r.stderr}")
    # the manifest binding lives at vendor/api; `ph` is only the
    # placeholder the leaf link happens to point at
    _write_manifest(parent, [{
        "name": "api", "url": f"{up}/docs/api", "ref": "latest",
        "path": "vendor/api"}])
    (parent / "vendor").mkdir()
    os.symlink(str(parent / "ph"), parent / "vendor" / "api")

    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    # the link leaf was retargeted and now serves upstream
    assert (parent / "vendor" / "api" / "x.txt").read_text() == \
        "api on master\n"
    # the placeholder behind it was never removed
    assert (parent / "ph" / ".gf" / "git" / "HEAD").is_file()


# ---------------------------------------------------------------------------
# Control — pull through a `gf worktree add` link chain still serves


def test_pull_through_added_worktree_link_chain_still_serves(tmp_path):
    """The plan-loop containment is ownership-aware: pulled from the
    added worktree, the spelled consumer path resolves into the OWNING
    root's `.gf/wt` checkout — inside the anchor — so the pull updates
    the source checkout and both link spellings serve the advance."""
    parent, up = _parent_with_binding(tmp_path)
    wt2 = tmp_path / "wt2"
    r = gf("-C", str(parent), "worktree", "add", str(wt2), check=False)
    if r.returncode != 0:
        pytest.fail(f"setup: worktree add rc={r.returncode}:\n"
                    f"{r.stdout}\n{r.stderr}")
    link2 = wt2 / "vendor" / "api"
    target_before = os.readlink(link2)

    _advance(up, tmp_path, "adv")
    r = gf("-C", str(wt2), "pull", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled api" in r.stdout
    assert (parent / "vendor" / "api" / "x.txt").read_text() == \
        "api adv\n"
    assert (link2 / "x.txt").read_text() == "api adv\n"
    # the link still spells the source consumer path; no `.gf` in wt2
    assert os.readlink(link2) == target_before
    assert not (wt2 / ".gf").exists()


# ---------------------------------------------------------------------------
# Layer 2d — depth-delta mid-path symlinks: the link's relative target
# anchors at the link parent's REALPATH
#
# A committed in-root `sub -> deep/nested/a` (spelled depth 1, real
# depth 3) redirects the consumer leaf to `deep/nested/a/x`. A relpath
# anchored at the SPELLED parent climbs one `..` too few: the leaf
# spells `../.gf/wt/...` but resolves from `deep/nested/a` into the
# nonexistent `deep/nested/.gf/wt/...` — a dangling consumer link under
# rc=0. The fix (`_place_consumer_link` realpaths both ends;
# `_worktree_link_folders` bases the relpath at the realpath'd parent
# while keeping the spelled source consumer path as target for
# link→link chains) is discriminated ONLY by a depth delta — a
# same-depth `sub -> inner` redirect makes the spelled and realpath'd
# anchors equivalent, which is why the pull arm gets a same-depth
# control below (the worktree-add control already lives in
# `test_worktree_add_midpath_symlink_inside_tree_still_links`).
#
# Pre-fix signatures verified against base 85856af are noted per test.


def _depth_delta_setup(tmp_path: Path) -> tuple[Path, Path]:
    """(parent, upstream): parent's master commits the in-root mid-path
    symlink `sub -> deep/nested/a` — spelled depth 1, REAL depth 3 —
    and gf.toml binds `sub/x`. The consumer leaf `sub/x` therefore
    lands at `deep/nested/a/x`, three levels below the root."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    deep = parent / "deep" / "nested" / "a"
    deep.mkdir(parents=True)
    (deep / "keep.txt").write_text("k\n")
    os.symlink("deep/nested/a", parent / "sub")
    _git("-C", str(parent), "add", "-A")
    _git("-C", str(parent), "commit", "-qm", "midpath symlink")
    _write_manifest(parent, [{
        "name": "api", "url": f"{up}/docs/api", "ref": "latest",
        "path": "sub/x"}])
    return parent, up


def _master_checkout(root: Path) -> Path:
    """The single `<root>/.gf/wt/<repo-key>/master` checkout dir."""
    found = list((root / ".gf" / "wt").glob("*/master"))
    if len(found) != 1:
        pytest.fail(
            f"setup: expected one master checkout, got {found}")
    return found[0]


def test_pull_links_through_depth_delta_midpath_symlink(tmp_path):
    """THE defect signature (base 85856af): the leaf lands at the link
    parent's REALPATH — `sub/x` materializes at `deep/nested/a/x` —
    while pre-fix the relative target anchored at the SPELLED parent
    `sub/` (depth 1): one `..` too few, so the leaf spelled
    `../.gf/wt/...` and resolved into the nonexistent
    `deep/nested/.gf/wt/...` — a DANGLING consumer link under rc=0
    `Pulled api`. `gf status` then resolved a phantom checkout (`[]`
    branch) and `gf rm` wedged on 'symlinked child; remove it from the
    owning worktree instead' (rc=1) — the dangling realpath is not
    under the root's `.gf/wt`."""
    parent, _up = _depth_delta_setup(tmp_path)

    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled api" in r.stdout
    # The leaf REALLY lands through `sub` at deep/nested/a/x.
    leaf = parent / "deep" / "nested" / "a" / "x"
    assert leaf.is_symlink()
    # A relative link spelling the real checkout, anchored at the
    # realpath'd parent.
    checkout = _master_checkout(parent) / "docs" / "api"
    assert checkout.is_dir()
    target = os.readlink(leaf)
    assert not os.path.isabs(target)
    assert target == os.path.relpath(checkout, leaf.parent)
    # ...so the SPELLED path resolves to real content.
    assert os.path.exists(parent / "sub" / "x"), (
        f"consumer link dangles: {target!r} resolves to "
        f"{os.path.realpath(leaf)}")
    assert os.path.realpath(parent / "sub" / "x") == \
        os.path.realpath(checkout)
    assert (parent / "sub" / "x" / "x.txt").read_text() == \
        "api on master\n"

    # Follow-up: `gf status` resolves the real checkout — clean, on
    # master, no porcelain output (pre-fix: `api <url> []`).
    r = gf("-C", str(parent), "status", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "[master]" in r.stdout, r.stdout
    assert len([l for l in r.stdout.splitlines() if l.strip()]) == 1, \
        r.stdout


def test_pull_links_through_same_depth_midpath_symlink(tmp_path):
    """Same-depth control: `sub -> inner` (spelled depth 1, real depth
    1) makes the spelled and realpath'd anchors equivalent — the pull
    links through it exactly as before. Guards the fix against
    refusing or mis-anchoring an in-root mid-path link; green both
    pre- and post-fix (the delta arm above is what discriminates)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    (parent / "inner").mkdir()
    (parent / "inner" / "keep.txt").write_text("k\n")
    os.symlink("inner", parent / "sub")
    _git("-C", str(parent), "add", "-A")
    _git("-C", str(parent), "commit", "-qm", "same-depth midpath link")
    _write_manifest(parent, [{
        "name": "api", "url": f"{up}/docs/api", "ref": "latest",
        "path": "sub/x"}])

    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled api" in r.stdout
    leaf = parent / "inner" / "x"
    assert leaf.is_symlink()
    checkout = _master_checkout(parent) / "docs" / "api"
    assert os.path.realpath(parent / "sub" / "x") == \
        os.path.realpath(checkout)
    assert (parent / "sub" / "x" / "x.txt").read_text() == \
        "api on master\n"


def test_rm_through_depth_delta_midpath_symlink_removes_only_link(tmp_path):
    """The `gf rm` follow-up: once the link resolves, `remove_child`'s
    `owns_consumer_link` sees the realpath under the root's `.gf/wt`
    and unlinks ONLY the leaf — no wedge, checkout and mid-path
    redirect preserved. (Pre-fix: rc=1 'sub/x is a symlinked child;
    remove it from the owning worktree instead' — the dangling
    realpath looked foreign.)"""
    parent, _up = _depth_delta_setup(tmp_path)
    r = gf("-C", str(parent), "pull", check=False)
    if r.returncode != 0:
        pytest.fail(f"setup: pull rc={r.returncode}:\n"
                    f"{r.stdout}\n{r.stderr}")
    checkout = _master_checkout(parent)

    r = gf("-C", str(parent), "rm", "sub/x", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Removed api" in r.stdout
    assert "owning worktree" not in (r.stderr + r.stdout)
    # only the link went — checkout, store, redirect, real dir intact
    assert not os.path.lexists(parent / "deep" / "nested" / "a" / "x")
    assert (checkout / "docs" / "api" / "x.txt").is_file()
    assert (parent / "deep" / "nested" / "a" / "keep.txt").is_file()
    assert (parent / "sub").is_symlink()
    # the binding left the manifest
    data = tomllib.loads((parent / "gf.toml").read_text())
    assert data.get("git_folder", []) == [], data


def test_worktree_add_links_through_depth_delta_midpath_symlink(tmp_path):
    """Worktree-add arm of the same anchor fix: branch `sym` commits
    `sub -> deep/nested/a` (in-root, spelled depth 1 → real depth 3),
    which `git worktree add` materializes in the NEW tree; the link
    step's relative target must base at the realpath'd parent while
    still SPELLING the source consumer path `parent/sub/x` for the
    link→link chain (spec L400).

    Pre-fix (base 85856af): `rel` anchored at the spelled `wt2/sub`
    — `../../parent/sub/x`, two `..`s short — so `deep/nested/a/x`
    spelled a target resolving to `wt2/deep/parent/sub/x` and
    dangled: rc=0 `Added worktree`, leaf a symlink, but nothing
    readable through `wt2/sub/x`."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    # Plain source binding at sub/x: the mid-path redirect exists only
    # in `sym`'s tree, so this arm isolates the worktree-add hunk —
    # the source link resolves pre- and post-fix alike.
    _write_manifest(parent, [{
        "name": "api", "url": f"{up}/docs/api", "ref": "latest",
        "path": "sub/x"}])
    r = gf("-C", str(parent), "pull", check=False)
    if r.returncode != 0:
        pytest.fail(f"setup: pull rc={r.returncode}:\n"
                    f"{r.stdout}\n{r.stderr}")
    _commit_branch_with(parent, "sym", {
        "sub": ("link", "deep/nested/a"),
        "deep/nested/a/keep.txt": ("file", "k\n")})
    wt2 = tmp_path / "wt2"

    r = gf("-C", str(parent), "worktree", "add", str(wt2), "sym",
           check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert os.path.realpath(wt2) in _worktree_paths(parent)
    # The leaf landed through the materialized mid-path symlink.
    leaf = wt2 / "deep" / "nested" / "a" / "x"
    assert leaf.is_symlink()
    # It still SPELLS the source consumer path — now anchored at the
    # realpath'd parent so the chain resolves through to the real
    # checkout.
    assert os.readlink(leaf) == os.path.relpath(
        parent / "sub" / "x", leaf.parent)
    assert os.path.exists(wt2 / "sub" / "x"), (
        f"chained link dangles: {os.readlink(leaf)!r} resolves to "
        f"{os.path.realpath(leaf)}")
    assert os.path.realpath(wt2 / "sub" / "x") == \
        os.path.realpath(parent / "sub" / "x")
    assert (wt2 / "sub" / "x" / "x.txt").read_text() == \
        "api on master\n"


# ---------------------------------------------------------------------------
# Layer 3 — `.git` repository-metadata containment (r14-f1)
#
# `.git` is the parent's private metadata — objects, refs, and the hooks
# git executes on checkout — never a legal consumer path. Three refusal
# layers, mirroring the `.gf` arms above:
#
# 1. `manifest._validate_consumer_path` rejects a `.git` SEGMENT in the
#    spelled `path` at read, so every manifest-consuming command fails
#    closed (rc=1 `gf:` envelope `reaches inside repository metadata
#    (.git)`, no traceback) before any write.
# 2. `ensure_consumer_link`'s realpath-parent guard refuses when a
#    committed mid-path symlink (`sub -> .git/hooks`) lands the link
#    parent inside `.git`.
# 3. The CLI operand parts-checks refuse `.git`-segment spellings:
#    `cmd_clone`'s anywhere-in-path test (which also closes the mid-path
#    `.gf` hole — `sub/.gf/x` slipped past the old `.gf`-ROOT
#    containment predicate), `cmd_init`'s `rel`-parts + `in_git_tree`
#    guard, and `cmd_worktree_add`'s `in_git_tree(new_parent)` guard.
#
# Pre-fix signatures verified against HEAD 7919520 are recorded per test.


def _hooks_listing(repo: Path) -> dict:
    """`.git/hooks` → {name: ('link', target) | ('dir',) | bytes} —
    the untouched-metadata witness for a refused write near the hook
    directory."""
    hooks = repo / ".git" / "hooks"
    out = {}
    for p in sorted(hooks.iterdir()):
        if p.is_symlink():
            out[p.name] = ("link", os.readlink(p))
        elif p.is_dir():
            out[p.name] = ("dir",)
        else:
            out[p.name] = p.read_bytes()
    return out


def test_git_metadata_manifest_path_fails_closed_everywhere(tmp_path):
    """THE P1 read gate: a COMMITTED `gf.toml` whose binding `path`
    spells `.git/hooks/post-checkout` — the hook git executes on every
    `git checkout` — is refused at manifest READ: `gf pull`, `gf ls`,
    `gf status` and `gf rm` all exit 1 with the IDENTICAL `gf:` envelope
    `... reaches inside repository metadata (.git)`, no traceback, and
    `.git/hooks` stays byte-identical.

    Pre-fix (HEAD 7919520): `_validate_consumer_path` had no `.git`
    segment check — the hostile manifest read clean and `gf pull`
    PLANTED the consumer link at `.git/hooks/post-checkout` (rc=0
    `Pulled evil`); `ls`/`status`/`rm` all served the binding (rc=0).
    """
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _write_manifest(parent, [{
        "name": "evil", "url": f"{up}/docs/api", "ref": "latest",
        "path": ".git/hooks/post-checkout"}])
    # gf.toml is committed content — the untrusted-input surface every
    # command reads.
    _git("-C", str(parent), "add", "gf.toml")
    _git("-C", str(parent), "commit", "-qm", "hostile manifest")
    hooks_before = _hooks_listing(parent)
    before = _tree_bytes(parent)

    envelopes = []
    for cmd in (("pull",), ("ls",), ("status",), ("rm", "evil")):
        r = gf("-C", str(parent), *cmd, check=False)
        err = r.stderr + r.stdout
        assert r.returncode == 1, (cmd, r.returncode, r.stdout, r.stderr)
        assert "Traceback" not in err, err
        assert r.stderr.startswith("gf: "), err
        assert "reaches inside repository metadata (.git)" in err, err
        # the refusal names the binding and its spelled path
        assert "'evil'" in err and "'.git/hooks/post-checkout'" in err, err
        assert "Pulled" not in r.stdout
        envelopes.append(r.stderr)

    # fail-closed at read: identical refusal from every command
    assert len(set(envelopes)) == 1, envelopes
    # `.git/hooks` is byte-identical — nothing planted at post-checkout
    assert _hooks_listing(parent) == hooks_before
    assert not os.path.lexists(parent / ".git" / "hooks" / "post-checkout")
    # and the refused reads wrote nothing anywhere in the tree
    assert _tree_bytes(parent) == before


def test_pull_refuses_midpath_symlink_into_git_hooks(tmp_path):
    """The committed mid-path symlink into `.git`: `sub` is a real
    committed mode-120000 symlink to `.git/hooks` (git stores the link
    text; it never resolves it), so `path = "sub/post-checkout"` passes
    the lexical gate yet lands its leaf in the parent's hook directory.
    The pull must refuse at the LINK layer — rc=1 `gf:` envelope naming
    the binding — and nothing appears at `.git/hooks/post-checkout`.

    Pre-fix: `ensure_consumer_link`'s root guard tested containment and
    `.gf` only, so the pull planted a consumer symlink at
    `.git/hooks/post-checkout` — a file inside the hook directory the
    parent's next `git checkout` reads (rc=0 `Pulled api`).
    """
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    os.symlink(".git/hooks", parent / "sub")
    _write_manifest(parent, [{
        "name": "api", "url": f"{up}/docs/api", "ref": "latest",
        "path": "sub/post-checkout"}])
    _git("-C", str(parent), "add", "sub", "gf.toml")
    _git("-C", str(parent), "commit", "-qm", "mid-path symlink into .git")
    hooks_before = _hooks_listing(parent)

    r = gf("-C", str(parent), "pull", check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in err
    # the store checkout's streamed git output can precede the envelope;
    # the refusal itself is a `gf:` folder-error line naming the binding
    assert ("gf: pull failed for git-folder 'api' (sub/post-checkout)"
            in r.stderr), err
    assert "repository metadata (.git)" in err, err
    assert "refusing to link" in err, err
    # nothing planted at the hook path — through either spelling
    assert not os.path.lexists(parent / ".git" / "hooks" / "post-checkout")
    assert not os.path.lexists(parent / "sub" / "post-checkout")
    assert _hooks_listing(parent) == hooks_before
    # the committed redirect itself is left alone
    assert os.readlink(parent / "sub") == ".git/hooks"


@pytest.mark.parametrize(
    "spelling",
    [".git/child", "sub/.gf/x"],
    ids=["git-segment", "midpath-gf-segment"],
)
def test_clone_refuses_metadata_segment_path(tmp_path, spelling):
    """`gf clone`'s anywhere-in-path parts-check refuses a `.git` OR
    `.gf` segment in the spelled child path before any fetch, mkdir, or
    manifest write of the binding.

    `.git/child` would plant the child gitdir inside the parent's own
    metadata (pre-fix: rc=0 `Cloned child into .git/child` — the child
    really was cloned under `.git/`). `sub/.gf/x` is the mid-path `.gf`
    hole: pre-fix the `.gf`-ROOT containment predicate missed it and the
    refusal surfaced only late, at the shelf layer, with a different
    envelope (`(sub/.gf/x)` naming the relative path, no `.git` phrase);
    a `sub/.gf/x` spelling that already satisfied `_is_git_folder_child`
    slipped even that and recorded a binding the manifest reader then
    wedged on (the next test pins that arm). Post-fix both spellings die
    identically at the operand check.
    """
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)

    r = gf("-C", str(parent), "clone", str(up), spelling, check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in err
    assert r.stderr.startswith("gf: "), err
    assert "clone failed for git-folder" in err, err
    assert ("resolves inside gf-managed storage (.gf) or repository "
            "metadata (.git)") in err, err
    # nothing planted at the spelled path or inside `.git`
    assert not os.path.lexists(parent / ".git" / "child")
    assert not os.path.lexists(parent / "sub")
    # and no binding for the refused path was recorded
    data = tomllib.loads((parent / "gf.toml").read_text())
    assert data.get("git_folder", []) == [], data


def test_clone_never_records_midpath_gf_binding(tmp_path):
    """The `sub/.gf/x` wedge hole itself: the spelled path already holds
    a git-folder child (`sub/.gf/x/.gf/git/HEAD` exists — planted here
    by hand, the way a pre-gf layout or hostile checkout could leave
    one). Pre-fix `cmd_clone` only tested the `.gf`-ROOT predicate, and
    `init_child` early-returns on an existing child BEFORE its storage
    guard — so the clone "succeeded" (rc=0 `Cloned x into sub/.gf/x`)
    and wrote a `path = "sub/.gf/x"` binding every later command refused
    on read: a manifest-recorded wedge, cleared only by hand-editing
    gf.toml. Post-fix the operand parts-check refuses up front and the
    binding is never recorded."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    # a pre-existing child at the spelled `.gf`-interior path
    gitdir = parent / "sub" / ".gf" / "x" / ".gf" / "git"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/master\n")
    child_before = _tree_bytes(parent / "sub")

    r = gf("-C", str(parent), "clone", str(up), "sub/.gf/x", check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in err
    assert r.stderr.startswith("gf: "), err
    assert ("resolves inside gf-managed storage (.gf) or repository "
            "metadata (.git)") in err, err
    # the binding was never recorded — no manifest wedge
    data = tomllib.loads((parent / "gf.toml").read_text())
    assert data.get("git_folder", []) == [], data
    # and the occupant at the spelled path is untouched
    assert _tree_bytes(parent / "sub") == child_before


@pytest.mark.parametrize(
    "spelling",
    [".git", "sub/.git/x"],
    ids=["dotgit", "nested"],
)
def test_init_refuses_git_metadata_target(tmp_path, spelling):
    """`gf init`'s `rel`-parts + `in_git_tree` guard refuses a `.git`
    segment anywhere in the spelled target. `.git` is the parent's own
    metadata dir — pre-fix `gf init .git` planted a fresh `.gf` gitdir
    INSIDE it (rc=0 `Initialized .git in .git`, `.git/.gf/git` created);
    `sub/.git/x` created `sub/.git/x` and recorded the binding (rc=0
    `Initialized x in sub/.git/x`). Post-fix both die at the operand
    check, before mkdir and before the manifest write."""
    parent = _parent(tmp_path)

    r = gf("-C", str(parent), "init", spelling, check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in err
    assert r.stderr.startswith("gf: "), err
    assert "init failed for git-folder" in err, err
    assert ("resolves inside gf-managed storage (.gf) or repository "
            "metadata (.git)") in err, err
    # nothing planted inside `.git` or at the spelled path
    assert not os.path.lexists(parent / ".git" / ".gf")
    assert not os.path.lexists(parent / "sub")
    # and no manifest (binding) was written
    assert not os.path.lexists(parent / "gf.toml")


def test_worktree_add_refuses_target_inside_git_metadata(tmp_path):
    """`gf worktree add` refuses a destination inside the parent's
    `.git` — the copied manifest and linked children would sit among the
    repo's own config/hooks. Pre-fix the `in_git_tree` guard did not
    exist and plain `git worktree add` happily created `.git/wt2`
    (rc=0 `Added worktree`, `.git/worktrees/wt2` registered)."""
    parent = _parent(tmp_path)
    _write_manifest(parent, [])  # `worktree add` requires a gf.toml

    r = gf("-C", str(parent), "worktree", "add", ".git/wt2",
           check=False)

    err = r.stderr + r.stdout
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in err
    assert r.stderr.startswith("gf: "), err
    assert ("resolves inside gf-managed storage (.gf) or repository "
            "metadata (.git)") in err, err
    # no worktree tree or registration inside `.git`
    assert not os.path.lexists(parent / ".git" / "wt2")
    assert not os.path.lexists(parent / ".git" / "worktrees" / "wt2")


# ---------------------------------------------------------------------------
# Layer 3 controls — the segment tests match EXACT `.git`/`.gf` parts;
# substrings and ordinary paths stay legal


@pytest.mark.parametrize(
    "leaf",
    ["x.git", "x.gf"],
    ids=["dotgit-substring", "dotgf-substring"],
)
def test_clone_allows_metadata_substring_leaf(tmp_path, leaf):
    """Control: a leaf merely CONTAINING `.git`/`.gf` as a substring is
    an ordinary consumer path — the parts-check never split-matches —
    and the clone succeeds (green pre- and post-fix)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)

    r = gf("-C", str(parent), "clone", str(up), leaf, check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (parent / leaf / "docs" / "api" / "x.txt").read_text() == \
        "api on master\n"


def test_pull_serves_plain_subdir_binding(tmp_path):
    """Control: a normal consumer path keeps serving — the `.git`
    segment check only fires on an actual `.git` part."""
    parent, _up = _parent_with_binding(tmp_path)

    r = gf("-C", str(parent), "pull", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Pulled api" in r.stdout
    assert (parent / "vendor" / "api" / "x.txt").read_text() == \
        "api on master\n"


def test_worktree_add_outside_parent_still_works(tmp_path):
    """Control: `gf worktree add ../wt` — a spelled `..` landing OUTSIDE
    the parent — is a legitimate worktree destination; only `.gf`/`.git`
    metadata trees refuse."""
    parent, _up = _parent_with_binding(tmp_path)

    r = gf("-C", str(parent), "worktree", "add", "../wt", check=False)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    wt = tmp_path / "wt"
    assert os.path.realpath(wt) in _worktree_paths(parent)
    # the sibling binding linked into the new worktree serves content
    assert (wt / "vendor" / "api" / "x.txt").read_text() == \
        "api on master\n"
