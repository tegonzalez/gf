# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`gf worktree add/list/remove` consumer-link semantics.

Pinned contract: `cli.cmd_worktree_add` links to the source worktree's
consumer path AS SPELLED (not the resolved checkout — retargets
propagate for both forms); `shelf.linked_git_folders_in_worktree`,
`linked_git_folder_symlinks_in_worktree`,
`unlink_linked_git_folders_in_worktree`, `is_linked_child` treat
link→consumer-link chains correctly.

Scope: worktree add links to the source consumer link, not the resolved
checkout; a source retarget propagates; list/--porcelain report linked
bindings with `<name> -> <relative-link-target>`; remove unlinks links
without touching source link/checkout/store; main/current-worktree
refusals unchanged; worktree-add 'child path already exists' error
carries name+path+operation, rc=1 (kept as a green guard).

Authorities: spec `gf worktree add` (L392-410 — L399 relative symlink at
`<wt>/<path>`; L400 "the link targets the source worktree's consumer
link (not the resolved checkout), so a later retarget propagates"),
`gf worktree list` (L412-437 — human `<name> -> <relative-link-target>`
L418; porcelain `git-folder <name> <relative-link-target>` L425),
`gf worktree remove` (L440-454 — unlink the links so the source is not
touched; L447 source children/stores/checkouts preserved); arch GF-D14 —
BOTH forms: a second-worktree link to a subfolder-binding
consumer link chains link→link; to a whole-repo child the "consumer
path" is the child dir itself.

Real git over local bare upstreams via `gf clone`; `gf` subprocess only.
(Relocated T3 retained from /tmp/ver1-s10 staging.)
"""

import os
import re
import subprocess
import tomllib
from pathlib import Path

from conftest import gf, git, push_branch


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


def _upstream(tmp_path: Path, name: str = "upstream",
              marker: str = "api on master") -> Path:
    up = tmp_path / name
    _git("init", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text(marker)
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text(f"tool in {name}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "init")
    _git("-C", work, "push", "origin", "master")
    return up


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


def _advance(up: Path, tmp_path: Path, tag: str) -> None:
    """Push one more commit to upstream master (docs/api + tools)."""
    work = tmp_path / f"_adv_{tag}"
    _git("clone", str(up), str(work))
    (work / "docs" / "api" / "x.txt").write_text(f"api {tag}")
    (work / "tools" / "t.txt").write_text(f"tool {tag}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", tag)
    _git("-C", work, "push", "origin", "master")


# ---------------------------------------------------------------------------
# worktree add — link spells the SOURCE consumer path (both forms)


def test_add_link_spells_source_consumer_link(tmp_path):
    """For a subfolder binding the new worktree's link spells the source
    worktree's consumer LINK as-written (`../../parent/vendor/api`), not
    the resolved checkout path under `.gf/wt` — the chain link→link is
    what lets a source retarget propagate (spec L399/L400; plan D12;
    contract: drop `source_child.resolve()`)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))

    link = wt2 / "vendor" / "api"
    assert link.is_symlink()
    target = os.readlink(link)
    assert not os.path.isabs(target)
    assert ".gf" not in target.split(os.sep), (
        f"link spells the resolved checkout, not the consumer link: "
        f"{target}")
    assert target == os.path.relpath(parent / "vendor" / "api",
                                     link.parent)
    # the chain resolves to the mapped checkout subdir either way
    assert link.resolve() == (parent / "vendor" / "api").resolve()


def test_add_whole_repo_link_spells_child_dir(tmp_path):
    """For a whole-repo child the "consumer path" is the child dir
    itself — the new link spells `../../<src>/<path>` and resolves to
    the child (spec L400 second form; unchanged behavior)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "libs/up")
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))

    link = wt2 / "libs" / "up"
    assert link.is_symlink()
    target = os.readlink(link)
    assert not os.path.isabs(target)
    assert target == os.path.relpath(parent / "libs" / "up", link.parent)
    assert link.resolve() == (parent / "libs" / "up").resolve()


def test_worktree_link_follows_source_link_retargets(tmp_path):
    """The worktree-add link points at the SOURCE worktree's consumer
    link, not the resolved checkout, so a later retarget in the source
    worktree propagates (spec L403 verbatim; GF-D14 link chain)."""
    up_a = _upstream(tmp_path, "up_a", "api in A")
    up_b = _upstream(tmp_path, "up_b", "api in B")
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up_a / "docs/api"), "vendor/api")
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    link2 = wt2 / "vendor" / "api"
    assert (link2 / "x.txt").read_text().strip() == "api in A"

    # retarget the source binding's link (override moves it to repo B)
    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\n'
        f'url = "{up_b}/docs/api"\n')
    gf("-C", str(parent), "pull")

    # wt2 sees the retargeted content because its link chains through
    # the source worktree's consumer link
    assert (link2 / "x.txt").read_text().strip() == "api in B"


# ---------------------------------------------------------------------------
# worktree list — linked bindings reported with the spelled target


def test_list_reports_linked_bindings(tmp_path):
    """Human `gf worktree list` prints `<name> -> <relative-link-target>`
    for each linked binding — the raw relative link spelling, resolved
    through the link→consumer-link chain for detection (spec L417-418).
    The source worktree's own children are owned, not linked, so they
    are not annotated."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))

    r = gf("-C", str(parent), "worktree", "list")
    lines = r.stdout.splitlines()
    api = [l for l in lines if re.search(r"\bapi -> ", l)]
    tools = [l for l in lines if re.search(r"\btools -> ", l)]
    assert len(api) == 1 and len(tools) == 1
    assert api[0].split(" -> ", 1)[1] == os.readlink(wt2 / "vendor" / "api")
    assert tools[0].split(" -> ", 1)[1] == os.readlink(
        wt2 / "vendor" / "tools")
    # owned children in the source worktree are not "linked"
    parent_lines = [l for l in lines if str(parent) in l]
    assert parent_lines and " -> " not in parent_lines[0]


