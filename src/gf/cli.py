# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import argparse
import importlib.metadata
import io
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from pathlib import Path

import tomli_w

from .backends import GitBackend, GitCliBackend
from . import layout, manifest, platform, runner, shelf
from .exceptions import DirtyError, GitError, GitFoldersError, ValidationError, folder_error


def die(msg: str, code: int = 1) -> None:
    print(f"gf: {msg}", file=sys.stderr)
    sys.exit(code)


def _get_version() -> str:
    """Return the git-folders package version.

    Uses importlib.metadata when the package is installed, with a
    pyproject.toml fallback for running from a source checkout.
    """
    try:
        return importlib.metadata.version("git-folders")
    except importlib.metadata.PackageNotFoundError:
        pass
    # Fallback: read pyproject.toml from the project root.
    here = Path(__file__).resolve()
    for p in (here.parent, *here.parents):
        candidate = p / "pyproject.toml"
        if candidate.is_file():
            with open(candidate, "rb") as f:
                data = tomllib.load(f)
            v = data.get("project", {}).get("version")
            if v:
                return v
            break
    # Final fallback matches the current package version in pyproject.toml.
    return "0.1.0"


def _logical_cwd() -> Path:
    return platform.logical_cwd()


def _apply_chdir(argv: list[str]) -> list[str]:
    """Apply leading global -C options and return the remaining argv untouched.

    Only `-C <path>` pairs that appear *before* the subcommand are treated as
    global `gf` options. Any `-C` that appears after the subcommand is kept in
    the returned argv so it can be forwarded to git (e.g. `gf diff -C`).
    """
    i = 0
    while i < len(argv):
        if argv[i] == "-C" and i + 1 < len(argv):
            path = argv[i + 1]
            os.chdir(path)
            os.environ["PWD"] = os.path.abspath(os.path.expanduser(path))
            i += 2
        else:
            break
    return argv[i:]


def _normalize_url(url: str) -> str:
    """Expand a bare host/path URL into an https:// URL."""
    if "://" in url or url.startswith("git@"):
        return url
    if "/" in url:
        head, _, _ = url.partition("/")
        if "." in head and " " not in head and not head.startswith("."):
            return f"https://{url}"
    return url


def _name_from_url(url: str) -> str:
    """Derive a git-folder name (basename) from a git URL or local path."""
    url = _normalize_url(url)
    if "://" in url:
        name = os.path.basename(urllib.parse.urlparse(url).path)
    elif url.startswith("git@"):
        rest = url[4:]
        if ":" in rest:
            _, path = rest.split(":", 1)
            name = os.path.basename(path)
        else:
            name = os.path.basename(rest)
    else:
        name = os.path.basename(url)
    return name.removesuffix(".git") or "git-folder"


def cmd_clone(args, backend: GitBackend) -> int:
    parent, manifest_data = _resolve(_logical_cwd())
    if parent is None:
        die("not inside a git repo")

    if manifest_data is None:
        manifest_data = {"git_folder": []}
        manifest.write_manifest(parent, manifest_data)

    url = _normalize_url(args.url)
    path = args.path
    name = args.name
    if name is None:
        name = _name_from_url(path) if path else _name_from_url(url)

    child_path = (
        Path(path).resolve()
        if path
        else (_logical_cwd() / Path(name).name).resolve()
    )
    if not child_path.is_relative_to(parent):
        die(folder_error(
            name, str(child_path), "clone",
            "child path must be inside the parent repo"))

    rel = child_path.relative_to(parent).as_posix()
    for folder in manifest_data.get("git_folder", []):
        if folder.get("name") == name:
            die(folder_error(
                name, rel, "clone",
                f"git-folder '{name}' already exists in manifest"))
        if Path(folder["path"]).as_posix() == rel:
            die(folder_error(
                name, rel, "clone",
                f"path {rel} is already used by git-folder '{folder['name']}'"))

    try:
        shelf.init_child(
            layout.resolve_checkout(child_path), url, args.branch or "", parent,
            backend=backend,
            depth=args.depth,
            single_branch=args.single_branch,
        )
    except GitFoldersError as e:
        die(folder_error(name, rel, "clone", str(e)), code=e.code)

    folders = manifest_data.setdefault("git_folder", [])
    folders.append({
        "name": name,
        "url": url,
        "ref": args.branch or "latest",
        "path": rel,
    })
    manifest.write_manifest(parent, manifest_data)
    print(f"Cloned {name} into {rel}")
    return 0


