# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import tomllib
import tomli_w
from pathlib import Path
from typing import Any

from . import layout


def load_checkout(co: layout.Checkout) -> dict[str, Any]:
    """Read the checkout's git-folders state, returning an empty dict if missing."""
    path = co.state
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def save_checkout(co: layout.Checkout, data: dict[str, Any]) -> None:
    """Write the checkout's git-folders state, replacing any prior content."""
    path = co.state
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def load(child: Path) -> dict[str, Any]:
    """Read the per-child git-folders state, returning an empty dict if missing."""
    return load_checkout(layout.resolve_checkout(child))


def save(child: Path, data: dict[str, Any]) -> None:
    """Write the per-child git-folders state, replacing any prior content."""
    save_checkout(layout.resolve_checkout(child), data)