def test_list_porcelain_git_folder_lines(tmp_path):
    """`gf worktree list --porcelain` emits `git-folder <name>
    <relative-link-target>` lines inside the worktree block
    (spec L419-429 — no arrow in porcelain)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))

    r = gf("-C", str(parent), "worktree", "list", "--porcelain")
    blocks = [b for b in r.stdout.split("\n\n") if b.strip()]
    wt2_block = [b for b in blocks
                 if f"worktree {wt2}" in b.splitlines()[0]]
    assert wt2_block, r.stdout
    folders = [l for l in wt2_block[0].splitlines()
               if l.startswith("git-folder ")]
    assert folders == [
        f"git-folder api {os.readlink(wt2 / 'vendor' / 'api')}"]


# ---------------------------------------------------------------------------
# worktree remove — unlink only; source untouched


def test_remove_unlinks_links_preserving_source(tmp_path):
    """`gf worktree remove` unlinks the linked children so
    `git worktree remove` never touches the source link, checkout, or
    store — the whole `.gf` tree is byte-identical (spec L444/L447;
    GF-D9/D14)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    # tracked manifest → the copy worktree-add drops into wt2 is
    # tracked-identical, so `git worktree remove` sees a clean tree
    # once the untracked links are unlinked
    git("add", "gf.toml", cwd=parent)
    git("commit", "-m", "manifest", cwd=parent)
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    assert (wt2 / "vendor" / "api").is_symlink()
    (parent / "vendor" / "api" / "dirty.txt").write_text("uncommitted")
    before = _gf_tree(parent)

    gf("-C", str(parent), "worktree", "remove", str(wt2))

    assert not wt2.exists()
    assert _gf_tree(parent) == before
    # source link still serves the checkout, dirty file included
    assert (parent / "vendor" / "api" / "dirty.txt").is_file()
    assert (parent / "vendor" / "api" / "x.txt").is_file()


def test_remove_failure_restores_links(tmp_path):
    """If `git worktree remove` refuses (here: untracked copied manifest
    makes wt2 dirty), the unlinked child links are restored — the
    worktree is left in its original state (impl contract on the L444
    unlink step; row-adjacent)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    link = wt2 / "vendor" / "api"
    target = os.readlink(link)
    # gf.toml was copied into wt2 untracked → git refuses removal
    r = gf("-C", str(parent), "worktree", "remove", str(wt2), check=False)
    assert r.returncode != 0
    assert link.is_symlink() and os.readlink(link) == target
    assert (link / "x.txt").is_file()


# ---------------------------------------------------------------------------
# refusals — unchanged


def test_remove_main_worktree_refused(tmp_path):
    """Refusing to remove the main worktree (spec L440 guard; unchanged)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    r = gf("-C", str(parent), "worktree", "remove", str(parent),
           check=False)
    assert r.returncode != 0
    assert "main worktree" in r.stderr
    assert (parent / "vendor" / "api").is_symlink()


def test_remove_current_worktree_refused(tmp_path):
    """Refusing to remove the worktree the command runs in (spec L440
    guard; unchanged)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    r = gf("-C", str(wt2), "worktree", "remove", str(wt2), check=False)
    assert r.returncode != 0
    assert "current worktree" in r.stderr
    assert (wt2 / "vendor" / "api").is_symlink()


def test_remove_unlisted_path_refused(tmp_path):
    """A path that is not a listed worktree is refused (spec L440 guard;
    unchanged, row-adjacent)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    stray = tmp_path / "stray"
    stray.mkdir()
    r = gf("-C", str(parent), "worktree", "remove", str(stray),
           check=False)
    assert r.returncode != 0
    assert "not a listed worktree" in r.stderr
    assert stray.is_dir()


