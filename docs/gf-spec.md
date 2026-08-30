---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-spec]; otherwise, do not modify."
---

# git-folders Specification

## Purpose

This document specifies the `gf` command-line tool and the `git-folders` manifest and child-state model.

## Overview

`git-folders` is a git-folder repository manager for git. A parent git repository declares git-folders in a manifest (`gf.toml`). Each git-folder is cloned into a user-defined child directory inside the parent workspace. The child is a real git worktree with its own history; its git metadata lives in `.gf/` instead of `.git/`, so agentic tools that scan for `.git` do not treat the child as a nested repository. The child gitdir at `child/.gf/git` is a full, self-contained gitdir that uses the actual git-folder URL as its `origin`.

## Terminology

| Term               | Meaning                                                                                  |
| ---                | ---                                                                                      |
| Parent repo        | The git repository that declares and consumes git-folders.                               |
| Consumer workspace | A single worktree of the parent repo.                                                    |
| Git-folder         | An external git repository consumed by the parent repo.                                  |
| Child              | A git-folder checkout in the consumer workspace.                                         |
| Workspace binding  | A manifest entry that maps a git-folder name to a URL, a reference, and a consumer path. |

## Physical layout

### Parent repository

```text
parent/
├── .git
├── gf.toml              # tracked canonical manifest
├── gf.local.toml        # untracked local overrides
└── <user-defined child path>/
    ├── .gf/             # git-folder git metadata and git-folders state
    │   ├── git/             # child gitdir
    │   │   ├── HEAD
    │   │   ├── index
    │   │   ├── config       # core.bare=false, core.worktree=...
    │   │   └── objects/     # self-contained object store
    │   └── state            # gf metadata (resolved ref, etc.)
    └── ...                  # git-folder files
```

The consumer child’s `.gf/git/` is a full gitdir: it holds its own `HEAD`, `index`, `refs`, `config`, and `objects`. The `origin` remote in the child points to the actual git-folder URL for both fetch and push. The child is self-contained and does not share an object store with any other child or an external shelf.

## Manifest

`gf.toml` is tracked in the parent repository. It contains the canonical git-folder list.

```toml
[[git_folder]]
name = "libfoo"
url = "https://github.com/foo/libfoo.git"
ref = "main"
path = "vendor/libfoo"

[[git_folder]]
name = "libbar"
url = "https://github.com/bar/libbar.git"
ref = "v1.2.0"
path = "extern/libbar"
```

`gf.local.toml` is gitignored and holds per-user overrides without changing tracked parent files.

```toml
[[git_folder_override]]
name = "libfoo"
url = "https://github.com/myfork/libfoo.git"
ref = "feature/x"
```

At runtime the effective URL and ref for a git-folder are the manifest values unless an override with the same `name` is present in `gf.local.toml`.

## Reference model

A binding `ref` may be one of:

- `commit` — a pinned SHA.
- `tag` — a pinned tag, resolved to its SHA.
- `branch` — a floating reference that resolves to the remote branch tip.
- `latest` — a floating reference that resolves to the remote default branch tip.

`gf` resolves a ref to a concrete SHA before checkout. For floating refs the resolved SHA is recorded in the child state so drift can be reported on subsequent runs.

## `.gf` directory

`.gf` is the analog of `.git` for a git-folder worktree. It is a directory at the child root. It contains at minimum:

- `git/` — the gitdir for this child.
- `state` — git-folders-specific metadata such as the last resolved ref and whether a local override is active.

The child gitdir is a full gitdir with its own `HEAD`, `index`, `refs`, `config`, and `objects` so the child can be on its own branch or commit.

Because `.gf` is a directory in the child worktree, git would normally report it as untracked. `gf clone`/`init` seeds `.gf/git/info/exclude` with the pattern `.gf/` so git ignores it.

## Parent and child gitignore handling

The parent repo must not track git-folder files. `gf` does not edit the parent `.gitignore`. When a child is created, `gf` prints a one-line recommendation such as `add "vendor/lib/" to .gitignore` so the user can decide how to keep git-folder files out of the parent history.

The child repo must ignore its own `.gf/` directory. `gf clone`/`init` adds `.gf/` to `.gf/git/info/exclude`.

## Public Surface

### Command reference

All commands support a global `-C <path>` option, just like `git -C`. It changes to `<path>` before resolving context and interpreting path arguments.

`gf --version` (or `gf -v`) prints the `git-folders` package version (sourced from `pyproject.toml` via `importlib.metadata`, with a `pyproject.toml` fallback when running from source) and exits 0. It is handled before subcommand dispatch, so it works without a parent repo.

#### Global `-C` vs. `git -C` ambiguity

