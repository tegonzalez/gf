# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import hashlib
import os
import re
import stat
import shutil
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

from .backends import GitBackend, GitCliBackend
from . import layout, manifest as _manifest, state
from .exceptions import DirtyError, GitError, GitFoldersError, ValidationError


def _default_backend() -> GitBackend:
    return GitCliBackend()


def _is_bare_repo_dir(path: Path) -> bool:
    """Whether `path` carries bare-repository structure.

    Mirrors git's `is_git_directory`: a `HEAD` file plus `objects/` and
    `refs/` directories. A directory merely containing a file named
    `HEAD` (e.g. a file tracked in some worktree) is not a repository.
    """
    return (
        (path / "HEAD").is_file()
        and (path / "objects").is_dir()
        and (path / "refs").is_dir()
    )


def _resolved_git_url(url: str) -> str:
    """Resolve a local filesystem url to the gitdir git can fetch from.

    If `url` points at a git-folder child, return the inner gitdir path so
    `git fetch` works against another child.
    """
    if "://" in url or url.startswith("git@"):
        return url
    p = Path(url).resolve()
    if not p.is_dir():
        return url
    if (p / ".git").is_dir() or _is_bare_repo_dir(p):
        return url
    gitdir = layout.whole_repo_checkout(p).gitdir
    if (gitdir / "HEAD").is_file():
        return str(gitdir)
    return url


def _is_remote_url(url: str) -> bool:
    """Return True if `url` spells a network remote rather than a local path."""
    return "://" in url or url.startswith("git@")


def _is_repo_dir(path: Path) -> bool:
    """Whether `path` is a repository boundary for the local URL walk-up.

    A directory is a repository when it carries a `.git` entry (directory,
    gitfile, or symlink), carries bare-repository structure (`HEAD`,
    `objects/`, `refs/` at the top level), or is a `gf` child
    (`.gf/git` with a HEAD).
    """
    if os.path.lexists(path / ".git"):
        return True
    if _is_bare_repo_dir(path):
        return True
    return (layout.whole_repo_checkout(path).gitdir / "HEAD").is_file()


def _validate_url_subdir(url: str, subdir: str) -> None:
    """Reject `.`, `..`, and empty segments in a resolved subdir."""
    if not subdir:
        return
    for segment in subdir.split("/"):
        if segment in ("", ".", ".."):
            kind = "an empty" if segment == "" else f"a '{segment}'"
            raise GitFoldersError(
                f"invalid url '{url}': the subdir '{subdir}' contains "
                f"{kind} path segment"
            )


class _GfInteriorError(GitFoldersError):
    """A local `url` resolved into `.gf` internals — a permanent refusal.

    Still `GitFoldersError` (the resolver's clear-error family), but a
    distinct type so the `resolve_repo_url` carves never downgrade it to
    the fetch-failure surface kept for ordinary unresolvable local paths.
    """


def _resolve_local_url(url: str, parent_root: Path | None) -> tuple[str, str]:
    """Resolve a local `url` by walking up to the nearest repository.

    Relative paths anchor at `parent_root` (the parent repo root); the leaf
    directory itself need not exist.
    """
    leaf = Path(url)
    if not leaf.is_absolute():
        leaf = (Path(parent_root) if parent_root is not None else Path.cwd()) / leaf
    leaf = leaf.resolve()
    # `.gf` is gf's private layout, never a repository or its content. A
    # leaf inside `.gf/wt` names a managed checkout — a `.git` entry
    # there is the removed gitfile's position, not a boundary (GF-D8).
    # Any other leaf at-or-inside a `.gf` subtree resolves only when the
    # leaf itself is the repository boundary: `.gf/git` and
    # `.gf/repos/<key>/git` still self-resolve as fetch sources (the
    # `_resolved_git_url` precedent); deeper interior spellings refuse
    # rather than mis-bind a bogus subdir into gf storage.
    if layout.in_gf_wt(leaf) or (
        layout.in_gf_tree(leaf) and not _is_repo_dir(leaf)
    ):
        raise _GfInteriorError(
            f"could not resolve '{url}': '{leaf}' is inside a '.gf' "
            "directory — gf's private layout, not a repository"
        )
    candidate = leaf
    while True:
        if _is_repo_dir(candidate):
            subdir = (
                "" if candidate == leaf
                else leaf.relative_to(candidate).as_posix()
            )
            _validate_url_subdir(url, subdir)
            return str(candidate), subdir
        if candidate.parent == candidate:
            raise GitFoldersError(
                f"could not resolve '{url}': no repository (a .git entry, "
                f"bare repository, or .gf/git child) found on the path"
            )
        candidate = candidate.parent


_REMOTE_URL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://[^/]+)(?:/(.*))?$")


def _remote_url_parts(url: str) -> tuple[str, list[str]]:
    """Split a remote url into its authority prefix and path segments.

    `https://host/org/repo` -> (`https://host`, ['org', 'repo']);
    `git@host:org/repo`     -> (`git@host:`,   ['org', 'repo']).
    """
    m = _REMOTE_URL_RE.match(url)
    if m:
        path = m.group(2)
        return m.group(1), (path.split("/") if path else [])
    host, sep, path = url[4:].partition(":")
    if sep:
        return url[: 4 + len(host) + 1], (path.split("/") if path else [])
    return url, []


def _remote_url_join(prefix: str, segments: list[str]) -> str:
    """Reassemble a remote url from its authority prefix and path segments."""
    glue = "" if prefix.endswith(":") else "/"
    return prefix + glue + "/".join(segments)


def _resolve_remote_url(
    url: str, backend: GitBackend | None
) -> tuple[str, str]:
    """Resolve a remote `url`: a `.git` segment boundary, else probes."""
    prefix, segments = _remote_url_parts(url)

    # A path segment ending in `.git` ends the repository after the first
    # such segment. No network is used.
    for i, segment in enumerate(segments):
        if segment.endswith(".git"):
            repo_url = _remote_url_join(prefix, segments[: i + 1])
            subdir = "/".join(segments[i + 1:])
            _validate_url_subdir(url, subdir)
            return repo_url, subdir

    # Otherwise probe `git ls-remote`: the full url first, then one path
    # segment shorter at a time. Terminal prompts are disabled so a wrong
    # prefix fails instead of stopping for a password.
    backend = backend or _default_backend()
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if not segments:
        if backend.git(
            "ls-remote", url, check=False, env=env,
        ).returncode == 0:
            return url, ""
    else:
        for n in range(len(segments), 0, -1):
            candidate = _remote_url_join(prefix, segments[:n])
            result = backend.git(
                "ls-remote", candidate, check=False, env=env,
            )
            if result.returncode == 0:
                subdir = "/".join(segments[n:])
                _validate_url_subdir(url, subdir)
                return candidate, subdir
    raise GitFoldersError(
        f"could not resolve '{url}': no prefix answers `git ls-remote`; "
        f"mark the repository boundary by writing '.git' after the "
        f"repository name"
    )


def resolve_repo_url(
    url: str,
    parent_root: Path | None = None,
    backend: GitBackend | None = None,
) -> tuple[str, str]:
    """Split `url` into `(repo_url, subdir)` per the URL-resolution rules.

    `subdir` is the repository-relative POSIX path inside the repository, or
    "" when `url` is the repository itself (a whole-repo binding). A local
    path walks up to the nearest repository directory; a remote url splits
    after a `.git` path segment or, failing that, at the longest prefix
    that answers `git ls-remote`. Raises `GitFoldersError` when nothing
    resolves.
    """
    if _is_remote_url(url):
        return _resolve_remote_url(url, backend)
    return _resolve_local_url(url, parent_root)


def resolve_ref(
    co: layout.Checkout,
    ref: str,
    backend: GitBackend | None = None,
) -> str:
    """Resolve a ref to a SHA using the checkout's shared refs."""
    backend = backend or _default_backend()
    gitdir = co.common_dir

    # Try as full SHA
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref

    # Try as a tag, local branch, remote branch, or commit-ish directly
    for refspec in (
        f"refs/tags/{ref}^{{}}",
        f"refs/heads/{ref}^{{}}",
        f"refs/remotes/origin/{ref}^{{}}",
        f"{ref}^{{}}",
    ):
        try:
            return backend.git_capture(
                "rev-parse", refspec, git_dir=gitdir,
            ).strip()
        except GitError:
            pass

    # Latest/default branch
    if ref in ("latest", ""):
        for candidate in (
            "HEAD",
            "refs/heads/main",
            "refs/heads/master",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
            "refs/remotes/origin/master",
        ):
            try:
                return backend.git_capture(
                    "rev-parse", candidate, git_dir=gitdir,
                ).strip()
            except GitError:
                continue

    raise ValidationError(f"could not resolve ref '{ref}' in {co.work_tree}")


def _ensure_origin_head(co: layout.Checkout, backend: GitBackend | None = None) -> None:
    """Ensure refs/remotes/origin/HEAD is a valid symbolic ref in the store."""
    backend = backend or _default_backend()
    gitdir = co.common_dir

    result = backend.git(
        "symbolic-ref", "refs/remotes/origin/HEAD",
        git_dir=gitdir, check=False,
    )
    if result.returncode == 0:
        target = result.stdout.strip()
        verify = backend.git(
            "show-ref", "--verify", target,
            git_dir=gitdir, check=False,
        )
        if verify.returncode == 0:
            return

    backend.git(
        "remote", "set-head", "origin", "-a",
        git_dir=gitdir, stream=True,
    )


def _effective_branch(
    co: layout.Checkout,
    ref: str,
    backend: GitBackend | None = None,
) -> str | None:
    """Return the local branch name for a branch ref or the remote default branch.

    Returns None for tags, commits, and other non-branch refs.
    """
    backend = backend or _default_backend()
    gitdir = co.common_dir

    if ref in ("latest", ""):
        _ensure_origin_head(co, backend)
        try:
            head = backend.git_capture(
                "symbolic-ref", "refs/remotes/origin/HEAD",
                git_dir=gitdir,
            ).strip()
            # Strip the whole `refs/remotes/origin/` prefix so a
            # slash-named default (feature/main) resolves as
            # `origin/feature/main` — the same prefix strip
            # `_store_default_branch` does. A symref outside the
            # prefix passes through unchanged and fails downstream
            # resolution honestly rather than being silently truncated.
            return head.removeprefix("refs/remotes/origin/")
        except GitError:
            return "master"

    if is_branch(co, ref, backend):
        return ref
    return None


def is_branch(
    co: layout.Checkout,
    ref: str,
    backend: GitBackend | None = None,
) -> bool:
    backend = backend or _default_backend()
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return False
    if ref in ("latest", ""):
        return False

    gitdir = co.common_dir
    for candidate in (f"refs/heads/{ref}", f"refs/remotes/origin/{ref}"):
        result = backend.git(
            "show-ref", "--verify", candidate,
            git_dir=gitdir, check=False,
        )
        if result.returncode == 0:
            return True
    return False


def _set_child_origin(
    co: layout.Checkout,
    url: str,
    backend: GitBackend | None = None,
) -> None:
    """Ensure the `origin` remote points to the actual git-folder URL."""
    backend = backend or _default_backend()
    gitdir = co.common_dir

    result = backend.git(
        "remote", "get-url", "origin",
        git_dir=gitdir, check=False,
    )
    if result.returncode == 0:
        backend.git(
            "remote", "set-url", "origin", url,
            git_dir=gitdir,
        )
    else:
        backend.git(
            "remote", "add", "origin", url,
            git_dir=gitdir,
        )

    # The wildcard line is coverage, append-only like a store's: write it
    # plainly only onto an empty key, and `--add` it when live lines lack
    # it — a bare `config` write would rewrite a `--single-branch`-
    # narrowed line, and fails outright once `_ensure_pinned_ref` made
    # the key multi-valued.
    lines = _live_refspec_lines(gitdir, backend)
    if not lines:
        backend.git(
            "config", "remote.origin.fetch", _WILDCARD_FETCH_REFSPEC,
            git_dir=gitdir,
        )
    elif _WILDCARD_FETCH_REFSPEC not in lines:
        backend.git(
            "config", "--add", "remote.origin.fetch",
            _WILDCARD_FETCH_REFSPEC, git_dir=gitdir,
        )


def _init_child_gitdir(
    co: layout.Checkout,
    backend: GitBackend | None = None,
) -> None:
    """Create a .gf/git gitdir in the checkout's worktree."""
    backend = backend or _default_backend()
    gitdir = co.gitdir
    # The `.gf`/`git` components appended to the child must be literal:
    # a symlinked `.gf` would redirect the gitdir mkdir — and every later
    # gitdir write — outside the child's own tree. The child's leaf
    # spelling may legitimately be a link, which the two-sided compare
    # inside `whole_repo_gitdir_is_real` already allows.
    if not layout.whole_repo_gitdir_is_real(co):
        raise ValidationError(
            f"child gitdir path {gitdir} resolves through a symlink")
    gitdir.mkdir(parents=True, exist_ok=True)

    # Initialize a fresh gitdir at .gf/git with this worktree.
    backend.git("init", git_dir=gitdir, work_tree=co.work_tree)

    # Make sure the child git ignores the .gf directory.
    exclude = gitdir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    patterns = set(exclude.read_text().splitlines()) if exclude.exists() else set()
    patterns.add(layout.GF_DIR)
    patterns.discard("")
    exclude.write_text("\n".join(sorted(patterns)) + "\n")


def _assert_no_gf_root_entry(
    backend: GitBackend, co: layout.Checkout, sha: str
) -> None:
    """Refuse to materialize `sha` when its tree carries a root `.gf`.

    `.gf` is gf-managed storage: a committed root entry of ANY kind —
    blob, tree, symlink, or gitlink (a submodule named `.gf`) — collides
    with the private layout checkout materialization must never create
    (a whole-repo child's own `.gf/git` lives there; a linked checkout's
    cone would place it under the worktree root). A symlink or gitlink
    additionally redirects later gf storage writes outside the root.
    `git ls-tree <sha> .gf` lists only the ROOT entry, so a nested
    `sub/.gf` stays legal while every root kind produces output and
    refuses. Probing `co.common_dir` covers both checkout kinds: the
    child's own gitdir for a whole-repo child, the shared repo store
    for a linked one.
    """
    if backend.git_capture(
        "ls-tree", sha, layout.GF_DIR, git_dir=co.common_dir,
    ).strip():
        raise ValidationError(
            f"refusing to check out {sha}: its root tree carries a "
            f"'{layout.GF_DIR}' entry — .gf is gf-managed storage")


def _resolve_remote_branch(co: layout.Checkout, branch: str, backend: GitBackend | None = None) -> str:
    """Resolve a remote tracking branch to a SHA in the checkout's store."""
    backend = backend or _default_backend()
    gitdir = co.common_dir
    try:
        return backend.git_capture(
            "rev-parse", f"refs/remotes/origin/{branch}",
            git_dir=gitdir,
        ).strip()
    except GitError as e:
        raise ValidationError(f"could not resolve remote branch 'origin/{branch}' in {co.work_tree}") from e


def _retention_ref(
    co: layout.Checkout, backend: GitBackend, *additional_tips: str,
) -> None:
    """Keep every protected tip under an immutable per-checkout name (GF-D23).

    The plural namespace coexists with the old singular leaf: retain its
    existing tip too, without overwriting or deleting that leaf. CAS
    creation refuses a conflicting name rather than clobbering a ref.
    """
    tips = set(additional_tips)
    for ref in ("HEAD", "refs/worktree/gf-retained"):
        result = backend.git(
            "rev-parse", "--verify", ref,
            git_dir=co.gitdir, work_tree=co.work_tree, check=False,
        )
        if result.returncode == 0:
            tips.add(result.stdout.strip())
    for sha in sorted(tips):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise ValidationError(f"cannot retain invalid commit identity {sha!r}")
        ref = f"refs/worktree/gf-retained-commits/{sha}"
        existing = backend.git(
            "rev-parse", "--verify", ref,
            git_dir=co.gitdir, work_tree=co.work_tree, check=False,
        )
        if existing.returncode == 0:
            if existing.stdout.strip() != sha:
                raise ValidationError(
                    f"retention ref {ref} names another tip; refusing to overwrite it")
            continue
        try:
            backend.git(
                "update-ref", ref, sha, "0" * len(sha),
                git_dir=co.gitdir, work_tree=co.work_tree,
            )
        except GitError as e:
            raise GitError(
                f"cannot retain {co.work_tree}'s commit {sha} under {ref}: "
                f"{e}; refusing the transition so the commit stays reachable"
            ) from e


def _ahead_behind(
    co: layout.Checkout, other: str, backend: GitBackend
) -> tuple[int, int]:
    """`(ahead, behind)` counts of HEAD against `other`, both local.

    `rev-list --left-right --count HEAD...<other>`: the left count is the
    commits HEAD carries that `other` lacks (ahead); the right count is
    the commits `other` carries that HEAD lacks (behind).
    """
    result = backend.git(
        "rev-list", "--left-right", "--count", f"HEAD...{other}",
        git_dir=co.gitdir, work_tree=co.work_tree, check=False,
    )
    if result.returncode != 0:
        return (-1, -1)
    parts = result.stdout.split()
    if len(parts) != 2:
        return (-1, -1)
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return (-1, -1)