def test_add_child_path_exists_error_carries_fields(tmp_path):
    """worktree-add 'child path already exists' on an identified folder
    carries name+path+operation with rc=1 — kept as a green guard
    (spec §Error handling).
    Trigger: the consumer link committed into parent HEAD materializes
    as a real path in the new worktree before the link step runs."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    git("add", "-f", "vendor/api", cwd=parent)  # commit the link
    git("commit", "-m", "track link", cwd=parent)

    r = gf("-C", str(parent), "worktree", "add", str(tmp_path / "wt2"),
           check=False)
    assert r.returncode == 1
    err = r.stderr + r.stdout
    assert "api" in err and "vendor/api" in err and "worktree add" in err


# ---------------------------------------------------------------------------
# F1 — pull inside an added worktree updates the SOURCE checkout


def test_pull_in_added_worktree_updates_source_checkout(tmp_path):
    """`gf pull` run INSIDE a `gf worktree add` worktree serves the
    binding through its link→consumer-link chain: the SOURCE worktree's
    shared checkout is fetched and applied — its consumer files move to
    the new upstream HEAD — while the added worktree gets NO `.gf`
    storage of its own and its link keeps spelling the source consumer
    path (never retargeted into new-worktree storage).

    Ruling F1 / spec L400 chain semantics. Defect shape pinned against:
    the pull treated the worktree as its own parent root, built
    `wt2/.gf` store+checkout, and retargeted the worktree's link into it
    while the source checkout stayed behind."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    link2 = wt2 / "vendor" / "api"
    target_before = os.readlink(link2)

    _advance(up, tmp_path, "adv")
    gf("-C", str(wt2), "pull")

    # the source checkout applied the new upstream HEAD — visible at the
    # source consumer path and through the worktree's link chain alike
    assert (parent / "vendor" / "api" / "x.txt").read_text().strip() == (
        "api adv")
    assert (link2 / "x.txt").read_text().strip() == "api adv"
    # no store/checkout materialized inside the added worktree
    assert not (wt2 / ".gf").exists()
    # and the link still spells the source consumer path — the chain is
    # intact rather than retargeted into new-worktree storage
    assert os.readlink(link2) == target_before
    assert link2.resolve() == (parent / "vendor" / "api").resolve()


# ---------------------------------------------------------------------------
# R8-A — a source-relative binding `url` anchors at the OWNING root
#
# `cmd_pull` resolves a binding's relative `url` against the root that owns
# the binding's checkout (`layout.owning_root(co)` for a store checkout,
# `manifest.find_parent_root(child.parent)` for a whole-repo child), never
# against the invoking root. A pull entered through a `gf worktree add`
# worktree at a DIFFERENT depth is the discriminating case: at the same
# depth `../producer` would resolve correctly by accident; nested inside
# the parent it spells a nonexistent `nested/producer/...` whose local
# walk-up finds `p` itself — the signature the defect produced.


def _root(tmp_path: Path, name: str) -> Path:
    """A plain parent repo (one README commit) at `tmp_path/<name>`."""
    root = tmp_path / name
    git("init", str(root), cwd=tmp_path)
    (root / "README").write_text(name)
    git("add", "README", cwd=root)
    git("commit", "-m", "root", cwd=root)
    return root


def _producer(tmp_path: Path, up: Path) -> Path:
    """Parent `producer` whose whole-repo `child` clones `up`."""
    producer = _root(tmp_path, "producer")
    gf("-C", str(producer), "clone", str(up), "child")
    return producer


def _store_names(root: Path) -> list[str]:
    """Sorted repo-key dirs under `<root>/.gf/repos` (empty when absent)."""
    repos = root / ".gf" / "repos"
    if not repos.is_dir():
        return []
    return sorted(p.name for p in repos.iterdir() if p.is_dir())


def _wt_names(root: Path) -> list[str]:
    """Sorted repo-key checkout trees under `<root>/.gf/wt`."""
    wt = root / ".gf" / "wt"
    if not wt.is_dir():
        return []
    return sorted(p.name for p in wt.iterdir() if p.is_dir())


