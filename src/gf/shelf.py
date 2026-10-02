# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import os
import re
import shutil
from pathlib import Path

from .backends import GitBackend, GitCliBackend
from . import manifest as _manifest, state
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
    if (p / ".gf" / "git" / "HEAD").is_file():
        return str(p / ".gf" / "git")
    return url


def _gitdir(child: Path) -> Path:
    return child / ".gf" / "git"


def resolve_ref(
    child: Path,
    ref: str,
    backend: GitBackend | None = None,
) -> str:
    """Resolve a ref to a SHA using the child gitdir."""
    backend = backend or _default_backend()
    gitdir = _gitdir(child)

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
                "rev-parse", refspec, git_dir=gitdir, work_tree=child,
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
                    "rev-parse", candidate, git_dir=gitdir, work_tree=child,
                ).strip()
            except GitError:
                continue

    raise ValidationError(f"could not resolve ref '{ref}' in {child}")


def _ensure_origin_head(child: Path, backend: GitBackend | None = None) -> None:
    """Ensure refs/remotes/origin/HEAD is a valid symbolic ref in the child."""
    backend = backend or _default_backend()
    gitdir = _gitdir(child)

    result = backend.git(
        "symbolic-ref", "refs/remotes/origin/HEAD",
        git_dir=gitdir, work_tree=child, check=False,
    )
    if result.returncode == 0:
        target = result.stdout.strip()
        verify = backend.git(
            "show-ref", "--verify", target,
            git_dir=gitdir, work_tree=child, check=False,
        )
        if verify.returncode == 0:
            return

    backend.git(
        "remote", "set-head", "origin", "-a",
        git_dir=gitdir, work_tree=child, stream=True,
    )


def _effective_branch(
    child: Path,
    ref: str,
    backend: GitBackend | None = None,
) -> str | None:
    """Return the local branch name for a branch ref or the remote default branch.

    Returns None for tags, commits, and other non-branch refs.
    """
    backend = backend or _default_backend()
    gitdir = _gitdir(child)

    if ref in ("latest", ""):
        _ensure_origin_head(child, backend)
        try:
            head = backend.git_capture(
                "symbolic-ref", "refs/remotes/origin/HEAD",
                git_dir=gitdir, work_tree=child,
            ).strip()
            return head.split("/")[-1]
        except GitError:
            return "master"

    if is_branch(child, ref, backend):
        return ref
    return None


def is_branch(
    child: Path,
    ref: str,
    backend: GitBackend | None = None,
) -> bool:
    backend = backend or _default_backend()
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return False
    if ref in ("latest", ""):
        return False

    gitdir = _gitdir(child)
    for candidate in (f"refs/heads/{ref}", f"refs/remotes/origin/{ref}"):
        result = backend.git(
            "show-ref", "--verify", candidate,
            git_dir=gitdir, work_tree=child, check=False,
        )
        if result.returncode == 0:
            return True
    return False


def _set_child_origin(
    child: Path,
    url: str,
    backend: GitBackend | None = None,
) -> None:
    """Ensure the child `origin` remote points to the actual git-folder URL."""
    backend = backend or _default_backend()
    gitdir = _gitdir(child)

    result = backend.git(
        "remote", "get-url", "origin",
        git_dir=gitdir, work_tree=child, check=False,
    )
    if result.returncode == 0:
        backend.git(
            "remote", "set-url", "origin", url,
            git_dir=gitdir, work_tree=child,
        )
    else:
        backend.git(
            "remote", "add", "origin", url,
            git_dir=gitdir, work_tree=child,
        )

    backend.git(
        "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*",
        git_dir=gitdir, work_tree=child,
    )


def _init_child_gitdir(
    child: Path,
    backend: GitBackend | None = None,
) -> None:
    """Create a .gf/git gitdir in child."""
    backend = backend or _default_backend()
    gf = child / ".gf"
    gitdir = gf / "git"
    gf.mkdir(parents=True, exist_ok=True)
    gitdir.mkdir(parents=True, exist_ok=True)

    # Initialize a fresh gitdir at .gf/git with this worktree.
    backend.git("init", git_dir=gitdir, work_tree=child)

    # Make sure the child git ignores the .gf directory.
    exclude = gitdir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    patterns = set(exclude.read_text().splitlines()) if exclude.exists() else set()
    patterns.add(".gf")
    patterns.discard("")
    exclude.write_text("\n".join(sorted(patterns)) + "\n")


def _resolve_remote_branch(child: Path, branch: str, backend: GitBackend | None = None) -> str:
    """Resolve a remote tracking branch to a SHA in the child gitdir."""
    backend = backend or _default_backend()
    gitdir = _gitdir(child)
    try:
        return backend.git_capture(
            "rev-parse", f"refs/remotes/origin/{branch}",
            git_dir=gitdir, work_tree=child,
        ).strip()
    except GitError as e:
        raise ValidationError(f"could not resolve remote branch 'origin/{branch}' in {child}") from e


