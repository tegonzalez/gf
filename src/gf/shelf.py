# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import os
import re
import shutil
import sys
from pathlib import Path

from .backends import GitBackend, GitCliBackend
from . import layout, manifest as _manifest, state
from .exceptions import DirtyError, GitError, GitFoldersError, ValidationError


def _default_backend() -> GitBackend:
    return GitCliBackend()


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
    if (p / ".git").is_dir() or (p / "HEAD").is_file():
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
    gitfile, or symlink), looks like a bare repository (`HEAD` at the top
    level), or is a `gf` child (`.gf/git` with a HEAD).
    """
    if os.path.lexists(path / ".git"):
        return True
    if (path / "HEAD").is_file():
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


def _resolve_local_url(url: str, parent_root: Path | None) -> tuple[str, str]:
    """Resolve a local `url` by walking up to the nearest repository.

    Relative paths anchor at `parent_root` (the parent repo root); the leaf
    directory itself need not exist.
    """
    leaf = Path(url)
    if not leaf.is_absolute():
        leaf = (Path(parent_root) if parent_root is not None else Path.cwd()) / leaf
    leaf = leaf.resolve()
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
            return head.split("/")[-1]
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

    backend.git(
        "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*",
        git_dir=gitdir,
    )


def _init_child_gitdir(
    co: layout.Checkout,
    backend: GitBackend | None = None,
) -> None:
    """Create a .gf/git gitdir in the checkout's worktree."""
    backend = backend or _default_backend()
    gitdir = co.gitdir
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


def _fetch_and_checkout(
    co: layout.Checkout,
    url: str,
    ref: str,
    backend: GitBackend | None = None,
    force: bool = False,
    depth: int | None = None,
    single_branch: bool = False,
) -> str:
    """Fetch `origin` and check out the resolved ref. Return the resolved SHA.

    If `force` is True, the checkout uses `-f` so a dirty worktree is
    overwritten to the checked-out files.

    If `depth` is given, `git fetch --depth=<n> origin` is used for a
    shallow clone.

    If `single_branch` is True, `remote.origin.fetch` is narrowed to the
    resolved branch after the initial fetch so subsequent fetches only
    fetch that branch's history. It is ignored for tag/commit refs.
    """
    backend = backend or _default_backend()

    fetch_args = ["fetch"]
    if depth is not None:
        fetch_args.append(f"--depth={depth}")
    fetch_args.append("origin")

    backend.git(*fetch_args, git_dir=co.common_dir, stream=True)

    branch = _effective_branch(co, ref, backend)
    if branch and single_branch:
        # Narrow the fetch refspec to the resolved branch so subsequent
        # fetches only fetch that branch's history.
        backend.git(
            "config", "remote.origin.fetch",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            git_dir=co.common_dir,
        )

    if branch:
        sha = _resolve_remote_branch(co, branch, backend)
        checkout_args = ["checkout"]
        if force:
            checkout_args.append("-f")
        checkout_args.extend(["-B", branch, f"origin/{branch}"])
        backend.git(
            *checkout_args, git_dir=co.gitdir, work_tree=co.work_tree, stream=True,
        )
    else:
        sha = resolve_ref(co, ref, backend)
        checkout_args = ["checkout"]
        if force:
            checkout_args.append("-f")
        checkout_args.append(sha)
        backend.git(
            *checkout_args, git_dir=co.gitdir, work_tree=co.work_tree, stream=True,
        )

    return sha


def _fetch_and_rebase(
    co: layout.Checkout,
    url: str,
    ref: str,
    backend: GitBackend | None = None,
    force: bool = False,
) -> str:
    """Fetch `origin` and rebase the local branch onto the remote tracking branch.

    Rebase is only meaningful for branch refs (including `latest`). For tags or
    commits, the resolved SHA is checked out directly and the rebase flag is
    ignored.
    """
    backend = backend or _default_backend()

    branch = _effective_branch(co, ref, backend)
    if not branch:
        return _fetch_and_checkout(co, url, ref, backend, force=force)

    backend.git("fetch", "origin", git_dir=co.common_dir, stream=True)

    sha = _resolve_remote_branch(co, branch, backend)
    try:
        # Ensure HEAD is on the local branch before rebasing. Using `-B <branch> HEAD`
        # recreates the branch at the current HEAD without moving the worktree.
        checkout_args = ["checkout"]
        if force:
            checkout_args.append("-f")
        checkout_args.extend(["-B", branch, "HEAD"])
        backend.git(*checkout_args, git_dir=co.gitdir, work_tree=co.work_tree, stream=True)
        backend.git("rebase", f"origin/{branch}", git_dir=co.gitdir, work_tree=co.work_tree, stream=True)
    except GitError as e:
        raise GitError(f"rebase of {co.work_tree} onto origin/{branch} failed; resolve or abort the rebase and try again") from e

    return sha