Because `gf` forwards trailing arguments to `git` for the `diff`, `log`, and `git` passthroughs, the global `-C <path>` option and `git`'s own `-C` can appear in similar positions. The rule is: a `-C` that appears **before** the subcommand is the global `gf -C` (it changes `gf`'s working directory); a `-C` that appears **after** the subcommand is passed through to `git`. For example:

- `gf -C vendor/libfoo diff` — `gf` changes to `vendor/libfoo`, then runs `git diff` in that child.
- `gf diff -C vendor/libfoo` — `gf` runs `git diff -C vendor/libfoo` in the current child, forwarding `-C` to `git diff`.

### `gf clone <url> [<path>] [-n <name>] [-b <ref>] [--depth <n>] [--single-branch]`

Add a git-folder to `gf.toml` and create its child worktree. This is the "add" operation: it is non-destructive.

- `<url>` is the repository to clone (remote, local, or bare host/path like `github.com/cursor/plugins`, which is expanded to `https://...`).
- `<path>` is the optional consumer path. If omitted, it defaults to the repository name (the last path component).
- The git-folder `name` is derived from the basename of `<path>` if given, otherwise from the basename of `<url>` (`.git` stripped). Use `-n <name>` to override it.
- `-b <ref>` sets the branch, tag, or commit to check out, like `git clone -b`. If omitted, it defaults to `latest`.
- `--depth <n>` is passed through to the child fetch as `git fetch --depth=<n> origin`, creating a shallow clone. It only affects the initial fetch; subsequent `gf pull` operations fetch normally.
- `--single-branch` narrows `remote.origin.fetch` to the resolved branch after the initial fetch, so subsequent fetches only fetch that branch's history. With `latest`, the remote default branch is used. It is ignored for tag/commit refs (which fetch the single ref needed for the checkout).
- If the path is already a git-folder (contains `<dir>/.gf/git/`), the command succeeds after adding the manifest entry; it does not re-clone or overwrite the existing worktree.
- Fail if the path exists, is not empty, and is not a git-folder.
- Create `<dir>/.gf/git/` as a full self-contained gitdir.
- Add `origin` pointing to the actual effective URL and `git fetch origin`.
- If any step from `git init` through `git checkout` fails, remove the child directory (if it did not exist before the command) or remove the `.gf/` directory (if the child existed but was not yet a git-folder), so no partial state is left behind.
- Resolve the effective ref in the child gitdir.
- If the effective ref is a branch (or `latest` resolves to the remote default branch), create a local tracking branch and check it out with `git checkout -B <branch> origin/<branch>`.
- If the effective ref is a tag or commit, check it out in a detached `HEAD`.
- Long-running git operations (`fetch`, `checkout`) stream stdout and stderr so progress is visible.
- Add `.gf/` to `.gf/git/info/exclude`.
- Print `add "<dir>/" to .gitignore` as a recommendation; do not modify the parent `.gitignore`.

### `gf pull [--rebase] [--force] [--autostash] [<path>...]`

Update or initialize selected children to their effective refs.

- With no arguments:
  - If `cwd` is inside a child, update that child.
  - Otherwise, update all children whose paths are at or below `cwd`.
- With path arguments, each argument selects the git-folder whose consumer path matches or contains the argument.
- For each selected child:
  - If the effective URL is a local path that resolves to the child path itself (the placeholder written by `gf init`), the child is skipped until a real upstream is configured, even if the child does not exist yet.
  - Resolve relative local URLs against the parent repo root.
  - If the child path does not exist or has no `.gf/git/`, create a full gitdir, set `origin` to the actual URL, `git fetch origin`, resolve the ref, and check it out.
  - If the child worktree is dirty:
    - Without `--force` or `--autostash`, abort with a clear message.
    - With `--autostash`, run `git stash push -u -m "gf autostash"` first, perform the update, then `git stash pop`. If the pop conflicts, the stash is left in place and `gf` errors clearly so the user can resolve and pop manually.
    - With `--force` (and not `--autostash`), proceed with the update even though the worktree is dirty. For non-rebase updates the checkout uses `-f` (`git checkout -f -B <branch> origin/<branch>` or `git checkout -f <sha>`), discarding local worktree changes to the checked-out files. For `--rebase --force`, the local branch is force-recreated at the current `HEAD` with `git checkout -f -B <branch> HEAD` before the rebase, which discards dirty worktree changes to the checked-out files; git itself may still refuse a rebase with a dirty worktree, so use `--autostash` to preserve local changes across the rebase instead.
  - Long-running git operations (`fetch`, `checkout`) stream stdout and stderr so progress is visible.
  - For `branch` refs or `latest`: fetch `origin` in the child and run `git checkout -B <branch> origin/<branch>`, creating a local tracking branch. With `--force`, use `git checkout -f -B <branch> origin/<branch>`.
  - With `--rebase` and a `branch` ref: fetch `origin`, switch to the local branch, and run `git rebase origin/<branch>` instead of resetting with `checkout -B`.
  - For `tag` or `commit`: `git checkout <resolved-sha>`. With `--force`, use `git checkout -f <sha>`.
  - Print `add "<dir>/" to .gitignore` when a missing child is initialized; do not modify the parent `.gitignore`.