def _resolve(cwd: Path) -> tuple[Path | None, dict | None]:
    """Discover the parent repo and gf.toml per the git-like protocol."""
    parent, _ = manifest.resolve_context(cwd)
    if parent is None:
        return None, None
    if not (parent / manifest.MANIFEST).is_file():
        return parent, None
    return parent, manifest.read_manifest(parent)


def _align_columns(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for row in rows:
        padded = [f"{col:<{w}}" for col, w in zip(row[:-1], widths[:-1])]
        padded.append(row[-1])
        lines.append("  ".join(padded))
    return lines


def _looks_remote(url: str) -> bool:
    """Return True if url looks like a network git URL."""
    return "://" in url or url.startswith("git@")


def cmd_pull(args, backend: GitBackend) -> int:
    """Pull selected children. Like ls, missing manifest is a quiet success."""
    cwd = _logical_cwd()
    parent, manifest_data = _resolve(cwd)
    if parent is None or manifest_data is None:
        return 0

    if not args.path:
        _, child = manifest.resolve_context(cwd)
        if child:
            args.path = [child.relative_to(parent).as_posix()]

    overrides = manifest.read_local_overrides(parent)
    selected = shelf.select_children(parent, cwd, args.path, manifest_data)
    if not selected:
        return 0

    folders = manifest_data.get("git_folder", [])
    pending: list[dict] = []
    for folder in selected:
        url, ref = manifest.effective_url_ref(folder, overrides)
        url = _normalize_url(url)
        link_path = parent / folder["path"]
        child = link_path.resolve()

        if not _looks_remote(url):
            resolved = parent / url
            if platform.same_path(resolved, child):
                # Local placeholder from `gf init`; nothing to pull yet.
                continue
            url = str(resolved.resolve())

        try:
            co = layout.resolve_checkout(child)
            if co.gitdir != co.common_dir:
                # Established shared-checkout binding: the recorded
                # resolution lives in the checkout — read it rather than
                # re-probing. An override/edit changing the URL resolves
                # the new spelling.
                repo_url = backend.git_capture(
                    "config", "remote.origin.url", git_dir=co.common_dir,
                ).strip()
                subdir = co.subdir
                if url != f"{repo_url}/{subdir}":
                    repo_url, subdir = shelf.resolve_repo_url(
                        url, parent, backend)
            else:
                try:
                    repo_url, subdir = shelf.resolve_repo_url(
                        url, parent, backend)
                except GitFoldersError:
                    # Unresolvable URLs keep the whole-repo failure
                    # surface: the fetch reports them with the usual
                    # failure exit code.
                    repo_url, subdir = url, ""
            if not subdir:
                shelf.update_child(
                    co, url, ref, parent,
                    override=manifest.override_active(folder, overrides),
                    rebase=args.rebase,
                    force=args.force,
                    autostash=args.autostash,
                    backend=backend,
                )
            else:
                effective_ref = ref or "latest"
                if co.gitdir == co.common_dir:
                    # The consumer path is not a link yet: convert a
                    # `gf init` placeholder (only `.gf`, no commits) —
                    # anything else is refused by ensure_consumer_link
                    # without deleting it.
                    shelf.strip_placeholder_child(child, backend)
                    shelf.ensure_shared_binding(
                        link_path, parent, repo_url, subdir, url,
                        effective_ref,
                        override=manifest.override_active(folder, overrides),
                        backend=backend,
                    )
                else:
                    pending.append({
                        "folder": folder,
                        "url": url,
                        "ref": effective_ref,
                        "child": link_path,
                        "co": co,
                        "repo_url": repo_url,
                        "subdir": subdir,
                        "override": manifest.override_active(
                            folder, overrides),
                    })
                    continue
            print(f"Pulled {folder['name']}")
        except GitFoldersError as e:
            die(folder_error(
                folder["name"], folder["path"], "pull", str(e)),
                code=e.code)

    try:
        for line in shelf.pull_shared_bindings(
            pending, folders, parent,
            rebase=args.rebase,
            force=args.force,
            autostash=args.autostash,
            backend=backend,
        ):
            print(line)
    except GitFoldersError as e:
        failed = getattr(e, "folder", None)
        if failed is None and pending:
            failed = pending[0]["folder"]
        if failed is None:
            die(str(e), code=e.code)
        die(folder_error(
            failed["name"], failed["path"], "pull", str(e)),
            code=e.code)
    return 0


def cmd_rm(args, backend: GitBackend) -> int:
    cwd = _logical_cwd()
    parent, manifest_data = _resolve(cwd)
    if parent is None or manifest_data is None:
        return 0

    if args.all:
        if args.path:
            die("rm --all does not accept path arguments")
        # Require that cwd resolves to the parent repo root to avoid
        # accidental mass removal from the wrong directory.
        if cwd.resolve() != parent.resolve():
            die(f"rm --all must be run from the parent repo root ({parent})")
        selected = list(manifest_data.get("git_folder", []))
    elif not args.path:
        _, child = manifest.resolve_context(cwd)
        if child is None:
            die("rm requires an explicit path when not inside a child")
        args.path = [child.relative_to(parent).as_posix()]
        selected = shelf.select_children(parent, cwd, args.path, manifest_data)
    else:
        selected = shelf.select_children(parent, cwd, args.path, manifest_data)

    if not selected:
        return 0

    names = {s["name"] for s in selected}
    kept = [s for s in manifest_data.get("git_folder", []) if s["name"] not in names]

    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".gf.toml.new", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump({**manifest_data, "git_folder": kept}, f)
    except Exception:
        os.unlink(tmp_path)
        raise

    for folder in selected:
        child = parent / folder["path"]
        try:
            shelf.remove_child(child, parent)
        except GitFoldersError as e:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            die(folder_error(
                folder["name"], folder["path"], "rm", str(e)),
                code=e.code)

    os.replace(tmp_path, parent / manifest.MANIFEST)
    for folder in selected:
        print(f"Removed {folder['name']}")
    return 0


def _head(co: layout.Checkout, backend: GitBackend) -> str:
    if not (co.gitdir / "HEAD").is_file():
        return "?"
    try:
        return backend.git_capture("rev-parse", "--short", "HEAD", git_dir=co.gitdir, work_tree=co.work_tree).strip()
    except GitFoldersError:
        return "?"


def _branch(co: layout.Checkout, backend: GitBackend) -> str:
    if not (co.gitdir / "HEAD").is_file():
        return ""
    try:
        out = backend.git_capture("rev-parse", "--abbrev-ref", "HEAD", git_dir=co.gitdir, work_tree=co.work_tree).strip()
    except GitFoldersError:
        return ""
    return "" if out == "HEAD" else out


def _porcelain(co: layout.Checkout, backend: GitBackend) -> str:
    if not (co.gitdir / "HEAD").is_file():
        return ""
    return backend.git_capture("status", "--porcelain", git_dir=co.gitdir, work_tree=co.work_tree)


def cmd_status(args, backend: GitBackend) -> int:
    """Show git porcelain status for selected children. Missing manifest is success."""
    cwd = _logical_cwd()
    parent, manifest_data = _resolve(cwd)
    if parent is None or manifest_data is None:
        return 0

    if not args.path:
        _, child = manifest.resolve_context(cwd)
        if child:
            args.path = [child.relative_to(parent).as_posix()]

    overrides = manifest.read_local_overrides(parent)
    selected = shelf.select_children(parent, cwd, args.path, manifest_data)
    if not selected:
        return 0

    rows: list[list[str]] = []
    porcelains: list[str] = []
    for folder in selected:
        co = layout.resolve_checkout((parent / folder["path"]).resolve())
        branch = _branch(co, backend)
        row = [folder["name"], folder["url"], f"[{branch}]"]
        if args.remote:
            url, ref = manifest.effective_url_ref(folder, overrides)
            try:
                state = shelf.drift(co, ref, remote=True, backend=backend)
            except GitFoldersError as e:
                die(folder_error(
                    folder["name"], folder["path"], "status", str(e)),
                    code=e.code)
            row.append(state)
        rows.append(row)
        porcelains.append(_porcelain(co, backend))

    for line, porcelain in zip(_align_columns(rows), porcelains):
        print(line)
        if porcelain.strip():
            print(porcelain, end="")
    return 0


def cmd_ls(args, backend: GitBackend) -> int:
    """List git-folders. Unlike other commands, ls always shows all git-folders
    when run without path arguments, even from inside a child."""
    cwd = _logical_cwd()
    parent, manifest_data = _resolve(cwd)
    if parent is None or manifest_data is None:
        return 0

    if args.path:
        selected = shelf.select_children(parent, cwd, args.path, manifest_data)
    else:
        selected = manifest_data.get("git_folder", [])

    rows: list[list[str]] = []
    for folder in selected:
        co = layout.resolve_checkout((parent / folder["path"]).resolve())
        branch = _branch(co, backend)
        head = _head(co, backend)
        porcelain = _porcelain(co, backend)
        dirty = porcelain.strip() != ""
        rows.append([
            folder["name"],
            folder["url"],
            f"[{branch}]",
            f"{head}{'*' if dirty else ''}",
        ])

    for line in _align_columns(rows):
        print(line)
    return 0


def cmd_init(args, backend: GitBackend) -> int:
    """Create an empty local child .gf gitdir and add it to gf.toml.

    Like `git init [<directory>]`, the optional positional argument is the
    consumer directory. The git-folder name is derived from the directory name.
    No remote is fetched and no ref is checked out. The git-folder URL is set
    to the local child path; it can be changed in `gf.toml` before `pull`.
    """
    cwd = _logical_cwd()
    parent, manifest_data = _resolve(cwd)
    if parent is None:
        die("not inside a git repo")

    target = Path(args.path).resolve() if args.path else cwd
    if not target.is_relative_to(parent):
        die(folder_error(
            args.name or target.name or str(target), str(target), "init",
            "child path must be inside the parent repo"))

    rel = target.relative_to(parent).as_posix()
    name = args.name if args.name else (target.name or rel)
    url = args.url if args.url else rel

    # Validate the target before touching the manifest.
    if target == parent or (target / ".git").exists():
        die(folder_error(
            name, rel, "init", f"{target} already has a .git directory"))
    if (target / layout.GF_DIR).exists():
        die(folder_error(
            name, rel, "init", f"{target} already has a .gf directory"))

    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)

    try:
        shelf.init_git_folder(layout.resolve_checkout(target), backend=backend)
    except GitFoldersError as e:
        die(folder_error(name, rel, "init", str(e)), code=e.code)

    # Now that the child is created, update the manifest.
    if manifest_data is None:
        manifest_data = {"git_folder": []}
        manifest.write_manifest(parent, manifest_data)
    for folder in manifest_data.get("git_folder", []):
        if folder.get("name") == name:
            die(folder_error(
                name, rel, "init",
                f"git-folder '{name}' already exists in manifest"))
        if Path(folder["path"]).as_posix() == rel:
            die(folder_error(
                name, rel, "init",
                f"path {rel} is already used by git-folder '{folder['name']}'"))

    folders = manifest_data.setdefault("git_folder", [])
    folders.append({
        "name": name,
        "url": url,
        "ref": args.branch or "latest",
        "path": rel,
    })
    manifest.write_manifest(parent, manifest_data)
    shelf._print_gitignore_recommendation(parent, target)

    print(f"Initialized {name} in {rel}")
    return 0