def _print_gitignore_recommendation(parent_root: Path, child: Path) -> None:
    """Recommend that the user add the child path to the parent .gitignore."""
    rel = child.relative_to(parent_root).as_posix() + "/"
    print(f'add "{rel}" to .gitignore')


def _cleanup_new_child(child: Path, existed_before: bool, was_git_folder_before: bool) -> None:
    """Undo the filesystem side effects of a failed child creation.

    If the child directory did not exist before we started, remove it entirely.
    If the child directory existed but was not yet a git-folder, remove only the
    `.gf` directory we created, leaving the original directory in place.
    """
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
) -> None:
    backend = backend or _default_backend()
    child = co.work_tree
    if _is_git_folder_child(co):
        return  # already a git-folder; clone is just an add to the manifest

    effective_ref = ref or "latest"
    resolved_url = _resolved_git_url(url)
    try:
        repo_url, subdir = resolve_repo_url(url, parent_root, backend)
    except GitFoldersError:
        # Unresolvable URLs keep the whole-repo failure surface: the fetch
        # below reports them with the usual failure exit code.
        repo_url, subdir = resolved_url, ""
    if subdir:
        ensure_shared_binding(
            child, parent_root, repo_url, subdir, url, effective_ref,
            override=override, backend=backend, single_branch=single_branch,
        )
        return

    if child.exists() and any(child.iterdir()):
        raise ValidationError(f"child path {child} already exists and is not empty")

    existed_before = child.exists()
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
        _cleanup_new_child(child, existed_before, was_git_folder_before)
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

    gitdir = co.gitdir
    gitdir.mkdir(parents=True, exist_ok=True)
    backend.git("init", git_dir=gitdir, work_tree=co.work_tree)

    # Make sure the child git ignores the .gf directory.
    exclude = gitdir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    patterns = set(exclude.read_text().splitlines()) if exclude.exists() else set()
    patterns.add(layout.GF_DIR)
    patterns.discard("")
    exclude.write_text("\n".join(sorted(patterns)) + "\n")


def update_child(
    co: layout.Checkout,
    url: str,
    ref: str,
    parent_root: Path,
    override: bool = False,
    rebase: bool = False,
    force: bool = False,
    autostash: bool = False,
    backend: GitBackend | None = None,
) -> None:
    backend = backend or _default_backend()
    child = co.work_tree
    effective_ref = ref or "latest"
    resolved_url = _resolved_git_url(url)
    try:
        repo_url, subdir = resolve_repo_url(url, parent_root, backend)
    except GitFoldersError:
        # Unresolvable URLs keep the whole-repo failure surface: the fetch
        # below reports them with the usual failure exit code.
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
            override=override, backend=backend,
        )
        return
    if subdir:
        # Established subfolder binding: the shared store's remote is the
        # repo URL, not the manifest's subdir spelling.
        resolved_url = repo_url

    if not _is_git_folder_child(co):
        if child.exists() and any(child.iterdir()):
            raise ValidationError(f"child path {child} exists and is not a git-folder")

        existed_before = child.exists()
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
            _cleanup_new_child(child, existed_before, was_git_folder_before)
            raise

    gitdir = co.gitdir
    dirty = backend.git_capture(
        "status", "--porcelain", git_dir=gitdir, work_tree=co.work_tree,
    ).strip()
    if dirty and not (force or autostash):
        raise DirtyError(f"child {child} is dirty; commit or stash before pulling")

    stashed = False
    if dirty and autostash:
        backend.git(
            "stash", "push", "-u", "-m", "gf autostash",
            git_dir=gitdir, work_tree=co.work_tree, stream=True,
        )
        stashed = True

    _set_child_origin(co, resolved_url, backend)

    try:
        if rebase:
            sha = _fetch_and_rebase(co, resolved_url, effective_ref, backend, force=force)
        else:
            sha = _fetch_and_checkout(co, resolved_url, effective_ref, backend, force=force)
    except GitFoldersError:
        if stashed:
            # The update failed; try to restore the stashed changes so the
            # user is not left without their work.
            try:
                backend.git("stash", "pop", git_dir=gitdir, work_tree=co.work_tree, stream=True)
            except GitError:
                pass
        raise

    if stashed:
        try:
            backend.git("stash", "pop", git_dir=gitdir, work_tree=co.work_tree, stream=True)
        except GitError as e:
            # Leave the stash in place and surface a clear error so the user
            # can resolve the conflict and pop manually.
            raise GitError(
                f"autostash pop for {child} conflicted; the stash was kept. "
                f"Resolve the conflict and run `git stash pop` manually. {e}"
            ) from e

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


