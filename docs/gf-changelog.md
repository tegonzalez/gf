---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-release-notes]; otherwise, do not modify."
---

# git-folders Changelog

## Unreleased

### User-facing behavior

- `gf clone <url>` now accepts a single URL, derives the name and path from it, and supports `-n` and `-b`.
- Bare host/path URLs like `github.com/cursor/plugins` are expanded to `https://`.
- `gf ls` and `gf status` display the git-folder `url` in the second column and align columns generically.
- `gf clone` and `gf pull` now create a local tracking branch for branch/default-branch refs instead of a detached `HEAD`.
- `gf pull` skips local placeholder URLs from `gf init`.

### Correctness and consistency

- All commands use a centralized `_resolve(cwd)` helper for manifest and parent discovery.
- `gf ls` is global; `gf status`, `pull`, and `rm` remain context-aware.
- `gf rm` only unregisters the git-folder and preserves the worktree.
- `gf status` and `gf pull` return quiet success when `gf.toml` is missing.

### Tests and quality

- Added `MockGitBackend` and in-process CLI permutation tests.
- Overrode `tmp_path` to `tests/fixtures/tmp/<uuid>` for fixture isolation.
- Added tests for clone, pull, status, rm, init, URL normalization, branch checkout, and column alignment.