### `gf rm <path>... [--all]`

Unregister a git-folder from the manifest and convert the child back to a normal git repo.

- With no arguments:
  - If `cwd` is inside a child, remove that child.
  - Otherwise, error; explicit path or `--all` is required to avoid accidental bulk removal.
- With `--all`:
  - No positional paths may be given.
  - `cwd` (or the directory set by the global `-C`) must resolve to the parent repo root; otherwise `gf rm --all` errors to avoid accidental mass removal from the wrong directory.
  - Selects every git-folder in the manifest.
- With explicit path arguments, each argument selects the git-folder whose consumer path matches or contains the argument.
- Compute the updated `gf.toml` and write it to a temporary file in the parent repo.
- Move `child/.gf/git` to `child/.git` so the user's worktree is preserved.
- Atomically replace `gf.toml` with the temporary file only after all selected children are converted.
- The child directory and its contents remain.

### `gf status [<path>...] [--remote]`

Show the git porcelain status for selected children. By default this command does not access the network.

- With no arguments:
  - If `cwd` is inside a child, show that child.
  - Otherwise, show all children whose paths are at or below `cwd`.
- With path arguments, each argument selects the git-folder whose consumer path matches or contains the argument.
- Prints one header line per child: `name url [branch]`.
- `branch` is the current local branch, or empty brackets for a detached `HEAD`.
- Followed by `git status --porcelain` output for any dirty files in that child.
- With `--remote`, append the drift state to the header line: `name url [branch] state`. `state` is one of `clean`, `behind`, `local-dirty`, `both`, or `missing` per the drift algorithm below. `--remote` does **not** access the network; drift is computed from the local remote-tracking refs already present in the child gitdir (the same refs `git status` compares against after a `git fetch`). Run `gf pull` or `gf git fetch` first to refresh those refs. The non-`--remote` output format is unchanged when `--remote` is not given. If the effective ref cannot be resolved locally, `gf status --remote` exits with a deterministic non-zero code instead of printing a drift state.

### `gf ls [<path>...]`

Quick list of git-folders. This command does not access the network.

- With no arguments, lists **all** git-folders in the manifest, even when run from inside a child. `ls` is intentionally global; it does not follow the child-context rule used by `status`, `pull`, and `rm`.
- With path arguments, each argument selects the git-folder whose consumer path matches or contains the argument.
- One line per child: `name url [branch] short-hash`.
- Columns are aligned using consistent spacing.
- Appends a `*` to the hash when the child has uncommitted worktree changes.
- `branch` is the current local branch, or empty brackets for a detached `HEAD`.

### `gf init [<path>] [-b <ref>] [-n <name>] [--url <url>]`

Create an empty local child `.gf` gitdir and add it to `gf.toml`.

- Like `git init [<directory>]`, the optional positional argument is the consumer directory.
- If `<path>` is omitted, the current directory becomes the child.
- If `<path>` is given, create the directory if it does not exist and initialize `.gf` inside it.
- Discovers the parent repo by walking from `cwd` up to the top-level `.git` (the first ancestor containing `.git`), then ensures `gf.toml` exists there.
- Adds the git-folder to `gf.toml`.
- The git-folder name is derived from the directory name. Use `-n <name>` to override it.
- Sets the git-folder URL to the local child path; update `gf.toml` before `pull` if an upstream is needed. If `--url <url>` is given, store that URL in `gf.toml` instead of the local placeholder, so a later `gf pull` fetches from the real upstream.
- Refuse to initialize a directory that already contains a `.git` or `.gf` directory.
- Print `add "<dir>/" to .gitignore` as a recommendation; do not modify the parent `.gitignore`.

### `gf sh [command...]`

Run a shell or a single command inside a child with the correct git environment.

- The child is selected by the global `-C` option or by the current working directory.
- Only works in directories that contain `.gf/git/`; it does not fall back to the parent `.git`.
- `GIT_DIR` points to `<child>/.gf/git` and `GIT_WORK_TREE` points to the child root.
- When stdout is a terminal, `gf` replaces itself with the command so interactive programs (e.g. `git log` with a pager) work.
- When stdout is not a terminal, the command output is captured and printed.
- With no command, starts an interactive shell.
- With one or more arguments, runs them as a single command and exits.
- A quoted command can also be passed with `-c <command>`.