def _git_env_for_child() -> dict[str, str]:
    """Return an environment dict pointing GIT_DIR and GIT_WORK_TREE at the current child."""
    cwd = _logical_cwd()
    parent, child = manifest.resolve_context(cwd)
    target = (child or cwd).resolve()

    co = layout.resolve_checkout(target)
    git_dir = co.gitdir.resolve()
    if not (git_dir / "HEAD").is_file():
        die("not inside a git-folder child")

    env = os.environ.copy()
    env["GIT_DIR"] = str(git_dir)
    env["GIT_WORK_TREE"] = str(co.work_tree)
    return env


def _run_child_command(cmd: list[str]) -> int:
    """Run a command inside the current child, exec on TTY, stream otherwise."""
    env = _git_env_for_child()
    if sys.stdout.isatty():
        result = runner.run_command(cmd, env, mode="exec")
        if result.returncode != 0:
            die(result.stderr, result.returncode)
        return 0
    result = runner.run_command(cmd, env, mode="stream")
    return result.returncode


def cmd_git(args) -> int:
    """Run an arbitrary `git` command inside the current child."""
    return _run_child_command(["git", *args.git_args])


def cmd_sh(args) -> int:
    """Run a shell or command inside a child with GIT_DIR set to the child gitdir.

    `sh` only works in directories that contain `.gf/git/`; it does not
    fall back to the parent `.git`.
    """
    if args.shell_command and args.sh_command:
        die("cannot use -c with a positional command")

    if args.shell_command:
        cmd = [os.environ.get("SHELL", "/bin/sh"), "-c", args.shell_command]
    elif args.sh_command:
        cmd = args.sh_command
    else:
        cmd = [os.environ.get("SHELL", "/bin/sh")]

    return _run_child_command(cmd)


