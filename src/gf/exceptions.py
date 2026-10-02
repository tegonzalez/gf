# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

class GitFoldersError(Exception):
    """Base class for git-folders failures with deterministic exit codes."""

    code = 1


class ValidationError(GitFoldersError):
    """Input or state validation failure."""

    code = 1


class GitError(GitFoldersError):
    """Network or git command failure."""

    code = 2


class DirtyError(GitFoldersError):
    """Child worktree has uncommitted changes blocking an update."""

    code = 3


def folder_error(name: str, path: str, op: str, detail: str) -> str:
    """Format an error raised while handling an identified git-folder.

    spec §Error handling: the message carries the git-folder name, its
    manifest-relative path, and the operation that failed. Every
    identified-folder die/raise site routes through this formatter so
    the three fields are present by construction.
    """
    return f"{op} failed for git-folder '{name}' ({path}): {detail}"