def drift(
    co: layout.Checkout,
    ref: str,
    remote: bool = False,
    backend: GitBackend | None = None,
) -> str:
    """Classify the drift state of the checkout against its effective ref.

    Returns one of `clean`, `behind`, `local-dirty`, `both`, or `missing`.

    This is a local-only operation. It never calls `git fetch`,
    `git remote`, `git ls-remote`, or any other network command. The
    effective ref is resolved against the local remote-tracking refs
    (`refs/remotes/origin/<branch>`), local tags, and local commits
    already present in the checkout's store — the same refs `git status`
    compares against after a `git fetch`. Run `gf pull` or `gf git fetch`
    first to refresh those refs.

    - `missing`: the checkout has no `HEAD` in its gitdir.
    - The effective ref is resolved to the SHA `pull` would check out
      (the remote tracking branch tip for branch/latest refs).
    - The checkout `HEAD` SHA and `git status --porcelain` are read.
    - `clean`: HEAD matches the resolved SHA and the worktree is clean.
    - `behind`: HEAD does not match and the worktree is clean.
    - `local-dirty`: HEAD matches and the worktree is dirty.
    - `both`: HEAD does not match and the worktree is dirty.

    If the effective ref cannot be resolved locally, raises
    `GitFoldersError` instead of returning a state, so `cmd_status` can
    surface a deterministic exit code.
    """
    backend = backend or _default_backend()
    gitdir = co.gitdir
    if not (gitdir / "HEAD").is_file():
        return "missing"

    resolved_sha = _resolve_effective_sha_local(co, ref, backend)

    head_sha = backend.git_capture(
        "rev-parse", "HEAD", git_dir=gitdir, work_tree=co.work_tree,
    ).strip()

    porcelain = backend.git_capture(
        "status", "--porcelain", git_dir=gitdir, work_tree=co.work_tree,
    ).strip()
    dirty = bool(porcelain)
    behind = head_sha != resolved_sha

    if behind and dirty:
        return "both"
    if behind:
        return "behind"
    if dirty:
        return "local-dirty"
    return "clean"


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


def remove_child(child: Path, parent_root: Path) -> None:
    """Unregister a git-folder child, preserving its worktree.

    A subfolder binding whose consumer link `parent_root` owns loses only
    the link: the checkout — including uncommitted work — its sparse cone,
    and the repo store are untouched (GF-D9, GF-D14). A whole-repo child
    keeps its files: `.gf/git` moves to `.git`.
    """
    if child.is_symlink():
        if layout.owns_consumer_link(parent_root, child):
            child.unlink()
            return
        raise GitFoldersError(
            f"{child} is a symlinked child; remove it from the owning worktree instead"
        )
    if child.exists() and child.resolve() != child:
        raise GitFoldersError(
            f"{child} is a symlinked child; remove it from the owning worktree instead"
        )
    if not child.is_dir():
        return

    gf_dir = child / layout.GF_DIR
    git_dir = layout.resolve_checkout(child).gitdir
    if git_dir.is_dir():
        if (child / ".git").exists():
            raise GitFoldersError(f"{child} already contains a .git directory")
        import shutil
        shutil.move(str(git_dir), str(child / ".git"))

    if gf_dir.is_dir():
        import shutil
        shutil.rmtree(gf_dir)


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