def cmd_diff(args) -> int:
    """Run `git diff` inside the current child."""
    return _run_child_command(["git", "diff", *args.git_args])


def cmd_log(args) -> int:
    """Run `git log` inside the current child."""
    return _run_child_command(["git", "log", *args.git_args])


def cmd_worktree_add(args, backend: GitBackend) -> int:
    """Add a parent git worktree and symlink managed git-folders into it."""
    parent, manifest_data = _resolve(_logical_cwd())
    if parent is None or manifest_data is None:
        die("not inside a git repo with gf.toml")

    new_parent = Path(args.path).resolve()

    folders = manifest_data.get("git_folder", [])

    wt_args = []
    if args.force:
        wt_args.append("--force")
    if args.new_branch:
        wt_args.extend(["-b", args.new_branch])
    if args.new_or_existing_branch:
        wt_args.extend(["-B", args.new_or_existing_branch])
    if args.commitish:
        wt_args.append(args.commitish)

    try:
        backend.git("worktree", "add", *wt_args, str(new_parent), cwd=parent, stream=True)
    except GitFoldersError as e:
        die(str(e), code=e.code)

    # Propagate the current manifest so commands in the new worktree see the
    # same git-folders, even when gf.toml is not yet committed.
    for manifest_name in (manifest.MANIFEST, manifest.LOCAL):
        src = parent / manifest_name
        dst = new_parent / manifest_name
        if src.exists():
            dst.write_text(src.read_text())

    for folder in folders:
        source_child = parent / folder["path"]
        if not manifest.is_git_folder_child(source_child):
            continue
        source_child = source_child.resolve()
        new_child = new_parent / folder["path"]
        new_child.parent.mkdir(parents=True, exist_ok=True)
        rel = os.path.relpath(source_child, new_child.parent)
        if new_child.is_symlink() or new_child.exists():
            if not args.force:
                die(folder_error(
                    folder["name"], folder["path"], "worktree add",
                    f"child path {new_child} already exists"))
            new_child.unlink()
        os.symlink(rel, new_child, target_is_directory=True)

    print(f"Added worktree at {new_parent}")
    return 0