def _assert_no_in_progress_op(co: layout.Checkout) -> None:
    """Refuse while a merge, rebase, or cherry-pick is in progress.

    MERGE_HEAD, CHERRY_PICK_HEAD, REVERT_HEAD, and the
    rebase-merge/rebase-apply state dirs all live in the per-worktree
    gitdir (`co.gitdir`): a shared store keeps each checkout's
    in-progress state separate, and a whole-repo child keeps its own.
    Stacking a checkout, merge, or rebase on top would compound or strand
    the interrupted operation — the user finishes or aborts it with
    their own git first, and `gf` says so truthfully instead of
    proceeding.
    """
    for marker, op, remedy in (
        ("rebase-merge", "rebase", "git rebase --continue` or `git rebase --abort"),
        ("rebase-apply", "rebase", "git rebase --continue` or `git rebase --abort"),
        ("MERGE_HEAD", "merge", "git merge --continue` or `git merge --abort"),
        ("CHERRY_PICK_HEAD", "cherry-pick",
         "git cherry-pick --continue` or `git cherry-pick --abort"),
        ("REVERT_HEAD", "revert",
         "git revert --continue` or `git revert --abort"),
    ):
        if (co.gitdir / marker).exists():
            raise ValidationError(
                f"a {op} is in progress in {co.work_tree}; finish or "
                f"abort it (`{remedy}`) before pulling — `gf` will not "
                f"stack an update on top of it")


_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _ignored_paths(co: layout.Checkout, backend: GitBackend) -> list[str]:
    """Enumerate the protected ignored set once for a transition sequence."""
    st = backend.git(
        "status", "--porcelain", "--ignored", "-z",
        git_dir=co.gitdir, work_tree=co.work_tree, cwd=co.work_tree, check=False)
    if st.returncode != 0:
        raise GitError(
            f"cannot enumerate ignored files in {co.work_tree}: "
            f"{st.stderr.strip() or st.stdout.strip()}")
    return [
        entry[3:] for entry in st.stdout.split("\0")
        if entry.startswith("!!") and entry[3:]
    ]


def _ignored_collisions(
    co: layout.Checkout,
    target_sha: str,
    backend: GitBackend,
    *,
    base_sha: str | None = None,
    ignored_paths: list[str] | None = None,
) -> list[str]:
    """Ignored files sitting at paths the `HEAD -> target` transition
    writes. Return the colliding paths, sorted; [] when none.

    `status --porcelain` does not list ignored files and `checkout`/
    `merge --ff-only` overwrite ignored paths silently (git's default is
    effectively `--overwrite-ignore`), so an ignored file occupying a
    path the incoming tree writes counts as dirty work — the protected
    set includes ignored files (spec Error handling). This runs
    post-fetch where `target_sha` is known; the callers' earlier
    porcelain gate stays the cheap tracked-dirt refusal.

    `status --porcelain --ignored -z` collapses an ignored directory to
    a `!! dir/` entry, so the collision test is path-prefix aware —
    ignored `build/` collides with incoming `build/x`. A worktree with
    an unborn HEAD diffs the target against the empty tree, making every
    incoming path a write. A `status` failure refuses (the ignored set
    is unprovable); a `diff` failure with ignored files present refuses
    for the same reason — the overlap cannot be proven empty.
    """
    ignored = (_ignored_paths(co, backend) if ignored_paths is None else ignored_paths)
    if not ignored:
        return []
    base = base_sha
    if base is None:
        head = backend.git(
            "rev-parse", "--verify", "HEAD^{commit}",
            git_dir=co.gitdir, work_tree=co.work_tree, check=False)
        base = head.stdout.strip() if head.returncode == 0 else _EMPTY_TREE_SHA
    diff = backend.git(
        "diff", "--no-relative", "--no-renames", "--name-only", "-z", base, target_sha,
        git_dir=co.gitdir, work_tree=co.work_tree, cwd=co.work_tree, check=False)
    if diff.returncode != 0:
        raise GitError(
            f"cannot determine the paths {co.work_tree} would write "
            f"while ignored files are present; refusing rather than "
            f"risking an overwrite: "
            f"{diff.stderr.strip() or diff.stdout.strip()}")
    changed = [p for p in diff.stdout.split("\0") if p]
    hits = {
        i for i in ignored
        if any(
            c == i.rstrip("/")
            or c.startswith(i.rstrip("/") + "/")
            or i.rstrip("/").startswith(c.rstrip("/") + "/")
            for c in changed
        )
    }
    return sorted(hits)


def _apply_ref(
    co: layout.Checkout,
    ref: str,
    branch: str | None,
    backend: GitBackend,
    *,
    rebase: bool = False,
    autostash: bool = False,
) -> str:
    """Apply a resolved ref to an existing checkout once. Return the resolved SHA.

    The single ref-application step both pull flows share (GF-D15
    composition): `_fetch_and_checkout` and `_fetch_and_rebase` call it
    after their own `fetch --no-tags origin`, and `pull_shared_bindings`
    calls it after the grouped store's one fetch — the two update
    algorithms differ in step order, not in the operation. `branch` is
    the caller-resolved effective branch (`None` for a tag/commit ref).

    Preservation contract (GF-D16/GF-D23): the outgoing HEAD is recorded
    under immutable `refs/worktree/gf-retained-commits/<sha>` before any transition that could
    strand it. A branch ref creates the local tracking branch at
    `origin/<branch>` when it does not exist (`checkout -b <branch>
    origin/<branch>`) or attaches to the existing one
    (`checkout <branch>`) and integrates fast-forward-only
    (`git merge --ff-only origin/<branch>`): a strictly-behind branch
    advances, up-to-date is a no-op, strictly-ahead keeps its local
    commits, and divergence refuses reporting the truthful ahead/behind
    counts plus the deliberate recoveries. `rebase` runs
    `git rebase origin/<branch>` instead of the merge — entered only by
    the user's explicit `--rebase`. A non-branch ref checks out the
    detached resolved SHA. `checkout -B`, `checkout -f`, `reset`, and
    `branch -f` are never issued: an unsafe transition refuses rather
    than falling back to a destructive one.
    """
    sha = (
        _resolve_remote_branch(co, branch, backend)
        if branch
        else resolve_ref(co, ref, backend)
    )
    _assert_no_in_progress_op(co)
    _assert_no_gf_root_entry(backend, co, sha)
    local_tip = None
    edges: list[tuple[str | None, str]] = []
    if branch:
        local = backend.git(
            "show-ref", "--verify", f"refs/heads/{branch}",
            git_dir=co.gitdir, check=False,
        )
        if local.returncode == 0:
            local_tip = local.stdout.split()[0]
            _assert_no_gf_root_entry(backend, co, local_tip)
            edges.append((None, local_tip))  # current HEAD -> attachment
            if rebase:
                # Rebase resets to upstream then replays local patches.
                # Every replay commit is a possible materialization, not
                # just the final tip. Comparing each parent edge covers
                # paths added then removed within the local history.
                edges.append((local_tip, sha))
                commits = backend.git_capture(
                    "rev-list", "--reverse", f"{sha}..{local_tip}",
                    git_dir=co.common_dir,
                ).splitlines()
                for commit in commits:
                    _assert_no_gf_root_entry(backend, co, commit)
                    parents = backend.git_capture(
                        "rev-list", "--parents", "-n", "1", commit,
                        git_dir=co.common_dir,
                    ).split()[1:]
                    edges.extend((parent, commit) for parent in parents)
                    if not parents:
                        edges.append((_EMPTY_TREE_SHA, commit))
            else:
                ahead = backend.git(
                    "merge-base", "--is-ancestor", sha, local_tip,
                    git_dir=co.common_dir, check=False,
                )
                if ahead.returncode not in (0, 1):
                    raise GitError("cannot prove the branch integration preserves history")
                # A strictly-ahead branch has no integration tree write.
                if ahead.returncode != 0:
                    ff = backend.git(
                        "merge-base", "--is-ancestor", local_tip, sha,
                        git_dir=co.common_dir, check=False,
                    )
                    if ff.returncode not in (0, 1):
                        raise GitError("cannot prove the branch integration preserves history")
                    if ff.returncode != 0:
                        counts = backend.git_capture(
                            "rev-list", "--left-right", "--count", f"{local_tip}...{sha}",
                            git_dir=co.common_dir,
                        ).split()
                        raise ValidationError(
                            f"local branch '{branch}' diverged from origin/{branch} "
                            f"(ahead {counts[0]}, behind {counts[1]}); refusing to move it "
                            "— resolve with `gf pull --rebase` or perform Git integration yourself")
                    edges.append((local_tip, sha))
        else:
            edges.append((None, sha))  # new branch at mirror tip
    else:
        edges.append((None, sha))
    ignored = _ignored_paths(co, backend)
    head_sha = None
    if ignored:
        head = backend.git(
            "rev-parse", "--verify", "HEAD^{commit}",
            git_dir=co.gitdir, work_tree=co.work_tree, check=False,
        )
        head_sha = head.stdout.strip() if head.returncode == 0 else _EMPTY_TREE_SHA
    colliding = sorted({
        path for base, target in edges
        for path in _ignored_collisions(
            co, target, backend, base_sha=base or head_sha, ignored_paths=ignored)
    })
    stashed = False
    if colliding:
        if not autostash:
            raise DirtyError(
                f"{co.work_tree} has ignored files the update would "
                f"overwrite: {', '.join(colliding)} — remove or relocate "
                f"them, or pull with --autostash so the stash carries "
                f"them through the update")
        _stash_autostash(co, backend)
        stashed = True
    try:
        result = _apply_transition(co, branch, sha, backend, rebase, local_tip)
    except Exception:
        # A stash taken by this gate is restored on every failure after
        # it — never orphaned — and a failed pop keeps the named entry.
        if stashed:
            try:
                _pop_autostash(co, backend)
            except GitError as pop_err:
                print(f"gf: warning: {pop_err}", file=sys.stderr)
        raise
    if stashed:
        _pop_autostash(co, backend)
    return result


def _apply_transition(
    co: layout.Checkout,
    branch: str | None,
    sha: str,
    backend: GitBackend,
    rebase: bool,
    local_tip: str | None = None,
) -> str:
    """Move the worktree to `sha` per the resolved ref kind.

    Split from `_apply_ref` so the post-fetch ignored-work stash can
    wrap every arm: the outgoing-HEAD retention ref, the detached
    checkout, the branch attach/create plus fast-forward merge, and the
    explicit `--rebase` arm. No `checkout -f`, `checkout -B`, `reset`,
    or `branch -f` is ever issued.
    """
    _retention_ref(co, backend, *((local_tip,) if local_tip else ()))
    if not branch:
        backend.git(
            "checkout", sha,
            git_dir=co.gitdir, work_tree=co.work_tree, stream=True,
        )
        return sha
    mirror = f"origin/{branch}"
    if backend.git(
        "show-ref", "--verify", f"refs/heads/{branch}",
        git_dir=co.gitdir, check=False,
    ).returncode != 0:
        # No local tracking branch yet: create it at the mirror tip and
        # check it out — creation, never a reset of an existing ref.
        backend.git(
            "checkout", "-b", branch, mirror,
            git_dir=co.gitdir, work_tree=co.work_tree, stream=True,
        )
    else:
        # Attach to the existing local branch; an existing ref is never
        # reset or recreated.
        backend.git(
            "checkout", branch,
            git_dir=co.gitdir, work_tree=co.work_tree, stream=True,
        )
    if rebase:
        try:
            backend.git(
                "rebase", mirror,
                git_dir=co.gitdir, work_tree=co.work_tree, stream=True,
            )
        except GitError as e:
            raise GitError(
                f"rebase of {co.work_tree} onto {mirror} failed; "
                f"resolve or abort the rebase and try again"
            ) from e
        return sha
    # Captured, not streamed: the merge's diffstat is git chatter, not
    # `gf` output — the pull's stdout carries only `gf`'s own lines.
    result = backend.git(
        "merge", "--ff-only", mirror,
        git_dir=co.gitdir, work_tree=co.work_tree, check=False,
    )
    if result.returncode == 0:
        return sha
    # A failed fast-forward on a clean tree means the local branch and
    # the mirror carry commits the other lacks (diverged or unrelated
    # histories): refuse without moving anything and report the truthful
    # state plus the deliberate recoveries.
    ahead, behind = _ahead_behind(co, mirror, backend)
    if ahead > 0 and behind > 0:
        raise ValidationError(
            f"local branch '{branch}' diverged from {mirror} (ahead "
            f"{ahead}, behind {behind}); refusing to move it — commit or "
            f"stash the local work and pull again, or run "
            f"`gf pull --rebase`")
    msg = result.stderr.strip() or result.stdout.strip()
    raise GitError(
        f"git merge --ff-only {mirror} failed for {co.work_tree}: {msg}")


def _fetch_and_checkout(
    co: layout.Checkout,
    url: str,
    ref: str,
    backend: GitBackend | None = None,
    depth: int | None = None,
    single_branch: bool = False,
    autostash: bool = False,
) -> str:
    """Fetch `origin` and check out the resolved ref. Return the resolved SHA.

    If `depth` is given, `git fetch --depth=<n> origin` is used for a
    shallow clone.

    If `single_branch` is True, `remote.origin.fetch` is narrowed to the
    resolved branch after the initial fetch so subsequent fetches only
    fetch that branch's history; the narrowed line replaces the wildcard
    only while it is the key's single value, and is `--add`ed alongside
    any other live coverage lines (a pinned tag's). It is ignored for
    tag/commit refs.

    There is no force arm: `gf` has no flag that updates over
    uncommitted work — a dirty tree is refused or autostashed by the
    caller, and `_apply_ref` never issues `checkout -f`.
    """
    backend = backend or _default_backend()

    # Unsafe legacy refspec state (`+` into user-owned refs) refuses
    # before any coverage write or fetch runs against this gitdir.
    _assert_safe_refspecs(co.common_dir, backend)
    # A narrowed child gitdir has the same non-branch coverage hole as a
    # narrowed repo store: cover a tag/commit `ref` before the fetch so
    # it lands the ref (the tag's appended line rides this fetch; a
    # missing commit gets its own one-shot `fetch --no-tags origin
    # <sha>`). `_fetch_and_rebase` delegates non-branch refs here, so
    # both whole-repo seams share the coverage.
    _ensure_pinned_ref(co.common_dir, url, ref, backend)

    fetch_args = ["fetch", "--no-tags"]
    if depth is not None:
        fetch_args.append(f"--depth={depth}")
    fetch_args.append("origin")

    _fetch_guarded(backend, co.common_dir, *fetch_args)

    branch = _effective_branch(co, ref, backend)
    if branch and single_branch:
        # Narrow the fetch refspec to the resolved branch so subsequent
        # fetches only fetch that branch's history. Coverage-aware like
        # `_set_child_origin`: a plain write when the key is absent or
        # still holds only the `remote add` wildcard — the one case where
        # narrowing actually shrinks coverage — and `config --add` when
        # other live lines (a pin's tag line, hand-added coverage) would
        # be clobbered; no write at all when the line is already there.
        narrowed = _branch_refspec(branch)
        lines = _live_refspec_lines(co.common_dir, backend)
        if narrowed not in lines:
            if not lines or lines == [_WILDCARD_FETCH_REFSPEC]:
                backend.git(
                    "config", "remote.origin.fetch", narrowed,
                    git_dir=co.common_dir,
                )
            else:
                backend.git(
                    "config", "--add", "remote.origin.fetch", narrowed,
                    git_dir=co.common_dir,
                )

    return _apply_ref(co, ref, branch, backend, autostash=autostash)


def _fetch_and_rebase(
    co: layout.Checkout,
    url: str,
    ref: str,
    backend: GitBackend | None = None,
    autostash: bool = False,
) -> str:
    """Fetch `origin` and rebase the local branch onto the remote tracking branch.

    Rebase is only meaningful for branch refs (including `latest`). For tags or
    commits, the resolved SHA is checked out directly and the rebase flag is
    ignored.
    """
    backend = backend or _default_backend()

    branch = _effective_branch(co, ref, backend)
    if not branch:
        return _fetch_and_checkout(co, url, ref, backend, autostash=autostash)

    _assert_safe_refspecs(co.common_dir, backend)
    _fetch_guarded(backend, co.common_dir, "fetch", "--no-tags", "origin")

    return _apply_ref(
        co, ref, branch, backend, rebase=True, autostash=autostash)


def _print_gitignore_recommendation(parent_root: Path, child: Path) -> None:
    """Recommend that the user add the child path to the parent .gitignore."""
    rel = child.relative_to(parent_root).as_posix() + "/"
    print(f'add "{rel}" to .gitignore')


def _cleanup_new_child(
    child: Path,
    existed_before: bool,
    was_git_folder_before: bool,
    parent_root: Path,
) -> None:
    """Undo the filesystem side effects of a failed child creation.

    If the child directory did not exist before we started, remove it entirely.
    If the child directory existed but was not yet a git-folder, remove only the
    `.gf` directory we created, leaving the original directory in place.

    Belt only: a spelling that resolves to the parent root itself (for
    example a `..`-suffixed leaf a caller failed to normalize) is never
    removed — cleanup must not rmtree the parent worktree or its own
    `.gf` storage.
    """
    if os.path.realpath(child) == os.path.realpath(parent_root):
        return
    if not existed_before:
        if child.exists():
            shutil.rmtree(child, ignore_errors=True)
    elif not was_git_folder_before:
        gf = child / layout.GF_DIR
        if gf.exists():
            shutil.rmtree(gf, ignore_errors=True)