def _fetch_store(store: Path, backend: GitBackend) -> None:
    """Fetch `origin` into the repo store, blob-filtered with fallback.

    When the server refuses a filtered fetch, warn and retry unfiltered.
    """
    try:
        backend.git(
            "fetch", "--filter=blob:none", "origin",
            git_dir=store, stream=True,
        )
        return
    except GitError:
        print(
            "gf: warning: remote refused a filtered fetch; "
            "falling back to a full fetch",
            file=sys.stderr,
        )
    backend.git("fetch", "origin", git_dir=store, stream=True)


def ensure_repo_store(
    store: Path,
    url: str,
    *,
    branch: str | None = None,
    single_branch: bool = False,
    backend: GitBackend | None = None,
) -> None:
    """Ensure the bare repo store for `url` exists at `store` with coverage.

    On first creation this runs `git init --bare`, records
    `remote.origin.url` verbatim (URL resolution and normalization are the
    caller's job), writes the first fetch refspec — the branch's line for
    `--single-branch`, else the wildcard — fetches blob-filtered with a
    full-fetch fallback, and repoints the store's `HEAD` at
    `refs/remotes/origin/HEAD` so the bare default symref cannot claim a
    branch a linked checkout wants to attach.

    Joining an existing store appends the requested branch's refspec line
    via `config --add` only when no live `remote.origin.fetch` line covers
    it — lines are append-only, never rewritten or removed.
    """
    backend = backend or _default_backend()
    if not (store / "HEAD").is_file():
        store.mkdir(parents=True, exist_ok=True)
        backend.git("init", "--bare", git_dir=store)
        backend.git("config", "remote.origin.url", url, git_dir=store)
        line = _branch_refspec(branch) if single_branch and branch \
            else _WILDCARD_FETCH_REFSPEC
        backend.git("config", "remote.origin.fetch", line, git_dir=store)
        _fetch_store(store, backend)
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
    if branch:
        line = _branch_refspec(branch)
        lines = _live_refspec_lines(store, backend)
        if _WILDCARD_FETCH_REFSPEC not in lines and line not in lines:
            backend.git(
                "config", "--add", "remote.origin.fetch", line,
                git_dir=store,
            )
            _fetch_store(store, backend)


def _checkout_record_valid(co: layout.Checkout) -> bool:
    """True when the worktree record for `co` exists and points at its
    checkout: the admin dir's `gitdir` file names `<work_tree>/.git`
    (never trust the admin dir's name alone)."""
    gitdir_file = co.gitdir / "gitdir"
    return gitdir_file.is_file() and Path(
        gitdir_file.read_text().strip()
    ) == co.work_tree / ".git"


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
    backend.git(
        "sparse-checkout", "set", "--cone", *union,
        git_dir=co.gitdir, work_tree=co.work_tree, stream=True,
    )
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
    state.save_checkout(co, {
        "resolved": sha,
        "ref": ref,
        "bindings": bindings,
    })


