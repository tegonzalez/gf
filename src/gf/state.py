import tomllib
import tomli_w
from pathlib import Path
from typing import Any


def load(child: Path) -> dict[str, Any]:
    """Read the per-child git-folders state, returning an empty dict if missing."""
    path = child / ".gf" / "state"
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def save(child: Path, data: dict[str, Any]) -> None:
    """Write the per-child git-folders state, replacing any prior content."""
    path = child / ".gf" / "state"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