def init_child(
    co: layout.Checkout,
    url: str,
    ref: str,
    parent_root: Path,
    override: bool = False,
    backend: GitBackend | None = None,
    depth: int | None = None,
    single_branch: bool = False,
    binding_path: str | None = None,
) -> None:
    backend = backend or _default_backend()
    child = co.work_tree
    # The recorded binding path must be segment-clean BEFORE the
    # existing-child early return below can wave it through:
    # `binding_path` is the manifest `rel` about to be recorded, and a
    # `.gf` or `.git` segment anywhere in its spelled parts names
    # managed storage — the `.gf` form is exactly what manifest
    # read-validation then refuses on every later command, a wedge
    # until the manifest is hand-edited. The realpath refusal below
    # cannot carry this check: a live consumer link's realpath
    # legitimately sits inside `.gf/wt`, so only the recorded spelling
    # is segment-tested — a live link's own `rel` is always clean.
    managed = next(
        (
            seg
            for seg in Path(binding_path).parts
            if seg in (layout.GF_DIR, ".git")
        ),
        None,
    ) if binding_path is not None else None
    if managed is not None:
        owner = "gf" if managed == layout.GF_DIR else "git"
        raise ValidationError(
            f"child path {binding_path} reaches inside {owner}-managed "
            f"storage ({managed})")
    if _is_git_folder_child(co):
        return  # already a git-folder; clone is just an add to the manifest

    # A consumer path resolving into `.gf` storage is never a bindable
    # child — the live-link re-add above stays the only store-checkout
    # add. A dead checkout's link (its record removed from the store's
    # `worktrees/`) or any other spelling landing under `.gf` must not
    # get a fresh gitdir planted at `co.gitdir`. `in_git_tree` adds the
    # `.git` sibling: a child inside the parent's repository metadata —
    # a `.git`-segment spelling or a leaf/mid-path link resolving
    # there — would hand git a path it reads as config or executes as
    # a hook.
    if (
        co.is_store_checkout
        or layout.in_gf_tree(child)
        or layout.in_git_tree(child)
    ):
        raise ValidationError(
            f"child path {child} resolves inside gf-managed storage "
            f"({layout.GF_DIR}) or repository metadata (.git)")

    effective_ref = ref or "latest"
    # A dangling symlink is occupancy, not a crash: `Path.exists()` is
    # False for one, so the non-empty check below and `child.mkdir`'s
    # exist_ok both misread it as absent and `os.mkdir` raises EEXIST.
    # Refuse it up front (the same `lexists` formula ensure_consumer_link
    # uses). A link to a real directory keeps falling through to the
    # non-empty check's write-through semantics.
    if os.path.lexists(child) and not child.exists():
        raise ValidationError(f"child path {child} is a dangling symlink")
    # An existing non-directory occupant (a plain file, or a link to one)
    # passes `child.exists()` yet crashes `iterdir` with NotADirectoryError.
    if child.exists() and not child.is_dir():
        raise ValidationError(f"child path {child} exists and is not a directory")
    if child.exists() and any(child.iterdir()):
        raise ValidationError(f"child path {child} already exists and is not empty")

    # clone/init is spec L140's unambiguous domain for the remote-probe
    # class: an all-probes-fail `ls-remote` walk propagates the resolver's
    # `.git`-boundary hint to the command envelope rather than degrading
    # to a fetch failure. A local path that simply contains no repository
    # keeps the fetch-failure surface (rc=2) and its cleanup semantics.
    try:
        repo_url, subdir = resolve_repo_url(url, parent_root, backend)
    except _GfInteriorError:
        # `.gf` interiors are refused outright: the clear refusal must
        # reach the user, not degrade to a fetch failure.
        raise
    except GitFoldersError:
        if _is_remote_url(url):
            raise
        # Unresolvable local path keeps the fetch-failure surface —
        # anchored at parent_root like the resolver's own anchor.
        repo_url, subdir = str((Path(parent_root) / url).resolve()), ""
    if subdir:
        ensure_shared_binding(
            child, parent_root, repo_url, subdir, url, effective_ref,
            override=override, backend=backend, single_branch=single_branch,
            depth=depth, binding_path=binding_path,
        )
        return

    # Origin and the fetch consume the anchored resolution `repo_url`,
    # never the spelled `url`: `_resolved_git_url` resolves a relative
    # path at the process cwd, which need not be the parent root (a
    # clone invoked from a subdirectory would anchor at the cwd and
    # fetch the wrong — or a decoy — repository).
    resolved_url = _resolved_git_url(repo_url)
    # `lexists`, not `exists`: a dangling link occupant counts as
    # pre-existing so failure cleanup preserves it rather than treating
    # the path as ours to remove.
    existed_before = os.path.lexists(child)
    was_git_folder_before = False

    try:
        child.mkdir(parents=True, exist_ok=True)
        _init_child_gitdir(co, backend)
        _set_child_origin(co, resolved_url, backend)

        sha = _fetch_and_checkout(
            co, resolved_url, effective_ref, backend,
            depth=depth, single_branch=single_branch,
        )

        # Record the resolved state.
        state.save_checkout(co, {
            "resolved": sha,
            "ref": effective_ref,
            "url": url,
            "override": override,
        })

        _print_gitignore_recommendation(parent_root, child)
    except Exception:
        _cleanup_new_child(
            child, existed_before, was_git_folder_before, parent_root)
        raise


def _is_git_folder_child(co: layout.Checkout) -> bool:
    return (co.gitdir / "HEAD").is_file()


def init_git_folder(co: layout.Checkout, backend: GitBackend | None = None) -> None:
    """Create a fresh `.gf` gitdir for the checkout without a git-folder binding.

    Like `git init`, this leaves an empty child worktree ready for later use.
    """
    backend = backend or _default_backend()
    child = co.work_tree
    if _is_git_folder_child(co) or (child / ".git").exists():
        raise ValidationError(f"{child} is already a git or git-folder directory")

    # A store checkout's `co.gitdir` is the repo store's worktree-record
    # area, never a fresh child gitdir — initializing there plants a
    # recordless gitdir that wedges the checkout key. `in_gf_tree` adds
    # the same guard `init_child`/`update_child` carry: a leaf link into
    # any `.gf` tree, or any other spelling whose realpath lands inside
    # gf-managed storage, is never a fresh child either. `in_git_tree`
    # is the `.git` sibling — a fresh gitdir planted inside the parent's
    # repository metadata would wedge the repo's own gitdir, and under
    # `hooks/` it is executable content.
    if (
        co.is_store_checkout
        or layout.in_gf_tree(child)
        or layout.in_git_tree(child)
    ):
        raise ValidationError(
            f"{child} resolves inside gf-managed storage "
            f"({layout.GF_DIR}) or repository metadata (.git)")

    gitdir = co.gitdir
    # Same storage-real check as `_init_child_gitdir`: the appended
    # `.gf`/`git` components must be literal — a symlinked `.gf`
    # (committed in a hostile tree or planted by hand) would redirect
    # the mkdir and all gitdir writes outside the child's tree. The
    # child leaf may itself be a link.
    if not layout.whole_repo_gitdir_is_real(co):
        raise ValidationError(
            f"child gitdir path {gitdir} resolves through a symlink")
    gitdir.mkdir(parents=True, exist_ok=True)
    backend.git("init", git_dir=gitdir, work_tree=co.work_tree)

    # Make sure the child git ignores the .gf directory.
    exclude = gitdir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    patterns = set(exclude.read_text().splitlines()) if exclude.exists() else set()
    patterns.add(layout.GF_DIR)
    patterns.discard("")
    exclude.write_text("\n".join(sorted(patterns)) + "\n")


def recorded_url(co: layout.Checkout):
    """The checkout record's scalar `url`, when one is recorded.

    On a whole-repo child this is the binding's recorded resolution; on
    a shared checkout it is the last-served binding's spelling — kept
    for compatibility, with the authoritative per-binding resolutions in
    the `binding_urls` map (see `recorded_binding_url`).
    """
    return state.load_checkout(co).get("url")


def recorded_binding_url(co: layout.Checkout, binding_path: str):
    """This binding's recorded effective url, or None when unrecorded.

    Shared-checkout records key each served binding's effective `url` by
    its manifest `path` in the `binding_urls` map (spec L181). A
    checkout recorded before the map existed has no entry — its binding
    resolves like an unrecorded one.
    """
    urls = state.load_checkout(co).get("binding_urls")
    if isinstance(urls, dict):
        return urls.get(binding_path)
    return None


def recorded_url_matches(
    recorded,
    url: str,
    parent_root: Path,
) -> bool:
    """Whether a recorded resolution `recorded` is the effective `url`.

    `recorded` is the looked-up record — the scalar `url` field or this
    binding's `binding_urls` entry — so the same comparison serves a
    whole-repo child and a shared-checkout binding (spec URL
    resolution): an existing child whose recorded url matches is never
    re-resolved; an unrecorded or changed url still resolves. A local
    recorded spelling compares by resolved path identity so a relative
    `url` recorded by `gf clone` still matches its absolute form on pull.
    """
    if not isinstance(recorded, str):
        return False
    if recorded == url:
        return True
    if _is_remote_url(recorded) or _is_remote_url(url):
        return False
    return (parent_root / recorded).resolve() == Path(url).resolve()


def _stash_autostash(co: layout.Checkout, backend: GitBackend) -> None:
    """`git stash push -a` minus the worktree's `.gf` anchor.

    `-a` carries ignored files through the update (spec `gf pull`), but
    a bare `-a` would also stash `.gf`: every whole-repo child ignores
    it via `info/exclude`, and stashing it removes the very gitdir that
    records the stash — self-amputation, not preservation. The
    `:(top)`/`:(top,exclude).gf` pathspec keeps the root anchor in place
    while still carrying tracked, untracked, and ignored work. A `.gf`
    deeper in the tree is recorded in the surviving gitdir's stash and
    restored by the pop, so nothing there is lost either.
    """
    backend.git(
        "stash", "push", "-a", "-m", "gf autostash",
        "--", ":(top)", ":(top,exclude).gf",
        git_dir=co.gitdir, work_tree=co.work_tree, cwd=co.work_tree, stream=True,
    )


def _pop_autostash(co: layout.Checkout, backend: GitBackend) -> None:
    """Restore the `gf autostash` entry, index partition included.

    `stash pop --index` reapplies the staged/unstaged split the push
    recorded. A conflicting or failed pop leaves the stash entry in the
    stash list — the work is never dropped — and raises a named report
    that carries the manual recovery; the caller surfaces it as the pull
    failure itself (success path) or alongside it (failure path).
    """
    try:
        backend.git(
            "stash", "pop", "--index",
            git_dir=co.gitdir, work_tree=co.work_tree, cwd=co.work_tree, stream=True,
        )
    except GitError as e:
        raise GitError(
            f"autostash pop for {co.work_tree} conflicted or failed; the "
            f"'gf autostash' stash entry was kept — recovery is manual: "
            f"resolve conflicted paths and `git stash drop` the entry "
            f"once its content is landed, or clear the obstruction and "
            f"re-run `git stash pop --index` when nothing applied. {e}"
        ) from e


def update_child(
    co: layout.Checkout,
    url: str,
    ref: str,
    parent_root: Path,
    override: bool = False,
    rebase: bool = False,
    autostash: bool = False,
    backend: GitBackend | None = None,
    binding_path: str | None = None,
) -> None:
    backend = backend or _default_backend()
    child = co.work_tree
    # Anchor containment as a shelf-layer invariant: the resolved child
    # must stay inside the root that owns the binding (`parent_root` is
    # the pull planner's anchor), or the mkdir/rmtree writes below land
    # outside the workspace.
    if not Path(os.path.realpath(child)).is_relative_to(
        Path(os.path.realpath(parent_root))
    ):
        raise ValidationError(
            f"child path {child} resolves outside {parent_root}")
    effective_ref = ref or "latest"
    resolved_url = _resolved_git_url(url)
    if recorded_url_matches(recorded_url(co), url, parent_root):
        # The recorded `.gf/state` url IS this binding's resolution —
        # an existing whole-repo child is never re-resolved (spec URL
        # resolution).
        repo_url, subdir = resolved_url, ""
    else:
        try:
            repo_url, subdir = resolve_repo_url(url, parent_root, backend)
        except _GfInteriorError:
            # A `.gf` interior refusal is permanent, not undecidable:
            # pull surfaces the resolver's clear message rather than a
            # fetch failure on gf's private path.
            raise
        except GitFoldersError:
            # First resolution of a remote URL — an `init` placeholder or
            # any binding whose checkout was never established (no
            # resolvable HEAD) — propagates the resolver's `.git`-
            # boundary hint (spec L140 governs exactly this undecidable
            # case). Everything else keeps the fetch-failure surface: a
            # local path that contains no repository, and an established
            # whole-repo binding whose transient ls-remote failure should
            # report a fetch failure.
            if _is_remote_url(url) and backend.git(
                "rev-parse", "--verify", "HEAD",
                git_dir=co.gitdir, check=False,
            ).returncode != 0:
                raise
            repo_url, subdir = resolved_url, ""

    if subdir and not co.subdir:
        # The binding's consumer path is not yet a link: convert an
        # `init`-created placeholder child (a directory holding only
        # `.gf`, no commits) and route through the shared-store seams.
        # Any other existing content is left for ensure_consumer_link to
        # refuse without destroying it.
        strip_placeholder_child(child, backend)
        ensure_shared_binding(
            child, parent_root, repo_url, subdir, url, effective_ref,
            override=override, backend=backend, binding_path=binding_path,
        )
        return
    if subdir:
        # Established subfolder binding: the shared store's remote is the
        # repo URL, not the manifest's subdir spelling.
        resolved_url = repo_url

    if not _is_git_folder_child(co):
        # Same `.gf`-storage refusal as `init_child`: this arm plants a
        # fresh gitdir at `co.gitdir` — for a store checkout the repo
        # store's worktree-record area — so a consumer path resolving
        # into `.gf` is never bindable here. `in_git_tree` refuses the
        # `.git` sibling: a binding path reaching the parent's
        # repository metadata (`hooks/` executes on checkout). Only the
        # whole-repo arm refuses: the `subdir` arms above legitimately
        # resolve `child` into `.gf/wt`.
        if (
            co.is_store_checkout
            or layout.in_gf_tree(child)
            or layout.in_git_tree(child)
        ):
            raise ValidationError(
                f"child path {child} resolves inside gf-managed storage "
                f"({layout.GF_DIR}) or repository metadata (.git)")
        # Same occupancy gates as `init_child`: a dangling symlink is
        # occupancy (refuse cleanly rather than crash `child.mkdir`), and
        # a non-directory occupant precedes `iterdir`.
        if os.path.lexists(child) and not child.exists():
            raise ValidationError(f"child path {child} is a dangling symlink")
        if child.exists() and not child.is_dir():
            raise ValidationError(f"child path {child} exists and is not a directory")
        if child.exists() and any(child.iterdir()):
            raise ValidationError(f"child path {child} exists and is not a git-folder")

        existed_before = os.path.lexists(child)
        was_git_folder_before = False
        try:
            child.mkdir(parents=True, exist_ok=True)
            _init_child_gitdir(co, backend)
            _set_child_origin(co, resolved_url, backend)

            sha = _fetch_and_checkout(co, resolved_url, effective_ref, backend)

            state.save_checkout(co, {
                "resolved": sha,
                "ref": effective_ref,
                "url": url,
                "override": override,
            })
            _print_gitignore_recommendation(parent_root, child)
            return
        except Exception:
            _cleanup_new_child(
                child, existed_before, was_git_folder_before, parent_root)
            raise

    # Fail closed on an established whole-repo child whose `.gf`
    # resolves through a link: `co.gitdir` then names a foreign gitdir
    # (the donor's, typically), and everything below — the dirty-check
    # status, `_set_child_origin`'s config write, the fetch, the
    # checkout — would operate on the donor's storage. A store
    # checkout's gitdir lives under the parent root's `.gf` and is
    # guarded by its own storage-real checks, so only the whole-repo
    # shape is tested here.
    if not co.is_store_checkout and not layout.whole_repo_gitdir_is_real(co):
        raise ValidationError(
            f"child gitdir path {co.gitdir} resolves through a symlink")

    gitdir = co.gitdir
    # Unscoped dirty check: a dirty child refuses exit 3 — `gf` has no
    # flag that updates over uncommitted work (`gf pull` carries no
    # `--force`); `--autostash` preserves it through the update instead.
    dirty = backend.git_capture(
        "status", "--porcelain", git_dir=gitdir, work_tree=co.work_tree,
    ).strip()
    if dirty and not autostash:
        raise DirtyError(
            f"child {child} is dirty (uncommitted changes); commit or "
            f"stash them first, or pull with --autostash")

    # Unsafe legacy refspec state refuses before the stash and before
    # any refspec write or fetch against this gitdir (GF-D17) — a
    # refused pull never cycles a stash it did not need.
    _assert_safe_refspecs(co.common_dir, backend)

    stashed = False
    if dirty:
        # `-a`, not `-u`: ignored files are protected work the stash
        # must carry through the update — `-u` would leave them in
        # place for checkout/merge to overwrite. The push excludes the
        # `.gf` anchor, which `-a` would otherwise carry away along
        # with the gitdir recording the stash.
        _stash_autostash(co, backend)
        stashed = True

    try:
        _set_child_origin(co, resolved_url, backend)
        if rebase:
            sha = _fetch_and_rebase(
                co, resolved_url, effective_ref, backend,
                autostash=autostash)
        else:
            sha = _fetch_and_checkout(
                co, resolved_url, effective_ref, backend,
                autostash=autostash)
    except Exception:
        # The update failed AFTER a stash was taken — a failed origin URL
        # update included — so the stash is restored on the way out too;
        # it is never orphaned. A pop that itself fails leaves the named
        # `gf autostash` entry in place and reports it.
        if stashed:
            try:
                _pop_autostash(co, backend)
            except GitError as pop_err:
                print(f"gf: warning: {pop_err}", file=sys.stderr)
        raise

    if stashed:
        _pop_autostash(co, backend)

    # Record the resolved state (merge so shared-checkout keys such as
    # the sparse `bindings` list survive a pull).
    rec = state.load_checkout(co)
    rec.update({
        "resolved": sha,
        "ref": effective_ref,
        "url": url,
        "override": override,
    })
    state.save_checkout(co, rec)


