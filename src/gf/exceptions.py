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