def cmd_worktree_list(args, backend: GitBackend) -> int:
    """List parent git worktrees and the git-folders linked into each."""
    parent, manifest_data = _resolve(_logical_cwd())
    if parent is None or manifest_data is None:
        die("not inside a git repo with gf.toml")

    worktrees = shelf.list_parent_worktrees(parent, backend)
    if args.porcelain:
        for wt in worktrees:
            wt_path = Path(wt["path"])
            print(f"worktree {wt['path']}")
            if wt["head"]:
                print(f"HEAD {wt['head']}")
            if wt["branch"]:
                print(f"branch refs/heads/{wt['branch']}")
            for name, target in shelf.linked_git_folders_in_worktree(wt_path, manifest_data):
                print(f"git-folder {name} {target}")
            print()
        return 0

    for wt in worktrees:
        wt_path = Path(wt["path"])
        if args.verbose and wt["head"]:
            print(f"{wt['path']}  ({wt['head']})")
        else:
            print(f"{wt['path']}")
        for name, target in shelf.linked_git_folders_in_worktree(wt_path, manifest_data):
            print(f"  {name} -> {target}")
    return 0


def cmd_worktree_remove(args, backend: GitBackend) -> int:
    """Remove a parent git worktree, guarding linked git-folder children.

    The target must be a listed, non-main, non-current worktree. Before
    calling `git worktree remove`, the relative git-folder symlinks
    inside the target are unlinked so the source children are not
    touched. If `git worktree remove` fails, those symlinks are restored
    so the worktree is left in its original state.
    """
    parent, manifest_data = _resolve(_logical_cwd())
    if parent is None or manifest_data is None:
        die("not inside a git repo with gf.toml")

    worktree_path = Path(args.path).resolve()

    # Validate the target is a listed worktree and is not the main or
    # current worktree. `git worktree list` reports the main worktree
    # first; refusing it and the current worktree protects the user from
    # removing the wrong tree.
    worktrees = shelf.list_parent_worktrees(parent, backend)
    listed_paths = [Path(wt["path"]).resolve() for wt in worktrees]
    if worktree_path not in listed_paths:
        die(f"{worktree_path} is not a listed worktree of {parent}")
    main_path = listed_paths[0] if listed_paths else None
    if main_path is not None and worktree_path == main_path:
        die(f"{worktree_path} is the main worktree; refusing to remove it")
    if worktree_path == _logical_cwd().resolve():
        die(f"{worktree_path} is the current worktree; refusing to remove it")

    # Capture the symlinks we unlink so they can be restored if
    # `git worktree remove` fails.
    symlinks = shelf.linked_git_folder_symlinks_in_worktree(worktree_path, manifest_data)
    for child, _target in symlinks:
        os.unlink(child)

    cmd_args = ["worktree", "remove"]
    if args.force:
        cmd_args.append("--force")
    cmd_args.append(str(worktree_path))
    try:
        backend.git(*cmd_args, cwd=parent, stream=True)
    except GitFoldersError as e:
        # Restore the symlinks we removed so the worktree is left intact.
        for child, target in symlinks:
            try:
                if not child.exists():
                    os.symlink(target, child, target_is_directory=True)
            except OSError:
                pass
        die(str(e), code=e.code)

    print(f"Removed worktree at {worktree_path}")
    return 0


