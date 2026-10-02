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

    @property
    def is_store_checkout(self) -> bool:
        """True when this checkout is a linked checkout of a repo store.

        THE single named discriminator per the architecture standing
        rule: consumers read this property and never re-compare
        `gitdir`/`common_dir` themselves. A whole-repo child keeps
        `common_dir == gitdir`; a subfolder binding's linked worktree has
        `gitdir` under the store's `worktrees/` while `common_dir` is the
        store itself.
        """
        return self.gitdir != self.common_dir


def _normalize_url(url: str) -> str:
    """Expand a bare host/path URL; the resolved-input subset of
    `cli._normalize_url`.

    Inputs here are already-resolved repo urls (recorded resolutions,
    store keys), never a raw user spelling, so the `<repo>.git/<subdir>`
    existence guard does not apply — a dotted first segment is host
    shorthand.
    """
    if "://" in url or url.startswith("git@"):
        return url
    if "/" in url:
        head, _, _ = url.partition("/")
        if "." in head and " " not in head and not head.startswith("."):
            return f"https://{url}"
    return url


def repo_key(repo_url: str) -> str:
    """Derive `<basename>-<first 8 hex of sha1(normalized repo URL)>`.

    The trailing `.git` alias holds only for remote spellings — a
    transport convention, so `https://h/r` ≡ `https://h/r.git` share one
    store. Local resolved paths never alias: `…/repo` and `…/repo.git`
    are distinct real directories and must not collapse into one
    store/checkout (the first binding's content would silently serve
    both). Migration: a binding whose local repo path ends `.git`
    derives a NEW key — its first pull re-resolves into a fresh
    store+checkout and retargets the consumer link, leaving the old
    store orphaned on disk (never deleted — uncommitted work survives,
    same class as GF-TRB-3 leftovers).
    """
    url = _normalize_url(repo_url).rstrip("/")
    if "://" in url or url.startswith("git@"):
        url = url.removesuffix(".git")
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
        state=wt.parent / f".{key}.state",
    )


def repo_store(root: Path, repo_url: str) -> Path:
    """Map a normalized repo URL to its shared store gitdir.

    Returns `<root>/.gf/repos/<repo-key>/git` — the bare object/ref store
    shared by every checkout of that repository under `root`.
    """
    root = Path(os.path.realpath(root))
    return root / GF_DIR / "repos" / repo_key(repo_url) / "git"


def storage_is_real(path: Path) -> bool:
    """True when `path`'s spelling IS its own realpath.

    The root-anchored storage invariant: `repo_store`/`subfolder_checkout`
    realpath the root but append `.gf`/`repos`/`wt` segments lexically, so
    a symlink planted at any appended component (`.gf`, `repos`, `wt`, a
    repo-key or checkout-key dir, or a committed `.gf` link in a hostile
    tree) makes the spelling and the physical location disagree — writes
    through it would land outside the parent root's custody. False means
    at least one component resolves through a link (the path may not
    exist; realpath resolves missing tails lexically).

    Write-side predicate only — never a read gate: layout outputs must
    keep resolving hostile storage for `resolve_checkout`,
    `owns_consumer_link`, `in_gf_wt` and discovery, so ls/status keep
    working under a hostile `.gf`. Callers compare the two-sided
    `realpath(gitdir) == realpath(child)/GF_DIR/"git"` form instead when
    the anchor leaf itself may legitimately be a symlink (whole-repo
    child).
    """
    return Path(os.path.realpath(path)) == path


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


def in_gf_tree(path: Path) -> bool:
    """True when `path`'s realpath is a `.gf` dir or lies inside one.

    `.gf` roots gf's private layout — a child's `git`/`state`, or a
    parent's `repos` stores and `wt` checkout trees — never repository
    content a consumer spelling may resolve into. `in_gf_wt` is the
    narrower checkout-tree guard; this one also covers the `.gf` dir
    itself and every other subtree, including the `.gf` interior's own
    repository boundaries (`.gf/git`, `.gf/repos/<key>/git`) — exempting
    those stays the caller's decision.
    """
    rp = Path(os.path.realpath(path))
    return any(a.name == GF_DIR for a in (rp, *rp.parents))


def in_git_tree(path: Path) -> bool:
    """True when `path`'s realpath is a `.git` dir or lies inside one.

    `.git` roots a repository's private metadata — objects, refs, and
    the hooks git executes on checkout — never content a consumer
    spelling may resolve into: a consumer link planted under
    `.git/hooks` runs as a hook on the parent repo's next `git
    checkout`. Exact segments only — `x.git` or `libfoo.git` spellings
    stay legal. Kept separate from `in_gf_tree`: the two reject
    different storages, and callers choose which apply.
    """
    rp = Path(os.path.realpath(path))
    return any(a.name == ".git" for a in (rp, *rp.parents))


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
            state=anc.parent / f".{anc.name}.state",
        )
    return whole_repo_checkout(Path(path))


def owning_root(co: Checkout) -> Path | None:
    """The `<root>` whose `.gf/wt` holds `co`'s work tree, else None.

    A store checkout's work tree is `<root>/.gf/wt/<repo-key>/<key>`; that
    root owns the checkout's repo store, record and consumer links — the
    root a command entered through a `gf worktree add` link chain must
    operate on (commands through symlinked children resolve to the
    source). A whole-repo checkout has no `.gf/wt` ancestor and returns
    None: the binding belongs to whichever root is operating on it.

    The shape match alone never adopts. `_gf_wt_root` matches lexically
    over `os.path.realpath`, which resolves nonexistent components
    lexically, so a committed mid-path symlink can spell a
    `.gf/wt/<r>/<k>` ancestor under a root that has no gf storage at
    all. Adoption requires the derived root to prove real:

    - a `.git` entry — `find_parent_root`'s own marker (dir, gitfile,
      or symlink), which covers normal parents, dead-link recovery with
      a wiped `.gf`, and worktree-add chains, and cannot be committed
      into a git tree; or
    - the matching repo store `<root>/.gf/repos/<repo-key>/git` with a
      `HEAD` — a root whose `.git` is gone but whose `.gf` storage is
      real.

    A root inside a `.gf` tree is never adopted, and `gf.toml` presence
    is no proof — the manifest is committable content.
    """
    anc = _gf_wt_root(Path(os.path.realpath(co.work_tree)))
    if anc is None:
        return None
    root = anc.parent.parent.parent.parent
    if in_gf_tree(root):  # never a root inside .gf
        return None
    if (root / ".git").exists() or (
        root / GF_DIR / "repos" / anc.parent.name / "git" / "HEAD"
    ).is_file():
        return root
    return None
