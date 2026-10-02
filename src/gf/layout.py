# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""The `.gf` physical layout: the single owner of gitdir, worktree, and state paths.

Filesystem-only: this module never calls git. Both binding forms resolve
through it — a whole-repo child maps to `child/.gf/git`; a subfolder binding
maps to a bare repo store under `<root>/.gf/repos/<repo-key>/git` plus a
sparse linked worktree at `<root>/.gf/wt/<repo-key>/<checkout-key>`.
"""

import hashlib
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

GF_DIR = ".gf"


@dataclass
class Checkout:
    """The git environment and on-disk locations of one binding or checkout.

    `gitdir` is the gitdir gf passes as GIT_DIR, `common_dir` the shared
    object/ref store (equal to `gitdir` for a whole-repo child), `work_tree`
    the working root, `subdir` the repo-relative mapped folder ("" for a
    whole-repo binding), and `state` the per-child or per-checkout state file.
    """

    gitdir: Path
    work_tree: Path
    common_dir: Path
    subdir: str
    state: Path


def _normalize_url(url: str) -> str:
    """Expand a bare host/path URL; mirrors `cli._normalize_url`."""
    if "://" in url or url.startswith("git@"):
        return url
    if "/" in url:
        head, _, _ = url.partition("/")
        if "." in head and " " not in head and not head.startswith("."):
            return f"https://{url}"
    return url


def repo_key(repo_url: str) -> str:
    """Derive `<basename>-<first 8 hex of sha1(normalized repo URL)>`."""
    url = _normalize_url(repo_url).rstrip("/").removesuffix(".git")
    base = url.replace(":", "/").rsplit("/", 1)[-1] or "repo"
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"{base}-{digest}"


def checkout_key_for_branch(branch: str) -> str:
    """Key a branch/`latest` checkout by branch name, `/` percent-encoded."""
    return urllib.parse.quote(branch, safe="")


def checkout_key_for_ref(ref: str) -> str:
    """Key a tag/commit checkout by ref string as `ref=<slug>` (detached)."""
    return f"ref={urllib.parse.quote(ref, safe='')}"


def whole_repo_checkout(child: Path) -> Checkout:
    """Map a whole-repo child to its `child/.gf` gitdir and state file."""
    gitdir = child / GF_DIR / "git"
    return Checkout(
        gitdir=gitdir,
        work_tree=child,
        common_dir=gitdir,
        subdir="",
        state=child / GF_DIR / "state",
    )


def subfolder_checkout(root: Path, repo_url: str, key: str, subdir: str) -> Checkout:
    """Map a subfolder binding to its repo store and linked-worktree checkout."""
    root = Path(os.path.realpath(root))
    k = repo_key(repo_url)
    store = root / GF_DIR / "repos" / k / "git"
    wt = root / GF_DIR / "wt" / k / key
    return Checkout(
        gitdir=store / "worktrees" / key,
        work_tree=wt,
        common_dir=store,
        subdir=subdir,
        state=wt.parent / f"{key}.state",
    )


def repo_store(root: Path, repo_url: str) -> Path:
    """Map a normalized repo URL to its shared store gitdir.

    Returns `<root>/.gf/repos/<repo-key>/git` — the bare object/ref store
    shared by every checkout of that repository under `root`.
    """
    root = Path(os.path.realpath(root))
    return root / GF_DIR / "repos" / repo_key(repo_url) / "git"


def _gf_wt_root(rp: Path) -> Path | None:
    """The `<root>/.gf/wt/<repo-key>/<checkout-key>` ancestor of realpath `rp`."""
    for anc in reversed([rp, *rp.parents]):
        parts = anc.parts
        if len(parts) >= 4 and parts[-4] == GF_DIR and parts[-3] == "wt":
            return anc
    return None


def in_gf_wt(path: Path) -> bool:
    """True when `path`'s realpath lies inside a `<root>/.gf/wt` tree.

    Everything under `.gf/wt` is gf-managed checkout storage: a `.git`
    entry there is a checkout's removed-gitfile position or upstream repo
    content, never a parent repo marker (plan D4 / arch GF-D8).
    """
    rp = Path(os.path.realpath(path))
    return any(a.name == "wt" and a.parent.name == GF_DIR for a in rp.parents)


def owns_consumer_link(root: Path, link: Path) -> bool:
    """True when `link`'s realpath lands under `<root>/.gf/wt` (GF-D14).

    The realpath follows link-to-link chains (`gf worktree add` links point
    at the source worktree's consumer link, not the checkout) and resolves
    a dangling consumer link lexically.
    """
    wt = Path(os.path.realpath(root)) / GF_DIR / "wt"
    return Path(os.path.realpath(link)).is_relative_to(wt)


def resolve_checkout(path: Path) -> Checkout:
    """Resolve a consumer or physical path to its checkout.

    A path whose realpath lands under `<root>/.gf/wt/<repo-key>/<checkout-key>`
    maps to that subfolder checkout — this follows consumer links (including
    `gf worktree add` links) without any state lookup or network. Any other
    path maps to the whole-repo layout at that path. Never returns None:
    whether a path is a child remains a HEAD-existence check for the caller.
    """
    rp = Path(os.path.realpath(path))
    anc = _gf_wt_root(rp)
    if anc is not None:
        root = anc.parent.parent.parent.parent
        store = root / GF_DIR / "repos" / anc.parent.name / "git"
        return Checkout(
            gitdir=store / "worktrees" / anc.name,
            work_tree=anc,
            common_dir=store,
            subdir="" if anc == rp else rp.relative_to(anc).as_posix(),
            state=anc.parent / f"{anc.name}.state",
        )
    return whole_repo_checkout(Path(path))