def test_pull_relative_url_through_nested_worktree_anchors_at_source(
        tmp_path):
    """R8-A core repro — the source-relative `url` anchors at the root
    that OWNS the shared checkout, not at the invoking worktree.

    `p`'s `vendor/api` binds `../producer/child/docs/api` — a subfolder
    of sibling `producer`'s whole-repo child. Pulling `vendor/api` from
    the NESTED added worktree `p/nested/wt2` resolves the spelling
    against `p`: the original store fetches the advanced upstream
    content, `p/vendor/api` keeps spelling its real checkout subdir and
    serves the new file through the link, and no second store or
    checkout materializes.

    Defect pinned against: anchoring `../producer/child/docs/api` at
    `wt2` spelled `p/nested/producer/child/docs/api`; the walk-up found
    `p` itself, so the pull built a bogus `p-<key>` store + checkout and
    retargeted `p/vendor/api` into the nonexistent
    `nested/producer/child/docs/api` subdir — rc 0, "Pulled api", a
    dangling link, and the binding vacated from the real checkout's
    record."""
    up = _upstream(tmp_path)
    producer = _producer(tmp_path, up)
    p = _root(tmp_path, "p")
    gf("-C", str(p), "clone", "../producer/child/docs/api", "vendor/api")
    wt2 = p / "nested" / "wt2"
    gf("-C", str(p), "worktree", "add", "nested/wt2")

    api_link = p / "vendor" / "api"
    wt_link = wt2 / "vendor" / "api"
    link_before = os.readlink(api_link)
    wt_link_before = os.readlink(wt_link)
    rk = _store_names(p)
    assert len(rk) == 1 and rk[0].startswith("child-")

    _advance(up, tmp_path, "adv")
    gf("-C", str(producer), "pull")  # producer/child now serves "api adv"
    r = gf("-C", str(wt2), "pull", "vendor/api", check=False)
    assert r.returncode == 0, r.stderr
    assert "Pulled api" in r.stdout

    # the source link still resolves to real, newly-fetched content —
    # through the worktree's chained link spelling as well
    assert (api_link / "x.txt").read_text().strip() == "api adv"
    assert (wt_link / "x.txt").read_text().strip() == "api adv"
    # no second store or checkout tree materialized under `p`, and the
    # added worktree gained no `.gf` storage of its own
    assert _store_names(p) == rk
    assert _wt_names(p) == rk
    assert not (wt2 / ".gf").exists()
    # both link spellings are byte-identical to before the pull
    assert os.readlink(api_link) == link_before
    assert os.readlink(wt_link) == wt_link_before
    # the served binding stays recorded on the original checkout: the
    # pull resolved `vendor/api`'s url to the producer child — never to
    # a `nested/producer` phantom — and did not vacate it
    st = tomllib.loads(
        (p / ".gf" / "wt" / rk[0] / ".master.state").read_text())
    assert st["binding_urls"]["vendor/api"] == str(
        (producer / "child" / "docs" / "api").resolve())
    assert st["bindings"] == ["docs/api"]


def test_pull_whole_repo_relative_url_through_nested_worktree(tmp_path):
    """R8-A whole-repo arm — a whole-repo child reached through a `gf
    worktree add` link resolves into a directory the SOURCE parent's
    walk-up owns, so its relative `url` anchors there too.

    `p`'s `vendor/wr` binds `../producer/child` whole-repo. Pulling
    `vendor/wr` from `p/nested/wt2` anchors at `p` (`child.parent`
    walk-up — a whole-repo checkout has no `.gf/wt` owner), fetches the
    child's real gitdir, and lands the advanced content; the child's
    recorded origin keeps naming `producer/child/.gf/git`.

    Defect pinned against: anchoring at `wt2` spelled the nonexistent
    `p/nested/producer/child`; `recorded_url_matches` accepted it
    against the same wrong anchor, `_set_child_origin` repointed the
    child's origin at the phantom, and `git fetch` died rc=2 — leaving
    the bogus origin recorded."""
    up = _upstream(tmp_path)
    producer = _producer(tmp_path, up)
    p = _root(tmp_path, "p")
    gf("-C", str(p), "clone", "../producer/child", "vendor/wr")
    wt2 = p / "nested" / "wt2"
    gf("-C", str(p), "worktree", "add", "nested/wt2")

    wr_child = p / "vendor" / "wr"
    wt_link = wt2 / "vendor" / "wr"
    wt_link_before = os.readlink(wt_link)
    assert wr_child.is_dir() and not wr_child.is_symlink()

    _advance(up, tmp_path, "adv")
    gf("-C", str(producer), "pull")
    r = gf("-C", str(wt2), "pull", "vendor/wr", check=False)
    assert r.returncode == 0, r.stderr
    assert "Pulled wr" in r.stdout

    # the fetch landed the advanced upstream commit in the source child
    # — visible through the worktree's chained link spelling as well
    assert (wr_child / "docs" / "api" / "x.txt").read_text().strip() == \
        "api adv"
    assert (wt_link / "docs" / "api" / "x.txt").read_text().strip() == \
        "api adv"
    # the child's origin still names the producer child's real gitdir —
    # never a wt2-anchored `nested/producer` phantom
    origin = _git(
        "--git-dir", wr_child / ".gf" / "git", "config",
        "remote.origin.url").stdout.strip()
    assert origin == str((producer / "child" / ".gf" / "git").resolve())
    # a whole-repo binding never builds parent `.gf` storage — not at `p`
    # and not at the invoking worktree
    assert not (p / ".gf").exists()
    assert not (wt2 / ".gf").exists()
    # the child stays a real directory; the worktree's link spelling is
    # untouched
    assert wr_child.is_dir() and not wr_child.is_symlink()
    assert os.readlink(wt_link) == wt_link_before