Example:

```bash
gf sh git status
gf sh -c "git diff"
gf sh -C vendor/libfoo git log -p
```

This is the supported way to run arbitrary git commands against a child. Direct `gf status/push` passthrough is still deferred.

### `gf git [args...]`

Run an arbitrary `git` command in a child with the correct git environment. This is equivalent to `gf sh git <args>`, but without the extra `sh` indirection.

- The child is selected by the global `-C` option or by the current working directory.
- Trailing arguments are passed directly to `git`.
- When stdout is a terminal, `gf` replaces itself with `git` so `less`/pagers work.
- When stdout is not a terminal, the output is streamed.

Example:

```bash
gf git status
gf git diff --cached
gf git -C vendor/libfoo log --oneline
```

### `gf diff [args...]`

Run `git diff` in a child with the correct git environment.

- The child is selected by the global `-C` option or by the current working directory.
- Trailing arguments are passed directly to `git diff`.
- When stdout is a terminal, `gf` replaces itself with `git diff` so `less`/pagers work.
- When stdout is not a terminal, the diff is streamed.

Example:

```bash
gf diff
gf diff --cached
gf diff -C vendor/libfoo
```

### `gf log [args...]`

Run `git log` in a child with the correct git environment.

- The child is selected by the global `-C` option or by the current working directory.
- Trailing arguments are passed directly to `git log`.
- When stdout is a terminal, `gf` replaces itself with `git log` so `less`/pagers work.
- When stdout is not a terminal, the log is streamed.

Example:

```bash
gf log
gf log --oneline
gf log -C vendor/libfoo -p
```

### `gf worktree add <path> [<commit-ish>] [-b <new-branch>] [-B <new-or-existing-branch>] [-f]`

Create a new git worktree of the parent repo and symlink each managed git-folder into it.

- `<path>` is passed to `git worktree add` (git itself decides if a path is valid).
- Branch and commit options are passed to `git worktree add`.
- After the parent worktree is created, the current `gf.toml` and `gf.local.toml` are copied to it so all `gf` commands work from the new worktree.
- `gf` places a relative symlink at `new-worktree/<git-folder-path>` for each git-folder that is a real child in the source worktree.
- The symlink points to the corresponding consumer child in the source worktree.
- The new worktree shares the git-folder's working tree; uncommitted changes are visible in both worktrees.
- `gf` commands run inside a symlinked child resolve to the source child and operate on it.
- `gf rm` cannot be run from a symlinked child; remove the git-folder from the owning worktree.

Example:

```bash
gf worktree add ../feature -b feature
gf worktree add /tmp/hotfix v1.2.0
```

### `gf worktree list [--porcelain] [--verbose]`

List the parent repo's git worktrees and annotate each with the git-folders linked into it.

- Runs `git worktree list --porcelain` in the parent repo to enumerate worktrees.
- For each worktree, walks the manifest child paths inside the worktree and reports the git-folders that are linked into it (a relative symlink to a git-folder child outside the worktree).
- Human output (default): one worktree per line, followed by an indented list of git-folders linked into it, formatted as `<name> -> <relative-link-target>`. With `--verbose`, the worktree line also shows the HEAD SHA.
- Porcelain output (`--porcelain`): one stable machine-readable block per worktree, terminated by a blank line:

  ```text
  worktree <path>
  HEAD <sha>
  branch <branch>
  git-folder <name> <relative-link-target>
  ...
  ```

  `branch` is omitted for a detached `HEAD`. `git-folder` lines are omitted when no git-folders are linked into the worktree.
- This command does not access the network.

Example:

```bash
gf worktree list
gf worktree list --porcelain
gf worktree list --verbose
```

### `gf worktree remove <path> [--force]`

Remove a parent git worktree, guarding any `gf`-created relative symlinks so the source git-folder children are not damaged.

- Before calling `git worktree remove`, walk the manifest child paths inside the target worktree. For each child path that is a relative symlink to a git-folder child outside the worktree, `os.unlink` the symlink so the source child is not touched by `git worktree remove`.
- Then call `git worktree remove [--force] <path>` in the parent repo.
- `--force` is forwarded to `git worktree remove`.
- The source git-folder children and their `.gf` metadata are preserved.
- Long-running git operations stream stdout and stderr.

Example:

```bash
gf worktree remove ../feature
gf worktree remove /tmp/hotfix --force
```