def is_linked_child(child: Path, parent_root: Path) -> bool:
    """Return True if `child` is a symlink to a git-folder child outside `parent_root`."""
    if not child.exists():
        return False
    resolved = child.resolve()
    if resolved.is_relative_to(parent_root.resolve()):
        return False
    return _manifest.is_git_folder_child(resolved)


def _resolve_effective_sha_local(
    co: layout.Checkout,
    ref: str,
    backend: GitBackend | None = None,
) -> str:
    """Resolve the effective ref to a SHA using only local refs.

    Never touches the network. For `latest` it reads the local
    `refs/remotes/origin/HEAD` symbolic ref and falls back to
    `refs/remotes/origin/main` then `refs/remotes/origin/master`
    without calling `git remote set-head`. Used by `gf status
    --remote`, which must be local-only.
    """
    backend = backend or _default_backend()
    gitdir = co.common_dir
    effective_ref = ref or "latest"

    if effective_ref in ("latest", ""):
        # Prefer the local origin/HEAD symbolic ref, then fall back to
        # origin/main and origin/master. No `git remote set-head` or
        # other network call is made.
        head_ref = "refs/remotes/origin/HEAD"
        result = backend.git(
            "symbolic-ref", head_ref,
            git_dir=gitdir, check=False,
        )
        if result.returncode == 0:
            target = result.stdout.strip()
            if target and backend.git(
                "show-ref", "--verify", target,
                git_dir=gitdir, check=False,
            ).returncode == 0:
                return backend.git_capture(
                    "rev-parse", target,
                    git_dir=gitdir,
                ).strip()
        for candidate in ("refs/remotes/origin/main", "refs/remotes/origin/master"):
            if backend.git(
                "show-ref", "--verify", candidate,
                git_dir=gitdir, check=False,
            ).returncode == 0:
                return backend.git_capture(
                    "rev-parse", candidate,
                    git_dir=gitdir,
                ).strip()
        raise ValidationError(
            f"could not resolve 'latest' for {co.work_tree}: no local "
            f"refs/remotes/origin/HEAD, main, or master; run `gf pull` "
            f"or `gf git fetch` first"
        )

    # Branch ref: resolve the remote tracking branch locally.
    if is_branch(co, effective_ref, backend):
        return _resolve_remote_branch(co, effective_ref, backend)

    # Tag or commit: resolve via local refs only.
    return resolve_ref(co, effective_ref, backend)


def binding_scope(
    parent_root: Path,
    folder: dict,
    co: layout.Checkout,
) -> list[str] | None:
    """Repo-relative pathspecs scoping a store-linked binding's porcelain.

    The binding's RECORDED consumer path resolved through the checkout:
    the link realpath position relative to `co.work_tree` — never
    positional `co.subdir`, which is "" when the link lands at the
    checkout root and over-narrow when `co` was resolved from a cwd deeper
    than the link root. A checkout-level or dangling link falls back to
    the recorded `bindings` union in the checkout's state. Returns None
    for whole-repo checkouts and when no recorded scope exists.
    """
    if not co.is_store_checkout:
        return None
    rp = Path(os.path.realpath(parent_root / folder["path"]))
    if rp != co.work_tree and rp.is_relative_to(co.work_tree):
        return [rp.relative_to(co.work_tree).as_posix()]
    rec = state.load_checkout(co)
    subs = [b for b in rec.get("bindings", []) if isinstance(b, str) and b]
    return subs or None


def scoped_porcelain(
    co: layout.Checkout,
    paths: list[str],
    backend: GitBackend | None = None,
) -> str:
    """`git status --porcelain -- <paths>` anchored at `co.work_tree`.

    Running the status with `cwd` at the worktree root pins the pathspec
    prefix — a cwd deeper than the link root cannot narrow or relocate it
    — and yields worktree-relative output spellings (`docs/api/x.txt`).
    When the worktree directory is gone, git cannot run status on it at
    all; the empty string stands in for "no recorded dirt" — `drift`
    reports the checkout itself as `missing`.

    `GIT_LITERAL_PATHSPECS=1` takes every operand after `--` literally:
    recorded subdirs are pathspec input, so a directory spelled
    `:(literal)foo` or `app/[id]` would otherwise parse as magic/glob
    and silently un-scope the run. Equivalent to a `:(literal)<path>`
    prefix per operand (every operand here is a recorded subdir) while
    leaving argv untouched for backends that match the
    `("status", "--porcelain")` head.
    """
    backend = backend or _default_backend()
    if not co.work_tree.is_dir():
        return ""
    return backend.git_capture(
        "status", "--porcelain", "--", *paths,
        git_dir=co.gitdir, work_tree=co.work_tree, cwd=co.work_tree,
        env={"GIT_LITERAL_PATHSPECS": "1"},
    )


def drift(
    co: layout.Checkout,
    ref: str,
    backend: GitBackend | None = None,
    paths: list[str] | None = None,
) -> str:
    """Classify the drift state of the checkout against its effective ref.

    Returns one of `clean`, `ahead`, `behind`, `diverged`, or `missing`;
    the four resolved relations gain a `-dirty` suffix when the worktree
    has uncommitted changes, with `local-dirty` naming the matching-HEAD
    dirty case. `missing` is never suffixed.

    This is a local-only operation. It never calls `git fetch`,
    `git remote`, `git ls-remote`, or any other network command. The
    effective ref is resolved against the local remote-tracking refs
    (`refs/remotes/origin/<branch>`), local tags, and local commits
    already present in the checkout's store — the same refs `git status`
    compares against after a `git fetch`. Run `gf pull` or `gf git fetch`
    first to refresh those refs.

    - `missing`: the checkout has no `HEAD` in its gitdir, its worktree
      directory is gone, a whole-repo child's `.gf` resolves through a
      link (the spelled gitdir is foreign storage, so nothing may be
      read from it), or — for a store checkout resolved through a
      binding's consumer path — the mapped subdirectory
      `co.work_tree/co.subdir` no longer exists (e.g. upstream removed
      the mapped directory and `pull` checked out the removal, leaving
      the consumer link dangling).
    - The effective ref is resolved to the SHA `pull` would check out
      (the remote tracking branch tip for branch/latest refs).
    - The checkout `HEAD` and `git status --porcelain` are read, then
      the history relation is classified by rev-list counts
      (`git rev-list --left-right --count HEAD...<sha>` semantics — the
      count of commits each side carries that the other lacks). An
      unborn HEAD (a child with no commits yet) counts as zero commits
      ahead of anything, so it reports `behind` rather than raising a
      git error.
    - `clean`: neither side has commits the other lacks.
    - `ahead`: HEAD has commits the resolved SHA lacks and the resolved
      SHA has none HEAD lacks.
    - `behind`: the resolved SHA has commits HEAD lacks and HEAD has
      none the resolved SHA lacks.
    - `diverged`: each side has commits the other lacks.
    - `-dirty` variants: the same relations with a dirty worktree.

    If the effective ref cannot be resolved locally, raises
    `GitFoldersError` instead of returning a state, so `cmd_status` can
    surface a deterministic exit code.
    """
    backend = backend or _default_backend()
    gitdir = co.gitdir
    # Degrade, don't fail closed: a whole-repo child whose `.gf`
    # resolves through a link has no gitdir of its own, and reading
    # through it would report the donor's HEAD/status as this child's.
    # A store checkout's gitdir lives under the parent's `.gf` and is
    # unaffected by the child's own `.gf` spelling.
    if (
        not (gitdir / "HEAD").is_file()
        or not co.work_tree.is_dir()
        or (
            not co.is_store_checkout
            and not layout.whole_repo_gitdir_is_real(co)
        )
    ):
        return "missing"
    # A subfolder binding's mapped directory is part of the checkout
    # root's worktree: upstream can remove it (e.g. `git rm -r` of the
    # mapped dir) while the shared checkout root itself stays alive.
    # The binding's content is then gone — the consumer link dangles —
    # so the binding is `missing`, not `clean`. Whole-repo checkouts and
    # checkout-root maps (`co.subdir == ""`) are already covered by the
    # worktree check above.
    if (
        co.is_store_checkout
        and co.subdir
        and not (co.work_tree / co.subdir).is_dir()
    ):
        return "missing"

    resolved_sha = _resolve_effective_sha_local(co, ref, backend)

    head = backend.git(
        "rev-parse", "HEAD", git_dir=gitdir, work_tree=co.work_tree,
        check=False,
    )
    head_sha = head.stdout.strip() if head.returncode == 0 else ""

    if paths is None:
        porcelain = backend.git_capture(
            "status", "--porcelain", git_dir=gitdir, work_tree=co.work_tree,
        ).strip()
    else:
        # Per-binding scope: dirt elsewhere in a shared checkout must not
        # mark a clean sibling `local-dirty`.
        porcelain = scoped_porcelain(co, paths, backend).strip()
    dirty = bool(porcelain)

    if head_sha:
        ahead, behind = _ahead_behind(co, resolved_sha, backend)
        if ahead < 0:
            raise GitError(
                f"could not classify drift for {co.work_tree}: "
                f"git rev-list HEAD...{resolved_sha} failed")
    else:
        # An unborn HEAD carries zero commits ahead of anything; the
        # behind count is every commit the resolved SHA reaches — a
        # resolved SHA names a commit, so it is always behind.
        result = backend.git(
            "rev-list", "--count", resolved_sha,
            git_dir=gitdir, work_tree=co.work_tree, check=False,
        )
        if result.returncode != 0:
            raise GitError(
                f"could not classify drift for {co.work_tree}: "
                f"git rev-list --count {resolved_sha} failed")
        try:
            behind = int(result.stdout.strip())
        except ValueError:
            raise GitError(
                f"could not classify drift for {co.work_tree}: "
                f"unexpected rev-list output {result.stdout.strip()!r}"
            ) from None
        ahead = 0

    if ahead > 0 and behind > 0:
        relation = "diverged"
    elif ahead > 0:
        relation = "ahead"
    elif behind > 0:
        relation = "behind"
    else:
        relation = "clean"

    if not dirty:
        return relation
    # `local-dirty` names the matching-HEAD dirty case; `missing` is
    # returned above and never carries a suffix.
    return "local-dirty" if relation == "clean" else f"{relation}-dirty"


def list_parent_worktrees(parent_root: Path, backend: GitBackend) -> list[dict]:
    """Run `git worktree list --porcelain` and parse it into a list of worktree dicts.

    Each dict has keys: `path` (str), `head` (str, may be empty), and
    `branch` (str|None, None for a detached HEAD).
    """
    out = backend.git_capture("worktree", "list", "--porcelain", cwd=parent_root)
    worktrees: list[dict] = []
    current: dict | None = None
    for line in out.splitlines():
        if not line.strip():
            if current is not None:
                worktrees.append(current)
                current = None
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value, "head": "", "branch": None}
        elif key == "HEAD" and current is not None:
            current["head"] = value
        elif key == "branch" and current is not None:
            # git emits `branch refs/heads/<name>`; keep only the short name.
            ref = value
            if ref.startswith("refs/heads/"):
                ref = ref[len("refs/heads/"):]
            current["branch"] = ref
        elif key == "detached" and current is not None:
            current["branch"] = None
    if current is not None:
        worktrees.append(current)
    return worktrees


def linked_git_folders_in_worktree(
    worktree_path: Path, manifest_data: dict
) -> list[tuple[str, str]]:
    """Return `[(name, relative_link_target), ...]` for git-folders linked into `worktree_path`.

    A git-folder is "linked" when its child path inside the worktree is a
    relative symlink to a git-folder child outside the worktree.
    """
    links: list[tuple[str, str]] = []
    for folder in manifest_data.get("git_folder", []):
        child = worktree_path / folder["path"]
        if not child.is_symlink():
            continue
        target = os.readlink(child)
        if os.path.isabs(target):
            continue
        resolved = child.resolve()
        if resolved.is_relative_to(worktree_path.resolve()):
            continue
        if not _manifest.is_git_folder_child(resolved):
            continue
        links.append((folder["name"], target))
    return links


def linked_git_folder_symlinks_in_worktree(
    worktree_path: Path, manifest_data: dict
) -> list[tuple[Path, str]]:
    """Return `[(child_path, link_target), ...]` for git-folder symlinks in `worktree_path`.

    Each `child_path` is the unresolved path inside the worktree that is a
    relative symlink to a git-folder child outside the worktree.
    `link_target` is the raw relative link target (as read by `os.readlink`),
    suitable for restoring the symlink with `os.symlink`.
    """
    links: list[tuple[Path, str]] = []
    for folder in manifest_data.get("git_folder", []):
        child = worktree_path / folder["path"]
        # A mid-path symlink the checkout materialized (a committed
        # `vendor -> /abs` link) puts the leaf outside the worktree:
        # skip it — worktree removal proceeds with outside leaves
        # untouched. A leaf whose parent chain lands inside `.git`
        # metadata is skipped the same way: unlinking there would edit
        # the repo's own config/hooks. The leaf's own realpath is never
        # tested here; a live consumer link resolves into `.gf/wt`
        # legitimately.
        child_parent = Path(os.path.realpath(child.parent))
        if (
            not child_parent.is_relative_to(
                Path(os.path.realpath(worktree_path)))
            or layout.in_git_tree(child_parent)
        ):
            continue
        if not child.is_symlink():
            continue
        target = os.readlink(child)
        if os.path.isabs(target):
            continue
        resolved = child.resolve()
        if resolved.is_relative_to(worktree_path.resolve()):
            continue
        if not _manifest.is_git_folder_child(resolved):
            continue
        links.append((child, target))
    return links


def unlink_linked_git_folders_in_worktree(
    worktree_path: Path, manifest_data: dict
) -> list[str]:
    """Remove the relative symlinks for linked git-folders inside `worktree_path`.

    Returns the list of git-folder names whose symlinks were removed. The
    source children outside the worktree are not touched.
    """
    names_by_path = {
        (worktree_path / f["path"]): f["name"]
        for f in manifest_data.get("git_folder", [])
    }
    removed: list[str] = []
    for child, _target in linked_git_folder_symlinks_in_worktree(worktree_path, manifest_data):
        os.unlink(child)
        removed.append(names_by_path[child])
    return removed


def _linked_gitfile_plan(git_dir: Path) -> list[tuple[str, Path]]:
    """Reconcile every registered linked-worktree gitfile before a move.

    `<git_dir>/worktrees/<n>/gitdir` names the external worktree's `.git`
    FILE — a small file containing `gitdir: <git_dir>/worktrees/<n>`.
    The gitdir's move leaves that spelling stale, so every record is
    preflighted here while the gitdir still sits at the old path, and
    any unreconcilable record refuses with the truthful class:

    - missing record: `<n>/gitdir` itself is absent — the record cannot
      name its external gitfile;
    - stale target: the external `.git` file is gone, is not a regular
      file, or names a path other than `git_dir/worktrees/<n>` (a
      re-registered or foreign worktree);
    - present-but-unwritable: the file names the old path but cannot be
      written.

    The refusal names the offending record and the recovery — `git
    worktree prune` drops a dead record, `git worktree repair` rebuilds
    a live one's linkage — so the binding stays fully gf-managed.
    Returns `(record_name, external_gitfile)` pairs for the post-move
    rewrite.
    """
    records_dir = git_dir / "worktrees"
    plan: list[tuple[str, Path]] = []
    if not records_dir.is_dir():
        return plan
    problems: list[str] = []
    for rec in sorted(p for p in records_dir.iterdir() if p.is_dir()):
        rec_gitdir = rec / "gitdir"
        if not rec_gitdir.is_file():
            problems.append(
                f"{rec}: missing 'gitdir' record file — cannot name the "
                f"worktree's external .git")
            continue
        try:
            ext_gitfile = Path(rec_gitdir.read_text().strip())
        except OSError as e:
            problems.append(f"{rec_gitdir}: unreadable record ({e})")
            continue
        expected = git_dir / "worktrees" / rec.name
        if not ext_gitfile.is_file():
            problems.append(
                f"{rec_gitdir}: gitfile {ext_gitfile} is missing or not "
                f"a regular file (stale target)")
            continue
        try:
            content = ext_gitfile.read_text()
        except OSError as e:
            problems.append(f"{ext_gitfile}: unreadable ({e})")
            continue
        first = content.splitlines()[0] if content.splitlines() else ""
        m = re.match(r"^gitdir:\s*(.*?)\s*$", first)
        if (
            m is None
            or os.path.realpath(m.group(1))
            != os.path.realpath(expected)
        ):
            problems.append(
                f"{ext_gitfile}: names a different gitdir than "
                f"{expected} (stale or foreign target)")
            continue
        if not os.access(ext_gitfile, os.W_OK):
            problems.append(
                f"{ext_gitfile}: present but not writable — the "
                f"converted path cannot be recorded in it")
            continue
        plan.append((rec.name, ext_gitfile))
    if problems:
        raise ValidationError(
            f"cannot reconcile linked-worktree records in {records_dir} "
            f"for the {git_dir} → {git_dir.parent.parent / '.git'} move: "
            + "; ".join(problems)
            + "; drop dead records with `git --git-dir "
            + str(git_dir)
            + " worktree prune` or repair live ones with `git worktree "
            + "repair`, then retry")
    return plan