def test_pull_through_nested_worktree_remote_and_direct_controls(
        tmp_path):
    """R8-A control arms — the same pulls from `p` directly still work,
    and a remote `file://` binding pulled through `p/nested/wt2` works.

    Direct pulls anchor the relative spellings at `p` either way; a
    remote url never enters local anchoring at all (`_looks_remote`
    short-circuits the `anchor / url` join), so both directions stay
    green with or without the fix — the pins guard against the anchor
    change regressing previously-working paths."""
    up = _upstream(tmp_path)
    producer = _producer(tmp_path, up)
    p = _root(tmp_path, "p")
    gf("-C", str(p), "clone", "../producer/child/docs/api", "vendor/api")
    gf("-C", str(p), "clone", "../producer/child", "vendor/wr")
    gf("-C", str(p), "clone", f"file://{up}/tools", "vendor/rtools")
    wt2 = p / "nested" / "wt2"
    gf("-C", str(p), "worktree", "add", "nested/wt2")

    api_link = p / "vendor" / "api"
    rtools_link = p / "vendor" / "rtools"
    api_target = os.readlink(api_link)
    rtools_target = os.readlink(rtools_link)

    _advance(up, tmp_path, "adv")
    gf("-C", str(producer), "pull")

    # the remote-url binding pulled through the nested worktree: its
    # store fetch lands the advanced commit and the link is untouched
    r = gf("-C", str(wt2), "pull", "vendor/rtools", check=False)
    assert r.returncode == 0, r.stderr
    assert "Pulled rtools" in r.stdout
    assert (rtools_link / "t.txt").read_text().strip() == "tool adv"
    assert (wt2 / "vendor" / "rtools" / "t.txt").read_text().strip() == \
        "tool adv"
    assert os.readlink(rtools_link) == rtools_target
    assert not (wt2 / ".gf").exists()

    # the same bindings pulled from `p` directly — the relative
    # spellings anchor at `p` either way and serve the same content
    r = gf("-C", str(p), "pull", check=False)
    assert r.returncode == 0, r.stderr
    assert (api_link / "x.txt").read_text().strip() == "api adv"
    assert (p / "vendor" / "wr" / "docs" / "api" / "x.txt"
            ).read_text().strip() == "api adv"
    assert (rtools_link / "t.txt").read_text().strip() == "tool adv"
    assert os.readlink(api_link) == api_target
    assert os.readlink(rtools_link) == rtools_target


# ---------------------------------------------------------------------------
# R9-1 — a foreign repo between child and manifest root cannot claim the
# relative-url anchor
#
# `cmd_pull` anchors a source-relative `url` at the root that OWNS the
# binding's declared path: `manifest.binding_root(child, folder["path"])`
# accepts only the ancestor under which `path` resolves to `child` (and
# which carries a `.git`/`gf.toml` root marker), after `layout.owning_root`
# handles store checkouts, with the invoking root as last fallback. The
# previous anchor, `find_parent_root(child.parent)`, walked `.git`
# ancestors and stopped at ANY of them — so a foreign `git init` at
# `p/nested`, sitting between the child `p/nested/kid` and the
# manifest-owning root `p`, claimed the anchor and `../dep3` re-resolved
# against `p/nested` as `p/dep3`.


def _plain_repo(path: Path, files: dict[str, str]) -> Path:
    """A plain `git init` repo at `path` with `files` committed on master."""
    _git("init", str(path))
    for name, content in files.items():
        (path / name).write_text(content)
    _git("-C", path, "add", "-A")
    _git("-C", path, "commit", "-qm", "init")
    return path


def _foreign_nested_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """`p` holding a foreign repo `p/nested` (plain `git init`, not a gf
    child), the real sibling repo `dep3`, and a same-named DECOY `p/dep3`
    that a `p/nested`-anchored `../dep3` would spell. Returns
    (p, dep3, decoy)."""
    p = _root(tmp_path, "p")
    _git("init", str(p / "nested"))
    dep3 = _plain_repo(tmp_path / "dep3", {"real.txt": "real dep3\n"})
    decoy = _plain_repo(
        p / "dep3", {"real.txt": "DECOY dep3\n", "decoy.txt": "decoy\n"})
    return p, dep3, decoy


def _advance_dep3(dep3: Path) -> None:
    """Commit `real dep3 adv` to dep3 — a pull bound to the real repo
    lands it; one bound to the decoy (or skipped) cannot produce it."""
    (dep3 / "real.txt").write_text("real dep3 adv\n")
    _git("-C", dep3, "add", "real.txt")
    _git("-C", dep3, "commit", "-qm", "adv")


