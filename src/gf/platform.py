"""POSIX path and process primitives owned by git-folders."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


def logical_cwd() -> Path:
    """Return the working directory, preferring the ``PWD`` spelling.

    When ``PWD`` is absolute and names the same directory as the process
    working directory, return it; otherwise fall back to ``os.getcwd()``.
    This keeps a symlinked spelling stable across calls. A command inside a
    ``gf worktree add`` symlink still resolves to the source child; this
    function does not change that target.
    """
    pwd = os.environ.get("PWD")
    if pwd and os.path.isabs(pwd):
        try:
            if same_path(Path(pwd), Path.cwd()):
                return Path(pwd)
        except OSError:
            pass
    return Path.cwd()


def same_path(a: Path | str, b: Path | str) -> bool:
    """Return whether ``a`` and ``b`` identify the same filesystem location.

    Uses ``(st_dev, st_ino)`` when both paths can be ``stat``ed; otherwise
    falls back to resolved path equality. A symlink and its target compare
    equal. Two spellings of a path that does not exist yet compare equal
    when their realpaths match, so a local placeholder URL can be skipped
    before the child directory is created.
    """
    a = Path(a)
    b = Path(b)
    try:
        st_a = a.stat()
        st_b = b.stat()
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)
    return (st_a.st_dev, st_a.st_ino) == (st_b.st_dev, st_b.st_ino)


def exec_or_run(
    argv: list[str],
    env: Mapping[str, str] | None = None,
    *,
    cwd: Path | str | None = None,
) -> None:
    """Replace the current process with ``argv``.

    Applies ``cwd`` with ``os.chdir`` first. Returns only when replacement
    fails or is mocked. Capture and stream stay in ``runner.py``.
    """
    if cwd is not None:
        os.chdir(cwd)
    if env is None:
        env = os.environ
    os.execvpe(argv[0], argv, env)
