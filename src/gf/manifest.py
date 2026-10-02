# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Optional

import tomli_w

from . import layout
from .exceptions import GitFoldersError, ValidationError

MANIFEST = "gf.toml"
LOCAL = "gf.local.toml"


def is_git_folder_child(path: Path) -> bool:
    """Return True if `path` (or a directory it symlinks to) contains a git-folder child."""
    return (layout.resolve_checkout(path).gitdir / "HEAD").is_file()


def find_parent_root(start: Path) -> Optional[Path]:
    """Walk up from start looking for a parent repo that contains .git.

    Context discovery, not binding ownership — `binding_root` owns the
    anchor side of a declared binding path.

    A `.git` inside `<root>/.gf/wt` belongs to a gf-managed checkout — the
    gitfile's canonical position before removal — and never marks a parent
    repo, so discovery walks past it (plan D4 / arch GF-D8).
    """
    for path in [start, *start.parents]:
        if (path / ".git").exists() and not layout.in_gf_wt(path):
            return path.resolve()
    return None


def binding_root(child: Path, path: str) -> Optional[Path]:
    """The ancestor of resolved `child` under which a manifest `path` spells it.

    Owning root for source-relative url anchoring: the root where the
    binding's declared consumer path is real. A foreign repository nested
    between the child and the declaring root cannot claim the anchor —
    `root / path` resolves elsewhere there. An ancestor inside `.gf`
    storage never qualifies either: the marker + spelling can coincide
    there (cone mode materializes repo-root files such as `gf.toml`,
    and the mapped `path` resolves inside the checkout), but gf's own
    storage is never a declaring root.
    """
    if Path(path).is_absolute():
        return None
    for anc in child.parents:
        if (anc / path).resolve() == child and not layout.in_gf_tree(
            anc
        ) and (
            (anc / ".git").exists() or (anc / MANIFEST).is_file()
        ):
            return anc
    return None


def find_child_root(path: Path, stop: Path) -> Optional[Path]:
    """If path is inside a child, return the child root (directory containing .gf).

    The returned path is *not* resolved, so symlinks into a child from another
    worktree are preserved. Callers that need the real gitdir can resolve it.
    """
    for p in [path, *path.parents]:
        if is_git_folder_child(p):
            return p
        if p == stop:
            break
    return None


def resolve_context(start: Path) -> tuple[Optional[Path], Optional[Path]]:
    """Return (parent_root, child_root) for the starting directory.

    When `start` passes through a symlink into another repo, the unresolved
    child path may not be a subpath of the resolved parent. In that case we
    resolve `start` and look for the child under the resolved parent.

    Callers must consume `child` in a spelling whose resolution does not
    depend on `cwd` — pass the absolute `child` itself (a `cwd / <abs>`
    join collapses to `<abs>`), never `child.relative_to(parent)`, which a
    `cwd / arg` re-anchoring in `select_children` would double onto a cwd
    inside the physical `.gf/wt` checkout.
    """
    start = Path(start)
    parent = find_parent_root(start)
    if parent is None:
        return None, None
    child = find_child_root(start, parent)
    if child and not child.is_relative_to(parent):
        child = find_child_root(start.resolve(), parent)
    return parent, child


def _validate_manifest(data: dict, path: Path, key: str) -> list[dict]:
    """Return the `key` folder list from parsed `data`, checking type shape.

    The list must hold tables whose `name`/`url`/`ref`/`path` — whichever
    are present — are strings; `git_folder` entries must carry all four
    (every consumer indexes them unconditionally). Anything else fails
    here, naming the file and the offending key, instead of surfacing as
    a TypeError/KeyError deep in shelf. Value semantics — what the paths
    and refs mean — are not checked here.
    """
    folders = data.get(key, [])
    if not isinstance(folders, list):
        raise GitFoldersError(
            f"invalid git-folders manifest at {path}: "
            f"'{key}' must be a list of tables")
    for i, entry in enumerate(folders):
        where = f"{key}[{i}]"
        if not isinstance(entry, dict):
            raise GitFoldersError(
                f"invalid git-folders manifest at {path}: "
                f"'{where}' must be a table")
        who = (f" (git-folder {entry['name']!r})"
               if isinstance(entry.get("name"), str) else "")
        if key == "git_folder":
            for field in ("name", "url", "ref", "path"):
                if field not in entry:
                    raise GitFoldersError(
                        f"invalid git-folders manifest at {path}: "
                        f"'{where}' is missing required key "
                        f"'{field}'{who}")
        for field in ("name", "url", "ref", "path"):
            if field in entry and not isinstance(entry[field], str):
                raise GitFoldersError(
                    f"invalid git-folders manifest at {path}: "
                    f"'{where}.{field}' must be a string{who}")
    return folders


