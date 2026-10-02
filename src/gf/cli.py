# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import argparse
import importlib.metadata
import io
import os
import re
import subprocess
import sys
import tomllib
import urllib.parse
from pathlib import Path

from .backends import GitBackend, GitCliBackend, clean_environ
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
    # Fallback: read pyproject.toml from the project root. A version probe
    # runs before main()'s try — an unreadable or malformed pyproject must
    # not traceback `--version`, so fs/TOML failures fall through to the
    # default below.
    here = Path(__file__).resolve()
    for p in (here.parent, *here.parents):
        candidate = p / "pyproject.toml"
        if candidate.is_file():
            try:
                with open(candidate, "rb") as f:
                    data = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
                break
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
            # Resolve the operand against the cwd that spelled it: an
            # abspath after chdir would anchor a relative spelling at the
            # destination itself (`one` -> `.../one/one`), failing
            # logical_cwd's same-inode check and losing the lexical
            # spelling bindings select on.
            dest = os.path.abspath(os.path.expanduser(path))
            try:
                os.chdir(dest)
            except OSError as e:
                die(f"cannot change to '{path}': {e.strerror or e}")
            os.environ["PWD"] = dest
            i += 2
        else:
            break
    return argv[i:]


def _normalize_url(url: str, base: Path | None = None) -> str:
    """Expand a bare host/path URL into an https:// URL.

    A dotted first segment is host shorthand only when neither it nor the
    full spelling exists under `base` — the root a relative url resolves
    against, defaulting to the cwd. An existing `<repo>.git/<subdir>`
    spelling stays the plain local path the spec defines a url to be.
    """
    if "://" in url or url.startswith("git@"):
        return url
    if "/" in url:
        head, _, _ = url.partition("/")
        if "." in head and " " not in head and not head.startswith("."):
            root = Path.cwd() if base is None else base
            if not (root / head).exists() and not (root / url).exists():
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

    url = _normalize_url(args.url, parent)
    path = args.path
    name = args.name

    # Keep the leaf literal and resolve only the parent chain: a leaf
    # that is already a consumer link must stay the spelled consumer
    # path so `rel` records the lexical path, not the physical checkout
    # the link resolves to (`resolve_checkout` does its own realpath).
    raw = (
        Path(path)
        if path
        else _logical_cwd() / Path(name or _name_from_url(url)).name
    )
    child_path = raw.parent.resolve() / raw.name
    # A `.`/`..` leaf is never a consumer link: normalize it through
    # realpath — never os.path.normpath, which pops the spelled parent
    # even when a mid-path symlink redirects it — so containment,
    # occupancy and `rel` all observe the true directory.
    if raw.name in (".", ".."):
        child_path = child_path.resolve()

    if name is None:
        # The default name derives from the normalized leaf so a
        # dot-segment leaf names the true directory (`missing/..` names
        # its resolved parent), never `.`/`..`.
        name = _name_from_url(child_path.name) if path else _name_from_url(url)
    # Containment still tests the resolved path — a mid-path or leaf
    # link landing outside the parent repo is refused — and the lexical
    # spelling, which `rel` must be able to express under the parent.
    if not (
        child_path.is_relative_to(parent)
        and child_path.resolve().is_relative_to(parent)
    ):
        die(folder_error(
            name, str(child_path), "clone",
            "child path must be inside the parent repo"))

    # A spelled child path must never carry a `.gf` or `.git` segment —
    # a `.gf/...` spelling or an ancestor consumer link that resolved
    # into a shared checkout writes inside gf's own storage, and a
    # `.git` segment (e.g. `.git/hooks/post-checkout`) would record a
    # consumer link the parent repo's own git operations read — hooks
    # execute on checkout. The segment test is anywhere-in-path on the
    # resolved spelling, so a mid-path committed symlink (sub ->
    # `.git/hooks`) is caught by its resolved parts too, and a
    # `sub/.gf/x` spelling refuses before it can record a binding the
    # manifest reader would wedge on. The leaf stays lexical, so a leaf
    # that is itself the existing consumer link is not caught here and
    # keeps its idempotent re-add path.
    if (
        layout.GF_DIR in child_path.relative_to(parent).parts
        or ".git" in child_path.relative_to(parent).parts
    ):
        die(folder_error(
            name, str(child_path), "clone",
            f"child path resolves inside gf-managed storage "
            f"({layout.GF_DIR}) or repository metadata (.git)"))

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
            binding_path=rel,
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
    # lexists so a `gf.toml` that exists but is not a readable regular
    # file (directory, fifo, dangling symlink) reaches read_manifest's
    # error envelope instead of silently counting as absent.
    if not os.path.lexists(parent / manifest.MANIFEST):
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
            # Absolute operand: `cwd / <abs>` collapses to `<abs>`, so the
            # selection target is `child` itself regardless of cwd — a
            # parent-relative spelling would re-anchor at a cwd inside the
            # physical `.gf/wt` checkout and select nothing.
            args.path = [str(child)]

    overrides = manifest.read_local_overrides(parent)
    selected = shelf.select_children(parent, cwd, args.path, manifest_data)
    if not selected:
        return 0

    folders = manifest_data.get("git_folder", [])
    planned: list[dict] = []
    # Resolve every selected binding's routing before changing anything
    # (spec `gf pull`): a refused url switch stops the pull with no
    # selected binding updated.
    for folder in selected:
        url, ref = manifest.effective_url_ref(folder, overrides)
        link_path = parent / folder["path"]
        child = link_path.resolve()
        co = layout.resolve_checkout(child)
        # A source-relative `url` anchors at the root that OWNS the
        # binding: the root whose declared manifest `path` spells the
        # resolved `child`, which is not necessarily the invoking root —
        # a pull entered through a `gf worktree add` link chain resolves
        # the consumer path into the source worktree's `.gf/wt` (a
        # whole-repo child reached the same way resolves into a
        # directory the source parent's `path` declaration owns).
        # Ownership is declared-path identity, never `.git` probing:
        # `binding_root` accepts only the ancestor where `root / path`
        # resolves to `child`, so a foreign repository nested between
        # the child and the declaring root cannot claim the anchor.
        # `owning_root` stays the first arm — canonical for a store
        # checkout — keeping `.gf/wt`-interior ancestors out of the
        # identity walk.
        anchor = (
            layout.owning_root(co)
            or manifest.binding_root(child, folder["path"])
            or parent
        )
        # The resolved consumer path must stay inside the root that owns
        # the binding — a mid-path symlink a checked-out tree
        # materialized would otherwise send update_child /
        # strip_placeholder_child writes outside the workspace.
        # `gf worktree add` link chains still pass: their resolution
        # lands in the OWNING root, which is what `anchor` names.
        if not Path(os.path.realpath(child)).is_relative_to(
            Path(os.path.realpath(anchor))
        ):
            die(folder_error(
                folder["name"], folder["path"], "pull",
                f"consumer path {link_path} resolves to {child}, "
                f"outside {anchor}"))
        url = _normalize_url(url, anchor)

        if not _looks_remote(url):
            resolved = anchor / url
            if platform.same_path(resolved, child):
                # Local placeholder from `gf init`; nothing to pull yet.
                continue
            url = str(resolved.resolve())

        try:
            if co.is_store_checkout:
                # Established shared-checkout binding: consult THIS
                # binding's recorded effective url in the checkout
                # state's `binding_urls` map (keyed by manifest `path`)
                # rather than re-probing — a store's origin keeps only
                # the creating spelling, so a differently-spelled sibling
                # reconstructed from `remote.origin.url` + `co.subdir`
                # always mismatched and re-resolved. An absent entry — a
                # checkout recorded before the map existed, or a link
                # whose `.gf` storage was removed — resolves like an
                # unrecorded binding; an override/edit changing the
                # effective url resolves the new spelling, and the
                # grouped phase recreates what is missing.
                repo_url = None
                recorded = shelf.recorded_binding_url(co, folder["path"])
                owner = layout.owning_root(co)
                if (
                    recorded is not None
                    and owner is not None
                    and (co.common_dir / "HEAD").is_file()
                    and shelf.recorded_url_matches(recorded, url, anchor)
                ):
                    origin = backend.git_capture(
                        "config", "remote.origin.url",
                        git_dir=co.common_dir,
                    ).strip()
                    # The recorded origin is a valid fetch target but a
                    # valid stand-in for the resolution only when it keys
                    # THIS store — a local `gf`-child boundary records
                    # its inner `.gf/git` gitdir, which keys elsewhere;
                    # anything else resolves so the local walk-up
                    # recovers the canonical repo path (no network).
                    if layout.repo_store(owner, origin) == co.common_dir:
                        repo_url, subdir = origin, co.subdir
                if repo_url is None:
                    repo_url, subdir = shelf.resolve_repo_url(
                        url, anchor, backend)
                    if not subdir:
                        # A lexical store checkout proves the binding was
                        # subfolder-form; an effective url that resolves
                        # to a whole repository is refused: nothing is
                        # repointed or converted (spec `gf pull`).
                        raise ValidationError(
                            "switching a subfolder binding to a whole "
                            f"repository is not supported: {url}")
            elif shelf.recorded_url_matches(
                    shelf.recorded_url(co), url, anchor):
                # Recorded resolution (`child/.gf/state` url): an
                # existing whole-repo child is never re-resolved.
                repo_url, subdir = url, ""
            else:
                try:
                    repo_url, subdir = shelf.resolve_repo_url(
                        url, anchor, backend)
                except GitFoldersError:
                    # Routing only: treat as whole-repo and let
                    # `update_child` decide the failure surface — it
                    # re-resolves and propagates the `.git`-boundary hint
                    # for first-resolution bindings, while established
                    # whole-repo bindings keep the fetch-failure surface.
                    repo_url, subdir = url, ""
            planned.append({
                "folder": folder,
                "url": url,
                "ref": ref,
                "link_path": link_path,
                "child": child,
                "co": co,
                "anchor": anchor,
                "repo_url": repo_url,
                "subdir": subdir,
            })
        except GitFoldersError as e:
            die(folder_error(
                folder["name"], folder["path"], "pull", str(e)),
                code=e.code)

    pending: list[dict] = []
    for it in planned:
        folder = it["folder"]
        try:
            if not it["subdir"]:
                shelf.update_child(
                    it["co"], it["url"], it["ref"], it["anchor"],
                    override=manifest.override_active(folder, overrides),
                    rebase=args.rebase,
                    autostash=args.autostash,
                    backend=backend,
                    binding_path=folder["path"],
                )
            else:
                if not it["co"].is_store_checkout:
                    # The consumer path is not a link yet: convert a
                    # `gf init` placeholder (only `.gf`, no commits) —
                    # anything else is refused by ensure_consumer_link
                    # without deleting it.
                    shelf.strip_placeholder_child(it["link_path"], backend)
                pending.append({
                    "folder": folder,
                    "url": it["url"],
                    "ref": it["ref"] or "latest",
                    "child": it["link_path"],
                    "co": it["co"],
                    "repo_url": it["repo_url"],
                    "subdir": it["subdir"],
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
            autostash=args.autostash,
            backend=backend,
        ):
            print(line)
    except GitFoldersError as e:
        # Lines accumulated before the failure (`Pulled <name>` for
        # bindings already served) are still real output — emit them
        # before the error.
        for line in getattr(e, "lines", ()):
            print(line)
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
        # Same cwd-independent operand as pull/status: `cwd / <abs>`
        # collapses to `<abs>`, so a physical `.gf/wt` cwd still selects
        # the binding rather than a doubled nonexistent path.
        args.path = [str(child)]
        selected = shelf.select_children(parent, cwd, args.path, manifest_data)
    else:
        selected = shelf.select_children(parent, cwd, args.path, manifest_data)

    if not selected:
        return 0

    names = {s["name"] for s in selected}
    kept = [s for s in manifest_data.get("git_folder", []) if s["name"] not in names]

    for folder in selected:
        child = parent / folder["path"]
        try:
            shelf.remove_child(child, parent)
        except GitFoldersError as e:
            die(folder_error(
                folder["name"], folder["path"], "rm", str(e)),
                code=e.code)

    # Atomic write (mkstemp + os.replace) shared with clone/init via
    # `write_manifest` — after the removals so a failed `remove_child`
    # leaves the manifest still listing every binding, and the swap
    # replaces a `gf.toml` symlink rather than writing through it.
    manifest.write_manifest(parent, {**manifest_data, "git_folder": kept})
    for folder in selected:
        print(f"Removed {folder['name']}")
    return 0


def _gitdir_usable(co: layout.Checkout) -> bool:
    """True when `co.gitdir` names this checkout's own storage.

    A whole-repo child whose `.gf` resolves through a link has no gitdir
    of its own — the spelled `child/.gf/git` reaches a foreign gitdir,
    whose HEAD/branch/status would render as this child's. Read-side
    callers degrade to their missing-gitdir fallbacks rather than die.
    A store checkout's gitdir lives under the parent's `.gf` storage
    (guarded at write sites by `layout.storage_is_real`), so only the
    whole-repo shape is tested here.
    """
    return co.is_store_checkout or layout.whole_repo_gitdir_is_real(co)


def _head(co: layout.Checkout, backend: GitBackend) -> str:
    """Short HEAD SHA; "?" when the gitdir has no HEAD or HEAD is unborn.

    The `is_file`/`--verify` early-outs are the legitimate missing-ref
    fallbacks (pre-clone path); a `--short` failure after HEAD verifies
    is a genuine git failure and propagates.
    """
    if not _gitdir_usable(co) or not (co.gitdir / "HEAD").is_file():
        return "?"
    if backend.git(
        "rev-parse", "--verify", "HEAD",
        git_dir=co.gitdir, work_tree=co.work_tree, check=False,
    ).returncode != 0:
        return "?"
    return backend.git_capture(
        "rev-parse", "--short", "HEAD",
        git_dir=co.gitdir, work_tree=co.work_tree).strip()


def _branch(co: layout.Checkout, backend: GitBackend) -> str:
    """Current branch; "" when detached, unborn, or the gitdir has no HEAD.

    `rev-parse --abbrev-ref HEAD` dies (rc=128) on an unborn HEAD, so the
    verify gate mirrors `_head`: a child with no commits yet renders
    empty brackets rather than the symbolic-ref target. A failure after
    HEAD verifies is a genuine git failure and propagates.
    """
    if not _gitdir_usable(co) or not (co.gitdir / "HEAD").is_file():
        return ""
    if backend.git(
        "rev-parse", "--verify", "HEAD",
        git_dir=co.gitdir, work_tree=co.work_tree, check=False,
    ).returncode != 0:
        return ""
    out = backend.git_capture(
        "rev-parse", "--abbrev-ref", "HEAD",
        git_dir=co.gitdir, work_tree=co.work_tree).strip()
    return "" if out == "HEAD" else out


def _porcelain(
    co: layout.Checkout,
    backend: GitBackend,
    paths: list[str] | None = None,
) -> str:
    if (
        not _gitdir_usable(co)
        or not (co.gitdir / "HEAD").is_file()
        or not co.work_tree.is_dir()
    ):
        return ""
    if paths is not None:
        return shelf.scoped_porcelain(co, paths, backend)
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
            # Absolute operand: `cwd / <abs>` collapses to `<abs>`, so the
            # selection target is `child` itself regardless of cwd — a
            # parent-relative spelling would re-anchor at a cwd inside the
            # physical `.gf/wt` checkout and select nothing.
            args.path = [str(child)]

    overrides = manifest.read_local_overrides(parent)
    selected = shelf.select_children(parent, cwd, args.path, manifest_data)
    if not selected:
        return 0

    rows: list[list[str]] = []
    porcelains: list[str] = []
    for folder in selected:
        co = layout.resolve_checkout((parent / folder["path"]).resolve())
        try:
            scope = shelf.binding_scope(parent, folder, co)
            branch = _branch(co, backend)
            row = [folder["name"], folder["url"], f"[{branch}]"]
            if args.remote:
                url, ref = manifest.effective_url_ref(folder, overrides)
                state = shelf.drift(co, ref, backend=backend, paths=scope)
                row.append(state)
            rows.append(row)
            porcelains.append(_porcelain(co, backend, paths=scope))
        except GitFoldersError as e:
            die(folder_error(
                folder["name"], folder["path"], "status", str(e)),
                code=e.code)

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
        try:
            scope = shelf.binding_scope(parent, folder, co)
            branch = _branch(co, backend)
            head = _head(co, backend)
            porcelain = _porcelain(co, backend, paths=scope)
        except GitFoldersError as e:
            die(folder_error(
                folder["name"], folder["path"], "ls", str(e)),
                code=e.code)
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

    if args.path:
        # Same leaf-literal normalization as `gf clone`: a leaf consumer
        # link stays the spelled consumer path so `rel` and the error
        # envelope name it rather than the physical path it resolves to.
        raw = Path(args.path)
        target = raw.parent.resolve() / raw.name
        # Same dot-segment leaf normalization as `gf clone`: a `.`/`..`
        # leaf is never a consumer link, so realpath it before the
        # containment, `target == parent`/`.git`, and occupancy guards
        # and `rel`/`target.name` observe the true directory.
        if raw.name in (".", ".."):
            target = target.resolve()
    else:
        target = cwd
    if not (
        target.is_relative_to(parent)
        and target.resolve().is_relative_to(parent)
    ):
        die(folder_error(
            args.name or target.name or str(target), str(target), "init",
            "child path must be inside the parent repo"))

    rel = target.relative_to(parent).as_posix()

    # Same `.gf`-storage refusal as `gf clone`: a spelled target inside the
    # parent's `.gf` — directly or through an ancestor consumer link —
    # must not let `target.mkdir` or `init_git_folder` write inside a
    # shared checkout or store. The resolved arm adds the leaf: a leaf
    # that is itself a link keeps its lexical spelling for `rel`, but its
    # resolution inside `.gf` is still never a bindable child (spec §init).
    # The `rel` arm is the manifest reader's own predicate on the recorded
    # path: a `.gf` segment anywhere in the spelling (`sub/.gf/x`, not only
    # a `.gf` root) would record a binding `_validate_consumer_path`
    # refuses on read, wedging every later command until hand-edited.
    # `.git` gets the same treatment: a `.git`-segment spelling or a
    # resolved landing inside the parent's repository metadata would put
    # the child gitdir where git reads config and executes hooks.
    if (
        target.is_relative_to(parent / layout.GF_DIR)
        or target.resolve().is_relative_to(
            (parent / layout.GF_DIR).resolve())
        or layout.GF_DIR in Path(rel).parts
        or ".git" in Path(rel).parts
        or layout.in_git_tree(target.resolve())
    ):
        die(folder_error(
            args.name or target.name or str(target), str(target), "init",
            f"child path resolves inside gf-managed storage "
            f"({layout.GF_DIR}) or repository metadata (.git)"))

    name = args.name if args.name else (target.name or rel)
    url = args.url if args.url else rel

    # Validate the target before any side effect: every refusal —
    # containment, `.gf`-storage, occupancy and the manifest collision
    # loop — precedes `target.mkdir` and `init_git_folder`, so a refused
    # init leaves nothing behind (same ordering as `gf clone`).
    if target == parent or (target / ".git").exists():
        die(folder_error(
            name, rel, "init", f"{target} already has a .git directory"))
    if (target / layout.GF_DIR).exists():
        die(folder_error(
            name, rel, "init", f"{target} already has a .gf directory"))

    # A missing gf.toml materializes through the single write below,
    # after the child is created.
    if manifest_data is None:
        manifest_data = {"git_folder": []}
    for folder in manifest_data.get("git_folder", []):
        if folder.get("name") == name:
            die(folder_error(
                name, rel, "init",
                f"git-folder '{name}' already exists in manifest"))
        if Path(folder["path"]).as_posix() == rel:
            die(folder_error(
                name, rel, "init",
                f"path {rel} is already used by git-folder '{folder['name']}'"))

    # A dangling symlink is occupancy, not a crash — the same refusal as
    # `init_child`/`update_child`. exists() is False for one, so without
    # this gate `target.mkdir`'s `lexists` guard would pass it through to
    # `init_git_folder`, whose gitdir mkdir cannot write through a
    # dangling link (os.mkdir still raises EEXIST on the link itself).
    if os.path.lexists(target) and not target.exists():
        die(folder_error(
            name, rel, "init", f"{target} is a dangling symlink"))
    # An existing non-directory occupant (a plain file, or a link to one)
    # reaches `init_git_folder`'s gitdir mkdir only as NotADirectoryError.
    if target.exists() and not target.is_dir():
        die(folder_error(
            name, rel, "init", f"{target} exists and is not a directory"))
    if not os.path.lexists(target):
        target.mkdir(parents=True, exist_ok=True)

    try:
        shelf.init_git_folder(layout.resolve_checkout(target), backend=backend)
    except GitFoldersError as e:
        die(folder_error(name, rel, "init", str(e)), code=e.code)

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


def _passthrough_target(op: str) -> tuple[layout.Checkout, Path]:
    """Resolve the caller's position to (checkout, physical subprocess cwd).

    The subprocess cwd is the caller's RESOLVED PHYSICAL position — under
    a store checkout that is `co.work_tree/co.subdir` (the mapped position
    with link indirection stripped, so `.` pathspecs and `--show-prefix`
    report the repo-relative position); under a whole-repo child it is the
    same directory spelled physically. A missing gitdir dies through the
    `folder_error` envelope when the cwd identifies a manifest binding
    (`op` names the entry point); positions outside every binding keep
    the bare die.
    """
    cwd = _logical_cwd()
    parent, child = manifest.resolve_context(cwd)
    target = (child or cwd).resolve()

    co = layout.resolve_checkout(target)
    git_dir = co.gitdir.resolve()
    # A whole-repo child whose `.gf` resolves through a link has no
    # gitdir of its own: `co.gitdir` then reaches a foreign gitdir and
    # the subprocess would run inside the donor's storage — fail closed
    # like a missing gitdir, naming the `.gf` resolution. A store
    # checkout's gitdir legitimately lives under the parent's `.gf`, so
    # only the whole-repo shape is tested.
    gitdir_real = (
        co.is_store_checkout or layout.whole_repo_gitdir_is_real(co))
    if not gitdir_real or not (git_dir / "HEAD").is_file():
        reason = (
            f"child gitdir path {co.gitdir} resolves through a symlink"
            if not gitdir_real
            else "not inside a git-folder child")
        folder = None
        if parent is not None and os.path.lexists(
                parent / manifest.MANIFEST):
            folder = shelf.folder_containing(
                parent, cwd, manifest.read_manifest(parent))
        if folder is not None:
            die(folder_error(
                folder["name"], folder["path"], op, reason))
        die(reason)
    return co, Path(os.path.realpath(cwd))


def _git_env_for_child(co: layout.Checkout) -> dict[str, str]:
    """Env pointing GIT_DIR at `co.gitdir` and GIT_WORK_TREE at `co.work_tree`.

    For a store checkout `co.gitdir` is the linked worktree's admin dir
    under the repo store; for a whole-repo child it is `child/.gf/git`.

    The base env is scrubbed so an ambient GIT_INDEX_FILE /
    GIT_OBJECT_DIRECTORY / GIT_CONFIG_* inherited from a foreign repo's
    environment cannot misdirect the child `git`; gf then sets
    GIT_DIR/GIT_WORK_TREE explicitly, so the `gf sh` contract (the user's
    git runs with GIT_DIR set) is intact — only INHERITED vars go.
    """
    env = clean_environ()
    env["GIT_DIR"] = str(co.gitdir.resolve())
    env["GIT_WORK_TREE"] = str(co.work_tree)
    return env


def _run_child_command(
    cmd: list[str], co: layout.Checkout, cwd: Path,
) -> int:
    """Run a command with the checkout env at the physical position,
    exec on TTY, stream otherwise."""
    env = _git_env_for_child(co)
    if sys.stdout.isatty():
        result = runner.run_command(cmd, env, mode="exec", cwd=cwd)
        if result.returncode != 0:
            die(result.stderr, result.returncode)
        return 0
    result = runner.run_command(cmd, env, mode="stream", cwd=cwd)
    return result.returncode


def cmd_git(args) -> int:
    """Run an arbitrary `git` command inside the current child."""
    co, cwd = _passthrough_target("git")
    return _run_child_command(["git", *args.git_args], co, cwd)


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

    co, cwd = _passthrough_target("sh")
    return _run_child_command(cmd, co, cwd)


def _scoped_dot(git_args: list[str], co: layout.Checkout, cwd: Path) -> list[str]:
    """`-- .` for diff/log when a store checkout and no user pathspec.

    The bare `.` pathspec means "the caller's position" — the subprocess
    cwd, which `_passthrough_target` anchors at the physical mapped
    subdir. A user-supplied `--` suppresses the append entirely so their
    pathspecs pass verbatim. So does any non-flag operand git's own
    no-`--` promotion would take as a pathspec: one naming an existing
    path under `cwd`, or one carrying pathspec magic — glob metachars
    `* ? [` or a leading `:`.
    """
    if not co.is_store_checkout or "--" in git_args:
        return []
    for token in git_args:
        if token.startswith("-"):
            continue
        if ((cwd / token).exists() or token.startswith(":")
                or any(mark in token for mark in "*?[")):
            return []
    return ["--", "."]


def cmd_diff(args) -> int:
    """Run `git diff` inside the current child, scoped to the binding."""
    co, cwd = _passthrough_target("diff")
    git_args = [*args.git_args, *_scoped_dot(args.git_args, co, cwd)]
    return _run_child_command(["git", "diff", *git_args], co, cwd)


def cmd_log(args) -> int:
    """Run `git log` inside the current child, scoped to the binding."""
    co, cwd = _passthrough_target("log")
    git_args = [*args.git_args, *_scoped_dot(args.git_args, co, cwd)]
    return _run_child_command(["git", "log", *git_args], co, cwd)


def _worktree_takeover_detail(new_child: Path, force: bool) -> str | None:
    """Error detail when the worktree link step may not take over an
    existing child path; None when it may proceed.

    A missing path links cleanly. Without `force` any collision is
    refused; under `-f` a symlink or regular file is replaced — never a
    real directory or other non-regular path, which the add did not
    create and unlink() cannot remove.
    """
    if not (new_child.is_symlink() or new_child.exists()):
        return None
    if not force:
        return f"child path {new_child} already exists"
    if new_child.is_symlink() or new_child.is_file():
        return None
    if new_child.is_dir():
        return (f"child path {new_child} is a real directory; "
                f"refusing to remove it")
    return (f"child path {new_child} is not a symlink or regular "
            f"file; refusing to remove it")


def _worktree_link_folders(
    parent: Path, new_parent: Path, folders: list[dict], force: bool,
) -> None:
    """Copy the manifest and link managed git-folders into the added
    worktree.

    Runs AFTER `git worktree add` registered `new_parent`, so it raises
    instead of dying and the caller can roll the worktree back: a
    folder-identified failure raises ValidationError carrying the
    folder_error envelope; an OSError escaping the manifest copy
    propagates raw.
    """
    # Propagate the current manifest so commands in the new worktree see the
    # same git-folders, even when gf.toml is not yet committed.
    for manifest_name in (manifest.MANIFEST, manifest.LOCAL):
        src = parent / manifest_name
        dst = new_parent / manifest_name
        if src.is_symlink() or not src.is_file():
            continue
        if dst.is_symlink():
            # A checked-out symlink (e.g. a committed `gf.toml -> /abs`)
            # is never written through — the copy replaces it.
            dst.unlink()
        elif os.path.lexists(dst) and not dst.is_file():
            raise ValidationError(
                f"{dst} in the new worktree is not a regular file; "
                f"refusing to copy {manifest_name} over it")
        dst.write_text(src.read_text())

    for folder in folders:
        source_child = parent / folder["path"]
        if not manifest.is_git_folder_child(source_child):
            continue
        # The link spells the source consumer path AS WRITTEN — for a
        # subfolder binding that is the consumer link itself, so a later
        # retarget of the source binding propagates through the chain
        # (spec L399/L400; GF-D14). Resolving here would freeze the link
        # at today's checkout position.
        new_child = new_parent / folder["path"]
        # A mid-path symlink the checkout materialized (a committed
        # `vendor -> /abs` link) must not redirect the unlink/replace
        # below outside the new worktree, into `.gf` storage, or into
        # the worktree's `.git` metadata — refuse so the caller's
        # rollback removes the worktree. The leaf's own realpath is
        # never tested: a checked-out link leaf is unlinked, not
        # followed.
        new_child_parent = Path(os.path.realpath(new_child.parent))
        if not (
            new_child_parent.is_relative_to(Path(os.path.realpath(new_parent)))
            and not layout.in_gf_tree(new_child_parent)
            and not layout.in_git_tree(new_child_parent)
        ):
            raise ValidationError(folder_error(
                folder["name"], folder["path"], "worktree add",
                f"child path resolves outside {new_parent} or into "
                f"gf-managed storage ({layout.GF_DIR}) or repository "
                f"metadata (.git)"))
        detail = _worktree_takeover_detail(new_child, force)
        if detail is not None:
            raise ValidationError(folder_error(
                folder["name"], folder["path"], "worktree add", detail))
        try:
            new_child.parent.mkdir(parents=True, exist_ok=True)
            if new_child.is_symlink() or new_child.exists():
                new_child.unlink()
            # The leaf lands at the parent's REALPATH (the mid-path
            # redirect above), so the relpath base is the realpath'd
            # parent — while the TARGET stays the literally-spelled
            # source consumer path for link→link chain propagation.
            rel = os.path.relpath(source_child, new_child_parent)
            os.symlink(rel, new_child, target_is_directory=True)
        except OSError as e:
            raise ValidationError(folder_error(
                folder["name"], folder["path"], "worktree add",
                str(e))) from e


def cmd_worktree_add(args, backend: GitBackend) -> int:
    """Add a parent git worktree and symlink managed git-folders into it.

    A failure after `git worktree add` rolls the registered worktree
    back (`git worktree remove --force`) before the error is reported,
    so a retry is never blocked by an orphaned tree.
    """
    parent, manifest_data = _resolve(_logical_cwd())
    if parent is None or manifest_data is None:
        die("not inside a git repo with gf.toml")

    new_parent = Path(args.path).resolve()

    # The worktree destination must never land inside ANY `.gf` tree —
    # a `sub/.gf/...` spelling, an outside-the-parent path carrying a
    # `.gf` segment (worktrees legitimately live outside the parent, so
    # a parent-relative predicate misses those), or one that resolves
    # through a consumer link into a shared checkout — nor inside a
    # `.git` metadata tree, where the copied manifest and linked
    # children would sit among the repo's own config/hooks.
    # `new_parent` is already the resolved target `git worktree add`
    # receives; `in_gf_tree`/`in_git_tree` test `.gf`/`.git` as exact
    # realpath segments.
    if layout.in_gf_tree(new_parent) or layout.in_git_tree(new_parent):
        die(f"worktree path {new_parent} resolves inside gf-managed "
            f"storage ({layout.GF_DIR}) or repository metadata (.git)")

    folders = manifest_data.get("git_folder", [])

    wt_args = []
    if args.force:
        wt_args.append("--force")
    if args.new_branch:
        wt_args.extend(["-b", args.new_branch])
    if args.new_or_existing_branch:
        wt_args.extend(["-B", args.new_or_existing_branch])
    wt_args.append(str(new_parent))
    if args.commitish:
        wt_args.append(args.commitish)

    # Pre-flight the takeover check at the destination: a child path
    # that already exists dies before `git worktree add` registers the
    # worktree. The post-add pass repeats it for content the checkout
    # materializes (and races).
    for folder in folders:
        if not manifest.is_git_folder_child(parent / folder["path"]):
            continue
        detail = _worktree_takeover_detail(
            new_parent / folder["path"], args.force)
        if detail is not None:
            die(folder_error(
                folder["name"], folder["path"], "worktree add", detail))

    try:
        backend.git("worktree", "add", *wt_args, cwd=parent, stream=True)
    except GitFoldersError as e:
        die(str(e), code=e.code)

    try:
        _worktree_link_folders(parent, new_parent, folders, args.force)
    except Exception as e:
        # A post-add failure must not orphan the registered worktree:
        # retry would fail on the existing directory and a plain remove
        # refuses the untracked copied manifest. `--force` drops the
        # registration and the tree; a locked worktree needs the force
        # doubled (-ff). If the rollback still fails, the user must hear
        # about the leftover registration, not just the original error.
        result = backend.git("worktree", "remove", "--force",
                             str(new_parent), cwd=parent, check=False)
        if result.returncode != 0:
            result = backend.git("worktree", "remove", "--force",
                                 "--force", str(new_parent),
                                 cwd=parent, check=False)
        if result.returncode != 0:
            # A nonzero remove does not prove the registration
            # survived: the tree may be gone while a later admin-dir
            # cleanup step failed (e.g. a remove shim that runs the
            # real remove then exits 1). Re-check the porcelain list so
            # the report reflects the actual registration state.
            detail = (result.stderr.strip() or result.stdout.strip()
                      or "unknown error")
            try:
                registered_paths = [
                    Path(wt["path"]).resolve()
                    for wt in shelf.list_parent_worktrees(parent, backend)
                ]
            except Exception:
                registered_paths = None
            if (registered_paths is not None
                    and new_parent not in registered_paths):
                # The remove actually landed despite its exit code;
                # report only the original post-add failure, exactly
                # as the rollback-success path does.
                die(str(e), code=getattr(e, "code", 1))
            if registered_paths is None:
                # The registration state could not be determined;
                # name the orphan and both remedies.
                die(f"{e} — and rollback of the new worktree also "
                    f"failed: {detail}; remove the orphaned worktree "
                    f"with `git worktree remove --force {new_parent}` "
                    f"or clear a leftover registration with "
                    f"`git worktree prune`")
            if new_parent.exists():
                hint = (f"remove the orphaned worktree with `git "
                        f"worktree remove --force {new_parent}`")
            else:
                # Registered but unlinked: `git worktree prune` drops
                # exactly this stale record, while another remove can
                # only fail on the missing tree again.
                hint = ("clear the orphaned worktree registration "
                        "with `git worktree prune`")
            die(f"{e} — and rollback of the new worktree also failed: "
                f"{detail}; {hint}")
        die(str(e), code=getattr(e, "code", 1))

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
    if _logical_cwd().resolve().is_relative_to(worktree_path):
        die(f"{worktree_path} contains the current working directory; "
            f"refusing to remove it")

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
    p_pull.add_argument("--autostash", action="store_true", dest="autostash", default=False, help="stash, pull, and restore local changes")

    p_rm = sub.add_parser("rm", help="remove selected children")
    p_rm.add_argument("path", nargs="*")
    p_rm.add_argument("--all", action="store_true", dest="all", default=False, help="unregister every git-folder (requires running from the parent repo root)")

    p_status = sub.add_parser("status", help="show git porcelain status")
    p_status.add_argument("path", nargs="*")
    p_status.add_argument("--remote", action="store_true", default=False, help="classify drift against local remote-tracking refs (clean/ahead/behind/diverged/missing, -dirty when the tree is dirty)")

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
        # `pull --force` was removed rather than repurposed: `gf` offers
        # no flag that waives preservation — a dirty tree is committed,
        # stashed, or carried through the update by --autostash. Name the
        # removal instead of a bare "unrecognized arguments" so a user
        # who learned the old flag gets the recovery, not just a refusal.
        if args.command == "pull" and any(
            a == "--force" or a.startswith("--force=") for a in unknown
        ):
            parser.error(
                "gf pull has no --force: updating over uncommitted work "
                "is never offered — commit or stash the work first, or "
                "pull with --autostash")
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")

    # GitFoldersError escaping a command (e.g. a manifest error raised by
    # `_resolve` before any per-folder envelope) dies cleanly with its
    # deterministic code instead of tracebacking. Errors already caught
    # inside a cmd_* exit through die()'s SystemExit, untouched here.
    # The trailing OSError catch is the completeness net over fs sites a
    # sweep can't exhaustively cover: a raw fs failure still dies with
    # the `gf:` envelope and exit 1 rather than a Python traceback.
    try:
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
    except GitFoldersError as e:
        die(str(e), code=e.code)
    except OSError as e:
        die(str(e))
    return 0