def _rewrite_linked_gitfiles(
    plan: list[tuple[str, Path]], new_git_dir: Path
) -> list[tuple[Path, str]]:
    """Repoint each reconciled external gitfile at `new_git_dir`.

    Runs after the `.gf/git` → `.git` move; a failure is a straggler —
    the converted checkout is intact and the record's gitfile still
    points at the old path, recoverable with `git worktree repair`
    (GF-TRB-13). Returns `(gitfile, detail)` pairs for the caller to
    report; never raises — the move itself already happened.
    """
    strays: list[tuple[Path, str]] = []
    for rec_name, ext_gitfile in plan:
        target = new_git_dir / "worktrees" / rec_name
        try:
            ext_gitfile.write_text(f"gitdir: {target}\n")
        except OSError as e:
            strays.append((ext_gitfile, str(e)))
    return strays


def _convert_whole_repo_gitdir(co: layout.Checkout) -> list[tuple[Path, str]]:
    """Move `child/.gf/git` to `child/.git`, worktree linkage reconciled.

    `core.worktree` and each record's `commondir` are path-stable across
    the move — the absolute worktree path and the `../..` relative
    spelling survive verbatim — while each external worktree's `.git`
    file holds the absolute old `worktrees/<n>` path and must be
    rewritten (GF-D19). `_linked_gitfile_plan` refuses before the move
    when any record cannot be reconciled, so a refused `rm` leaves the
    binding fully gf-managed; `_rewrite_linked_gitfiles` runs after the
    move and returns the straggler list for reporting.
    """
    git_dir = co.gitdir
    new_dir = co.work_tree / ".git"
    plan = _linked_gitfile_plan(git_dir)
    try:
        shutil.move(str(git_dir), str(new_dir))
    except OSError as e:
        raise ValidationError(
            f"cannot move {git_dir} to {new_dir}: {e}") from e
    return _rewrite_linked_gitfiles(plan, new_dir)


def _teardown_gf_dir(co: layout.Checkout) -> None:
    """Remove the provably gf-owned parts of a converted child's `.gf`.

    `.gf/git` already moved to `.git`; `.gf/state` is this binding's own
    record and is unlinked. `.gf` itself is removed only when nothing
    else remains — a child that is itself a parent root legitimately
    holds `.gf/wt` and `.gf/repos` for bindings it declares, and any
    planted or foreign entry is retained and reported rather than
    recursively removed (GF-D19, GF-D22).
    """
    gf_dir = co.work_tree / layout.GF_DIR
    state_file = gf_dir / "state"
    if state_file.is_file() or os.path.islink(state_file):
        try:
            state_file.unlink()
        except OSError as e:
            raise ValidationError(
                f"cannot remove {state_file}: {e}") from e
    if not gf_dir.is_dir():
        return
    try:
        leftovers = sorted(p.name for p in gf_dir.iterdir())
    except OSError as e:
        raise ValidationError(
            f"cannot list {gf_dir}: {e}") from e
    if not leftovers:
        try:
            gf_dir.rmdir()
        except OSError as e:
            raise ValidationError(
                f"cannot remove {gf_dir}: {e}") from e
        return
    print(
        f"gf: kept {gf_dir}: it still holds content not provably this "
        f"binding's ({', '.join(leftovers)}); retained — remove it by "
        f"hand once you have checked it",
        file=sys.stderr,
    )


def remove_child(child: Path, parent_root: Path) -> None:
    """Unregister a git-folder child, preserving its worktree.

    A subfolder binding whose consumer link `parent_root` owns loses only
    the link: the checkout — including uncommitted work — its sparse cone,
    its per-checkout state record, and the repo store are untouched
    (GF-D9, GF-D14). A whole-repo child is converted, not deleted: its
    `.gf/git` moves to `.git` — `HEAD`, index, refs (the
    `refs/worktree/gf-retained` retention ref included), config and
    `origin` all intact, `core.worktree` absolute and path-stable — and
    registered linked worktrees keep resolving through rewritten
    gitfiles. A missing or dangling consumer link still unregisters:
    absence of the link is not absence of the binding's work.
    """
    # `<root>/.gf` is gf's own storage — never a removable child: a
    # corrupted or hand-edited manifest path must not let `gf rm` modify
    # it. Test the `..`-normalized spelling and the resolved parent chain
    # (a leaf link inside `.gf` is still `.gf` content either way).
    gf_root = parent_root / layout.GF_DIR
    if Path(os.path.normpath(child)).is_relative_to(
        Path(os.path.normpath(gf_root))
    ) or (Path(os.path.realpath(child.parent)) / child.name).is_relative_to(
        Path(os.path.realpath(gf_root))
    ):
        raise ValidationError(
            f"{child} names a path inside {gf_root}; gf storage is not a "
            f"removable child"
        )
    if child.is_symlink():
        if layout.owns_consumer_link(parent_root, child):
            try:
                child.unlink()
            except OSError as e:
                raise ValidationError(
                    f"cannot remove consumer link {child}: {e}") from e
            return
        raise GitFoldersError(
            f"{child} is a symlinked child; remove it from the owning worktree instead"
        )
    if child.exists() and child.resolve() != child:
        raise GitFoldersError(
            f"{child} is a symlinked child; remove it from the owning worktree instead"
        )
    # A missing consumer path unregisters without touching storage —
    # the link's absence says nothing about the binding's retained work.
    if not child.is_dir():
        return

    gf_dir = child / layout.GF_DIR
    co = layout.resolve_checkout(child)
    # Fail closed — `gf rm` must never move or delete a foreign gitdir.
    # A `.gf` swapped for a link (or a store-co-shaped resolution, whose
    # gitdir legitimately sits under a root's `.gf/repos`) fails the
    # compare, so the move below can never carry the donor's gitdir to
    # `child/.git` and the teardown cannot follow the link.
    if not layout.whole_repo_gitdir_is_real(co):
        raise ValidationError(
            f"child gitdir path {co.gitdir} resolves through a symlink")
    git_dir = co.gitdir
    if git_dir.is_dir():
        if (child / ".git").exists():
            raise GitFoldersError(f"{child} already contains a .git directory")
        strays = _convert_whole_repo_gitdir(co)
        for stray_path, detail in strays:
            print(
                f"gf: warning: linked-worktree gitfile {stray_path} was "
                f"not repointed ({detail}) and still names the old "
                f".gf/git location; repair it inside the converted "
                f"folder with `git -C {child} worktree repair "
                f"{stray_path.parent}` (GF-TRB-13)",
                file=sys.stderr,
            )
    _teardown_gf_dir(co)


# --- shared store / checkout / consumer-link machinery ----------------------

_WILDCARD_FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"


def _branch_refspec(branch: str) -> str:
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"


def _live_refspec_lines(store: Path, backend: GitBackend) -> list[str]:
    """The store's live `remote.origin.fetch` values — the coverage record."""
    result = backend.git(
        "config", "--get-all", "remote.origin.fetch",
        git_dir=store, check=False,
    )
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def _assert_safe_refspecs(store: Path, backend: GitBackend) -> None:
    """Refuse a live fetch refspec that can move user-owned refs.

    `remote.origin.fetch` lines are append-only and a leading `+`
    (forced update) is legitimate only on a destination inside gf's
    remote-tracking mirror `refs/remotes/origin/*` (GF-D17): every line
    gf itself writes lands there. A forced line whose destination sits
    anywhere else — `refs/heads/*`, `refs/tags/*`, `HEAD` — would move
    user-owned refs on every fetch, so it refuses before any fetch or
    write runs against the gitdir. A colon-less refspec has no
    destination and cannot move a ref, so it does not refuse.

    The offending line is named in the refusal and never rewritten or
    removed — repairing a hand-edited or foreign line is the user's own
    `git config` call, not a silent migration.
    """
    for line in _live_refspec_lines(store, backend):
        spec = line.strip()
        if not spec.startswith("+") or ":" not in spec:
            continue
        dest = spec.split(":", 1)[1]
        if not dest.startswith("refs/remotes/origin/"):
            raise ValidationError(
                f"remote.origin.fetch line '{spec}' in {store} "
                f"force-fetches into user-owned refs (destination "
                f"'{dest}' is outside refs/remotes/origin/*); refusing "
                f"to fetch or write against this gitdir — repair or "
                f"remove the line with `git config`")


_REJECTED_REF_RE = re.compile(r"!\s+\[rejected\]\s+(\S+)\s+->")


def _fetch_guarded(
    backend: GitBackend, git_dir: Path, *args: str
) -> None:
    """`git fetch` with a rejected ref update surfaced as a refusal.

    A non-forced `refs/tags/<ref>` coverage line makes the fetch decline
    a tag that upstream moved — git prints `! [rejected] <name> ->
    <name>  (would clobber existing tag)` and exits nonzero. That is a
    divergence refusal (exit 1), not a transport failure (exit 2): the
    local ref is preserved untouched and the user resolves the move
    deliberately. Any other fetch failure propagates unchanged as the
    `GitError` it already is.
    """
    try:
        backend.git(*args, git_dir=git_dir, stream=True)
    except GitError as e:
        hits = _REJECTED_REF_RE.findall(str(e))
        if hits:
            raise ValidationError(
                f"fetch into {git_dir} declined a moved ref: upstream's "
                f"{', '.join(hits)} differs from the local copy, which "
                f"was preserved — resolve it deliberately (inspect, "
                f"rename, delete, or re-point it with your own git) and "
                f"pull again") from e
        raise


def _ensure_branch_coverage(
    store: Path, branch: str, backend: GitBackend
) -> None:
    """Append `branch`'s fetch refspec line when no live line covers it.

    Refspec lines are append-only (spec Fetch refspecs): existing lines
    are never rewritten or removed, and the wildcard already covers every
    branch, so a covered branch records nothing.
    """
    line = _branch_refspec(branch)
    lines = _live_refspec_lines(store, backend)
    if _WILDCARD_FETCH_REFSPEC not in lines and line not in lines:
        backend.git(
            "config", "--add", "remote.origin.fetch", line,
            git_dir=store,
        )


def _ensure_pinned_ref(
    store: Path, repo_url: str, ref: str, backend: GitBackend
) -> None:
    """Cover a non-branch `ref` the store's fetch refspecs may not reach.

    A tag or bare commit never matches a `refs/heads/*` refspec line, and
    git's tag auto-follow only lands tags pointing into fetched history —
    so a store narrowed by `--single-branch` (or a whole-repo child gitdir
    narrowed the same way) cannot reach a tag whose commit lives on an
    unfetched branch, or a commit no advertised ref names. Two mechanisms,
    run before the caller's store fetch so coverage lands with it:

    - a 40-hex commit: `cat-file -e` probes the store; on a miss one
      `git fetch origin <sha>` pulls it — no refspec names a bare sha,
      and a server without allow-reachable-sha1-in-want may refuse (that
      limit is git's own; local upstreams always allow).
    - a tag: `rev-parse refs/tags/<ref>^{}` verifies the store's copy;
      on a miss one `ls-remote` probes `refs/tags/<ref>` upstream and,
      when advertised, `config --add` appends the tag's refspec line —
      append-only like every line — so the caller's fetch lands it. A
      landed tag persists, so later bindings do not re-cover it.

    `latest`/"" and refs the store already holds as branches stay with
    the branch machinery (`_ensure_branch_coverage`, the wildcard); a
    ref upstream advertises as neither branch nor tag is left for the
    caller's `could not resolve ref` error — unchanged.
    """
    if ref in ("latest", ""):
        return
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        if backend.git(
            "cat-file", "-e", ref, git_dir=store, check=False,
        ).returncode != 0:
            backend.git(
                "fetch", "--no-tags", "origin", ref,
                git_dir=store, stream=True,
            )
        return
    if backend.git(
        "rev-parse", f"refs/tags/{ref}^{{}}",
        git_dir=store, check=False,
    ).returncode == 0:
        return
    for candidate in (f"refs/heads/{ref}", f"refs/remotes/origin/{ref}"):
        if backend.git(
            "show-ref", "--verify", candidate,
            git_dir=store, check=False,
        ).returncode == 0:
            return
    if _upstream_has_tag(repo_url, ref, backend):
        # Non-forced: a tag must never be re-pointed by a fetch — a moved
        # upstream tag is a refusal condition for the caller, not
        # something `gf` silently tracks (GF-D17).
        backend.git(
            "config", "--add", "remote.origin.fetch",
            f"refs/tags/{ref}:refs/tags/{ref}",
            git_dir=store,
        )


def _fetch_store(
    store: Path, backend: GitBackend, depth: int | None = None
) -> None:
    """Fetch `origin` into the repo store, blob-filtered with fallback.

    When the server refuses a filtered fetch, warn and retry unfiltered.
    `depth` is the clone's `--depth=<n>` for the fetch that creates the
    store; joins and pulls pass None and fetch normally.
    """
    shallow = [f"--depth={depth}"] if depth else []
    try:
        _fetch_guarded(
            backend, store,
            "fetch", "--filter=blob:none", "--no-tags", *shallow, "origin",
        )
        return
    except ValidationError:
        # A moved-ref rejection is not a filter refusal: surface it,
        # do not burn the fallback retry on it.
        raise
    except GitError:
        print(
            "gf: warning: remote refused a filtered fetch; "
            "falling back to a full fetch",
            file=sys.stderr,
        )
    _fetch_guarded(backend, store, "fetch", "--no-tags", *shallow, "origin")


def _assert_store_origin(
    backend: GitBackend, store: Path, repo_url: str
) -> None:
    """Refuse an existing repo store whose origin is not this binding's.

    `remote.origin.url` was written once at store creation from the
    binding's resolved URL, and no legitimate gf path rewrites it
    (whole-repo children re-set their own child origin each pull, never
    the shared store's). A rewritten origin — a hand edit, a foreign
    repo-key directory, a store copied over another binding's — means
    every refspec write and `fetch origin` below would run against
    storage gf never configured.

    The comparison is `repo_key` equality — "spellings gf keys to one
    store" — not textual equality: a repository legitimately joins its
    store under sibling spellings the store keeps only one of (the
    `…/r` ≡ `…/r.git` transport alias, or the creating spelling versus
    this binding's). Any origin that keys differently names a different
    repository and refuses.
    """
    result = backend.git(
        "config", "--get", "remote.origin.url",
        git_dir=store, check=False,
    )
    expected = _resolved_git_url(repo_url)
    actual = result.stdout.strip()
    if result.returncode != 0 or (
        layout.repo_key(actual) != layout.repo_key(expected)
    ):
        raise ValidationError(
            f"repo store {store} remote.origin.url '{actual}' no longer "
            f"matches the binding's recorded resolution '{expected}' — "
            f"refusing storage gf did not configure")


def ensure_repo_store(
    store: Path,
    url: str,
    *,
    branch: str | None = None,
    ref: str | None = None,
    pins: Iterable[str] = (),
    single_branch: bool = False,
    depth: int | None = None,
    backend: GitBackend | None = None,
) -> None:
    """Ensure the bare repo store for `url` exists at `store` with coverage.

    On first creation this runs `git init --bare`, records
    `remote.origin.url` as the fetchable URL (a local `gf`-child
    boundary resolves to its inner gitdir via `_resolved_git_url`;
    other URL resolution and keying normalization stay the caller's
    job), writes the first fetch refspec — the branch's line for
    `--single-branch`, else the wildcard — covers a pinned non-branch
    `ref` via `_ensure_pinned_ref` when no branch was resolved, and then
    every ref in `pins` the same way (`pins` is the grouped pull's
    extension of the single `ref` slot: several bindings share the one
    creating fetch, so each of their non-branch refs gets the coverage —
    a tag's refspec line is `--add`ed so the creating fetch lands it, a
    missing commit pulls its own one-shot `fetch origin <sha>`), fetches
    blob-filtered with a full-fetch fallback (`depth` applies only to
    this creating fetch), and
    repoints the store's `HEAD` at `refs/remotes/origin/HEAD` so the bare
    default symref cannot claim a branch a linked checkout wants to
    attach.

    Joining an existing store appends the requested branch's refspec line
    via `config --add` only when no live `remote.origin.fetch` line covers
    it — lines are append-only, never rewritten or removed — and covers a
    non-branch `ref` (tag/commit) via `_ensure_pinned_ref`, then runs
    one `git fetch --filter=blob:none origin` (full-fetch fallback) before
    the caller resolves the effective ref; a join never skips the fetch.
    `pins` is create-arm only — join-side coverage for grouped bindings
    stays in the caller's coverage loop.
    """
    # `.gf`, `repos`, the repo-key dir and `git` must all be literal:
    # `repo_store` realpaths the root but appends those segments
    # lexically, so a symlink planted at any of them redirects the mkdir,
    # `git init` and every fetch through the link — a committed or
    # hand-placed `.gf` link would write the store outside the root.
    # Refuse before the HEAD probe so both create and join arms are
    # covered; read-side resolution paths keep working under a hostile
    # `.gf` by design.
    if not layout.storage_is_real(store):
        raise ValidationError(
            f"repo store path {store} resolves through a symlink")
    backend = backend or _default_backend()
    if not (store / "HEAD").is_file():
        store.mkdir(parents=True, exist_ok=True)
        backend.git("init", "--bare", git_dir=store)
        # `init --bare` preserves a pre-existing config: a store dir that
        # predates this call can carry a hostile `remote.origin.fetch`
        # line, which refuses before gf writes its own coverage.
        _assert_safe_refspecs(store, backend)
        backend.git(
            "config", "remote.origin.url", _resolved_git_url(url),
            git_dir=store,
        )
        line = _branch_refspec(branch) if single_branch and branch \
            else _WILDCARD_FETCH_REFSPEC
        backend.git("config", "remote.origin.fetch", line, git_dir=store)
        # A pinned tag/commit ref is outside every branch refspec line:
        # cover it before the creating fetch so the tag's `--add`ed line
        # lands with it (a missing commit pulls its own one-shot
        # `fetch origin <sha>`). The helper must run after the plain
        # write above: on the still-absent key its `--add` would leave
        # the tag's line single-valued, and the plain write would then
        # silently replace it.
        if not branch and ref:
            _ensure_pinned_ref(store, url, ref, backend)
        for pin in pins:
            _ensure_pinned_ref(store, url, pin, backend)
        _fetch_store(store, backend, depth=depth)
        try:
            backend.git(
                "remote", "set-head", "origin", "-a", git_dir=store,
                check=False,
            )
            backend.git(
                "symbolic-ref", "HEAD", "refs/remotes/origin/HEAD",
                git_dir=store,
            )
        except GitError:
            # Best-effort; the remote's default branch may be unfetchable.
            pass
        return
    # Join arm only: the store must still be the one this binding's
    # resolution configured — refuse before any refspec write or fetch
    # runs against a retargeted origin — and a pre-existing forced
    # refspec into user-owned refs refuses the same way (GF-D17).
    _assert_store_origin(backend, store, url)
    _assert_safe_refspecs(store, backend)
    if branch:
        _ensure_branch_coverage(store, branch, backend)
    elif ref is not None:
        _ensure_pinned_ref(store, url, ref, backend)
    _fetch_store(store, backend)