def ensure_checkout(
    co: layout.Checkout,
    ref: str,
    *,
    backend: GitBackend | None = None,
) -> None:
    """Ensure the shared linked checkout `co` exists, widened to its union.

    Creates `<root>/.gf/wt/<repo-key>/<key>` via
    `git worktree add --no-checkout --detach`, verifies the worktree record
    under `<store>/worktrees/` by its `gitdir` file, applies
    `sparse-checkout set --cone` to the union of the checkout's recorded
    binding subdirs plus `co.subdir`, attaches `checkout -B` for a branch
    ref or a detached HEAD for a tag/commit ref, removes the `.git`
    gitfile, locks the worktree idempotently, and writes the per-checkout
    state record including its `bindings` list.

    A checkout path occupied by anything without its matching worktree
    record is an error — gf never rebuilds over existing files. On failure
    only the artifacts this call created are torn down; the repo store is
    never removed.
    """
    backend = backend or _default_backend()
    store = co.common_dir
    admin = co.gitdir
    wt = co.work_tree

    branch = ref if is_branch(co, ref, backend) else None
    sha = resolve_ref(co, ref, backend)

    if _checkout_record_valid(co):
        # Existing checkout: widen the sparse cone and refresh the record.
        if not wt.is_dir():
            if os.path.lexists(wt):
                raise GitFoldersError(
                    f"checkout path {wt} exists but is not a directory; "
                    f"remove it or restore the checkout, then retry"
                )
            wt.mkdir(parents=True, exist_ok=True)
        bindings = _sparse_union(co, backend)
        gitfile = wt / ".git"
        if os.path.lexists(gitfile):
            os.unlink(gitfile)
        _lock_worktree(co, backend)
        _save_checkout_state(co, ref, sha, bindings)
        return

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
        if branch:
            backend.git(
                "checkout", "-B", branch, f"origin/{branch}",
                git_dir=admin, work_tree=wt, stream=True,
            )
        else:
            backend.git(
                "checkout", "--detach", sha,
                git_dir=admin, work_tree=wt, stream=True,
            )
        gitfile = wt / ".git"
        if os.path.lexists(gitfile):
            os.unlink(gitfile)
        _lock_worktree(co, backend)
    except Exception:
        if created:
            # Undo only what this call created — the worktree record and
            # the checkout dir. The shared repo store stays.
            shutil.rmtree(admin, ignore_errors=True)
            shutil.rmtree(wt, ignore_errors=True)
        raise
    _save_checkout_state(co, ref, sha, bindings)


def ensure_consumer_link(
    link: Path, target: Path
) -> tuple[str, str | None] | None:
    """Ensure `link` is a relative symlink to `target`.

    Returns None when the link already points at `target`; otherwise a
    (action, previous-target) pair describing what this call changed so a
    later failure can undo exactly it via `rollback_consumer_link`.
    Existing non-link content is never destroyed: an empty directory is
    replaced; anything else is an error.
    """
    if link.is_symlink():
        if Path(os.path.realpath(link)) == Path(os.path.realpath(target)):
            return None
        old = os.readlink(link)
        link.unlink()
        try:
            _place_consumer_link(link, target)
        except Exception:
            os.symlink(old, link, target_is_directory=True)
            raise
        return ("retargeted", old)
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
    return ("replaced-dir" if replaced else "created", None)


