# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""The `ref=` checkout-key contract for subfolder bindings.

These tests pin the spec-visible checkout-key behavior of subfolder
bindings: the directory names under `<root>/.gf/wt/<repo-key>/`, the
`.<checkout-key>.state` file, the consumer-link targets, and the branch a
shared checkout stays attached to. Expectations derive only from
docs/gf-spec.md and docs/gf-arch.md:

- spec "Reference model" / "Subfolder-binding layout": a floating
  (`branch`/`latest`) checkout keys by `quote(resolved branch,
  safe='')`; a pinned (tag/commit) checkout keys by `'ref=' +
  quote(ref, safe='')`. Checkouts live at `wt/<repo-key>/<checkout-key>/`
  with per-checkout state at `wt/<repo-key>/.<checkout-key>.state`; the
  consumer path is a relative symlink to `<checkout>/<subdir>`.
- spec "Reference model" + arch GF-D7: `=` never appears in a
  percent-encoded branch key, so a `ref=` pinned key cannot collide with
  any branch name — names that collided under the retired `ref-<slug>`
  scheme (branch `ref-v0.1` vs pinned `v0.1`) are distinct checkouts.
- spec "Reference model": bindings sharing a repo URL and resolving to
  the same checkout key share one checkout, sparse cone unioned.
- spec "Reference model": `gf init -b dev` keeps its checkout on `dev`
  after a later `gf clone` (implicit `latest`) on the same repo —
  `latest` is the default when `-b` is omitted.
- spec "gf rm": a subfolder binding's consumer link and manifest entry
  are removed; the checkout — including uncommitted work — the sparse
  cone, and the repo store are not touched.
- spec "gf status" / "gf ls": `branch` is the branch of the shared
  checkout serving the binding, `[]` for a detached HEAD.