def _checkout_record_valid(co: layout.Checkout) -> bool:
    """True when the worktree record for `co` exists and points at its
    checkout: the admin dir's `gitdir` file names `<work_tree>/.git`
    (never trust the admin dir's name alone)."""
    gitdir_file = co.gitdir / "gitdir"
    if not gitdir_file.is_file():
        return False
    try:
        text = gitdir_file.read_text().strip()
    except (OSError, ValueError):
        # An unreadable or non-UTF-8 record cannot prove it names this
        # checkout — count it absent (the same refusal envelope a
        # missing record gets) rather than escaping as a traceback.
        return False
    return Path(text) == co.work_tree / ".git"


def _checkout_record_dir(co: layout.Checkout) -> Path | None:
    """The worktree record `co`'s own `git worktree add` created.

    The created record is not always `co.gitdir`: when that name is
    already taken under the store's worktree records (e.g. by a foreign
    worktree), `worktree add` picks a suffixed name like `<key>1` and
    writes it into the checkout's `.git` gitfile. Resolve the gitfile's
    `gitdir:` line first; once the gitfile is gone, `co.gitdir` counts
    only while its own `gitdir` file names this work tree. Returns None
    when no created record is identifiable — a record naming a different
    work tree is never resolved, so callers can never rmtree a foreign
    record or the repo store itself.
    """
    gitfile = co.work_tree / ".git"
    try:
        text = gitfile.read_text()
    except (OSError, ValueError):
        text = ""
    for line in text.splitlines():
        if line.startswith("gitdir:"):
            record = Path(line[len("gitdir:"):].strip())
            if not record.is_absolute():
                record = gitfile.parent / record
            record = Path(os.path.realpath(record))
            if record.parent == Path(os.path.realpath(co.gitdir.parent)):
                return record
            break
    if _checkout_record_valid(co):
        return co.gitdir
    return None


def _sparse_union(co: layout.Checkout, backend: GitBackend) -> list[str]:
    """Apply the sparse cone for the union of recorded binding subdirs
    plus `co.subdir`; return the union list.

    Cone mode materializes each mapped subdirectory's tree in full and,
    by design, files at the repository root and files directly inside
    each ancestor directory of a mapped subdirectory. Unmapped directory
    trees are filtered — including a same-named directory at another
    depth. "Exposes only the mapped subdirectory" is a property of the
    consumer link, not of the hidden checkout's contents."""
    rec = state.load_checkout(co)
    bindings = {
        s for s in rec.get("bindings", []) if isinstance(s, str) and s
    }
    if co.subdir:
        bindings.add(co.subdir)
    union = sorted(bindings)
    # `--` ends option parsing so a directory spelled like an option is
    # taken literally. `--skip-checks` (git >= 2.36) admits verbatim a
    # member with glob characters (`app/[id]`) or a leading `!` (`!foo`
    # — git rejects such operands as patterns). Flagging any `!`-leading
    # `/`-segment (`app/!foo`) is a harmless superset: git's check is
    # operand-leading only, and the flag merely relaxes validation. The
    # spec floor is git 2.35, which rejects the flag — pass it only when
    # a member needs it and, when git reports the option unknown, retry
    # the identical call without it (2.35 accepts those spellings
    # verbatim anyway).
    args = ["sparse-checkout", "set", "--cone"]
    if any(
        set(d) & set("*?[]\\") or d.startswith("!") or "/!" in d
        for d in union
    ):
        args.append("--skip-checks")
    args += ["--", *union]
    # `sparse-checkout set` resolves its dir operands against the process
    # cwd's worktree prefix — pin cwd to the checkout root so a caller
    # physically inside a mapped subdir (`cd -P` into `.gf/wt`) cannot
    # re-anchor `docs/api` to `docs/docs/api` (mirrors `scoped_porcelain`).
    result = backend.git(
        *args, git_dir=co.gitdir, work_tree=co.work_tree,
        cwd=co.work_tree, stream=True, check=False,
    )
    if result.returncode != 0:
        if "--skip-checks" in args and re.search(
            r"unknown option|unrecognized option", result.stderr,
            re.IGNORECASE,
        ):
            backend.git(
                *(a for a in args if a != "--skip-checks"),
                git_dir=co.gitdir, work_tree=co.work_tree,
                cwd=co.work_tree, stream=True,
            )
        else:
            msg = result.stderr.strip() or result.stdout.strip()
            raise GitError(f"git {' '.join(args)} failed: {msg}")
    return union


def _lock_worktree(co: layout.Checkout, backend: GitBackend) -> None:
    """`git worktree lock` idempotently — tolerates an already-locked record."""
    result = backend.git(
        "worktree", "lock", "--reason", "gf-managed", str(co.work_tree),
        git_dir=co.common_dir, check=False,
    )
    if result.returncode != 0 and not (co.gitdir / "locked").is_file():
        raise GitError(
            result.stderr.strip()
            or f"git worktree lock failed for {co.work_tree}"
        )


def _save_checkout_state(
    co: layout.Checkout, ref: str, sha: str, bindings: list[str]
) -> None:
    """Merge the recomputed record: `bindings` is the union of the
    checkout's served subdirs (additive — it only grows until a binding
    vacates), so the preserved `url`/`override`/`binding_urls` fields
    stay consistent with it; `_vacate_checkout` owns the pruning."""
    rec = state.load_checkout(co)
    rec.update({
        "resolved": sha,
        "ref": ref,
        "bindings": bindings,
    })
    state.save_checkout(co, rec)


def ensure_checkout(
    co: layout.Checkout,
    ref: str,
    *,
    backend: GitBackend | None = None,
) -> bool:
    """Ensure the shared linked checkout `co` exists, widened to its union.

    Creates `<root>/.gf/wt/<repo-key>/<key>` via
    `git worktree add --no-checkout --detach`, verifies the worktree record
    under `<store>/worktrees/` by its `gitdir` file, applies
    `sparse-checkout set --cone` to the union of the checkout's recorded
    binding subdirs plus `co.subdir`, then runs `_apply_ref` — the single
    ref-application operation (`checkout -b` / attach + `merge --ff-only`
    for a branch ref, a detached checkout for a tag/commit ref, never
    `-B`/`-f`) — removes the `.git` gitfile, locks the worktree
    idempotently, and writes the per-checkout state record including its
    `bindings` list.

    A checkout path occupied by anything without its matching worktree
    record is an error — gf never rebuilds over existing files. On failure
    only the artifacts this call created are torn down; the repo store is
    never removed.

    Returns True only when this call completed the create arm (worktree
    add + record verify + cone + gitfile/lock + state); False when the
    join arm ran — the checkout already existed and belongs to the call
    or process that created it, so a caller may tear the checkout down
    on its own later failure only when this returned True. A raise
    inside this function never reaches the caller's flag: the create
    arm self-cleans and the join arm leaves a foreign checkout alone.
    """
    # Work tree and worktree record must be literal paths under the real
    # root: `subfolder_checkout` appends `.gf`/`wt`/`worktrees` segments
    # lexically after realpath'ing the root, so a symlink at any of them
    # (a committed `.gf` link in a worktree-add tree included) would
    # redirect `worktree add`, record writes and the state file outside
    # the root. Refuse before the branch/sha store probes so create and
    # join arms are covered.
    for path in (co.work_tree, co.gitdir):
        if not layout.storage_is_real(path):
            raise ValidationError(
                f"checkout storage path {path} resolves through a symlink")
    backend = backend or _default_backend()
    store = co.common_dir
    admin = co.gitdir
    wt = co.work_tree

    branch = ref if is_branch(co, ref, backend) else None
    try:
        sha = resolve_ref(co, ref, backend)
        resolved = True
    except ValidationError:
        resolved = False
    if not resolved or (
        not branch
        and backend.git(
            "cat-file", "-e", sha, git_dir=store, check=False,
        ).returncode != 0
    ):
        # `--no-tags` fetches never auto-follow: a tag/commit the store's
        # live refspecs do not cover (created for a different binding's
        # ref, or narrowed by --single-branch) stays unresolvable until
        # covered — the same `_ensure_pinned_ref` + one-fetch coverage
        # the clone and pull seams run. A ref upstream does not
        # advertise as a tag or a named commit keeps the resolve's own
        # `could not resolve ref` error — unchanged.
        if branch or ref in ("latest", ""):
            sha = resolve_ref(co, ref, backend)
        else:
            url = backend.git(
                "remote", "get-url", "origin", git_dir=store, check=False,
            )
            if url.returncode != 0:
                sha = resolve_ref(co, ref, backend)
            else:
                _assert_safe_refspecs(store, backend)
                _ensure_pinned_ref(store, url.stdout.strip(), ref, backend)
                _fetch_guarded(
                    backend, store, "fetch", "--no-tags", "origin")
                sha = resolve_ref(co, ref, backend)
    # The `.gf` root-entry refusal precedes BOTH materialization arms:
    # the create arm's `worktree add` + checkout and the existing-record
    # arm's `_sparse_union` cone rebuild (it materializes files from the
    # resolved tree the same way a checkout does). A branch ref probes
    # `origin/<branch>` as well: the create arm materializes that tip,
    # which a stale `refs/heads/<branch>` or a same-named tag can
    # shadow in `resolve_ref`.
    _assert_no_gf_root_entry(backend, co, sha)
    if branch:
        _assert_no_gf_root_entry(
            backend, co, _resolve_remote_branch(co, branch, backend))

    if _checkout_record_valid(co):
        # Existing checkout: widen the sparse cone and refresh the record.
        recreated = False
        if not wt.is_dir():
            if os.path.lexists(wt):
                raise GitFoldersError(
                    f"checkout path {wt} exists but is not a directory; "
                    f"remove it or restore the checkout, then retry"
                )
            wt.mkdir(parents=True, exist_ok=True)
            recreated = True
        bindings = _sparse_union(co, backend)
        if recreated:
            # The record's checkout dir was gone and this call recreated
            # it empty: nothing user-owned can exist inside a dir `gf`
            # just mkdir'd, so the index's missing entries are `gf`'s own
            # vanished materialization, not deletions to preserve.
            # `sparse-checkout set` only moves skip-worktree bits — it
            # never rewrites index entries already marked present — so
            # write the cone back out of the index with
            # `checkout-index` (skip-worktree entries stay absent).
            backend.git(
                "checkout-index", "-f", "-a",
                git_dir=co.gitdir, work_tree=wt,
            )
        gitfile = wt / ".git"
        if os.path.lexists(gitfile):
            os.unlink(gitfile)
        _lock_worktree(co, backend)
        _save_checkout_state(co, ref, sha, bindings)
        return False

    if os.path.lexists(wt):
        if wt.is_dir() and not any(wt.iterdir()):
            wt.rmdir()
        else:
            raise GitFoldersError(
                f"checkout {wt} exists but its worktree record {admin} is "
                f"missing from repo store {store}; refusing to rebuild "
                f"over existing files. To recover, remove {wt} and its "
                f"state file, or restore the record with `git --git-dir "
                f"{store} worktree repair`, then retry"
            )

    created = False
    try:
        backend.git(
            "worktree", "add", "--no-checkout", "--detach", str(wt),
            f"origin/{branch}" if branch else sha,
            git_dir=store, stream=True,
        )
        created = True
        if not _checkout_record_valid(co):
            raise GitFoldersError(
                f"git worktree add did not create the expected worktree "
                f"record {admin} for checkout {wt}"
            )
        bindings = _sparse_union(co, backend)
        _apply_ref(co, ref, branch, backend)
        gitfile = wt / ".git"
        if os.path.lexists(gitfile):
            os.unlink(gitfile)
        _lock_worktree(co, backend)
    except Exception:
        if created:
            # Undo only what this call created — the worktree record the
            # add actually created (not necessarily `admin`: a taken
            # record name makes git pick a suffixed one, and `admin` may
            # hold a foreign record) and the checkout dir. The shared
            # repo store and any foreign record stay.
            record = _checkout_record_dir(co)
            if record is not None:
                shutil.rmtree(record, ignore_errors=True)
            shutil.rmtree(wt, ignore_errors=True)
        raise
    _save_checkout_state(co, ref, sha, bindings)
    return True


def ensure_consumer_link(
    link: Path, target: Path, root: Path | None = None
) -> tuple[str, str | None] | None:
    """Ensure `link` is a relative symlink to `target`.

    Returns None when the link already points at `target`; otherwise a
    (action, previous-target) pair describing what this call changed so a
    later failure can undo exactly it via `rollback_consumer_link`.
    Existing non-link content is never destroyed: an empty directory is
    replaced; anything else is an error.

    When `root` is given it bounds every write below (unlink/rmdir/
    mkdir/symlink): the spelled `link` must normalize inside `root`, and
    `link`'s realpath PARENT must stay inside `root`, outside `.gf`
    storage, and outside `.git` metadata — a committed mid-path symlink
    must not redirect the unlink/replace outside the owning root, and a
    landing inside `.git` would put the link where the parent's own git
    operations read it (`hooks/` executes on checkout). The leaf's own
    realpath is never tested: a live consumer link resolves into
    `.gf/wt` legitimately.
    """
    if root is not None:
        link_parent = Path(os.path.realpath(link.parent))
        if not (
            Path(os.path.normpath(link)).is_relative_to(
                Path(os.path.normpath(root)))
            and link_parent.is_relative_to(Path(os.path.realpath(root)))
            and not layout.in_gf_tree(link_parent)
            and not layout.in_git_tree(link_parent)
        ):
            raise ValidationError(
                f"consumer path {link} escapes {root} or reaches into "
                f"gf-managed storage ({layout.GF_DIR}) or repository "
                f"metadata (.git); refusing to link")
    if link.is_symlink():
        if Path(os.path.realpath(link)) == Path(os.path.realpath(target)):
            return None
        try:
            old = os.readlink(link)
            link.unlink()
            try:
                _place_consumer_link(link, target)
            except Exception:
                os.symlink(old, link, target_is_directory=True)
                raise
            return ("retargeted", old)
        except OSError as e:
            raise ValidationError(f"consumer path {link}: {e}") from e
    try:
        if os.path.lexists(link):
            if link.is_dir() and not any(link.iterdir()):
                link.rmdir()
                replaced = True
            else:
                raise ValidationError(
                    f"consumer path {link} exists and is not a symlink or "
                    f"empty directory"
                )
        else:
            replaced = False
        _place_consumer_link(link, target)
    except OSError as e:
        raise ValidationError(f"consumer path {link}: {e}") from e
    return ("replaced-dir" if replaced else "created", None)


def _place_consumer_link(link: Path, target: Path) -> None:
    """Create the relative symlink, reporting fs errors as ValidationError."""
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        # The leaf lands at link.parent's REALPATH — a committed in-root
        # mid-path symlink (sub -> deep/nested/a) redirects it there — so
        # the relative target must be computed from the real base, not the
        # spelled parent. The target is realpath'd too so a `.gf`-interior
        # symlink component cannot steer the spelling.
        os.symlink(
            os.path.relpath(
                os.path.realpath(target), os.path.realpath(link.parent)),
            link,
            target_is_directory=True,
        )
    except OSError as e:
        raise ValidationError(f"consumer path {link}: {e}") from e


def rollback_consumer_link(
    link: Path, action: tuple[str, str | None] | None
) -> None:
    """Undo a link created or retargeted by `ensure_consumer_link`."""
    if not action:
        return
    name, old = action
    try:
        if link.is_symlink():
            link.unlink()
        if name == "retargeted" and old is not None:
            os.symlink(old, link, target_is_directory=True)
        elif name == "replaced-dir":
            link.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValidationError(
            f"cannot restore consumer path {link}: {e}") from e