def _place_consumer_link(link: Path, target: Path) -> None:
    """Create the relative symlink, reporting fs errors as ValidationError."""
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(
            os.path.relpath(target, link.parent), link,
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
    if link.is_symlink():
        link.unlink()
    if name == "retargeted" and old is not None:
        os.symlink(old, link, target_is_directory=True)
    elif name == "replaced-dir":
        link.mkdir(parents=True, exist_ok=True)


def _remote_default_branch(url: str, backend: GitBackend) -> str | None:
    """The remote's default branch from `git ls-remote --symref`, or None."""
    result = backend.git(
        "ls-remote", "--symref", url, "HEAD", check=False,
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
        "ls-remote", url, f"refs/heads/{branch}", check=False,
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

    Removes the linked worktree, its `<store>/worktrees/<key>` admin dir,
    and the `<key>.state` file — never the repo store itself.
    """
    backend.git(
        "worktree", "remove", "--force", str(co.work_tree),
        git_dir=co.common_dir, check=False,
    )
    shutil.rmtree(co.gitdir, ignore_errors=True)
    shutil.rmtree(co.work_tree, ignore_errors=True)
    try:
        co.state.unlink()
    except OSError:
        pass


def _print_binding_gitignore(parent_root: Path, child: Path) -> None:
    """Recommend the binding's consumer path for the parent .gitignore."""
    rel = child.relative_to(parent_root).as_posix()
    print(f'add "{rel}" to .gitignore')


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
) -> None:
    """Create or join the shared-store binding whose consumer link is `child`.

    Ensures the repo store (`--single-branch` narrows the first refspec
    line only when this call creates the store; joins never re-narrow),
    derives the checkout key from the resolved branch — `latest` resolves
    to the effective branch first, tags/commits key as `ref=<ref>` —
    ensures the shared sparse checkout, places the relative consumer link
    at `child` pointing into `<checkout>/<subdir>`, and records the
    binding's state. On a later-step failure the link is rolled back and
    only the checkout this call created is torn down; the store and other
    bindings' checkouts stay.
    """
    store = layout.repo_store(parent_root, repo_url)
    branch = _binding_branch(store, repo_url, ref, backend)
    store_created = not (store / "HEAD").is_file()
    ensure_repo_store(
        store, repo_url, branch=branch, single_branch=single_branch,
        backend=backend,
    )
    key = (
        layout.checkout_key_for_branch(branch)
        if branch else layout.checkout_key_for_ref(ref)
    )
    co = layout.subfolder_checkout(parent_root, repo_url, key, subdir)
    checkout_existed = _checkout_record_valid(co)
    ensure_checkout(co, branch or ref, backend=backend)

    action = None
    try:
        action = ensure_consumer_link(child, co.work_tree / co.subdir)
        rec = state.load_checkout(co)
        rec.update({"url": url, "override": override})
        state.save_checkout(co, rec)
    except Exception:
        rollback_consumer_link(child, action)
        if not checkout_existed:
            _remove_checkout(co, backend)
        raise

    if store_created:
        print(f'add "{layout.GF_DIR}/" to .gitignore')
    _print_binding_gitignore(parent_root, child)


def strip_placeholder_child(child: Path, backend: GitBackend | None = None) -> bool:
    """Remove a `gf init` placeholder child and return True.

    A placeholder is a real directory whose only entry is `.gf` and whose
    gitdir has no commits (`rev-parse --verify HEAD` fails). Any other
    content — user files, or a `.gf` gitdir with history — is left
    untouched and False is returned: conversion refuses by deleting
    nothing.
    """
    backend = backend or _default_backend()
    if (
        child.is_dir() and not child.is_symlink()
        and [p.name for p in child.iterdir()] == [layout.GF_DIR]
        and backend.git(
            "rev-parse", "--verify", "HEAD",
            git_dir=child / layout.GF_DIR / "git", check=False,
        ).returncode != 0
    ):
        shutil.rmtree(child)
        return True
    return False


def _apply_shared_checkout(
    co: layout.Checkout,
    ref: str,
    branch: str | None,
    backend: GitBackend,
    *,
    rebase: bool = False,
    force: bool = False,
) -> str:
    """Apply the resolved ref to an existing shared checkout once.

    Mirrors `_fetch_and_checkout`/`_fetch_and_rebase` minus the fetch —
    a grouped pull already fetched the repo store once.
    """
    if branch:
        sha = _resolve_remote_branch(co, branch, backend)
        if rebase:
            checkout_args = ["checkout"]
            if force:
                checkout_args.append("-f")
            checkout_args.extend(["-B", branch, "HEAD"])
            backend.git(
                *checkout_args, git_dir=co.gitdir, work_tree=co.work_tree,
                stream=True,
            )
            try:
                backend.git(
                    "rebase", f"origin/{branch}",
                    git_dir=co.gitdir, work_tree=co.work_tree, stream=True,
                )
            except GitError as e:
                raise GitError(
                    f"rebase of {co.work_tree} onto origin/{branch} "
                    f"failed; resolve or abort the rebase and try again"
                ) from e
            return sha
        checkout_args = ["checkout"]
        if force:
            checkout_args.append("-f")
        checkout_args.extend(["-B", branch, f"origin/{branch}"])
        backend.git(
            *checkout_args, git_dir=co.gitdir, work_tree=co.work_tree,
            stream=True,
        )
        return sha
    sha = resolve_ref(co, ref, backend)
    checkout_args = ["checkout"]
    if force:
        checkout_args.append("-f")
    checkout_args.append(sha)
    backend.git(
        *checkout_args, git_dir=co.gitdir, work_tree=co.work_tree,
        stream=True,
    )
    return sha


def _vacate_checkout(
    old_co: layout.Checkout,
    folders: list[dict],
    parent_root: Path,
) -> None:
    """Drop moved bindings from a vacated shared checkout's record.

    The recorded `bindings` are recomputed from the manifest: a subdir
    stays only while some consumer link still resolves into this
    checkout's mapped directory. The worktree's files — tracked and
    uncommitted — are never touched.
    """
    if not _checkout_record_valid(old_co):
        return
    live = set()
    for folder in folders:
        child = parent_root / folder["path"]
        if not child.is_symlink():
            continue
        fco = layout.resolve_checkout(child.resolve())
        if fco.gitdir != fco.common_dir and fco.gitdir == old_co.gitdir:
            live.add(fco.subdir)
    rec = state.load_checkout(old_co)
    bindings = [
        b for b in rec.get("bindings", [])
        if isinstance(b, str) and b in live
    ]
    if bindings != rec.get("bindings"):
        rec["bindings"] = bindings
        state.save_checkout(old_co, rec)


def _siblings_served(
    folders: list[dict],
    excluded_names: set,
    parent_root: Path,
    store: Path,
    key: str,
) -> list[dict]:
    """Manifest bindings outside `excluded_names` whose consumer link
    currently resolves into the shared checkout (`store`, `key`)."""
    served = []
    for folder in folders:
        if folder.get("name") in excluded_names:
            continue
        child = parent_root / folder["path"]
        if not child.is_symlink():
            continue
        co = layout.resolve_checkout(child.resolve())
        if (
            co.gitdir != co.common_dir
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
    force: bool = False,
    autostash: bool = False,
    backend: GitBackend | None = None,
) -> list[str]:
    """Pull resolved subfolder-binding `items`, grouped by store+checkout.

    Per repo store: `ensure_repo_store` coverage (creating the store with
    wildcard refspec + fetch when missing; an uncovered explicit branch
    gets one fetch-probe that records its refspec line only when it lands)
    followed by exactly one store fetch. Resolution is recorded/local —
    never an `ls-remote`/`remote set-head` re-probe. Per checkout key:
    `ensure_checkout` per binding (record check + cone-union widening +
    gitfile/lock), one dirty check over the union cone — `--force` /
    `--autostash` apply to the whole checkout and a dirty shared checkout
    blocks every binding it serves — then a single ref apply. Per binding:
    `ensure_consumer_link` create/retarget to `<checkout>/<subdir>` plus a
    merged `save_checkout`; a retarget away from another checkout drops
    the binding from the vacated checkout's recorded bindings. Returns one
    output line per binding served; unselected siblings whose shared
    checkout moved are marked `(moved with <leader>)`.
    """
    backend = backend or _default_backend()
    lines: list[str] = []
    excluded = {it["folder"]["name"] for it in items}

    by_store: dict[Path, list[dict]] = {}
    for it in items:
        it["store"] = layout.repo_store(parent_root, it["repo_url"])
        by_store.setdefault(it["store"], []).append(it)

    for store, sitems in by_store.items():
        repo_url = sitems[0]["repo_url"]
        fetched = False
        if not (store / "HEAD").is_file():
            ensure_repo_store(store, repo_url, backend=backend)
            fetched = True
        for it in sitems:
            ref = it["ref"]
            if ref in ("latest", ""):
                branch = _store_default_branch(store, backend)
            elif re.fullmatch(r"[0-9a-f]{40}", ref):
                branch = None
            elif _store_has_branch(store, ref, backend):
                branch = ref
            elif backend.git(
                "fetch", "origin", _branch_refspec(ref),
                git_dir=store, check=False,
            ).returncode == 0:
                # An uncovered branch on a narrow store: the fetch-probe
                # landed it, so append its refspec line for future pulls.
                backend.git(
                    "config", "--add", "remote.origin.fetch",
                    _branch_refspec(ref), git_dir=store,
                )
                branch, fetched = ref, True
            else:
                branch = None
            it["branch"] = branch
            it["key"] = (
                layout.checkout_key_for_branch(branch)
                if branch else layout.checkout_key_for_ref(ref or "latest")
            )
        if not fetched:
            _fetch_store(store, backend)

        by_key: dict[str, list[dict]] = {}
        for it in sitems:
            by_key.setdefault(it["key"], []).append(it)

        for key, kitems in by_key.items():
            lead = kitems[0]
            ref, branch = lead["ref"], lead["branch"]
            try:
                co = layout.subfolder_checkout(
                    parent_root, repo_url, key, lead["subdir"])
                existed = _checkout_record_valid(co)
                for it in kitems:
                    ensure_checkout(
                        layout.subfolder_checkout(
                            parent_root, repo_url, key, it["subdir"]),
                        branch or ref, backend=backend)

                rec = state.load_checkout(co)
                cone = sorted(
                    {b for b in rec.get("bindings", [])
                     if isinstance(b, str) and b}
                    | {it["subdir"] for it in kitems})
                dirty = backend.git_capture(
                    "status", "--porcelain", "--", *cone,
                    git_dir=co.gitdir, work_tree=co.work_tree,
                ).strip()
                if dirty and not (force or autostash):
                    raise DirtyError(
                        f"checkout {co.work_tree} is dirty; commit or "
                        f"stash before pulling")

                stashed = False
                if dirty and autostash:
                    backend.git(
                        "stash", "push", "-u", "-m", "gf autostash",
                        git_dir=co.gitdir, work_tree=co.work_tree,
                        stream=True,
                    )
                    stashed = True
                try:
                    if existed:
                        sha = _apply_shared_checkout(
                            co, ref, branch, backend,
                            rebase=rebase, force=force)
                    else:
                        # `ensure_checkout` already applied the ref when it
                        # created the checkout.
                        sha = resolve_ref(co, branch or ref, backend)
                except GitFoldersError:
                    if stashed:
                        try:
                            backend.git(
                                "stash", "pop", git_dir=co.gitdir,
                                work_tree=co.work_tree, stream=True)
                        except GitError:
                            pass
                    raise
                if stashed:
                    try:
                        backend.git(
                            "stash", "pop", git_dir=co.gitdir,
                            work_tree=co.work_tree, stream=True)
                    except GitError as e:
                        raise GitError(
                            f"autostash pop for {co.work_tree} conflicted; "
                            f"the stash was kept. Resolve the conflict and "
                            f"run `git stash pop` manually. {e}") from e
            except GitFoldersError as e:
                e.folder = lead["folder"]
                raise

            for it in kitems:
                it_co = layout.subfolder_checkout(
                    parent_root, repo_url, key, it["subdir"])
                try:
                    ensure_consumer_link(
                        it["child"], it_co.work_tree / it_co.subdir)
                    old_co = it["co"]
                    if old_co.gitdir != old_co.common_dir and (
                        old_co.common_dir != store
                        or old_co.gitdir != it_co.gitdir
                        or old_co.subdir != it["subdir"]
                    ):
                        _vacate_checkout(old_co, folders, parent_root)
                    rec = state.load_checkout(it_co)
                    rec.update({
                        "resolved": sha,
                        "ref": it["ref"],
                        "url": it["url"],
                        "override": it["override"],
                    })
                    state.save_checkout(it_co, rec)
                except GitFoldersError as e:
                    e.folder = it["folder"]
                    raise
                lines.append(f"Pulled {it['folder']['name']}")
            for sib in _siblings_served(
                    folders, excluded, parent_root, store, key):
                lines.append(
                    f"Pulled {sib['name']} "
                    f"(moved with {lead['folder']['name']})")
    return lines


def select_children(parent_root: Path, cwd: Path, args: list[str], manifest: dict) -> list[dict]:
    """Select git-folders from the manifest based on context and args.

    Matching is realpath-aware (spec "Target selection rules"): a target
    that resolves inside a binding's map selects the innermost such
    binding; otherwise the target selects every binding at or below it.
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
    contains `target`, else every binding at or below `target`."""
    inside = [s for s in folders if _path_matches(parent_root, s["path"], target)]
    if inside:
        deepest = max(len(_map_root(parent_root, s["path"]).parts) for s in inside)
        return [s for s in inside
                if len(_map_root(parent_root, s["path"]).parts) == deepest]
    return [s for s in folders if _below(parent_root, s["path"], target)]


def _map_root(parent_root: Path, folder_path: str) -> Path:
    """The realpath a binding's consumer path maps to: the child directory
    for a whole-repo binding, the mapped checkout subdir for a subfolder
    binding."""
    return Path(os.path.realpath(parent_root / folder_path))


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
    """True when the binding's consumer path or its map is at-or-below `target`."""
    unresolved = parent_root / folder_path
    return unresolved.is_relative_to(target) or _map_root(
        parent_root, folder_path
    ).is_relative_to(Path(os.path.realpath(target)))
