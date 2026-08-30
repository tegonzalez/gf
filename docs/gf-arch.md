---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-design]; otherwise, do not modify."
---

# git-folders Architecture

## Purpose

Describe the high-level architecture of the `git-folders` tool.

## Overview

`git-folders` is a git-folder repository manager for git. A parent git repository declares git-folders in a tracked manifest (`gf.toml`). Each git-folder is cloned into a user-defined child directory inside the parent workspace. The child is a real git worktree with its own history; its git metadata lives in `.gf/` instead of `.git/`. The child gitdir at `child/.gf/git` is a full, self-contained gitdir with `origin` pointing to the actual git-folder URL.

## Module Inventory

### Modules and responsibilities

### `src/gf/cli.py`

The command-line entry point and parser. It implements the global `-C <path>` option (only when it appears before the subcommand), `--version` / `-v`, command dispatch, and the project-root discovery protocol. Key helpers:

- `_resolve(cwd)` — central manifest/parent discovery.
- `_align_columns(rows)` — generic column alignment for `ls` and `status`.
- `_normalize_url(url)` / `_name_from_url(url)` — URL shorthand expansion and basename name derivation.
- `_get_version()` — package version from `importlib.metadata` with a `pyproject.toml` fallback.
- `_apply_chdir(argv)` — applies leading global `-C <path>` options and returns the remaining argv; trailing `-C` is left for git passthrough commands.
- `cmd_clone` — supports `--depth <n>` and `--single-branch`.
- `cmd_pull` — supports `--rebase`, `--force`, and `--autostash`.
- `cmd_status` — supports `--remote` drift classification.
- `cmd_ls`, `cmd_rm` (with `--all`), `cmd_init` (with `-n <name>` and `--url <url>`).
- `cmd_diff`, `cmd_log`, `cmd_git` — passthrough commands that forward trailing `argparse.REMAINDER` args to git, with explicit reconstruction to preserve token order.
- `cmd_worktree_add`, `cmd_worktree_list`, `cmd_worktree_remove`.
- `cmd_sh` — runs a shell or command with `GIT_DIR` set to the child gitdir.

### `src/gf/shelf.py`

The git operations layer. It manages child gitdir creation, ref resolution, checkout, and drift classification.

- `_init_child_gitdir` — creates `.gf/git` as a full, self-contained gitdir.
- `_set_child_origin` — configures `origin` to point to the actual git-folder URL.
- `resolve_ref` — resolves `latest`, branch, tag, and commit refs to a SHA in the child gitdir.
- `_resolve_remote_branch` — resolves a remote tracking branch (`refs/remotes/origin/<branch>`) directly, avoiding ambiguity with local branches or tags.
- `_effective_branch` — maps `latest` to the remote default branch and recognizes branch refs.
- `_fetch_and_checkout` / `_fetch_and_rebase` — fetch `origin` and check out or rebase the resolved ref; support `--force`, `--depth`, and `--single-branch` where applicable.
- `init_child` / `update_child` — initialize or update a child; `update_child` supports `--rebase`, `--force`, and `--autostash`. For branch refs it fetches `origin` and checks out a local tracking branch (`git checkout -B <branch> origin/<branch>`). If child creation fails, `_cleanup_new_child` removes the partial `.gf/git` and the child directory (only what the tool created).
- `drift` — classifies a child as `clean`, `behind`, `local-dirty`, or `both` against its effective ref, with optional `git fetch origin` when `--remote` is used.
- `list_parent_worktrees` — parses `git worktree list --porcelain` into worktree records.
- `linked_git_folders_in_worktree` / `unlink_linked_git_folders_in_worktree` — detect and guard relative symlinks to git-folder children inside a parent worktree.
- `remove_child` — unregisters a child by moving `.gf/git` to `.git`.
- `select_children` — selects git-folders from the manifest by context and path arguments, with a fallback to matching by git-folder name.

### `src/gf/manifest.py`

Manifest reading, writing, and discovery. Resolves the parent repo context, reads `gf.toml`, `gf.local.toml` overrides, and computes effective URL/ref for each git-folder.

### `src/gf/state.py`

Per-child metadata stored at `.gf/state`. Records the resolved SHA, requested ref, effective URL, and whether a local override is active.

### `src/gf/backends.py`

- `GitBackend` protocol for spawning or mocking git.
- `GitCliBackend` — default real git backend.

### `src/gf/runner.py`

- Shared command runner with three modes: `capture`, `stream`, and `exec`.
- `capture` collects output for parsing.
- `stream` echoes stdout/stderr live while collecting it for error messages.
- `exec` replaces the process when stdout is a terminal so interactive programs (e.g. pagers) work.

### `tests/mock_git.py`

In-memory mock git backend used by `test_cli_permutations.py`. It simulates `init`, `remote add/set-url`, `remote set-head`, `fetch`, `checkout`, `merge --ff-only`, `rev-parse`, `show-ref`, `symbolic-ref`, `status --porcelain`, and `config` commands against a `pyfakefs` filesystem.

## Architecture

### Data flow

```text
parent repo
├── gf.toml            # canonical manifest
├── gf.local.toml      # untracked local overrides
└── <child>/
    ├── .gf/git/       # child gitdir
    │   ├── HEAD
    │   ├── objects/       # self-contained object store
    │   ├── refs/
    │   └── config         # origin = actual git-folder URL
    ├── .gf/state      # resolved ref metadata
    └── ...                # git-folder worktree
```

## Design Decisions

### Seams and contracts

1. **Manifest contract** — `gf.toml` is the single source of truth; `gf.local.toml` may override `url` and `ref` by git-folder `name`.
1. **Gitdir contract** — child `.gf/git` is a full gitdir, not bare; all objects and refs live in the child.
1. **Origin contract** — child `origin` fetches from and pushes to the actual git-folder URL.
1. **Ref contract** — `latest` resolves to the remote default branch; branch refs track `origin/<branch>`; tag/commit refs resolve to a SHA and may detach.
1. **Discovery contract** — `_resolve(cwd)` walks up from `cwd` to find the parent `.git` and `gf.toml`.