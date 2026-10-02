# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import os
import tempfile
import tomllib
import tomli_w
from pathlib import Path
from typing import Any

from . import layout
from .exceptions import GitFoldersError


def load_checkout(co: layout.Checkout) -> dict[str, Any]:
    """Read the checkout's git-folders state, returning an empty dict if missing."""
    path = co.state
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise GitFoldersError(
            f"corrupt git-folders state at {path}: {e}; remove it "
            f"(or restore it from a checkout) and rerun") from e
    except OSError as e:
        raise GitFoldersError(
            f"cannot read git-folders state at {path}: {e}") from e


def save_checkout(co: layout.Checkout, data: dict[str, Any]) -> None:
    """Write the checkout's git-folders state, replacing any prior content."""
    path = co.state
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    except OSError as e:
        raise GitFoldersError(
            f"cannot write git-folders state at {path}: {e}") from e
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
                f"cannot write git-folders state at {path}: {e}") from e
        raise


def load(child: Path) -> dict[str, Any]:
    """Read the per-child git-folders state, returning an empty dict if missing."""
    return load_checkout(layout.resolve_checkout(child))


def save(child: Path, data: dict[str, Any]) -> None:
    """Write the per-child git-folders state, replacing any prior content."""
    save_checkout(layout.resolve_checkout(child), data)