def test_pull_relative_url_skips_foreign_repo_anchor(tmp_path):
    """R9-1 core repro — `p`'s `nested/kid` binds `../dep3`, but a foreign
    `git init p/nested` sits between the child and `p`. Anchoring the url
    at the child's nearest `.git` ancestor spelled `p/nested/../dep3` —
    the DECOY repo `p/dep3` — so the pull fetched foreign history into
    the child and persisted the decoy as its `remote.origin.url`.
    `binding_root` accepts only the ancestor whose declared `path`
    spelling resolves to the child, so the anchor is `p` and the pull
    keeps serving the real `dep3`.

    Pre-fix signature (verified against HEAD's `find_parent_root`
    anchor): rc=0 `Pulled kid` with `From …/p/dep3` in stdout,
    `remote.origin.url` repointed at `p/dep3`, `real.txt` reading
    `DECOY dep3`, and `decoy.txt` materialized in `p/nested/kid`."""
    p, dep3, decoy = _foreign_nested_fixture(tmp_path)
    gf("-C", str(p), "clone", "../dep3", "nested/kid")
    kid = p / "nested" / "kid"
    assert (kid / "real.txt").read_text().strip() == "real dep3"

    _advance_dep3(dep3)
    r = gf("-C", str(p), "pull", check=False)
    assert r.returncode == 0, r.stderr
    assert "Pulled kid" in r.stdout

    # the binding still points at the real dep3 — never the decoy
    origin = _git("--git-dir", kid / ".gf" / "git", "config",
                  "remote.origin.url").stdout.strip()
    assert origin == str(dep3.resolve())
    assert origin != str(decoy.resolve())
    # the recorded resolution names the real dep3 too
    st = tomllib.loads((kid / ".gf" / "state").read_text())
    assert st["url"] == str(dep3.resolve())
    # content came from the real dep3's advance, not foreign history
    assert (kid / "real.txt").read_text().strip() == "real dep3 adv"
    assert not (kid / "decoy.txt").exists()
    # a whole-repo child builds no parent `.gf` storage — not at `p` and
    # not inside the foreign repo either
    assert not (p / ".gf").exists()
    assert not (p / "nested" / ".gf").exists()


def test_pull_relative_url_through_worktree_skips_foreign_repo_anchor(
        tmp_path):
    """R9-1 through a `gf worktree add` link — same fixture, pulled from
    `p/deep/wt2` (a different-depth link; `p/nested/wt2` would sit INSIDE
    the foreign repo). The worktree's `nested/kid` link resolves into the
    source child `p/nested/kid`, so the anchor must still be `p` — the
    root whose declared `path` spells the resolved child — never the
    invoking worktree and never the foreign `p/nested`.

    Pre-fix signature (verified): the same hijack as the direct pull —
    `From …/p/dep3`, origin repointed at the decoy, decoy content
    checked out into the source child."""
    p, dep3, decoy = _foreign_nested_fixture(tmp_path)
    gf("-C", str(p), "clone", "../dep3", "nested/kid")
    gf("-C", str(p), "worktree", "add", "deep/wt2")
    wt2 = p / "deep" / "wt2"
    wt_link = wt2 / "nested" / "kid"
    wt_target = os.readlink(wt_link)

    _advance_dep3(dep3)
    r = gf("-C", str(wt2), "pull", check=False)
    assert r.returncode == 0, r.stderr
    assert "Pulled kid" in r.stdout

    kid = p / "nested" / "kid"
    origin = _git("--git-dir", kid / ".gf" / "git", "config",
                  "remote.origin.url").stdout.strip()
    assert origin == str(dep3.resolve())
    assert origin != str(decoy.resolve())
    # the source child applied the real upstream's advance — visible
    # through the worktree's chained link spelling as well
    assert (kid / "real.txt").read_text().strip() == "real dep3 adv"
    assert (wt_link / "real.txt").read_text().strip() == "real dep3 adv"
    assert not (kid / "decoy.txt").exists()
    # the worktree link still spells the source consumer path, and no
    # `.gf` storage materialized at the invoking worktree or at `p`
    assert os.readlink(wt_link) == wt_target
    assert not (wt2 / ".gf").exists()
    assert not (p / ".gf").exists()
    assert not (p / "nested" / ".gf").exists()