def _remote_default_branch(url: str, backend: GitBackend) -> str | None:
    """The remote's default branch from `git ls-remote --symref`, or None."""
    result = backend.git(
        "ls-remote", "--symref", _resolved_git_url(url), "HEAD",
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        m = re.match(r"ref:\s+refs/heads/(\S+)\s+HEAD", line)
        if m:
            return m.group(1)
    return None


def _upstream_has_branch(url: str, branch: str, backend: GitBackend) -> bool:
    """True when `url` advertises `refs/heads/<branch>` (one `ls-remote`)."""
    result = backend.git(
        "ls-remote", _resolved_git_url(url), f"refs/heads/{branch}",
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _upstream_has_tag(url: str, tag: str, backend: GitBackend) -> bool:
    """True when `url` advertises `refs/tags/<tag>` (one `ls-remote`)."""
    result = backend.git(
        "ls-remote", _resolved_git_url(url), f"refs/tags/{tag}",
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _store_default_branch(store: Path, backend: GitBackend) -> str | None:
    """The branch `refs/remotes/origin/HEAD` names in the store, if any."""
    result = backend.git(
        "symbolic-ref", "-q", "refs/remotes/origin/HEAD",
        git_dir=store, check=False,
    )
    if result.returncode != 0:
        return None
    prefix = "refs/remotes/origin/"
    target = result.stdout.strip()
    return target[len(prefix):] if target.startswith(prefix) else None


def _store_has_branch(store: Path, branch: str, backend: GitBackend) -> bool:
    """True when `refs/remotes/origin/<branch>` is already in the store."""
    return backend.git(
        "show-ref", "--verify", f"refs/remotes/origin/{branch}",
        git_dir=store, check=False,
    ).returncode == 0


def _binding_branch(
    store: Path, repo_url: str, ref: str, backend: GitBackend
) -> str | None:
    """The branch a binding's ref resolves to, else None (detached).

    `latest` resolves to the effective branch BEFORE key derivation: the
    remote default via `ls-remote --symref`, falling back to the store's
    `origin/HEAD`. An explicit branch names itself when it exists upstream
    or is already in the store. Tags, commits, and unresolvable refs
    produce a detached checkout.
    """
    if ref in ("latest", ""):
        return _remote_default_branch(repo_url, backend) \
            or _store_default_branch(store, backend)
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return None
    if _upstream_has_branch(repo_url, ref, backend):
        return ref
    return ref if _store_has_branch(store, ref, backend) else None


def _remove_checkout(co: layout.Checkout, backend: GitBackend) -> None:
    """Tear down a checkout and worktree record this call created.

    Removes the linked worktree, the worktree record that checkout's own
    `worktree add` created (not necessarily `co.gitdir`: a taken record
    name makes git pick a suffixed one), and the `.<key>.state` file —
    never the repo store itself or a record naming another work tree.
    `git worktree remove --force` already deletes the record the
    checkout's `.git` gitfile names; the conditional rmtree covers what
    git refuses (a locked record) or never made.

    Rollback must never delete through a hostile `.gf` symlink: a
    non-real work-tree or record spelling means the ensure step refused
    before anything landed, so every step below — `worktree remove`, the
    record rmtree, the checkout rmtree, the state unlink — could only
    reach foreign content and is skipped.
    """
    if not (
        layout.storage_is_real(co.work_tree)
        and layout.storage_is_real(co.gitdir)
    ):
        return
    backend.git(
        "worktree", "remove", "--force", str(co.work_tree),
        git_dir=co.common_dir, check=False,
    )
    record = _checkout_record_dir(co)
    if record is not None:
        shutil.rmtree(record, ignore_errors=True)
    shutil.rmtree(co.work_tree, ignore_errors=True)
    try:
        co.state.unlink()
    except OSError:
        pass


def _remove_repo_store(store: Path) -> None:
    """Remove a repo store the failing command itself created.

    `store` is `<root>/.gf/repos/<repo-key>/git`; the store tree — with
    any worktree records and state this call left under it — is removed
    and the `<repo-key>` ancestry dir pruned when empty. Never call this
    for a pre-existing or serving store; the caller's
    `store_created`/`store_existed` flag is the discriminator. Guarded:
    a store that gained worktree records under `<store>/worktrees/`
    since that flag was taken is serving another binding — a concurrent
    join whose checkout completed — and is kept. Any record present at
    removal time is foreign: the failing call's own record is already
    gone (`ensure_shared_binding`'s except path removes it through
    `_remove_checkout`, and the `pull_shared_bindings` site removes only
    on a store-phase failure before its own `ensure_checkout`).

    A non-real `store` spelling (a symlink at `.gf`, `repos`, the
    repo-key dir or `git`) names foreign content — `ensure_repo_store`
    refused it before anything was created — so rollback leaves the
    resolved tree and its `<repo-key>` ancestry untouched rather than
    rmtree through a hostile `.gf` link.
    """
    if not layout.storage_is_real(store):
        return
    records = store / "worktrees"
    try:
        if records.is_dir() and any(records.iterdir()):
            return
    except OSError:
        # A listing race (the store is already going away) falls
        # through to the best-effort rmtree; this failure path keeps
        # the original error.
        pass
    shutil.rmtree(store, ignore_errors=True)
    try:
        store.parent.rmdir()
    except OSError:
        pass


def _binding_gitignore_line(parent_root: Path, child: Path) -> str:
    """The .gitignore recommendation line for the binding's consumer path."""
    rel = child.relative_to(parent_root).as_posix()
    return f'add "{rel}" to .gitignore'


def _print_binding_gitignore(parent_root: Path, child: Path) -> None:
    """Recommend the binding's consumer path for the parent .gitignore."""
    print(_binding_gitignore_line(parent_root, child))


def ensure_shared_binding(
    child: Path,
    parent_root: Path,
    repo_url: str,
    subdir: str,
    url: str,
    ref: str,
    *,
    override: bool,
    backend: GitBackend,
    single_branch: bool = False,
    depth: int | None = None,
    binding_path: str | None = None,
) -> None:
    """Create or join the shared-store binding whose consumer link is `child`.

    Ensures the repo store (`--single-branch` narrows the first refspec
    line only when this call creates the store; joins never re-narrow;
    `depth` applies only to the fetch that creates the store — a join
    ignores it),
    derives the checkout key from the resolved branch — `latest` resolves
    to the effective branch first, tags/commits key as `ref=<ref>` —
    ensures the shared sparse checkout, places the relative consumer link
    at `child` pointing into `<checkout>/<subdir>`, and records the
    binding's state — including its effective `url` in the per-binding
    `binding_urls` map keyed by `binding_path` (the manifest consumer
    `path`). On a later-step failure the link is rolled back and
    only the checkout this call created is torn down; a repo store this
    call itself created is removed too, while pre-existing stores and
    other bindings' checkouts always stay.
    """
    store = layout.repo_store(parent_root, repo_url)
    branch = _binding_branch(store, repo_url, ref, backend)
    store_created = not (store / "HEAD").is_file()
    try:
        ensure_repo_store(
            store, repo_url, branch=branch, ref=ref,
            single_branch=single_branch, depth=depth, backend=backend,
        )
        key = (
            layout.checkout_key_for_branch(branch)
            if branch else layout.checkout_key_for_ref(ref)
        )
        co = layout.subfolder_checkout(parent_root, repo_url, key, subdir)
        # `created` — returned by ensure_checkout, not a pre-call record
        # probe — is the only sound destructive-rollback trigger below: a
        # snapshot taken here goes stale the moment a concurrent process's
        # record appears between the probe and the join arm, and that
        # process's live checkout must survive this call's later
        # link/state failure. A raise inside ensure_checkout never reaches
        # the flag: its create arm self-cleans and its join arm leaves a
        # foreign checkout alone.
        created = ensure_checkout(co, branch or ref, backend=backend)

        action = None
        try:
            action = ensure_consumer_link(
                child, co.work_tree / co.subdir, root=parent_root)
            rec = state.load_checkout(co)
            rec.update({"url": url, "override": override})
            if binding_path is not None:
                urls = rec.get("binding_urls")
                if not isinstance(urls, dict):
                    urls = {}
                urls[binding_path] = url
                rec["binding_urls"] = urls
            state.save_checkout(co, rec)
        except Exception:
            rollback_consumer_link(child, action)
            # created=False (this call joined an existing checkout)
            # means another creator's checkout survives our failure.
            # The parent-repository lock (GF-D20) already serialized
            # the concurrent-joiner race this residual once named.
            if created:
                _remove_checkout(co, backend)
            raise
    except Exception:
        if store_created:
            _remove_repo_store(store)
        raise

    if store_created:
        print(f'add "{layout.GF_DIR}/" to .gitignore')
    _print_binding_gitignore(parent_root, child)


def _pristine_gitdir_eligible(co: layout.Checkout, backend: GitBackend) -> bool:
    """An unchanged initialization is disposable only if it carried no work.

    Git templates can seed private objects, refs, config and hooks before
    gf records a fingerprint. Reject those cheaply before reading bytes.
    Inherited global identity is not local metadata and does not block.
    """
    def empty_tree(path: Path) -> bool:
        return (path.is_dir() and not path.is_symlink()
                and all(empty_tree(child) for child in path.iterdir()))

    if not all(empty_tree(co.gitdir / name) for name in ("objects", "refs")):
        return False
    template = backend.git(
        "config", "--get", "init.templateDir", git_dir=co.gitdir, check=False,
    )
    # Configured templates are user-owned inputs, including files whose
    # names resemble Git's default samples. Missing (rc=1) alone proves
    # the default scaffold; an error or configured template has no proof.
    if template.returncode != 1:
        return False
    allowed = {"HEAD", "config", "description", "hooks", "info", "objects", "refs", "branches"}
    branches = co.gitdir / "branches"
    if branches.exists() and not empty_tree(branches):
        return False
    if any(p.name not in allowed or p.is_symlink() for p in co.gitdir.iterdir()):
        return False
    hooks = co.gitdir / "hooks"
    if hooks.exists() and (not hooks.is_dir() or any(
        not p.name.endswith(".sample") or p.is_symlink() or not p.is_file()
        for p in hooks.iterdir()
    )):
        return False
    info = co.gitdir / "info"
    if info.exists() and (not info.is_dir() or any(
        p.name != "exclude" or p.is_symlink() or not p.is_file()
        for p in info.iterdir()
    )):
        return False
    exclude = info / "exclude"
    if exclude.exists() and any(
        line.strip() and not line.lstrip().startswith("#") and line.strip() not in (layout.GF_DIR, layout.GF_DIR + "/")
        for line in exclude.read_text(errors="replace").splitlines()
    ):
        return False
    description = co.gitdir / "description"
    if description.exists() and description.read_bytes() != (
        b"Unnamed repository; edit this file 'description' to name the repository.\n"
    ):
        return False
    config = backend.git(
        "config", "--local", "--null", "--list", git_dir=co.gitdir, check=False,
    )
    if config.returncode != 0:
        return False
    values: dict[str, str] = {}
    boolean_keys = {"core.filemode", "core.logallrefupdates", "core.ignorecase", "core.precomposeunicode"}
    for entry in filter(None, config.stdout.split("\0")):
        key, separator, value = entry.partition("\n")
        if not separator or key in values:
            return False
        if key in boolean_keys:
            if value not in ("true", "false"):
                return False
        elif key == "core.repositoryformatversion":
            if value != "0":
                return False
        elif key == "core.bare":
            if value != "false":
                return False
        elif key == "core.worktree":
            if Path(value).resolve() != co.work_tree.resolve():
                return False
        else:
            return False
        values[key] = value
    return bool(values)


def _gitdir_fingerprint(gitdir: Path) -> str | None:
    """Hash a pristine gitdir's names, modes and bytes without following links.

    This ownership proof is minted only after fresh local initialization.
    Unsupported node types have no pristine proof and may never be removed.
    """
    digest = hashlib.sha256()
    def visit(path: Path) -> bool:
        mode = path.lstat().st_mode
        name = os.fsencode(path.relative_to(gitdir).as_posix())
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(mode.to_bytes(8, "big"))
        if stat.S_ISREG(mode):
            data = path.read_bytes()
            digest.update(len(data).to_bytes(8, "big") + data)
        elif stat.S_ISDIR(mode):
            for child in sorted(path.iterdir()):
                if not visit(child):
                    return False
        else:
            return False
        return True
    return digest.hexdigest() if visit(gitdir) else None


def record_pristine_placeholder(co: layout.Checkout, backend: GitBackend) -> None:
    """Record only cmd_init's freshly created metadata for later conversion."""
    if not _pristine_gitdir_eligible(co, backend):
        return
    fingerprint = _gitdir_fingerprint(co.gitdir)
    if fingerprint is not None:
        state.save_checkout(co, {"pristine_gitdir": fingerprint})


def strip_placeholder_child(child: Path, backend: GitBackend | None = None) -> bool:
    """Convert only a fully proven pristine local initialization (GF-D22)."""
    if not child.is_dir() or child.is_symlink():
        return False
    co = layout.whole_repo_checkout(child)
    if not (co.gitdir / "HEAD").is_file():
        return False
    if not layout.whole_repo_gitdir_is_real(co):
        raise ValidationError(f"placeholder gitdir {co.gitdir} resolves through a symlink")
    record = state.load_checkout(co)
    fingerprint = record.get("pristine_gitdir")
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or set(record) != {"pristine_gitdir"}
        or sorted(p.name for p in child.iterdir()) != [layout.GF_DIR]
        or sorted(p.name for p in co.gitdir.parent.iterdir()) != ["git", "state"]
        or not _pristine_gitdir_eligible(co, backend or _default_backend())
        or _gitdir_fingerprint(co.gitdir) != fingerprint
    ):
        raise ValidationError(
            f"cannot prove {child}'s placeholder Git metadata and contents are pristine; "
            "refusing conversion and preserving its refs, objects, index and configuration")
    shutil.rmtree(child)
    return True


def _vacate_checkout(
    old_co: layout.Checkout,
    parent_root: Path,
) -> None:
    """Drop moved bindings from a vacated shared checkout's record.

    The recorded `bindings` are recomputed from the manifest of the
    checkout's OWNING root (`parent_root`) — never from the invoking
    worktree's copied `folders`, which can lag the source manifest when
    a binding is cloned in after `worktree add` snapshotted it: a subdir
    stays only while some consumer link still resolves into this
    checkout's mapped directory, and a `binding_urls` entry stays only
    while its consumer path still resolves here — a vacated binding's
    recorded url does not linger. The worktree's files — tracked and
    uncommitted — are never touched.

    Pruning requires a successfully parsed owning manifest: an absent,
    non-regular, unreadable or corrupt `gf.toml` cannot prove which
    bindings are still live, so the record is left untouched rather than
    emptied — a wiped record would un-materialize still-linked siblings
    on the next `_sparse_union` cone rebuild and drop their recorded
    urls.
    """
    if not _checkout_record_valid(old_co):
        return
    if not (parent_root / _manifest.MANIFEST).is_file():
        return
    try:
        mf = _manifest.read_manifest(parent_root)
    except (OSError, GitFoldersError, tomllib.TOMLDecodeError,
            UnicodeDecodeError):
        return
    folders = mf.get("git_folder", [])
    live = set()
    live_paths = set()
    for folder in folders:
        child = parent_root / folder["path"]
        if not child.is_symlink():
            continue
        fco = layout.resolve_checkout(child.resolve())
        if fco.is_store_checkout and fco.gitdir == old_co.gitdir:
            live.add(fco.subdir)
            live_paths.add(folder["path"])
    rec = state.load_checkout(old_co)
    bindings = [
        b for b in rec.get("bindings", [])
        if isinstance(b, str) and b in live
    ]
    urls = rec.get("binding_urls")
    pruned_urls = (
        {k: v for k, v in urls.items() if k in live_paths}
        if isinstance(urls, dict) else None
    )
    changed = False
    if bindings != rec.get("bindings"):
        rec["bindings"] = bindings
        changed = True
    if pruned_urls is not None and pruned_urls != urls:
        rec["binding_urls"] = pruned_urls
        changed = True
    if changed:
        state.save_checkout(old_co, rec)


def _siblings_served(
    folders: list[dict],
    excluded_names: set,
    parent_root: Path,
    store: Path,
    key: str,
) -> list[dict]:
    """Manifest bindings outside `excluded_names` whose consumer link
    currently resolves into the shared checkout (`store`, `key`).

    `folders` must be the OWNING root's manifest (`parent_root`), not
    the invoking worktree's copy — the same skew `_vacate_checkout`
    guards against."""
    served = []
    for folder in folders:
        if folder.get("name") in excluded_names:
            continue
        child = parent_root / folder["path"]
        if not child.is_symlink():
            continue
        co = layout.resolve_checkout(child.resolve())
        if (
            co.is_store_checkout
            and co.common_dir == store
            and co.gitdir.name == key
        ):
            served.append(folder)
    return served


def pull_shared_bindings(
    items: list[dict],
    folders: list[dict],
    parent_root: Path,
    *,
    rebase: bool = False,
    autostash: bool = False,
    backend: GitBackend | None = None,
) -> list[str]:
    """Pull resolved subfolder-binding `items`, grouped by owning root +
    store + checkout.

    Each item's owning root derives from its resolved checkout: a store
    checkout lives under `<root>/.gf/wt/...`, so a pull invoked through a
    `gf worktree add` link chain operates on the SOURCE worktree's store,
    checkout and consumer link; an unresolved binding materializes under
    `parent_root`. Per repo store: `ensure_repo_store` coverage — when
    the store is missing, each binding's branch/key resolves via
    `_binding_branch` BEFORE creation (its store probes tolerate the
    missing store) and every non-branch binding's `ref` goes in as a
    `pins` entry, so each pin's `_ensure_pinned_ref` coverage lands with
    the one creating fetch; on a pre-existing store an uncovered
    explicit branch that upstream advertises gets its refspec line
    appended — append-only, probed by one `ls-remote`; a `latest` whose
    store origin/HEAD is missing or dangles probes the remote's default
    the same way and repoints the symref; a tag/commit ref gets
    `_ensure_pinned_ref` coverage — a tag's refspec line when upstream
    advertises it, or a one-shot `fetch origin <sha>` for a missing
    commit — followed by exactly one store fetch.
    Resolution is otherwise recorded/local — no other `ls-remote`/
    `remote set-head` re-probe. Per checkout key:
    `ensure_checkout` per binding (record check + cone-union widening +
    gitfile/lock), one unscoped dirty check over the materialized
    checkout — `--autostash` applies to the whole checkout, a dirty
    shared checkout blocks every binding it serves, and there is no
    force flag that would update over uncommitted work — then a single
    ref apply. Per binding:
    `ensure_consumer_link` create/retarget to `<checkout>/<subdir>` plus a
    merged `save_checkout` that also records the binding's effective `url`
    in the `binding_urls` map keyed by its manifest `path`; a retarget
    away from another checkout drops the binding from the vacated
    checkout's recorded bindings and url map. Returns one
    output line per binding served; unselected siblings whose shared
    checkout moved are marked `(moved with <leader>)`. `.gitignore`
    recommendation lines precede the served lines: one for a store this
    pull created, one for each consumer link it created or placed over an
    empty directory.
    """
    backend = backend or _default_backend()
    lines: list[str] = []
    excluded = {it["folder"]["name"] for it in items}

    # Each item is served by the root that OWNS its checkout: a resolved
    # store checkout lives under `<root>/.gf/wt/...`, so a pull invoked
    # through a `gf worktree add` link chain fetches the source
    # worktree's store and retargets ITS consumer link — never a second
    # `.gf` under the invoking worktree. A binding without a store
    # checkout (a missing or never-placed link) is materialized under
    # this pull's `parent_root`.
    by_store: dict[tuple[Path, Path], list[dict]] = {}
    for it in items:
        owner = (
            layout.owning_root(it["co"])
            if it["co"].is_store_checkout else None)
        it["owning_root"] = owner if owner is not None else parent_root
        it["store"] = layout.repo_store(it["owning_root"], it["repo_url"])
        by_store.setdefault((it["owning_root"], it["store"]), []).append(it)

    for (owning_root, store), sitems in by_store.items():
        repo_url = sitems[0]["repo_url"]
        store_existed = (store / "HEAD").is_file()
        try:
            # The same storage-real invariant `ensure_repo_store`
            # enforces: an existing store bypasses that function, so a
            # symlinked `.gf`/`repos`/repo-key/`git` component must be
            # refused here before the coverage loop writes refspec
            # lines, fetches, or repoints origin/HEAD through the link
            # into foreign storage.
            if not layout.storage_is_real(store):
                raise ValidationError(
                    f"repo store path {store} resolves through a symlink")
            if store_existed:
                # An existing store bypasses ensure_repo_store's
                # creation-time origin write, so check the binding
                # here: refuse before the coverage loop below writes
                # refspec lines or fetches through a store whose
                # origin no longer matches this binding's resolution.
                # A pre-existing forced refspec into user-owned refs
                # refuses the same way (GF-D17) — before any coverage
                # write or the store fetch.
                _assert_store_origin(backend, store, repo_url)
                _assert_safe_refspecs(store, backend)
            if not store_existed:
                # The clone path's pattern, grouped: resolve every
                # binding's branch/key BEFORE the store exists —
                # `_binding_branch`'s store probes tolerate the missing
                # store — and feed each non-branch binding's `ref` into
                # the create step as a pin so its `_ensure_pinned_ref`
                # coverage lands with the one creating fetch. Covering a
                # pin after that fetch would append a tag's refspec line
                # that no fetch ever reads.
                for it in sitems:
                    ref = it["ref"]
                    try:
                        branch = _binding_branch(
                            store, repo_url, ref, backend)
                    except GitFoldersError as e:
                        e.folder = it["folder"]
                        raise
                    it["branch"] = branch
                    it["key"] = (
                        layout.checkout_key_for_branch(branch)
                        if branch
                        else layout.checkout_key_for_ref(ref or "latest")
                    )
                # A store created here already fetched inside
                # ensure_repo_store — it gets no second fetch below.
                ensure_repo_store(
                    store, repo_url,
                    pins=[it["ref"] for it in sitems
                          if it["branch"] is None],
                    backend=backend,
                )
                lines.append(f'add "{layout.GF_DIR}/" to .gitignore')
            # A store this pull created resolved its bindings and pin
            # coverage above; the coverage loop below serves only a
            # pre-existing store.
            for it in (sitems if store_existed else ()):
                ref = it["ref"]
                try:
                    if ref in ("latest", ""):
                        branch = _store_default_branch(store, backend)
                        if branch is None or not _store_has_branch(
                            store, branch, backend
                        ):
                            # The store's origin/HEAD is missing or dangles
                            # over a branch a narrow store never fetched:
                            # probe the remote's default once (the same
                            # ls-remote carve as an uncovered explicit
                            # branch), record coverage so the store fetch
                            # below lands it, and repoint origin/HEAD so
                            # later `latest` resolutions stay local.
                            remote = _remote_default_branch(repo_url, backend)
                            if remote is not None:
                                _ensure_branch_coverage(store, remote, backend)
                                backend.git(
                                    "symbolic-ref",
                                    "refs/remotes/origin/HEAD",
                                    f"refs/remotes/origin/{remote}",
                                    git_dir=store,
                                )
                                branch = remote
                    elif re.fullmatch(r"[0-9a-f]{40}", ref):
                        # A pinned commit no refspec names: pull it with
                        # a one-shot `fetch origin <sha>` when the store
                        # lacks it (local upstreams always allow it).
                        _ensure_pinned_ref(store, repo_url, ref, backend)
                        branch = None
                    elif _store_has_branch(store, ref, backend):
                        branch = ref
                    elif _upstream_has_branch(repo_url, ref, backend):
                        # An uncovered branch a narrow store lacks but
                        # upstream has: append its refspec line
                        # (append-only) so the store fetch below lands it.
                        _ensure_branch_coverage(store, ref, backend)
                        branch = ref
                    else:
                        # A tag-ish ref the branch refspecs cannot cover:
                        # append the tag's line when upstream advertises
                        # it so the store fetch below lands it; anything
                        # else falls through to `could not resolve ref`.
                        _ensure_pinned_ref(store, repo_url, ref, backend)
                        branch = None
                except GitFoldersError as e:
                    e.folder = it["folder"]
                    raise
                it["branch"] = branch
                it["key"] = (
                    layout.checkout_key_for_branch(branch)
                    if branch
                    else layout.checkout_key_for_ref(ref or "latest")
                )
            if store_existed:
                # Exactly one store fetch per pull, after all refspec
                # appends.
                _fetch_store(store, backend)
        except Exception as e:
            # A bare store-phase failure (store create, the single
            # fetch) is attributed to a binding OF THIS STORE; the
            # coverage loop already named its own item. `lines` lets
            # the caller print the lines of bindings already served.
            if getattr(e, "folder", None) is None:
                e.folder = sitems[0]["folder"]
            e.lines = lines
            if not store_existed:
                # A store this pull created is removed so a retry
                # starts from a clean slate: a leaked half-made store
                # keeps HEAD on refs/heads/master, which the join's
                # `worktree add` then reports as already used. A
                # pre-existing store is never removed — even when the
                # coverage loop appended refspec lines first.
                _remove_repo_store(store)
            raise

        # The manifest that decides which bindings this root serves is
        # the OWNING root's own — the invoking worktree's copied
        # `folders` can lag it (a binding cloned in after `worktree add`
        # snapshotted the copy), and `_vacate_checkout`/`_siblings_served`
        # must not miss a link that still resolves here. An unreadable or
        # corrupt owning manifest degrades to no served siblings: it only
        # drives cosmetic `(moved with ...)` lines, and `_vacate_checkout`
        # already skips pruning under the same condition — degrading
        # beats aborting a valid pull.
        mf = {}
        if (owning_root / _manifest.MANIFEST).is_file():
            try:
                mf = _manifest.read_manifest(owning_root)
            except (OSError, GitFoldersError, tomllib.TOMLDecodeError,
                    UnicodeDecodeError):
                mf = {}
        owning_folders = mf.get("git_folder", [])

        by_key: dict[str, list[dict]] = {}
        for it in sitems:
            by_key.setdefault(it["key"], []).append(it)

        for key, kitems in by_key.items():
            lead = kitems[0]
            ref, branch = lead["ref"], lead["branch"]
            try:
                co = layout.subfolder_checkout(
                    owning_root, repo_url, key, lead["subdir"])
                existed = _checkout_record_valid(co)
                for it in kitems:
                    ensure_checkout(
                        layout.subfolder_checkout(
                            owning_root, repo_url, key, it["subdir"]),
                        branch or ref, backend=backend)

                # The dirty check covers the whole materialized
                # checkout, exactly as `drift` and `update_child`
                # observe it: skip-worktree bits already confine
                # git's report to materialized paths, so an
                # unscoped status sees dirt the subdir union would
                # miss (e.g. a root-level file cone mode
                # materializes). A dirty shared checkout blocks every
                # binding it serves — there is no force flag that would
                # update over uncommitted work.
                dirty = backend.git_capture(
                    "status", "--porcelain",
                    git_dir=co.gitdir, work_tree=co.work_tree,
                ).strip()
                if dirty and not autostash:
                    raise DirtyError(
                        f"checkout {co.work_tree} is dirty (uncommitted "
                        f"changes); commit or stash them first, or pull "
                        f"with --autostash")

                stashed = False
                if dirty:
                    # `-a`, not `-u` — the stash must carry ignored
                    # files too, or the checkout/merge below overwrites
                    # them silently; the `:(exclude).gf` pathspec keeps
                    # the worktree's own anchor out of it.
                    _stash_autostash(co, backend)
                    stashed = True
                try:
                    if existed:
                        sha = _apply_ref(
                            co, ref, branch, backend, rebase=rebase,
                            autostash=autostash)
                    else:
                        # `ensure_checkout` already applied the ref when it
                        # created the checkout.
                        sha = resolve_ref(co, branch or ref, backend)
                except Exception:
                    # Restore on the way out: a stash taken for this
                    # update is never orphaned by a failure inside it.
                    if stashed:
                        try:
                            _pop_autostash(co, backend)
                        except GitError as pop_err:
                            print(
                                f"gf: warning: {pop_err}",
                                file=sys.stderr)
                    raise
                if stashed:
                    _pop_autostash(co, backend)
            except GitFoldersError as e:
                e.folder = lead["folder"]
                e.lines = lines
                raise

            for it in kitems:
                it_co = layout.subfolder_checkout(
                    owning_root, repo_url, key, it["subdir"])
                link = owning_root / it["folder"]["path"]
                try:
                    action = ensure_consumer_link(
                        link, it_co.work_tree / it_co.subdir,
                        root=owning_root)
                    old_co = it["co"]
                    if old_co.is_store_checkout and (
                        old_co.common_dir != store
                        or old_co.gitdir != it_co.gitdir
                        or old_co.subdir != it["subdir"]
                    ):
                        _vacate_checkout(old_co, owning_root)
                    rec = state.load_checkout(it_co)
                    rec.update({
                        "resolved": sha,
                        "ref": it["ref"],
                        "url": it["url"],
                        "override": it["override"],
                    })
                    urls = rec.get("binding_urls")
                    if not isinstance(urls, dict):
                        urls = {}
                    urls[it["folder"]["path"]] = it["url"]
                    rec["binding_urls"] = urls
                    state.save_checkout(it_co, rec)
                except GitFoldersError as e:
                    e.folder = it["folder"]
                    e.lines = lines
                    raise
                if action and action[0] in ("created", "replaced-dir"):
                    # A link this pull placed at the consumer path gets
                    # the same .gitignore recommendation
                    # `ensure_shared_binding` prints; retargeted links
                    # already had one when they were created.
                    lines.append(
                        _binding_gitignore_line(owning_root, link))
                lines.append(f"Pulled {it['folder']['name']}")
            for sib in _siblings_served(
                    owning_folders, excluded, owning_root, store, key):
                lines.append(
                    f"Pulled {sib['name']} "
                    f"(moved with {lead['folder']['name']})")
    return lines


def select_children(parent_root: Path, cwd: Path, args: list[str], manifest: dict) -> list[dict]:
    """Select git-folders from the manifest based on context and args.

    Matching is realpath-aware (spec "Target selection rules"): a target
    that resolves inside a binding's map selects the innermost such
    binding; otherwise the target selects every binding at or below it.
    A spelled operand names its binding lexically too, so siblings that
    alias one map stay distinct under selection.
    """
    folders = manifest.get("git_folder", [])
    if not folders:
        return []

    if args:
        selected = []
        for arg in args:
            matched = _select_for_path(parent_root, folders, cwd / arg)
            if not matched and any(folder.get("name") == arg for folder in folders):
                # Fallback: arg is a git-folder name.
                matched = [s for s in folders if s.get("name") == arg]
            selected.extend(matched)
        return selected

    # No args: cwd inside a child -> that child; otherwise all children below cwd.
    return _select_for_path(parent_root, folders, cwd)


def _select_for_path(parent_root: Path, folders: list[dict], target: Path) -> list[dict]:
    """Match one path against bindings: the innermost binding whose map
    contains `target`, else every binding at or below `target`.

    A spelled consumer path disambiguates in two places. Inside the
    realpath-inside set it breaks equal-depth ties — sibling links onto
    one map collapse to a single realpath depth, so `one`/`two` operands
    onto the same checkout subdir select only the spelled binding — and,
    when no map contains `target` at all, the lexically innermost binding
    containing the spelled target answers before `_below`. A spelled path
    traversing a link INTO a deeper binding's map still resolves by
    realpath: the deeper map is innermost regardless of the spelling.
    """
    spelled = [s for s in folders
               if _lexical_match(parent_root, s["path"], target)]
    inside = [s for s in folders if _path_matches(parent_root, s["path"], target)]
    if inside:
        deepest = max(len(_map_root(parent_root, s["path"]).parts) for s in inside)
        innermost = [s for s in inside
                     if len(_map_root(parent_root, s["path"]).parts) == deepest]
        named = [s for s in innermost if s in spelled]
        return named or innermost
    if spelled:
        deepest = max(len(_lexical_root(parent_root, s["path"]).parts)
                      for s in spelled)
        return [s for s in spelled
                if len(_lexical_root(parent_root, s["path"]).parts) == deepest]
    return [s for s in folders if _below(parent_root, s["path"], target)]


def _lexical_root(parent_root: Path, folder_path: str) -> Path:
    """The binding's consumer path normalized lexically: `.`/`..` segments
    resolved without following links, so sibling links onto one map stay
    distinct."""
    return Path(os.path.normpath(parent_root / folder_path))


def _lexical_match(parent_root: Path, folder_path: str, target: Path) -> bool:
    """True when the spelled `target` names the binding's consumer path or a
    path inside it, comparing path segments only (no link following)."""
    return Path(os.path.normpath(target)).is_relative_to(
        _lexical_root(parent_root, folder_path)
    )


def _map_root(parent_root: Path, folder_path: str) -> Path:
    """The realpath a binding's consumer path maps to: the child directory
    for a whole-repo binding, the mapped checkout subdir for a subfolder
    binding."""
    return Path(os.path.realpath(parent_root / folder_path))


def folder_containing(parent_root: Path, path: Path, manifest: dict) -> dict | None:
    """The manifest binding whose map contains `path` (innermost), if any.

    Unlike `_select_for_path`'s `_below` fallback, a path merely ABOVE a
    binding's consumer dir does not identify it — used by the passthrough
    envelope where only an inside-the-map cwd pins a folder.
    """
    folders = manifest.get("git_folder", [])
    inside = [s for s in folders if _path_matches(parent_root, s["path"], path)]
    if not inside:
        return None
    deepest = max(len(_map_root(parent_root, s["path"]).parts) for s in inside)
    return next(
        s for s in inside
        if len(_map_root(parent_root, s["path"]).parts) == deepest)


def _path_matches(parent_root: Path, folder_path: str, target: Path) -> bool:
    """True when `target` resolves at or inside the binding's map.

    `target` is the unresolved path the user provided or the current cwd;
    the binding's map is the realpath of its consumer path, which follows
    consumer links and `gf worktree add` link chains.
    """
    return Path(os.path.realpath(target)).is_relative_to(
        _map_root(parent_root, folder_path)
    )


def _below(parent_root: Path, folder_path: str, target: Path) -> bool:
    """True when the binding's consumer path or its map is at-or-below `target`.

    The lexical arm normalizes `..`/`.` segments (mirroring
    `_lexical_match`): `sub/../vendor` names `vendor`, so a spelled
    parent still reaches the bindings below it. The map arm keeps the
    raw operand — realpath must traverse mid-path links so `..` after a
    consumer link pops inside the link target's tree, not lexically."""
    unresolved = parent_root / folder_path
    return unresolved.is_relative_to(
        Path(os.path.normpath(target))) or _map_root(
        parent_root, folder_path
    ).is_relative_to(Path(os.path.realpath(target)))