def _fetch_and_checkout(
    child: Path,
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
    gitdir = _gitdir(child)

    fetch_args = ["fetch"]
    if depth is not None:
        fetch_args.append(f"--depth={depth}")
    fetch_args.append("origin")

    backend.git(*fetch_args, git_dir=gitdir, work_tree=child, stream=True)

    branch = _effective_branch(child, ref, backend)
    if branch and single_branch:
        # Narrow the fetch refspec to the resolved branch so subsequent
        # fetches only fetch that branch's history.
        backend.git(
            "config", "remote.origin.fetch",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            git_dir=gitdir, work_tree=child,
        )

    if branch:
        sha = _resolve_remote_branch(child, branch, backend)
        checkout_args = ["checkout"]
        if force:
            checkout_args.append("-f")
        checkout_args.extend(["-B", branch, f"origin/{branch}"])
        backend.git(
            *checkout_args, git_dir=gitdir, work_tree=child, stream=True,
        )
    else:
        sha = resolve_ref(child, ref, backend)
        checkout_args = ["checkout"]
        if force:
            checkout_args.append("-f")
        checkout_args.append(sha)
        backend.git(
            *checkout_args, git_dir=gitdir, work_tree=child, stream=True,
        )

    return sha


def _fetch_and_rebase(
    child: Path,
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
    gitdir = _gitdir(child)

    branch = _effective_branch(child, ref, backend)
    if not branch:
        return _fetch_and_checkout(child, url, ref, backend, force=force)

    backend.git("fetch", "origin", git_dir=gitdir, work_tree=child, stream=True)

    sha = _resolve_remote_branch(child, branch, backend)
    try:
        # Ensure HEAD is on the local branch before rebasing. Using `-B <branch> HEAD`
        # recreates the branch at the current HEAD without moving the worktree.
        checkout_args = ["checkout"]
        if force:
            checkout_args.append("-f")
        checkout_args.extend(["-B", branch, "HEAD"])
        backend.git(*checkout_args, git_dir=gitdir, work_tree=child, stream=True)
        backend.git("rebase", f"origin/{branch}", git_dir=gitdir, work_tree=child, stream=True)
    except GitError as e:
        raise GitError(f"rebase of {child} onto origin/{branch} failed; resolve or abort the rebase and try again") from e

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
        gf = child / ".gf"
        if gf.exists():
            shutil.rmtree(gf, ignore_errors=True)


def init_child(
    child: Path,
    url: str,
    ref: str,
    parent_root: Path,
    override: bool = False,
    backend: GitBackend | None = None,
    depth: int | None = None,
    single_branch: bool = False,
) -> None:
    backend = backend or _default_backend()
    if _is_git_folder_child(child):
        return  # already a git-folder; clone is just an add to the manifest
    if child.exists() and any(child.iterdir()):
        raise ValidationError(f"child path {child} already exists and is not empty")

    existed_before = child.exists()
    was_git_folder_before = False
    effective_ref = ref or "latest"
    resolved_url = _resolved_git_url(url)

    try:
        child.mkdir(parents=True, exist_ok=True)
        _init_child_gitdir(child, backend)
        _set_child_origin(child, resolved_url, backend)

        sha = _fetch_and_checkout(
            child, resolved_url, effective_ref, backend,
            depth=depth, single_branch=single_branch,
        )

        # Record the resolved state.
        state.save(child, {
            "resolved": sha,
            "ref": effective_ref,
            "url": url,
            "override": override,
        })

        _print_gitignore_recommendation(parent_root, child)
    except Exception:
        _cleanup_new_child(child, existed_before, was_git_folder_before)
        raise


def _is_git_folder_child(child: Path) -> bool:
    return (child / ".gf" / "git" / "HEAD").is_file()


def init_git_folder(target: Path, backend: GitBackend | None = None) -> None:
    """Create a fresh `.gf` gitdir at `target` without a git-folder binding.

    Like `git init`, this leaves an empty child worktree ready for later use.
    """
    backend = backend or _default_backend()
    if _is_git_folder_child(target) or (target / ".git").exists():
        raise ValidationError(f"{target} is already a git or git-folder directory")

    gf = target / ".gf"
    gitdir = gf / "git"
    gf.mkdir(parents=True, exist_ok=True)
    gitdir.mkdir(parents=True, exist_ok=True)
    backend.git("init", git_dir=gitdir, work_tree=target)

    # Make sure the child git ignores the .gf directory.
    exclude = gitdir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    patterns = set(exclude.read_text().splitlines()) if exclude.exists() else set()
    patterns.add(".gf")
    patterns.discard("")
    exclude.write_text("\n".join(sorted(patterns)) + "\n")


def update_child(
    child: Path,
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
    effective_ref = ref or "latest"
    resolved_url = _resolved_git_url(url)

    if not _is_git_folder_child(child):
        if child.exists() and any(child.iterdir()):
            raise ValidationError(f"child path {child} exists and is not a git-folder")

        existed_before = child.exists()
        was_git_folder_before = False
        try:
            child.mkdir(parents=True, exist_ok=True)
            _init_child_gitdir(child, backend)
            _set_child_origin(child, resolved_url, backend)

            sha = _fetch_and_checkout(child, resolved_url, effective_ref, backend)

            state.save(child, {
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

    gitdir = _gitdir(child)
    dirty = backend.git_capture(
        "status", "--porcelain", git_dir=gitdir, work_tree=child,
    ).strip()
    if dirty and not (force or autostash):
        raise DirtyError(f"child {child} is dirty; commit or stash before pulling")

    stashed = False
    if dirty and autostash:
        backend.git(
            "stash", "push", "-u", "-m", "gf autostash",
            git_dir=gitdir, work_tree=child, stream=True,
        )
        stashed = True

    _set_child_origin(child, resolved_url, backend)

    try:
        if rebase:
            sha = _fetch_and_rebase(child, resolved_url, effective_ref, backend, force=force)
        else:
            sha = _fetch_and_checkout(child, resolved_url, effective_ref, backend, force=force)
    except GitFoldersError:
        if stashed:
            # The update failed; try to restore the stashed changes so the
            # user is not left without their work.
            try:
                backend.git("stash", "pop", git_dir=gitdir, work_tree=child, stream=True)
            except GitError:
                pass
        raise

    if stashed:
        try:
            backend.git("stash", "pop", git_dir=gitdir, work_tree=child, stream=True)
        except GitError as e:
            # Leave the stash in place and surface a clear error so the user
            # can resolve the conflict and pop manually.
            raise GitError(
                f"autostash pop for {child} conflicted; the stash was kept. "
                f"Resolve the conflict and run `git stash pop` manually. {e}"
            ) from e

    # Record the resolved state.
    state.save(child, {
        "resolved": sha,
        "ref": effective_ref,
        "url": url,
        "override": override,
    })


def is_linked_child(child: Path, parent_root: Path) -> bool:
    """Return True if `child` is a symlink to a git-folder child outside `parent_root`."""
    if not child.exists():
        return False
    resolved = child.resolve()
    if resolved.is_relative_to(parent_root.resolve()):
        return False
    return _manifest.is_git_folder_child(resolved)


def _resolve_effective_sha(
    child: Path,
    ref: str,
    backend: GitBackend | None = None,
) -> str:
    """Resolve the effective ref to the SHA `pull` would check out.

    For `branch` and `latest` refs this is the remote tracking branch tip
    (`refs/remotes/origin/<branch>`). For tags and commits it is the
    peeled SHA. This is the SHA drift is measured against.
    """
    backend = backend or _default_backend()
    effective_ref = ref or "latest"

    branch = _effective_branch(child, effective_ref, backend)
    if branch:
        return _resolve_remote_branch(child, branch, backend)
    return resolve_ref(child, effective_ref, backend)


def _resolve_effective_sha_local(
    child: Path,
    ref: str,
    backend: GitBackend | None = None,
) -> str:
    """Resolve the effective ref to a SHA using only local refs.

    Like `_resolve_effective_sha`, but never touches the network. For
    `latest` it reads the local `refs/remotes/origin/HEAD` symbolic ref
    and falls back to `refs/remotes/origin/main` then
    `refs/remotes/origin/master` without calling `git remote set-head`.
    Used by `gf status --remote`, which must be local-only.
    """
    backend = backend or _default_backend()
    gitdir = _gitdir(child)
    effective_ref = ref or "latest"

    if effective_ref in ("latest", ""):
        # Prefer the local origin/HEAD symbolic ref, then fall back to
        # origin/main and origin/master. No `git remote set-head` or
        # other network call is made.
        head_ref = "refs/remotes/origin/HEAD"
        result = backend.git(
            "symbolic-ref", head_ref,
            git_dir=gitdir, work_tree=child, check=False,
        )
        if result.returncode == 0:
            target = result.stdout.strip()
            if target and backend.git(
                "show-ref", "--verify", target,
                git_dir=gitdir, work_tree=child, check=False,
            ).returncode == 0:
                return backend.git_capture(
                    "rev-parse", target,
                    git_dir=gitdir, work_tree=child,
                ).strip()
        for candidate in ("refs/remotes/origin/main", "refs/remotes/origin/master"):
            if backend.git(
                "show-ref", "--verify", candidate,
                git_dir=gitdir, work_tree=child, check=False,
            ).returncode == 0:
                return backend.git_capture(
                    "rev-parse", candidate,
                    git_dir=gitdir, work_tree=child,
                ).strip()
        raise ValidationError(
            f"could not resolve 'latest' for {child}: no local "
            f"refs/remotes/origin/HEAD, main, or master; run `gf pull` "
            f"or `gf git fetch` first"
        )

    # Branch ref: resolve the remote tracking branch locally.
    if is_branch(child, effective_ref, backend):
        return _resolve_remote_branch(child, effective_ref, backend)

    # Tag or commit: resolve via local refs only.
    return resolve_ref(child, effective_ref, backend)


def drift(
    child: Path,
    ref: str,
    remote: bool = False,
    backend: GitBackend | None = None,
) -> str:
    """Classify the drift state of `child` against its effective ref.

    Returns one of `clean`, `behind`, `local-dirty`, `both`, or `missing`.

    This is a local-only operation. It never calls `git fetch`,
    `git remote`, `git ls-remote`, or any other network command. The
    effective ref is resolved against the local remote-tracking refs
    (`refs/remotes/origin/<branch>`), local tags, and local commits
    already present in the child gitdir — the same refs `git status`
    compares against after a `git fetch`. Run `gf pull` or `gf git fetch`
    first to refresh those refs.

    - `missing`: the child has no `.gf/git/HEAD`.
    - The effective ref is resolved to the SHA `pull` would check out
      (the remote tracking branch tip for branch/latest refs).
    - The child `HEAD` SHA and `git status --porcelain` are read.
    - `clean`: HEAD matches the resolved SHA and the worktree is clean.
    - `behind`: HEAD does not match and the worktree is clean.
    - `local-dirty`: HEAD matches and the worktree is dirty.
    - `both`: HEAD does not match and the worktree is dirty.

    If the effective ref cannot be resolved locally, raises
    `GitFoldersError` instead of returning a state, so `cmd_status` can
    surface a deterministic exit code.
    """
    backend = backend or _default_backend()
    gitdir = _gitdir(child)
    if not (gitdir / "HEAD").is_file():
        return "missing"

    resolved_sha = _resolve_effective_sha_local(child, ref, backend)

    head_sha = backend.git_capture(
        "rev-parse", "HEAD", git_dir=gitdir, work_tree=child,
    ).strip()

    porcelain = backend.git_capture(
        "status", "--porcelain", git_dir=gitdir, work_tree=child,
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


def remove_child(child: Path) -> None:
    """Unregister a git-folder child, moving its gitdir to .git in the worktree.

    The consumer worktree files are preserved; only the `.gf` wrapper
    is removed.
    """
    if child.is_symlink() or (child.exists() and child.resolve() != child):
        raise GitFoldersError(
            f"{child} is a symlinked child; remove it from the owning worktree instead"
        )
    if not child.is_dir():
        return

    gf_dir = child / ".gf"
    git_dir = gf_dir / "git"
    if git_dir.is_dir():
        if (child / ".git").exists():
            raise GitFoldersError(f"{child} already contains a .git directory")
        import shutil
        shutil.move(str(git_dir), str(child / ".git"))

    if gf_dir.is_dir():
        import shutil
        shutil.rmtree(gf_dir)


def select_children(parent_root: Path, cwd: Path, args: list[str], manifest: dict) -> list[dict]:
    """Select git-folders from the manifest based on context and args."""
    folders = manifest.get("git_folder", [])
    if not folders:
        return []

    if args:
        selected = []
        for arg in args:
            target = cwd / arg
            matched = [s for s in folders if _path_matches(parent_root, s["path"], target)]
            if not matched and any(folder.get("name") == arg for folder in folders):
                # Fallback: arg is a git-folder name.
                matched = [s for s in folders if s.get("name") == arg]
            selected.extend(matched)
        return selected

    # No args: cwd inside a child -> that child; otherwise all children below cwd.
    child = _manifest.find_child_root(cwd, parent_root)
    if child:
        return [s for s in folders if _path_matches(parent_root, s["path"], child)]

    return [s for s in folders if _path_matches(parent_root, s["path"], cwd)]


def _path_matches(parent_root: Path, folder_path: str, target: Path) -> bool:
    """Match a git-folder path to a target, handling symlinked children.

    `target` is the unresolved path the user provided or the current cwd.
    """
    folder_unresolved = parent_root / folder_path
    folder_resolved = folder_unresolved.resolve()
    target_resolved = target.resolve()

    if folder_resolved == target_resolved:
        return True
    if target_resolved.is_relative_to(folder_resolved) or folder_resolved.is_relative_to(target_resolved):
        return True
    if folder_unresolved.is_relative_to(target):
        return True
    if target.is_relative_to(folder_unresolved):
        return True
    return False