def test_pull_skips_init_placeholder_under_foreign_repo(tmp_path):
    """R9-1 placeholder arm — `gf init nested/child` under the foreign
    `p/nested` records the placeholder url `nested/child`, which resolves
    at `p` onto the child itself (`same_path` skip: nothing to pull yet).
    The `.git`-walk anchor resolved `nested/child` against the foreign
    repo as `p/nested/nested/child`, walked up to the foreign `.git`, and
    bound the path as a SUBFOLDER of `p/nested` itself.

    Pre-fix signature (verified): the pull destroyed the placeholder
    (`strip_placeholder_child` removed `nested/child`), materialized a
    `p/.gf/repos/nested-*` store keyed on the foreign repo, and died
    rc=1 `could not resolve ref 'latest' in …/p/.gf/wt/nested-*/ref=latest`
    — consumer path gone, store leaked at `p`."""
    p = _root(tmp_path, "p")
    _git("init", str(p / "nested"))
    gf("-C", str(p), "init", "nested/child")
    child = p / "nested" / "child"
    assert sorted(x.name for x in child.iterdir()) == [".gf"]
    assert (child / ".gf" / "git" / "HEAD").is_file()

    r = gf("-C", str(p), "pull", check=False)
    assert r.returncode == 0, r.stderr
    # the placeholder is skipped — nothing is pulled, fetched or served
    assert "Pulled" not in r.stdout

    # `nested/child` is intact: still a real dir holding only its `.gf`
    # placeholder gitdir — never stripped, converted or linked away
    assert child.is_dir() and not child.is_symlink()
    assert sorted(x.name for x in child.iterdir()) == [".gf"]
    assert (child / ".gf" / "git" / "HEAD").is_file()
    # no `.gf` store leaked — not at the manifest root and not inside
    # the foreign repo the buggy anchor bound the url to
    assert not (p / ".gf").exists()
    assert not (p / "nested" / ".gf").exists()


# ---------------------------------------------------------------------------
# R10-4 — a pull through a stale `worktree add` manifest must serve the
# OWNING root's manifest, not the invoking worktree's snapshot
#
# `gf worktree add` copies gf.toml/gf.local.toml into the new worktree at
# add time; a binding cloned into the source root afterwards is invisible
# to that copy. `pull_shared_bindings` decided the shared checkout's
# served set — `_vacate_checkout`'s pruning scan and the
# `(moved with …)` sibling listing alike — from the INVOKING root's
# `folders`, so a pull through the stale worktree (a) dropped the new
# binding's `bindings`/`binding_urls` entries from the checkout record
# and (b) could not report it `(moved with …)`; the next cone union then
# dematerialized its subdir and left the source consumer link dangling.
# The vacate scan and sibling listing now read the OWNING root's
# manifest — `_vacate_checkout(old_co, parent_root)` loads it itself —
# once per `(owning_root, store)` group.
#
# Pre-fix signatures (verified against HEAD d0536ef):
#   retarget pull through wt2: master record `bindings` drops "libs/lib"
#       (-> ["tools"]) and `binding_urls` drops "vendor/lib"; the same
#       pull's cone union then shrinks to `tools`, so `p/vendor/lib`'s
#       `l.txt` stops resolving (dangling consumer link).
#   moved-with pull through wt2: no `Pulled lib (moved with api)` line —
#       the stale copy cannot see `lib`.


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
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))

    # the source manifest gains a third binding on the same checkout —
    # after wt2's snapshot was taken
    _push_lib_subdir(up, tmp_path)
    gf("-C", str(parent), "clone", str(up / "libs/lib"), "vendor/lib")

    rk = _store_names(parent)
    assert len(rk) == 1, rk
    st = _checkout_state(parent, rk[0], "master")
    assert set(st["bindings"]) == {"docs/api", "libs/lib", "tools"}
    assert set(st["binding_urls"]) == {
        "vendor/api", "vendor/tools", "vendor/lib"}

    # the setup premise: wt2's copied manifest predates `vendor/lib`,
    # and no lib consumer link was ever placed inside wt2
    copied = tomllib.loads((wt2 / "gf.toml").read_text())
    assert [f["path"] for f in copied["git_folder"]] == [
        "vendor/api", "vendor/tools"]
    assert not os.path.lexists(wt2 / "vendor" / "lib")
    return up, parent, wt2, rk[0]