## Target selection rules

| Invocation                                    | Behavior                                                                                                                                                |
| ---                                           | ---                                                                                                                                                     |
| `gf <cmd>` with no args, `cwd` inside a child | Operate on that child.                                                                                                                                  |
| `gf <cmd>` with no args, `cwd` not a child    | Operate on all children whose paths are at or below `cwd`.                                                                                              |
| `gf <cmd> <arg>...`                           | Each `arg` is a path. If it points at or into a child, operate on that child; if it is a parent of multiple children, operate on all children below it. |

`rm` is the exception to the "all below cwd" default: no args in a non-child context is an error.

## Update algorithm

For a single child during `gf pull`:

1. Determine the effective URL and ref from `gf.toml` and `gf.local.toml`.
1. Resolve the effective URL to a real git URL (expand bare host/path, resolve local gf children to their inner gitdir).
1. If the child path does not exist or has no `.gf/git/`:
   - Create a full gitdir at `.gf/git`.
   - Add `origin` pointing to the actual URL.
   - `git fetch origin`.
   - Print `add "<dir>/" to .gitignore` as a recommendation.
   - Fail if the path exists, is not empty, and is not a git-folder.
1. Check the child dirty state with `git status --porcelain`. If dirty, abort.
1. Apply the ref:
   - For `branch` or `latest`: fetch `origin` in the child and run `git checkout -B <branch> origin/<branch>`, where `<branch>` is the requested branch or the remote default branch for `latest`.
   - For `tag`/`commit`: `git checkout <sha>`.
1. Record the resolved SHA in the child state.

## Drift algorithm

For a single child during `gf status --remote`:

1. `gf status --remote` is local-only. It does **not** call `git fetch`, `git remote`, `git ls-remote`, or any other network command. Drift is computed from the local refs already present in the child gitdir, exactly like `git status` compares `refs/heads/<branch>` to `refs/remotes/origin/<branch>` after a `git fetch`. Run `gf pull` or `gf git fetch` to refresh the remote-tracking refs first.
1. If the child is missing (no `child/.gf/git/HEAD`), the state is `missing`.
1. Read the effective ref from `gf.toml` and `gf.local.toml`.
1. Resolve the effective ref to a SHA in the child gitdir using only local refs:
   - For `branch` refs: `refs/remotes/origin/<branch>`.
   - For `latest`: `refs/remotes/origin/HEAD` if it is a valid symbolic ref, otherwise `refs/remotes/origin/main`, otherwise `refs/remotes/origin/master`. No `git remote set-head` or other network call is made.
   - For `tag` or `commit`: the peeled SHA from local refs.
   - If the ref cannot be resolved locally, raise a clear error (do not report `clean`); `gf status --remote` exits with a deterministic non-zero code.
1. Read the child `HEAD` SHA.
1. Run `git status --porcelain` in the child.
1. Classify:
   - `clean`: child `HEAD` matches the resolved SHA and the worktree is clean.
   - `behind`: child `HEAD` does not match the resolved SHA and the worktree is clean.
   - `local-dirty`: child `HEAD` matches the resolved SHA and the worktree has modifications.
   - `both`: child `HEAD` does not match the resolved SHA and the worktree has modifications.
   - `missing`: the child has no `.gf/git/HEAD`.

## Security and authority

- No secrets are written to the manifest or logs.
- `gf` does not commit or push on the user’s behalf. git-folder changes happen only when the user runs `git` through `gf sh` or another shell with `GIT_DIR` set.
- Local overrides live in `gf.local.toml`, which is never tracked.
- Parent tracked files change only when `gf.toml` is edited by `gf clone`/`init` or `gf rm`.

## Error handling

- Exit codes are deterministic:
  - `0`: success or no drift.
  - `1`: general failure or validation error.
  - `2`: network/git failure.
  - `3`: dirty worktree preventing update.
- Error messages include the git-folder name, path, and the operation that failed.

## Testing

- Unit tests for manifest parsing, ref resolution, path selection, and effective URL/ref merging.
- Integration tests with local bare repositories and multiple parent `git worktree` checkouts.
- End-to-end tests:
  - Fresh parent clone; `gf clone` a git-folder at a custom path; verify `child/.gf/git/config` has a single `origin` remote pointing at the actual URL; verify no `.git` in the child.
  - Edit a git-folder file; `gf status` shows `local-dirty`; `gf sh -c "git diff"` works.
  - Add a parent `git worktree`; `gf pull` in the new worktree; verify the new worktree uses a symlink to the source child and has an independent `HEAD`.
  - Switch a git-folder to a fork via `gf.local.toml`; `gf pull` updates without changing tracked parent files.