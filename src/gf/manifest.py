# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

from pathlib import Path
from typing import Any, Optional

import tomllib
import tomli_w

from . import layout

MANIFEST = "gf.toml"
LOCAL = "gf.local.toml"


def is_git_folder_child(path: Path) -> bool:
    """Return True if `path` (or a directory it symlinks to) contains a git-folder child."""
    return (layout.resolve_checkout(path).gitdir / "HEAD").is_file()


def find_parent_root(start: Path) -> Optional[Path]:
    """Walk up from start looking for a parent repo that contains .git.

    A `.git` inside `<root>/.gf/wt` belongs to a gf-managed checkout — the
    gitfile's canonical position before removal — and never marks a parent
    repo, so discovery walks past it (plan D4 / arch GF-D8).
    """
    for path in [start, *start.parents]:
        if (path / ".git").exists() and not layout.in_gf_wt(path):
            return path.resolve()
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
    resolve `start` and look for the child under the resolved parent so the
    caller can safely call `child.relative_to(parent)`.
    """
    start = Path(start)
    parent = find_parent_root(start)
    if parent is None:
        return None, None
    child = find_child_root(start, parent)
    if child and not child.is_relative_to(parent):
        child = find_child_root(start.resolve(), parent)
    return parent, child


def read_manifest(parent_root: Path) -> dict:
    path = parent_root / MANIFEST
    with open(path, "rb") as f:
        return tomllib.load(f)


def read_local_overrides(parent_root: Path) -> list[dict]:
    path = parent_root / LOCAL
    if not path.is_file():
        return []
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data.get("git_folder_override", [])


def write_manifest(parent_root: Path, data: dict) -> None:
    with open(parent_root / MANIFEST, "wb") as f:
        tomli_w.dump(data, f)


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