def test_pull_via_stale_worktree_manifest_preserves_unseen_records(
        tmp_path):
    """R10-4 core — a retarget pull through the stale worktree prunes
    only what the OWNING manifest stopped serving from the checkout.

    wt2's gf.toml predates `vendor/lib`; pulling through wt2 with an
    api→dev override retargets `vendor/api` into a new `dev` checkout and
    vacates `master`. The vacate scan must read `parent`'s manifest —
    reading the stale copy drops `libs/lib`/`vendor/lib` from the record,
    and the same pull's cone union then dematerializes `libs/lib`,
    leaving `parent/vendor/lib` dangling.
    """
    up, parent, wt2, rk = _stale_worktree_setup(tmp_path)
    push_branch(up, "dev", "dev content")
    # the override lives in wt2's own copy: pulling through wt2 (not the
    # source root) is what routes the vacate scan past the stale manifest
    (wt2 / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "dev"\n')

    r = gf("-C", str(wt2), "pull", check=False)
    assert r.returncode == 0, r.stderr
    assert "Pulled api" in r.stdout
    assert "Pulled tools" in r.stdout

    # the vacated `master` record dropped the moved api but keeps the
    # binding only `parent`'s manifest can see
    st = _checkout_state(parent, rk, "master")
    assert set(st["bindings"]) == {"libs/lib", "tools"}
    assert set(st["binding_urls"]) == {"vendor/tools", "vendor/lib"}
    # the unseen sibling still rides the moved shared checkout — reported
    # even though wt2's manifest copy cannot name it
    assert "Pulled lib (moved with tools)" in r.stdout
    dev = _checkout_state(parent, rk, "dev")
    assert set(dev["bindings"]) == {"docs/api"}
    assert set(dev["binding_urls"]) == {"vendor/api"}

    # api's consumer link retargeted into the dev checkout — reachable
    # through wt2's chained link as well
    dev_wt = parent / ".gf" / "wt" / rk / "dev"
    assert (parent / "vendor" / "api").resolve() == (
        dev_wt / "docs" / "api").resolve()
    assert (wt2 / "vendor" / "api" / "x.txt").is_file()
    # the pin: `libs/lib` stayed materialized — the cone union still saw
    # the stale-invisible binding, so `parent/vendor/lib` never dangles
    lib_file = parent / "vendor" / "lib" / "l.txt"
    assert lib_file.is_file()
    assert lib_file.read_text() == "lib payload\n"

    # A follow-up pull from the owning root re-serves every manifest
    # binding: api swings back to `master` (no override at `parent`),
    # which itself vacates the `dev` record — cleaned to empty.
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, r.stderr
    st = _checkout_state(parent, rk, "master")
    assert set(st["bindings"]) == {"docs/api", "libs/lib", "tools"}
    assert set(st["binding_urls"]) == {
        "vendor/api", "vendor/tools", "vendor/lib"}
    dev = _checkout_state(parent, rk, "dev")
    assert dev.get("bindings") == []
    assert not dev.get("binding_urls")
    assert lib_file.read_text() == "lib payload\n"
    assert (wt2 / "vendor" / "api" / "x.txt").is_file()


def test_pull_via_stale_worktree_reports_unseen_moved_siblings(tmp_path):
    """R10-4 reporting arm — `(moved with …)` lists the bindings the
    OWNING manifest serves from the moved checkout, not only those the
    invoking worktree's stale copy names.

    Pulling just `vendor/api` through wt2 moves the shared `master`
    checkout; `parent`'s manifest serves `tools` AND `lib` from it, so
    both are reported. The stale-copy read reports `tools` alone.
    """
    up, parent, wt2, rk = _stale_worktree_setup(tmp_path)
    _advance(up, tmp_path, "adv")

    r = gf("-C", str(wt2), "pull", "vendor/api", check=False)
    assert r.returncode == 0, r.stderr
    assert "Pulled api" in r.stdout
    assert "Pulled tools (moved with api)" in r.stdout
    assert "Pulled lib (moved with api)" in r.stdout

    # the shared checkout's sparse cone covers the sibling only the
    # owning manifest knows — its served file exists inside the checkout
    wt = parent / ".gf" / "wt" / rk / "master"
    assert (wt / "libs" / "lib" / "l.txt").is_file()
    assert (parent / "vendor" / "api" / "x.txt").read_text().strip() == \
        "api adv"
    assert (parent / "vendor" / "tools" / "t.txt").read_text().strip() == \
        "tool adv"
    assert (parent / "vendor" / "lib" / "l.txt").read_text() == \
        "lib payload\n"
    assert (wt2 / "vendor" / "api" / "x.txt").read_text().strip() == \
        "api adv"
    # the pull reports the sibling through the owning root — it does NOT
    # place a consumer link the worktree's manifest never declared
    assert not os.path.lexists(wt2 / "vendor" / "lib")


def test_pull_via_stale_worktree_vacate_still_prunes_dropped_binding(
        tmp_path):
    """R10-4 control — the owning-manifest read is not keep-everything:
    a binding the OWNING root genuinely dropped is still pruned.

    `gf -C parent rm vendor/lib` removes the manifest entry and the
    consumer link after the snapshot; the same wt2 retarget pull then
    recomputes the vacated `master` record against `parent`'s real
    manifest — `lib`'s recorded subdir and url are gone exactly as they
    were before the fix (the fix changes WHICH manifest is read, not
    whether pruning runs)."""
    up, parent, wt2, rk = _stale_worktree_setup(tmp_path)
    gf("-C", str(parent), "rm", "vendor/lib")
    assert not os.path.lexists(parent / "vendor" / "lib")
    push_branch(up, "dev", "dev content")
    (wt2 / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "dev"\n')

    r = gf("-C", str(wt2), "pull", check=False)
    assert r.returncode == 0, r.stderr
    assert "Pulled api" in r.stdout
    assert "Pulled tools" in r.stdout

    # the `master` record is cleaned exactly: the moved api and the
    # owning-root-removed lib are both pruned; tools alone remains
    st = _checkout_state(parent, rk, "master")
    assert st["bindings"] == ["tools"]
    assert set(st["binding_urls"]) == {"vendor/tools"}
    dev = _checkout_state(parent, rk, "dev")
    assert set(dev["bindings"]) == {"docs/api"}
    assert set(dev["binding_urls"]) == {"vendor/api"}