def _validate_consumer_path(entry: dict, source: str) -> None:
    """Refuse a binding `path` that escapes the root or reaches `.gf`/`.git`.

    `gf.toml` is committed content, so its `path` spellings are untrusted
    input to every command that consumes them: an absolute or `..`-
    escaping spelling would direct link/checkout writes outside the
    workspace, a `.gf` segment would write inside gf's own storage, and
    a `.git` segment would plant the consumer link inside the parent
    repo's metadata — under `hooks/` the next `git checkout` executes
    it. The check is lexical — a mid-path symlink a checked-out tree
    materializes is the write sites' realpath check, not this one's.
    """
    name = entry.get("name") if isinstance(entry, dict) else None
    path = entry.get("path") if isinstance(entry, dict) else None
    if not isinstance(path, str) or not path:
        raise ValidationError(
            f"{source}: git-folder {name!r} path is not a non-empty "
            f"string")
    norm = os.path.normpath(path)
    if (
        os.path.isabs(path)
        or norm in (".", "..")
        or norm.startswith(".." + os.sep)
    ):
        raise ValidationError(
            f"{source}: git-folder {name!r} path {path!r} escapes the "
            f"parent root")
    if layout.GF_DIR in Path(norm).parts:
        raise ValidationError(
            f"{source}: git-folder {name!r} path {path!r} reaches inside "
            f"gf-managed storage ({layout.GF_DIR})")
    if ".git" in Path(norm).parts:
        raise ValidationError(
            f"{source}: git-folder {name!r} path {path!r} reaches inside "
            f"repository metadata (.git)")


def read_manifest(parent_root: Path) -> dict:
    """Read `gf.toml` under `parent_root`, returning {} when it is absent.

    A present-but-unreadable or malformed manifest raises GitFoldersError
    naming the file rather than leaking OSError/TOMLDecodeError.
    """
    path = parent_root / MANIFEST
    if not path.is_file():
        if path.exists() or path.is_symlink():
            raise GitFoldersError(
                f"cannot read git-folders manifest at {path}: "
                f"not a regular file")
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise GitFoldersError(
            f"corrupt git-folders manifest at {path}: {e}") from e
    except OSError as e:
        raise GitFoldersError(
            f"cannot read git-folders manifest at {path}: {e}") from e
    for entry in _validate_manifest(data, path, "git_folder"):
        _validate_consumer_path(entry, MANIFEST)
    return data


def read_local_overrides(parent_root: Path) -> list[dict]:
    """Read `gf.local.toml` overrides under `parent_root`; [] when absent."""
    path = parent_root / LOCAL
    if not path.is_file():
        if path.exists() or path.is_symlink():
            raise GitFoldersError(
                f"cannot read git-folders manifest at {path}: "
                f"not a regular file")
        return []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise GitFoldersError(
            f"corrupt git-folders manifest at {path}: {e}") from e
    except OSError as e:
        raise GitFoldersError(
            f"cannot read git-folders manifest at {path}: {e}") from e
    overrides = _validate_manifest(data, path, "git_folder_override")
    # Overrides match bindings by `name` and carry only `url`/`ref`, but
    # a `path` key an override happens to carry is validated the same
    # way — consistent refusal rather than an inert hostile spelling.
    for entry in overrides:
        if isinstance(entry, dict) and "path" in entry:
            _validate_consumer_path(entry, LOCAL)
    return overrides


def write_manifest(parent_root: Path, data: dict) -> None:
    """Write `gf.toml` atomically, replacing any prior content.

    mkstemp inside the parent root + os.replace, mirroring
    `state.save_checkout`: a torn write never reaches the manifest, and
    the replace swaps the `gf.toml` NAME rather than writing through a
    committed symlink planted at that path.
    """
    path = parent_root / MANIFEST
    try:
        fd, tmp = tempfile.mkstemp(
            dir=parent_root, prefix=f".{MANIFEST}.", suffix=".tmp")
    except OSError as e:
        raise GitFoldersError(
            f"cannot write git-folders manifest at {path}: {e}") from e
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(e, OSError):
            raise GitFoldersError(
                f"cannot write git-folders manifest at {path}: {e}") from e
        raise


def effective_url_ref(git_folder: dict, overrides: list[dict]) -> tuple[str, str]:
    url = git_folder["url"]
    ref = git_folder["ref"]
    for o in overrides:
        if o.get("name") == git_folder.get("name"):
            url = o.get("url", url)
            ref = o.get("ref", ref)
    return url, ref


def override_active(git_folder: dict, overrides: list[dict]) -> bool:
    for o in overrides:
        if o.get("name") == git_folder.get("name"):
            if o.get("url") is not None or o.get("ref") is not None:
                return True
    return False