def main(argv: list[str] | None = None, backend: GitBackend | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = _apply_chdir(argv)
    real_backend = backend or GitCliBackend()

    # Handle --version / -v before subcommand dispatch. A -v that appears
    # before the subcommand is the global version flag; a -v after a
    # subcommand (e.g. `gf worktree list -v`) is forwarded to the subparser.
    if argv and argv[0] in ("--version", "-v"):
        print(_get_version())
        return 0

    parser = argparse.ArgumentParser(
        prog="gf",
        description="git-folders: manage git repositories as ordinary folders inside a parent workspace",
    )
    # Register --version on the top-level parser so `gf --help` lists it.
    # Only the long form is registered here to avoid colliding with the
    # subparser `-v` options (e.g. `gf worktree list -v` for verbose).
    # `gf -v` and `gf --version` are still handled by the pre-dispatch
    # check above so they work without a parent repo and before subcommand
    # parsing.
    parser.add_argument(
        "--version",
        action="version",
        version=_get_version(),
        help="print the git-folders version and exit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_clone = sub.add_parser("clone", help="clone a git-folder into a child path")
    p_clone.add_argument("url", help="repository url")
    p_clone.add_argument("path", nargs="?", default=None, help="consumer path (default: url basename)")
    p_clone.add_argument("-n", "--name", dest="name", default=None, help="git-folder name (default: path or url basename)")
    p_clone.add_argument("--branch", "-b", dest="branch", default="", help="branch, tag, or commit to check out (default: latest)")
    p_clone.add_argument("--depth", type=int, default=None, help="create a shallow clone with history truncated to N commits")
    p_clone.add_argument("--single-branch", action="store_true", dest="single_branch", default=False, help="fetch only the resolved branch")

    p_pull = sub.add_parser("pull", help="pull selected children")
    p_pull.add_argument("path", nargs="*")
    p_pull.add_argument("--rebase", action="store_true", dest="rebase", default=False, help="rebase the local branch after fetching")
    p_pull.add_argument("--force", action="store_true", dest="force", default=False, help="allow updating a dirty child (checkout -f)")
    p_pull.add_argument("--autostash", action="store_true", dest="autostash", default=False, help="stash, pull, and restore local changes")

    p_rm = sub.add_parser("rm", help="remove selected children")
    p_rm.add_argument("path", nargs="*")
    p_rm.add_argument("--all", action="store_true", dest="all", default=False, help="unregister every git-folder (requires running from the parent repo root)")

    p_status = sub.add_parser("status", help="show git porcelain status")
    p_status.add_argument("path", nargs="*")
    p_status.add_argument("--remote", action="store_true", default=False, help="classify drift against local remote-tracking refs (clean/behind/local-dirty/both/missing)")

    p_ls = sub.add_parser("ls", help="list git-folders")
    p_ls.add_argument("path", nargs="*")

    p_init = sub.add_parser("init", help="create an empty .gf child gitdir and add it to gf.toml")
    p_init.add_argument("path", nargs="?", default=None, help="consumer path (default: current directory)")
    p_init.add_argument("--branch", "-b", dest="branch", default="", help="branch, tag, or commit to track (default: latest)")
    p_init.add_argument("-n", "--name", dest="name", default=None, help="git-folder name (default: directory name)")
    p_init.add_argument("--url", dest="url", default=None, help="upstream URL to store in gf.toml instead of the local placeholder")

    p_diff = sub.add_parser("diff", help="run git diff in a child")
    p_diff.add_argument("git_args", nargs=argparse.REMAINDER, default=[], metavar="args", help="arguments passed to git diff")
    p_log = sub.add_parser("log", help="run git log in a child")
    p_log.add_argument("git_args", nargs=argparse.REMAINDER, default=[], metavar="args", help="arguments passed to git log")

    p_worktree = sub.add_parser("worktree", help="manage parent git worktrees")
    w_sub = p_worktree.add_subparsers(dest="worktree_command", required=True)
    p_wt_add = w_sub.add_parser("add", help="add a parent worktree with git-folders symlinked from the source")
    p_wt_add.add_argument("path", help="path for the new worktree")
    p_wt_add.add_argument("commitish", nargs="?", default=None, help="commit/branch/tag to check out")
    p_wt_add.add_argument("-b", dest="new_branch", default=None, help="create and check out a new branch")
    p_wt_add.add_argument("-B", dest="new_or_existing_branch", default=None, help="create or reset a branch")
    p_wt_add.add_argument("-f", "--force", action="store_true", help="force")

    p_wt_list = w_sub.add_parser("list", help="list parent worktrees and the git-folders linked into each")
    p_wt_list.add_argument("--porcelain", action="store_true", help="machine-readable output")
    p_wt_list.add_argument("--verbose", "-v", action="store_true", help="include the HEAD SHA in human output")

    p_wt_remove = w_sub.add_parser("remove", help="remove a parent worktree, guarding linked git-folder children")
    p_wt_remove.add_argument("path", help="path of the worktree to remove")
    p_wt_remove.add_argument("--force", "-f", action="store_true", help="force removal")

    p_git = sub.add_parser("git", help="run git in the current child")
    p_git.add_argument("git_args", nargs=argparse.REMAINDER, default=[], metavar="args", help="git command and arguments")

    p_sh = sub.add_parser("sh", help="run a shell or command with GIT_DIR set")
    p_sh.add_argument("-c", dest="shell_command", default=None, help="run a shell command string")
    p_sh.add_argument("sh_command", nargs=argparse.REMAINDER, default=[], metavar="command", help="command and arguments to run")

    args, unknown = parser.parse_known_args(argv)
    if args.command in ("git", "diff", "log"):
        # Reconstruct passthrough args from the original argv to preserve
        # token order. REMAINDER stops at the first option-looking token, so
        # flags land in `unknown` and the captured positionals come first,
        # which would reorder `gf log --oneline -n 1` into `git log 1 --oneline -n`.
        # After _apply_chdir the subcommand name is argv[0]; everything after
        # it is the passthrough, sliced verbatim to keep order intact.
        cmd_idx = argv.index(args.command)
        args.git_args = list(argv[cmd_idx + 1:])
    elif unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")

    if args.command == "clone":
        return cmd_clone(args, real_backend)
    elif args.command == "pull":
        return cmd_pull(args, real_backend)
    elif args.command == "rm":
        return cmd_rm(args, real_backend)
    elif args.command == "status":
        return cmd_status(args, real_backend)
    elif args.command == "ls":
        return cmd_ls(args, real_backend)
    elif args.command == "init":
        return cmd_init(args, real_backend)
    elif args.command == "diff":
        return cmd_diff(args)
    elif args.command == "log":
        return cmd_log(args)
    elif args.command == "git":
        return cmd_git(args)
    elif args.command == "worktree":
        if args.worktree_command == "add":
            return cmd_worktree_add(args, real_backend)
        elif args.worktree_command == "list":
            return cmd_worktree_list(args, real_backend)
        elif args.worktree_command == "remove":
            return cmd_worktree_remove(args, real_backend)
    elif args.command == "sh":
        return cmd_sh(args)
    return 0
