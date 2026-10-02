# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""The checkout resolver in `src/gf/layout.py`.

Written from the governing documents only: docs/gf-spec.md ("Physical layout",
"Subfolder-binding layout", "Reference model", "`.gf` directory", "Target
selection rules", "gf sh"), docs/gf-arch.md (`src/gf/layout.py` module entry,
Layout contract, Store and checkout contract, GF-D5/D7/D8/D13), and
docs/gf-constraints.md ("Do not construct `.gf` layout paths outside the
checkout resolver"). No expectation below is derived from implementation
output.

The public surface these tests pin (each name cited to its source):

  Checkout                    — record returned by the resolver; fields
                                gitdir, work_tree, common_dir, subdir, state
                                (arch `src/gf/layout.py`; spec Terminology
                                "Checkout resolver").
  repo_key(repo_url) -> str   — `<basename>-<first 8 hex of sha1(normalized
                                repo URL)>` (spec "`.gf` directory").
                                "normalized" here means: the URL as spelled
                                after `cli._normalize_url` shorthand expansion
                                (spec "gf clone"), any trailing `/` removed,
                                and — for remote spellings only — one
                                trailing `.git` on the last path segment
                                removed: remote `repo` and `repo.git` are
                                one repository and share one store (GF-D5),
                                while local `…/repo` and `…/repo.git` are
                                distinct real directories and never alias
                                (R14-F5).
  checkout_key_for_branch(b)  — floating branch/`latest` checkout key by
                                resolved branch name:
                                `urllib.parse.quote(resolved branch,
                                safe='')` (spec "Reference model",
                                arch GF-D7).
  checkout_key_for_ref(ref)   — pinned tag/commit checkout key by ref
                                string: `'ref=' + quote(ref, safe='')`
                                (same sources). `=` never occurs in a
                                percent-encoding, so the `ref=` prefix
                                cannot collide with any branch name —
                                the separation the retired `ref-<slug>`
                                scheme failed to provide.
  whole_repo_checkout(child)  — `child/.gf/git`, `child`, same gitdir as
                                common_dir, no subdir, `child/.gf/state`
                                (spec "Physical layout" / "`.gf` directory").
  subfolder_checkout(root, repo_url, key, subdir) — `<root>/.gf` layout for a
                                subfolder binding (spec "Subfolder-binding
                                layout"); `key` is a checkout key from the two
                                derivations above.
  resolve_checkout(path)      — the single resolver: an existing subfolder
                                binding resolves from its consumer link's
                                realpath (no network, no state file); any
                                other path is the whole-repo mapping (spec
                                "`.gf` directory", "Target selection rules";
                                arch Layout contract).
"""

import hashlib
import os
import re
import subprocess
import urllib.parse
from pathlib import Path

from gf.layout import (
    Checkout,
    checkout_key_for_branch,
    checkout_key_for_ref,
    repo_key,
    resolve_checkout,
    subfolder_checkout,
    whole_repo_checkout,
)


REPO_URL = "https://github.com/foo/libfoo"


def _sha8(text: str) -> str:
    """First 8 hex of sha1(text) — the spec's repo-key suffix formula."""
    return hashlib.sha1(text.encode()).hexdigest()[:8]


# The repo key of REPO_URL (aka REPO_URL + ".git") under the normalization
# rules above — computed here so resolver tests stay independent of repo_key.
K = "libfoo-" + _sha8(REPO_URL)


def _realpath(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _seed_subfolder_layout(root: Path, key: str, subdir: str) -> tuple[Path, Path]:
    """Create a subfolder checkout's on-disk shape and its consumer link.

    Only the checkout's mapped subdir and the relative consumer link are
    created — no repo store, no worktree admin dir, no `.state` file — because
    resolution must work from the link's realpath alone.
    """
    checkout = root / ".gf" / "wt" / K / key
    mapped = checkout / subdir
    mapped.mkdir(parents=True)
    link = root / "vendor" / "api"
    link.parent.mkdir(parents=True)
    link.symlink_to(os.path.relpath(mapped, link.parent))
    return checkout, link


def _assert_subfolder_checkout(c: Checkout, root: Path, key: str, subdir: str) -> None:
    """The five-field mapping for a subfolder binding (spec 'Subfolder-binding
    layout', spec 'gf sh' GIT_DIR rule, arch GF-D13)."""
    store = _realpath(root) / ".gf" / "repos" / K / "git"
    assert Path(c.common_dir) == store
    # The gitdir is the linked-worktree admin dir inside the store: the
    # checkout's own `.git` gitfile is removed (GF-D8), and creation verifies
    # the record at <store>/worktrees/<checkout-key> (GF-D13).
    assert Path(c.gitdir) == store / "worktrees" / key
    assert Path(c.work_tree) == _realpath(root) / ".gf" / "wt" / K / key
    assert Path(c.state) == _realpath(root) / ".gf" / "wt" / K / ("." + key + ".state")
    assert str(c.subdir) == subdir


def _assert_whole_repo_checkout(c: Checkout, child: Path) -> None:
    """The five-field mapping for a whole-repo binding (spec 'Physical
    layout', '`.gf` directory')."""
    assert Path(c.gitdir).resolve() == (child / ".gf" / "git").resolve()
    assert Path(c.work_tree).resolve() == child.resolve()
    assert Path(c.common_dir).resolve() == (child / ".gf" / "git").resolve()
    assert not c.subdir
    assert Path(c.state).resolve() == (child / ".gf" / "state").resolve()


# --- repo_key -----------------------------------------------------------


def test_repo_key_format_is_basename_dash_8_lowercase_hex():
    assert re.fullmatch(r"libfoo-[0-9a-f]{8}", repo_key(REPO_URL))


def test_repo_key_ignores_trailing_dot_git():
    """Remote `repo` and `repo.git` spell one repository: the `.git`
    suffix is part of neither the basename (spec 'gf clone' name
    derivation strips it) nor the normalized URL."""
    expected = "libfoo-" + _sha8(REPO_URL)
    assert repo_key(REPO_URL + ".git") == expected
    assert repo_key(REPO_URL) == expected
    # `file://` is a remote spelling: the `.git` alias holds there too.
    assert repo_key("file:///abs/path/repo.git") == repo_key(
        "file:///abs/path/repo")


def test_repo_key_normalizes_bare_host_path():
    """`github.com/org/repo` is shorthand for `https://github.com/org/repo`
    (spec 'gf clone' URL expansion), so it hashes to the same key."""
    assert repo_key("github.com/foo/libfoo") == repo_key(REPO_URL)


def test_repo_key_ignores_trailing_slash():
    assert repo_key(REPO_URL + "/") == repo_key(REPO_URL)
    assert repo_key(REPO_URL + ".git/") == repo_key(REPO_URL)


def test_repo_key_for_ssh_url_keeps_transport_in_hash():
    """ssh and https URLs are different remotes and get different stores."""
    assert repo_key("git@github.com:foo/libfoo.git") == "libfoo-" + _sha8(
        "git@github.com:foo/libfoo"
    )
    assert repo_key("git@github.com:foo/libfoo.git") != repo_key(REPO_URL)


def test_repo_key_for_local_path():
    """Local paths never alias (R14-F5): `…/repo` and `…/repo.git` are
    distinct real directories, each keyed by its own spelling — sharing
    one store would silently serve the first repo's content for both."""
    assert repo_key("/abs/path/repo") == "repo-" + _sha8("/abs/path/repo")
    assert repo_key("/abs/path/repo.git") == "repo.git-" + _sha8(
        "/abs/path/repo.git")
    assert repo_key("/abs/path/repo") != repo_key("/abs/path/repo.git")


def test_repo_key_differs_for_different_repos():
    assert repo_key(REPO_URL) != repo_key("https://github.com/foo/other")
    assert repo_key(REPO_URL) != repo_key("https://github.com/bar/libfoo")


def test_repo_key_differs_for_different_local_repos():
    """Control: two DIFFERENT non-`.git` local paths stay distinct too —
    the R14-F5 contract widens local keying, it does not collapse it."""
    assert repo_key("/abs/path/repo") != repo_key("/abs/path/other")
    assert repo_key("/abs/path/repo") != repo_key("/abs/other/repo")


# --- checkout_key -------------------------------------------------------
#
# The checkout-key contract (spec "Reference model", arch GF-D7):
#   floating checkout (branch/`latest`) -> checkout_key_for_branch:
#                                          quote(resolved_branch, safe='')
#   pinned checkout (tag/commit)        -> checkout_key_for_ref:
#                                          'ref=' + quote(ref, safe='')
# `=` can never occur in a percent-encoded branch key, so the `ref=`
# prefix cannot collide with any branch name — closing the `ref-<slug>`
# collisions (e.g. branch `ref-v0.1` vs pinned `v0.1`).


def _checkout_key(ref: str, *, resolved_branch: str | None = None) -> str:
    """Evaluate the checkout-key contract for one binding ref.

    `resolved_branch` is the branch a floating `branch`/`latest` checkout
    resolved to; pass None for a pinned tag/commit ref. The contract's
    surface is the existing pair — `checkout_key_for_branch` keys a
    floating checkout by its resolved branch, `checkout_key_for_ref`
    keys a pinned checkout by its ref string.
    """
    if resolved_branch is not None:
        return checkout_key_for_branch(resolved_branch)
    return checkout_key_for_ref(ref)


def test_checkout_key_branch_is_verbatim():
    """spec 'Reference model': a floating branch checkout keys by the
    resolved branch name, percent-encoded."""
    assert _checkout_key("main", resolved_branch="main") == "main"
    assert (
        _checkout_key("release-1.2_fix", resolved_branch="release-1.2_fix")
        == "release-1.2_fix"
    )


def test_checkout_key_latest_uses_resolved_default_branch():
    """`latest` checkouts key by the resolved (remote default) branch —
    the same key a `branch` ref of that name gets, so they share one
    checkout (spec 'Reference model', arch GF-D7)."""
    assert _checkout_key("latest", resolved_branch="master") == "master"
    assert _checkout_key("latest", resolved_branch="dev") == _checkout_key(
        "dev", resolved_branch="dev"
    )


def test_checkout_key_encodes_slash_into_one_path_component():
    """spec 'Reference model': each `/` in the resolved branch name is
    percent-encoded so the key is one path component."""
    key = _checkout_key("feature/x", resolved_branch="feature/x")
    assert key == "feature%2Fx"
    assert "/" not in key
    assert Path(key).parent == Path(".")
    assert Path(key).name == key


def test_checkout_key_encodes_every_slash():
    assert (
        _checkout_key("a/b/c", resolved_branch="a/b/c") == "a%2Fb%2Fc"
    )


def test_checkout_key_non_ascii_branch_encodes_utf8_percent():
    """spec 'Reference model': `quote` encodes non-ASCII branch names as
    %HH UTF-8 bytes — a PR-head/Unicode branch still keys to one path
    component."""
    key = _checkout_key("büro/main", resolved_branch="büro/main")
    assert key == "b%C3%BCro%2Fmain"
    assert urllib.parse.quote("büro/main", safe="") == key
    assert Path(key).name == key


def test_checkout_key_pinned_tag_uses_ref_equals():
    """spec 'Reference model': pinned checkouts key as `ref=` + quoted
    ref."""
    assert _checkout_key("v1.2.0") == "ref=v1.2.0"


def test_checkout_key_pinned_commit_uses_ref_equals():
    sha = "a1b2c3d4" * 5
    assert _checkout_key(sha) == "ref=" + sha


def test_checkout_key_pinned_ref_encodes_slashes():
    """A tag may contain `/`; the quoted ref stays one path component
    under the `ref=` prefix."""
    key = _checkout_key("releases/v1")
    assert key == "ref=releases%2Fv1"
    assert "/" not in key
    assert Path(key).name == key


def test_checkout_key_ref_equals_never_collides_with_branch_names():
    """arch GF-D7: `=` cannot occur in a percent-encoded branch key, so a
    `ref=` pinned key cannot alias a branch checkout.

    Each pair below is a discriminating case: under the retired
    `ref-<slug>` scheme the first three spellings either collided or sat
    on the same namespace boundary; the contract separates them.
    """
    pairs = [
        # collided under ref-<slug>: both keyed `ref-v0.1`
        ("ref-v0.1", "v0.1"),
        # collided under ref-<slug>: both keyed `ref-foo`
        ("ref-foo", "foo"),
        # a literal `=` branch name encodes to %3D — never the `ref=`
        # prefix a pinned `foo` gets
        ("ref=foo", "foo"),
        # `%` in a branch name encodes to %25; pinned `=x` keys `ref=%3Dx`
        ("ref%3Dx", "=x"),
        # a branch and a tag spelled the same are different checkouts
        ("main", "main"),
    ]
    for branch, pinned_ref in pairs:
        branch_key = _checkout_key(branch, resolved_branch=branch)
        pinned_key = _checkout_key(pinned_ref)
        assert branch_key != pinned_key, (branch, pinned_ref)
        assert pinned_key.startswith("ref=")
    # The exact spellings are the contract, not just the distinctness.
    assert _checkout_key("ref-v0.1", resolved_branch="ref-v0.1") == "ref-v0.1"
    assert _checkout_key("v0.1") == "ref=v0.1"
    assert _checkout_key("ref=foo", resolved_branch="ref=foo") == "ref%3Dfoo"
    assert _checkout_key("ref%3Dx", resolved_branch="ref%3Dx") == "ref%253Dx"
    assert _checkout_key("=x") == "ref=%3Dx"
    assert _checkout_key("main", resolved_branch="main") == "main"
    assert _checkout_key("main") == "ref=main"


# --- mapping constructors ------------------------------------------------


def test_whole_repo_checkout_mapping(tmp_path):
    child = tmp_path / "vendor" / "lib"  # need not exist: pure path mapping
    c = whole_repo_checkout(child)
    assert isinstance(c, Checkout)
    _assert_whole_repo_checkout(c, child)


def test_subfolder_checkout_mapping(tmp_path):
    root = tmp_path / "parent"
    key = "feature%2Fx"  # a checkout key from checkout_key (Reference model)
    c = subfolder_checkout(root, REPO_URL, key, "docs/api")
    assert isinstance(c, Checkout)
    _assert_subfolder_checkout(c, root, key, "docs/api")


def test_subfolder_checkout_derives_repo_key_from_url(tmp_path):
    """repo-key derivation lives in the resolver module, not the caller."""
    root = tmp_path / "parent"
    c = subfolder_checkout(root, REPO_URL + ".git", "main", "docs")
    assert Path(c.work_tree).parent == _realpath(root) / ".gf" / "wt" / K


# --- resolve_checkout ----------------------------------------------------


def test_resolve_checkout_whole_repo_child(tmp_path):
    child = tmp_path / "vendor" / "lib"
    (child / ".gf" / "git").mkdir(parents=True)
    _assert_whole_repo_checkout(resolve_checkout(child), child)


def test_resolve_checkout_whole_repo_child_through_symlink(tmp_path):
    """A linked whole-repo child resolves to the real child location (spec
    'Path identity'); the returned paths may keep either spelling."""
    real = tmp_path / "parent1" / "vendor" / "lib"
    (real / ".gf" / "git").mkdir(parents=True)
    link = tmp_path / "parent2" / "vendor" / "lib"
    link.parent.mkdir(parents=True)
    link.symlink_to(os.path.relpath(real, link.parent))
    c = resolve_checkout(link)
    assert Path(c.gitdir).resolve() == (real / ".gf" / "git").resolve()
    assert Path(c.work_tree).resolve() == real.resolve()
    assert Path(c.state).resolve() == (real / ".gf" / "state").resolve()


def test_resolve_checkout_from_consumer_link_realpath(tmp_path):
    """An existing subfolder binding resolves purely from the consumer link's
    realpath: no repo store, no state file, no network (spec '`.gf`
    directory')."""
    root = tmp_path / "parent"
    checkout, link = _seed_subfolder_layout(root, "master", "docs/api")

    c = resolve_checkout(link)
    assert isinstance(c, Checkout)
    _assert_subfolder_checkout(c, root, "master", "docs/api")
    # No state file exists, yet resolution succeeded.
    assert not (_realpath(root) / ".gf" / "wt" / K / ".master.state").exists()
    assert Path(c.work_tree) == _realpath(checkout)


def test_resolve_checkout_through_worktree_add_link(tmp_path):
    """`gf worktree add` links to the source worktree's consumer link, not the
    checkout; the realpath still lands in the source parent's store and
    resolves to the same checkout (spec '`.gf` directory', arch GF-D14)."""
    root1 = tmp_path / "parent1"
    checkout, link1 = _seed_subfolder_layout(root1, "master", "docs/api")

    root2 = tmp_path / "parent2"
    link2 = root2 / "vendor" / "api"
    link2.parent.mkdir(parents=True)
    link2.symlink_to(os.path.relpath(link1, link2.parent))

    c = resolve_checkout(link2)
    _assert_subfolder_checkout(c, root1, "master", "docs/api")
    assert Path(c.work_tree) == _realpath(checkout)


def test_resolve_checkout_from_physical_path_under_gf_wt(tmp_path):
    """A `cd -P` physical path inside the checkout resolves the same checkout
    (spec 'Target selection rules')."""
    root = tmp_path / "parent"
    checkout, _ = _seed_subfolder_layout(root, "master", "docs/api")

    c = resolve_checkout(checkout / "docs" / "api")
    _assert_subfolder_checkout(c, root, "master", "docs/api")


def test_resolve_checkout_below_link_reports_position(tmp_path):
    """For a path deeper than the consumer link, `subdir` is that path's
    position relative to the checkout. The resolver is forbidden a state
    lookup, so it cannot recover the binding's recorded subdir any other way
    than from the path it was given."""
    root = tmp_path / "parent"
    checkout, link = _seed_subfolder_layout(root, "master", "docs/api")
    deeper = link / "sub" / "pkg"
    deeper.mkdir(parents=True)

    c = resolve_checkout(deeper)
    assert Path(c.work_tree) == _realpath(checkout)
    assert str(c.subdir) == "docs/api/sub/pkg"


def test_resolve_checkout_at_checkout_root(tmp_path):
    root = tmp_path / "parent"
    checkout, _ = _seed_subfolder_layout(root, "master", "docs/api")

    c = resolve_checkout(checkout)
    assert Path(c.work_tree) == _realpath(checkout)
    assert not c.subdir


def test_resolve_checkout_dangling_consumer_link(tmp_path):
    """realpath resolves a dangling link lexically, so a binding whose
    checkout is absent still maps to its checkout record (the `missing`
    drift decision belongs to the caller, not the resolver)."""
    root = tmp_path / "parent"
    link = root / "vendor" / "api"
    link.parent.mkdir(parents=True)
    link.symlink_to(os.path.relpath(root / ".gf" / "wt" / K / "master" / "docs" / "api", link.parent))

    c = resolve_checkout(link)
    _assert_subfolder_checkout(c, root, "master", "docs/api")


# --- resolver is filesystem-only ----------------------------------------


def test_resolver_makes_no_subprocess_calls(tmp_path, monkeypatch):
    """arch `src/gf/layout.py`: the resolver 'is filesystem-only: it performs
    no git calls'. Spawning anything during resolution is a violation."""

    def boom(*args, **kwargs):
        raise AssertionError("checkout resolver spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "system", boom)

    root = tmp_path / "parent"
    _, link = _seed_subfolder_layout(root, "master", "docs/api")

    resolve_checkout(link)
    resolve_checkout(tmp_path)
    whole_repo_checkout(tmp_path / "child")
    subfolder_checkout(root, REPO_URL, "master", "docs/api")
    repo_key(REPO_URL)
    _checkout_key("feature/x", resolved_branch="feature/x")
    _checkout_key("v1.2.0")


# --- receiving check from the plan row ----------------------------------


def test_gf_layout_literals_live_only_in_layout_module():
    """Slice receiving check: `grep -n '"\\.gf"' src/gf/` must match only
    layout.py (constraint 'Do not construct `.gf` layout paths outside the
    checkout resolver')."""
    src = Path(__file__).resolve().parent.parent / "src" / "gf"
    offenders = sorted(
        p.name
        for p in src.glob("*.py")
        if p.name != "layout.py" and '".gf"' in p.read_text()
    )
    assert offenders == []