Adapted to the landed command path: the in-link `git rev-parse
--abbrev-ref HEAD` check was replaced by reading git's own worktree
record at `<store>/worktrees/<key>/HEAD`, because under GF-D8 plain git
inside a consumer link resolves to the PARENT repository, not the
checkout. Everything else is verbatim: the pins are spec-anchored and
validate the `gf rm`/`status` behavior they assert.
"""

import re
import shutil
from pathlib import Path

from conftest import gf, git


# --- fixtures ----------------------------------------------------------


def _real_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _subfolder_upstream(tmp_path: Path) -> Path:
    """Bare upstream whose trees carry subdirs, floating branches, tags.

    master:     docs/api/api.txt + tools/tool.txt
    dev:        adds docs/api/dev.txt
    ref-v0.1:   adds docs/api/branch-marker.txt  (named like an
                old-scheme pinned key — the collision case)
    feature/x:  adds docs/api/feature-x.txt      (slash branch name)
    master.state: adds docs/api/master-state.txt (a branch whose key is
                spelled exactly like master's pre-migration state file)
    v1.2, v0.1: lightweight tags on master
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)

    work = tmp_path / "_seed_work"
    git("clone", str(upstream), str(work), cwd=tmp_path)
    (work / "docs" / "api").mkdir(parents=True)
    (work / "tools").mkdir()
    (work / "docs" / "api" / "api.txt").write_text("api on master")
    (work / "tools" / "tool.txt").write_text("tool on master")
    git("add", "-A", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("push", "-q", "origin", "master", cwd=work)

    for branch, marker, content in (
        ("dev", "dev.txt", "api on dev"),
        ("ref-v0.1", "branch-marker.txt", "api on ref-v0.1 branch"),
        ("feature/x", "feature-x.txt", "api on feature/x branch"),
        ("master.state", "master-state.txt", "api on master.state branch"),
    ):
        git("checkout", "-q", "-b", branch, "master", cwd=work)
        (work / "docs" / "api" / marker).write_text(content)
        git("add", "-A", cwd=work)
        git("commit", "-m", branch, cwd=work)
        git("push", "-q", "origin", branch, cwd=work)
        # checkout back to master; the marker leaves the worktree with it
        git("checkout", "-q", "master", cwd=work)

    git("tag", "v1.2", cwd=work)
    git("tag", "v0.1", cwd=work)
    git("push", "-q", "origin", "v1.2", "v0.1", cwd=work)
    shutil.rmtree(work)
    return upstream


def _repo_key_dir(parent: Path) -> Path:
    """The single repo-store checkout dir under `<root>/.gf/wt/`.

    `<repo-key>` is `<basename>-<8 hex>` (spec '.gf directory'); the hash
    input is pinned by test_layout's repo_key tests, so here we pin the
    store shape and take the key by format.
    """
    wt = parent / ".gf" / "wt"
    entries = [p for p in wt.iterdir() if p.is_dir()]
    assert len(entries) == 1, f"expected one repo store, found {entries}"
    assert re.fullmatch(r"upstream-[0-9a-f]{8}", entries[0].name)
    return entries[0]


def _checkout_dirs(repo_key_dir: Path) -> list[str]:
    return sorted(p.name for p in repo_key_dir.iterdir() if p.is_dir())


def _status_row(stdout: str, name: str, url: Path) -> str | None:
    for line in stdout.splitlines():
        if re.match(rf"{re.escape(name)}\s+{re.escape(str(url))}\s", line):
            return line
    return None


def _assert_master_and_dotted_state_coexist(parent: Path) -> None:
    """The `master`/`master.state` coexistence claim, both orders.

    `master.state` is a checkout DIR (the dotted branch's key); the state
    records are the dotfiles `.<key>.state` beside the checkout keys.
    """
    rk = _repo_key_dir(parent)
    assert _checkout_dirs(rk) == ["master", "master.state"]
    # wt/<rk>/.<key>.state — state lives outside the checkout-key
    # namespace, so `master.state` stays a usable checkout dir.
    assert (rk / ".master.state").is_file()
    assert (rk / ".master.state.state").is_file()

    api = parent / "vendor" / "api"
    api_state = parent / "vendor" / "api-state"
    assert api.resolve() == (rk / "master" / "docs" / "api").resolve()
    assert api_state.resolve() == (
        rk / "master.state" / "docs" / "api"
    ).resolve()

    # Each consumer link serves its own branch: the `master.state`
    # marker exists only through the dotted branch's checkout.
    assert (api / "api.txt").read_text() == "api on master"
    assert not (api / "master-state.txt").exists()
    assert (api_state / "api.txt").read_text() == "api on master"
    assert (api_state / "master-state.txt").read_text() == (
        "api on master.state branch"
    )


# --- pinned-ref checkouts -----------------------------------------------


def test_clone_tag_ref_creates_ref_equals_checkout(tmp_path):
    """spec 'Reference model' + 'Subfolder-binding layout': a pinned tag
    creates its checkout under `ref=<quoted ref>`; `gf rm` removes the
    consumer link while the checkout stays (spec 'gf rm')."""
    upstream = _subfolder_upstream(tmp_path)
    parent = _real_parent(tmp_path)

    gf(
        "-C", str(parent), "clone",
        str(upstream / "docs" / "api"), "vendor/api",
        "-b", "v1.2",
    )

    rk = _repo_key_dir(parent)
    checkout = rk / "ref=v1.2"
    assert checkout.is_dir()
    # Per-checkout state file is named .<checkout-key>.state.
    assert (rk / ".ref=v1.2.state").is_file()

    # The consumer path is a symlink into the checkout's mapped subdir.
    link = parent / "vendor" / "api"
    assert link.is_symlink()
    assert link.resolve() == (checkout / "docs" / "api").resolve()
    assert (link / "api.txt").read_text() == "api on master"

    # A tag checkout is detached: status shows empty brackets.
    row = _status_row(
        gf("-C", str(parent), "status").stdout, "api", upstream / "docs" / "api"
    )
    assert row is not None and re.search(r"\[\]", row)

    # `gf rm` removes the consumer link and manifest entry; the checkout
    # (and its state) is retained.
    gf("-C", str(parent), "rm", "vendor/api")
    assert not link.exists() and not link.is_symlink()
    manifest = (parent / "gf.toml").read_text()
    assert 'name = "api"' not in manifest
    assert 'path = "vendor/api"' not in manifest
    assert checkout.is_dir()
    assert (rk / ".ref=v1.2.state").is_file()


def test_ref_equals_separates_old_scheme_collision_pair(tmp_path):
    """spec 'Reference model': pinned names that collided under
    `ref-<slug>` are distinguished — branch `ref-v0.1` and tag `v0.1` get
    two checkouts (`ref-v0.1`, `ref=v0.1`), not one."""
    upstream = _subfolder_upstream(tmp_path)
    parent = _real_parent(tmp_path)

    gf(
        "-C", str(parent), "clone",
        str(upstream / "docs" / "api"), "vendor/api-branch",
        "-b", "ref-v0.1",
    )
    gf(
        "-C", str(parent), "clone",
        str(upstream / "tools"), "vendor/api-tag",
        "-b", "v0.1",
    )

    rk = _repo_key_dir(parent)
    assert _checkout_dirs(rk) == ["ref-v0.1", "ref=v0.1"]

    # Each consumer link lands inside its own checkout's mapped subdir.
    api_branch = parent / "vendor" / "api-branch"
    api_tag = parent / "vendor" / "api-tag"
    assert api_branch.resolve() == (rk / "ref-v0.1" / "docs" / "api").resolve()
    assert api_tag.resolve() == (rk / "ref=v0.1" / "tools").resolve()

    # Content proves the branch checkout really tracks branch `ref-v0.1`,
    # not the tag's commit the old colliding key would have shared.
    assert (api_branch / "branch-marker.txt").read_text() == (
        "api on ref-v0.1 branch"
    )
    assert (api_tag / "tool.txt").read_text() == "tool on master"


def test_dotted_branch_key_coexists_with_state_records(tmp_path):
    """spec 'Subfolder-binding layout': per-checkout state lives at
    `wt/<repo-key>/.<checkout-key>.state` — outside the checkout-key
    namespace — so a branch literally named `master.state` gets checkout
    dir `master.state/` next to `master/` while each checkout's record
    is a separate dotfile. Here `master` is cloned first."""
    upstream = _subfolder_upstream(tmp_path)
    parent = _real_parent(tmp_path)

    gf(
        "-C", str(parent), "clone",
        str(upstream / "docs" / "api"), "vendor/api",
        "-b", "master",
    )
    gf(
        "-C", str(parent), "clone",
        str(upstream / "docs" / "api"), "vendor/api-state",
        "-b", "master.state",
    )

    _assert_master_and_dotted_state_coexist(parent)


def test_dotted_branch_key_coexists_with_state_records_reversed(tmp_path):
    """Same coexistence in the other creation order: `master.state`
    first, then `master` — the dotted checkout (and its
    `.master.state.state` record) precede the `master` checkout and its
    `.master.state` record."""
    upstream = _subfolder_upstream(tmp_path)
    parent = _real_parent(tmp_path)

    gf(
        "-C", str(parent), "clone",
        str(upstream / "docs" / "api"), "vendor/api-state",
        "-b", "master.state",
    )
    gf(
        "-C", str(parent), "clone",
        str(upstream / "docs" / "api"), "vendor/api",
        "-b", "master",
    )

    _assert_master_and_dotted_state_coexist(parent)


def test_clone_branch_ref_creates_quoted_branch_checkout(tmp_path):
    """A `branch` ref keys the checkout by `quote(branch, safe='')` — a
    slash encodes into one path component, never a nested directory."""
    upstream = _subfolder_upstream(tmp_path)
    parent = _real_parent(tmp_path)

    gf(
        "-C", str(parent), "clone",
        str(upstream / "docs" / "api"), "vendor/api",
        "-b", "feature/x",
    )

    rk = _repo_key_dir(parent)
    checkout = rk / "feature%2Fx"
    assert checkout.is_dir()
    assert (rk / ".feature%2Fx.state").is_file()
    assert not (rk / "feature").exists()  # no extra path level
    assert (parent / "vendor" / "api" / "feature-x.txt").read_text() == (
        "api on feature/x branch"
    )


def test_latest_and_named_branch_share_one_checkout(tmp_path):
    """spec 'Reference model': bindings resolving to the same checkout key
    share one checkout — `latest` (master) and branch `master` are one
    key, one worktree, two consumer links."""
    upstream = _subfolder_upstream(tmp_path)
    parent = _real_parent(tmp_path)

    gf(
        "-C", str(parent), "clone",
        str(upstream / "docs" / "api"), "vendor/api",
    )  # latest -> master
    gf(
        "-C", str(parent), "clone",
        str(upstream / "tools"), "vendor/tools",
        "-b", "master",
    )

    rk = _repo_key_dir(parent)
    assert _checkout_dirs(rk) == ["master"]
    assert (parent / "vendor" / "api").resolve() == (
        rk / "master" / "docs" / "api"
    ).resolve()
    assert (parent / "vendor" / "tools").resolve() == (
        rk / "master" / "tools"
    ).resolve()


def test_init_ref_keeps_checkout_on_dev_after_clone_latest(tmp_path):
    """spec 'Reference model': `gf init -b dev` keeps its checkout on
    `dev` after a later `gf clone` (implicit `latest`) of the same
    repository — `latest` is the default when `-b` is omitted."""
    upstream = _subfolder_upstream(tmp_path)
    parent = _real_parent(tmp_path)

    # Binding created by `gf init` with ref=dev and a subfolder URL is
    # converted to a consumer link on first pull.
    gf(
        "-C", str(parent), "init", "vendor/api",
        "-b", "dev", "--url", str(upstream / "docs" / "api"),
    )
    gf("-C", str(parent), "pull")

    rk = _repo_key_dir(parent)
    assert (rk / "dev").is_dir()
    assert (parent / "vendor" / "api" / "dev.txt").read_text() == "api on dev"

    # A later same-repo clone at `latest` creates its own `master`
    # checkout; the init'd binding's checkout stays on `dev`.
    gf(
        "-C", str(parent), "clone",
        str(upstream / "tools"), "vendor/tools",
    )
    assert _checkout_dirs(rk) == ["dev", "master"]

    api = parent / "vendor" / "api"
    assert api.resolve() == (rk / "dev" / "docs" / "api").resolve()
    # The checkout stays attached to `dev`: read git's own worktree record
    # (plain git inside the consumer link resolves to the parent — GF-D8).
    admin_head = (
        parent / ".gf" / "repos" / rk.name / "git" / "worktrees" / "dev"
        / "HEAD"
    ).read_text().strip()
    assert admin_head == "ref: refs/heads/dev"

    row = _status_row(
        gf("-C", str(parent), "status").stdout, "api", upstream / "docs" / "api"
    )
    assert row is not None and "[dev]" in row

    # `gf rm` unlinks the consumer path and unregisters the binding; the
    # `dev` checkout it was served by is untouched.
    gf("-C", str(parent), "rm", "vendor/api")
    assert not api.exists() and not api.is_symlink()
    manifest = (parent / "gf.toml").read_text()
    assert 'name = "api"' not in manifest
    assert 'path = "vendor/api"' not in manifest
    assert (rk / "dev").is_dir()
