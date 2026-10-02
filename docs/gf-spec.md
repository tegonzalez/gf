---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-spec]; otherwise, do not modify."
---

# git-folders Specification

## Purpose

This document specifies the `gf` command-line tool and the `git-folders` manifest and child-state model.

## Overview

`git-folders` is a git-folder repository manager for git. A parent git repository declares git-folders in a manifest (`gf.toml`). Each git-folder is cloned into a user-defined child directory inside the parent workspace. The child is a real git worktree with its own history; its git metadata lives in `.gf/` instead of `.git/`, so agentic tools that scan for `.git` do not treat the child as a nested repository. The child gitdir at `child/.gf/git` is a full, self-contained gitdir that uses the actual git-folder URL as its `origin`.

A binding has one of two forms. A **whole-repo binding** maps a complete repository to a consumer path, as described above. A **subfolder binding** maps one subdirectory of a repository to a consumer path: the `url` field is a plain path to a folder inside a repository, and `gf` finds where the repository ends (see [URL resolution](#url-resolution)). Several subfolder bindings of one repository share a single bare repo store and, when their refs resolve to the same branch, a single sparse linked worktree under `<root>/.gf`; a relative symlink at the consumer path exposes only the mapped subdirectory. Both forms are first-class: every command in this specification applies to both, and whole-repo behavior is unchanged.

## Terminology

| Term                   | Meaning                                                                                                                                                     |
| ---                    | ---                                                                                                                                                         |
| Parent repo            | The git repository that declares and consumes git-folders.                                                                                                  |
| Consumer workspace     | A single worktree of the parent repo.                                                                                                                       |
| Git-folder             | An external git repository consumed by the parent repo.                                                                                                     |
| Child                  | A git-folder checkout in the consumer workspace.                                                                                                            |
| Workspace binding      | A manifest entry that maps a git-folder name to a URL, a reference, and a consumer path.                                                                    |
| Whole-repo binding     | A binding whose effective `url` names a complete repository. Its child uses `child/.gf/git`.                                                                |
| Subfolder binding      | A binding whose effective `url` resolves to a folder inside a repository rather than to the repository itself.                                              |
| Repo URL / Subdir      | The two parts of a subfolder `url` after URL resolution: the repository URL and the repository-relative, POSIX-separated folder path.                       |
| URL resolution         | Finding where the repository ends inside an effective `url`; see [URL resolution](#url-resolution).                                                         |
| Repo store             | `<root>/.gf/repos/<repo-key>/git`: the bare common gitdir for one repo URL; holds objects, refs, and `origin`, shared by that repo's subfolder bindings.    |
| Checkout               | A linked worktree of a repo store at `<root>/.gf/wt/<repo-key>/<checkout-key>`, sparse-checked-out to the union of its bindings' subdirs.                   |
| Consumer link          | The relative symlink at a subfolder binding's `path` that targets `<checkout>/<subdir>`.                                                                    |
| Checkout resolver      | The single function that maps a binding to its `gitdir`, `work_tree`, `common_dir`, `subdir`, and state path. Both binding forms go through it.             |
| POSIX host             | Linux or macOS. The host operating systems `gf` supports.                                                                                                   |
| Logical cwd            | The directory named by `PWD` when `PWD` is absolute and names the same directory as the process working directory; otherwise the process working directory. |

## Host platform

`gf` runs on POSIX hosts. Linux and macOS are supported and behave identically: no command changes its algorithm, output, or exit code because of the host it runs on. Windows is not supported.

Two behaviors depend on the host. They are specified here once and are the same on every supported host.

### Path identity

`gf` resolves its working directory as the logical cwd. When `PWD` is absolute and names the same directory as the process working directory, `gf` uses the `PWD` spelling; otherwise it uses the process working directory. A child reached through a relative symlink — the link `gf worktree add` places at `new-worktree/<git-folder-path>` — therefore keeps the spelling the user typed. A `gf` command run inside that symlink still resolves to the source child and operates on it.

The global `-C <path>` option sets both the process working directory and the logical cwd to `<path>`.

Two paths name the same location when they name the same directory, not when their strings match. `gf` compares paths by directory identity, so a symlinked spelling and the directory it resolves to are one location. If either path does not exist yet, they name the same location when their resolved spellings are equal. That comparison is what lets `gf pull` skip a local placeholder URL before the child directory is created.

### Process replacement

When a passthrough command (`gf sh`, `gf git`, `gf diff`, `gf log`) writes to a terminal, `gf` replaces its own process with the requested command so pagers and interactive programs own the terminal. When stdout is not a terminal, the command runs as a child process and its output is captured or streamed. Process replacement is the POSIX `exec` family; `gf` specifies no other replacement mechanism.

The `bin/gf` development launcher re-execs `.venv/bin/python` when that file exists and is not already the current interpreter. A virtual environment belongs to one host. A `.venv` left over from another host is a dirty tree: remove it and run `uv sync` on this host. `gf` does not probe interpreter ABI or fall back to the current interpreter to paper over that case. The installed console script (`gf` from `pyproject.toml`) is unaffected: it already runs under the interpreter that installed it. When `.venv` is absent, `bin/gf` continues under the current interpreter with `src/` on `sys.path`.

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

### Subfolder-binding layout

```text
parent/
├── .git
├── gf.toml                  # tracked canonical manifest
├── .gf/                     # parent-root store directory for subfolder bindings
│   ├── repos/<repo-key>/git/    # bare repo store: objects, refs, origin
│   └── wt/<repo-key>/<checkout-key>/   # sparse linked worktree of the store
└── <consumer path>          # relative symlink → .gf/wt/<repo-key>/<checkout-key>/<subdir>
```

The repo store is created with `git init --bare` and carries `remote.origin.url` and its fetch refspec lines (see [Fetch refspecs](#fetch-refspecs)); fetches use `--filter=blob:none` so objects arrive once per repository. Each checkout is a linked worktree (`git worktree add --no-checkout --detach`), sparse-checked-out in cone mode to the union of its bindings' subdirs. After creation `gf` removes the checkout's `.git` gitfile and runs `git worktree lock`, so plain `git` and `.git`-scanning tools inside a consumer link resolve to the parent repository and `git worktree prune` leaves the checkout alone. `gf` addresses the checkout only through `GIT_DIR`/`GIT_WORK_TREE`.

A whole-repo binding and subfolder bindings of the same repository do not share object storage.

#### Fetch refspecs

A repo store created without `--single-branch` gets the wildcard refspec `+refs/heads/*:refs/remotes/origin/*`. A store created with `--single-branch` gets only `+refs/heads/<branch>:refs/remotes/origin/<branch>` for the resolved branch. When a later binding needs a branch that no existing line covers, `gf` appends that branch's line with `git config --add remote.origin.fetch`. `gf` never removes or rewrites an existing refspec line, so every checkout of the store keeps fetching what it needs.

#### Checkout integrity

When `gf` creates a checkout it verifies that git placed the worktree record at `<store>/worktrees/<checkout-key>`, and fails without creating the consumer link if git chose another name. Every checkout that `gf pull` touches is locked with `git worktree lock`; an already-locked checkout is left as is. A checkout directory whose worktree record is missing — for example after `git worktree unlock` followed by `git worktree prune` — is reported as an error with recovery steps. `gf` never rebuilds a checkout over existing files.

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

### URL resolution

A `url` is a plain path: to a repository, or to a folder inside one. There is no separator syntax; `https://github.com/org/repo/docs/api`, `git@github.com:org/repo/docs/api`, and `../other/docs/api` are all valid. `gf` finds where the repository ends:

1. **Local path.** Walk up from the path to the nearest directory that is a repository (a `.git` entry, a bare repository, or a `gf` child with `.gf/git`). That directory is the repo URL; the rest is the subdir.
1. **Remote URL with a segment ending in `.git`.** The repository ends after the first such segment, e.g. `https://host/org/repo.git/docs/api`. No network is used.
1. **Other remote URL.** Probe with `git ls-remote`: first the full URL, then each shorter prefix, one path segment at a time. The longest prefix that answers is the repo URL; the rest is the subdir. Probes run with terminal prompts disabled (`GIT_TERMINAL_PROMPT=0`), so a wrong prefix never stops to ask for a password; credential helpers and SSH keys still apply.

If the full `url` is itself the repository, the binding is a whole-repo binding and behaves exactly as before. If no prefix resolves — for example a private remote that needs an interactive login — `gf` stops with an error that tells the user to mark the boundary by writing `.git` after the repository name.

Resolution runs only in `gf clone` and in `gf pull` for a binding that has no recorded resolution or whose effective `url` changed since it was recorded. The result is recorded in the per-checkout state. `gf status`, `gf ls`, and the passthrough commands never resolve a URL. A whole-repo child that already exists is never re-resolved.

The subdir is a nonempty repository-relative path in POSIX form; `.`, `..`, and empty segments are rejected. An override may change `url` or `ref`; a changed `url` is re-resolved on the next `gf pull`, and a changed repo URL selects a different repo store.

## Reference model

A binding `ref` may be one of:

- `commit` — a pinned SHA.
- `tag` — a pinned tag, resolved to its SHA.
- `branch` — a floating reference that resolves to the remote branch tip.
- `latest` — a floating reference that resolves to the remote default branch tip.

`gf` resolves a ref to a concrete SHA before checkout. For floating refs the resolved SHA is recorded in the child state so drift can be reported on subsequent runs.

For subfolder bindings, the ref determines the checkout key:

- checkout key — `latest`/`branch` checkouts: `urllib.parse.quote(resolved branch, safe='')` (one path component by construction); `pinned` checkouts: `'ref=' + quote(ref, safe='')` — `=` never appears in a percent-encoded branch key, so no branch name collides with a pinned key; pinned names previously colliding under the old `ref-<slug>` scheme (e.g. `ref-v0.1` vs `v0.1` pins) are distinguished and pinned branch-linking regression tests are added (`gf init --ref dev` keeps its checkout on `dev` after a later `gf clone --latest`)
- `branch` and `latest` checkouts are attached to their local branch; `tag` and `commit` checkouts are detached.

Bindings that share a repo URL and resolve to the same checkout key share one checkout, and its sparse cone is the union of their subdirs. A second checkout of one branch in one store is never created; `gf` widens the existing checkout instead.

## `.gf` directory

`.gf` is the analog of `.git` for a git-folder worktree, and it exists at two places.

At a whole-repo child root, `child/.gf/` contains at minimum:

- `git/` — the gitdir for this child.
- `state` — git-folders-specific metadata such as the last resolved ref and whether a local override is active.

The child gitdir is a full gitdir with its own `HEAD`, `index`, `refs`, `config`, and `objects` so the child can be on its own branch or commit.

At the parent root, `<root>/.gf/` hosts subfolder-binding storage:

- `repos/<repo-key>/git/` — the bare repo store for one repo URL, where `<repo-key>` is `<basename>-<first 8 hex of sha1(normalized repo URL)>`.
- `wt/<repo-key>/<checkout-key>/` — the sparse linked worktrees of that store.
- `wt/<repo-key>/<checkout-key>.state` — per-checkout metadata: the resolved SHA, requested ref, repo URL, the bindings served with each binding's effective `url` and subdir (the recorded URL resolution), and whether a local override is active.

An existing subfolder binding's checkout is resolved from the realpath of its consumer link, walking up to the `<root>/.gf/wt/<repo-key>/<checkout-key>` ancestor. This needs no network and no state lookup, and it follows `gf worktree add` symlinks into the source parent's store.

Because `.gf` is a directory in the child worktree, git would normally report it as untracked. `gf clone`/`init` seeds `.gf/git/info/exclude` with the pattern `.gf/` so the child ignores it.

## Parent and child gitignore handling

The parent repo must not track git-folder files. `gf` does not edit the parent `.gitignore`. When a child is created, `gf` prints a one-line recommendation such as `add "vendor/lib/" to .gitignore` so the user can decide how to keep git-folder files out of the parent history.

The child repo must ignore its own `.gf/` directory. `gf clone`/`init` adds `.gf/` to `.gf/git/info/exclude`.

For a subfolder binding, the consumer path is a symlink, and a trailing-slash pattern such as `vendor/api/` does not match a symlink. `gf` therefore recommends `add "vendor/api" to .gitignore`, without the trailing slash. When the first subfolder binding creates the parent-root store, `gf` also prints `add ".gf/" to .gitignore` once. `gf` still does not edit `.gitignore`.

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

- `<url>` is the repository to clone (remote, local, or bare host/path like `github.com/cursor/plugins`, which is expanded to `https://...`). A path to a folder inside a repository creates a subfolder binding; `gf` finds the repository boundary by [URL resolution](#url-resolution).
- `<path>` is the optional consumer path. If omitted, it defaults to the last path component of `<url>`: the repository name, or the folder name for a subfolder URL.
- The git-folder `name` is derived from the basename of `<path>` if given, otherwise from the basename of `<url>` (`.git` stripped). Use `-n <name>` to override it.
- `-b <ref>` sets the branch, tag, or commit to check out, like `git clone -b`. If omitted, it defaults to `latest`.
- `--depth <n>` is passed through to the child fetch as `git fetch --depth=<n> origin`, creating a shallow clone. It only affects the initial fetch; subsequent `gf pull` operations fetch normally. For a subfolder binding it applies only when this clone creates the repo store; bindings joining an existing store ignore it.
- `--single-branch` narrows `remote.origin.fetch` to the resolved branch after the initial fetch, so subsequent fetches only fetch that branch's history. With `latest`, the remote default branch is used. It is ignored for tag/commit refs (which fetch the single ref needed for the checkout). For a subfolder binding it narrows only a repo store that this clone creates; joining an existing store follows [Fetch refspecs](#fetch-refspecs).
- If the path is already a git-folder (a whole-repo child contains `<dir>/.gf/git/`; a subfolder binding path is a consumer link into `<root>/.gf`), the command succeeds after adding the manifest entry; it does not re-clone or overwrite the existing worktree.
- Fail if the path exists, is not empty, and is not a git-folder or a consumer link.
- For a whole-repo URL, create `<dir>/.gf/git/` as a full self-contained gitdir, add `origin` pointing to the actual effective URL, and `git fetch origin`.
- For a subfolder URL, create or reuse the repo store for the repo URL (`git init --bare`, `remote.origin.url`, fetch refspec lines per [Fetch refspecs](#fetch-refspecs), `git fetch --filter=blob:none origin`; if the server refuses filtered fetch, fall back to a full fetch with a warning), ensure the checkout for the resolved key (`git worktree add --no-checkout --detach`, `sparse-checkout set --cone` with the union of its bindings' subdirs, `checkout -B` for a branch or detached checkout for a tag/commit, remove the `.git` gitfile, `git worktree lock`), and create the relative consumer link at `<path>` targeting `<checkout>/<subdir>`. Joining an existing checkout re-links only: it never checks anything out, so a binding removed with `gf rm` and added back finds its uncommitted work in place.
- If any step from `git init` through `git checkout` fails, remove the child directory (if it did not exist before the command) or remove the `.gf/` directory (if the child existed but was not yet a git-folder), so no partial state is left behind. For a subfolder binding, remove the consumer link and any checkout this command created; a repo store or checkout that already served other bindings is retained.
- Resolve the effective ref in the child gitdir or repo store.
- If the effective ref is a branch (or `latest` resolves to the remote default branch), create a local tracking branch and check it out with `git checkout -B <branch> origin/<branch>`.
- If the effective ref is a tag or commit, check it out in a detached `HEAD`.
- Long-running git operations (`fetch`, `checkout`) stream stdout and stderr so progress is visible.
- Add `.gf/` to `.gf/git/info/exclude` for a whole-repo child.
- Print `add "<dir>/" to .gitignore` as a recommendation; do not modify the parent `.gitignore`. For a subfolder binding, print `add "<path>" to .gitignore` without the trailing slash, and also `add ".gf/" to .gitignore` when this clone first creates the parent-root store directory.

### `gf pull [--rebase] [--force] [--autostash] [<path>...]`

Update or initialize selected children to their effective refs.

- With no arguments:
  - If `cwd` is inside a child, update that child.
  - Otherwise, update all children whose paths are at or below `cwd`.
- With path arguments, each argument selects the git-folder whose consumer path matches or contains the argument.
- A binding with no recorded URL resolution, or whose effective `url` changed, is resolved first ([URL resolution](#url-resolution)); local and relative paths resolve against the parent repo root, and a local `gf` child resolves to its inner gitdir.
- Selected subfolder bindings are grouped and applied per the update algorithm below: one fetch per repo store, one dirty check and one checkout per shared checkout, sparse-cone union when a binding joins a checkout, and consumer links created or retargeted when an override changes the effective repo URL, subdir, or checkout key.
- A shared checkout serves every binding linked into it, so updating it moves all of them. `gf` prints one `Pulled <name>` line per binding the updated checkout serves and marks a binding that was not selected as `(moved with <selected name>)`. `--autostash` and `--force` act on the whole checkout, and the output lists every binding whose files they affected.
- A binding created by `gf init` whose `url` resolves to a subfolder is converted on its first pull: if the child directory has no commits and no files other than `.gf`, `gf` removes it and creates the consumer link; otherwise it stops with an error and deletes nothing.
- For each selected whole-repo child:
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

Unregister a git-folder from the manifest. A whole-repo child is converted back to a normal git repo; a subfolder binding's consumer link is removed.

- With no arguments:
  - If `cwd` is inside a child, remove that child.
  - Otherwise, error; explicit path or `--all` is required to avoid accidental bulk removal.
- With `--all`:
  - No positional paths may be given.
  - `cwd` (or the directory set by the global `-C`) must resolve to the parent repo root; otherwise `gf rm --all` errors to avoid accidental mass removal from the wrong directory.
  - Selects every git-folder in the manifest.
- With explicit path arguments, each argument selects the git-folder whose consumer path matches or contains the argument.
- Compute the updated `gf.toml` and write it to a temporary file in the parent repo.
- For a whole-repo binding, move `child/.gf/git` to `child/.git` so the user's worktree is preserved.
- For a subfolder binding, remove only the consumer link and the manifest entry. The checkout — including uncommitted work — its sparse cone, and the repo store are not touched. The folder therefore disappears from the consumer path. Adding the same `url` back with `gf clone` re-links to the same checkout and restores the folder with its uncommitted work. Removing checkouts that no binding links to is out of scope.
- A subfolder binding can be removed only from the parent root that owns its checkout (see [Target selection rules](#target-selection-rules)).
- Atomically replace `gf.toml` with the temporary file only after all selected children are converted or unlinked.
- A whole-repo child directory and its contents remain.

### `gf status [<path>...] [--remote]`

Show the git porcelain status for selected children. By default this command does not access the network.

- With no arguments:
  - If `cwd` is inside a child, show that child.
  - Otherwise, show all children whose paths are at or below `cwd`.
- With path arguments, each argument selects the git-folder whose consumer path matches or contains the argument.
- Prints one header line per child: `name url [branch]`. For a subfolder binding, `url` is the effective manifest URL (the full path to the folder), and `branch` is the branch of the shared checkout serving the binding.
- `branch` is the current local branch, or empty brackets for a detached `HEAD`.
- Followed by `git status --porcelain` output for any dirty files in that child. For a subfolder binding, the porcelain run is scoped to the binding's subdir (`status --porcelain -- <subdir>` against the checkout), so only paths inside the mapping are reported.
- With `--remote`, append the drift state to the header line: `name url [branch] state`. `state` is one of `clean`, `behind`, `local-dirty`, `both`, or `missing` per the drift algorithm below. `--remote` does **not** access the network; drift is computed from the local remote-tracking refs already present in the child gitdir or repo store (the same refs `git status` compares against after a `git fetch`). Run `gf pull` or `gf git fetch` first to refresh those refs. The non-`--remote` output format is unchanged when `--remote` is not given. If the effective ref cannot be resolved locally, `gf status --remote` exits with a deterministic non-zero code instead of printing a drift state.

### `gf ls [<path>...]`

Quick list of git-folders. This command does not access the network.

- With no arguments, lists **all** git-folders in the manifest, even when run from inside a child. `ls` is intentionally global; it does not follow the child-context rule used by `status`, `pull`, and `rm`.
- With path arguments, each argument selects the git-folder whose consumer path matches or contains the argument.
- One line per child: `name url [branch] short-hash`. For a subfolder binding, `url` is the full path to the folder and `branch`/`short-hash` describe the shared checkout serving it.
- Columns are aligned using consistent spacing.
- Appends a `*` to the hash when the child has uncommitted worktree changes. For a subfolder binding, only changes inside its subdir mark it dirty.
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
- `init` never resolves the URL and has no special case for subfolder URLs. The binding form is decided on the first `gf pull`, which converts the child to a consumer link when the URL resolves to a subfolder (see `gf pull`).
- Refuse to initialize a directory that already contains a `.git` or `.gf` directory.
- Print `add "<dir>/" to .gitignore` as a recommendation; do not modify the parent `.gitignore`.

### `gf sh [command...]`

Run a shell or a single command inside a child with the correct git environment.

- The child is selected by the global `-C` option or by the current working directory.
- Only works in directories that contain `.gf/git/` or resolve to a consumer link; it does not fall back to the parent `.git`.
- `GIT_DIR` points to the child's gitdir (`<child>/.gf/git` for a whole-repo binding; `<store>/worktrees/<checkout-key>` for a subfolder binding, with `GIT_WORK_TREE` at the checkout) and `GIT_WORK_TREE` points to the child's working root.
- For a subfolder binding, the process working directory is the physical mapped subdirectory inside the checkout, so relative path arguments behave naturally; the command is a pure passthrough.
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
- For a subfolder binding, the command runs with cwd at the physical mapped subdirectory of the shared checkout and no pathspec is added; `gf git log` therefore shows whole-repository history. This is a pure passthrough.
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
- For a subfolder binding, `gf` appends `-- .` to the arguments when the user passes no pathspec (no `--` among the trailing args), so the default diff is scoped to the mapping. A user-supplied `--` passes through untouched.
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
- For a subfolder binding, `gf` appends `-- .` to the arguments when the user passes no pathspec (no `--` among the trailing args), so the default log follows the mapping; passing an explicit `--` or any pathspec disables the scope. Use `gf git log` for unscoped repository history.
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
- The symlink points to the corresponding consumer child in the source worktree. For a subfolder binding, the link targets the source worktree's consumer link (not the resolved checkout), so a later retarget in the source worktree propagates.
- The new worktree shares the git-folder's working tree; uncommitted changes are visible in both worktrees.
- `gf` commands run inside a symlinked child resolve to the source child or checkout and operate on it.
- `gf rm` cannot be run from a symlinked child; remove the git-folder from the owning worktree. For a subfolder binding, the owning worktree is the parent root whose `.gf/wt` contains the link's realpath.

Example:

```bash
gf worktree add ../feature -b feature
gf worktree add /tmp/hotfix v1.2.0
```

### `gf worktree list [--porcelain] [--verbose]`

List the parent repo's git worktrees and annotate each with the git-folders linked into it.

- Runs `git worktree list --porcelain` in the parent repo to enumerate worktrees.
- For each worktree, walks the manifest child paths inside the worktree and reports the git-folders that are linked into it (a relative symlink to a git-folder child, or to a subfolder binding's consumer link, outside the worktree).
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

- Before calling `git worktree remove`, walk the manifest child paths inside the target worktree. For each child path that is a relative symlink to a git-folder child, or to a subfolder binding's consumer link, outside the worktree, `os.unlink` the symlink so the source is not touched by `git worktree remove`.
- Then call `git worktree remove [--force] <path>` in the parent repo.
- `--force` is forwarded to `git worktree remove`.
- The source git-folder children, repo stores, and checkouts, and their `.gf` metadata, are preserved. `gf`-locked checkouts under `.gf/wt` are unaffected by `git worktree prune`.
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

For subfolder bindings, "inside a child" is decided by realpath matching: a cwd or path argument inside a consumer link (or inside the physical `.gf/wt` checkout it resolves to) selects that binding. When cwd resolves inside more than one binding — for example nested `docs` and `docs/api` bindings sharing one checkout — no-argument selection chooses the innermost binding. A physical path under `<root>/.gf` still resolves through the consumer link's realpath, so operating from a `cd -P` physical cwd selects the owning binding rather than nothing. A consumer link is owned by the parent root whose `.gf/wt` contains its realpath; in any other parent worktree it is a linked child.

`rm` is the exception to the "all below cwd" default: no args in a non-child context is an error.

## Update algorithm

For a single whole-repo child during `gf pull`:

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

For selected subfolder bindings during `gf pull`, the work is grouped so shared stores and checkouts are touched once:

1. Take each binding's repo URL and subdir from its recorded URL resolution, or resolve the effective `url` when there is no record or the `url` changed ([URL resolution](#url-resolution)). Resolve the effective ref and its checkout key. An override that changes the repo URL moves the binding to another store; an override that changes the effective checkout key retargets the consumer link.
1. Group the bindings by repo store; run exactly one `git fetch --filter=blob:none origin` per store (falling back to a full fetch with a warning when the server refuses filters). A store that needs creating gets `git init --bare`, `remote.origin.url`, and its first refspec line first; a binding whose branch no line covers appends one ([Fetch refspecs](#fetch-refspecs)).
1. Group the bindings by checkout key within each store. For each checkout:
   - Ensure the checkout exists (`git worktree add --no-checkout --detach`, then verify the worktree record per [Checkout integrity](#checkout-integrity)), widening its sparse cone (`sparse-checkout set --cone <union of subdirs>`) when a new binding joins it. A checkout directory without its worktree record stops this checkout with an error.
   - Run one dirty check (`status --porcelain` over the union cone). If dirty, apply the same `--force`/`--autostash` rules as a whole-repo child to the whole checkout before continuing; without them, abort.
   - Apply the ref once: `checkout -B <branch> origin/<branch>` for branch/`latest`, `checkout <sha>` for tag/commit, `--rebase` honored for branch refs.
   - Remove the `.git` gitfile after creation, and lock the checkout with `git worktree lock` if it is not locked.
1. Create or retarget each binding's consumer link to `<checkout>/<subdir>`, record per-checkout state at `<root>/.gf/wt/<repo-key>/<checkout-key>.state`, and print one line per binding the checkout serves, as described under `gf pull`.

## Drift algorithm

For a single child during `gf status --remote`:

1. `gf status --remote` is local-only. It does **not** call `git fetch`, `git remote`, `git ls-remote`, or any other network command. Drift is computed from the local refs already present in the child gitdir, exactly like `git status` compares `refs/heads/<branch>` to `refs/remotes/origin/<branch>` after a `git fetch`. Run `gf pull` or `gf git fetch` to refresh the remote-tracking refs first.
1. If the child is missing (no `child/.gf/git/HEAD`), the state is `missing`.
1. Read the effective ref from `gf.toml` and `gf.local.toml`.
1. Resolve the effective ref to a SHA in the child gitdir or repo store using only local refs:
   - For `branch` refs: `refs/remotes/origin/<branch>`.
   - For `latest`: `refs/remotes/origin/HEAD` if it is a valid symbolic ref, otherwise `refs/remotes/origin/main`, otherwise `refs/remotes/origin/master`. No `git remote set-head` or other network call is made.
   - For `tag` or `commit`: the peeled SHA from local refs.
   - If the ref cannot be resolved locally, raise a clear error (do not report `clean`); `gf status --remote` exits with a deterministic non-zero code.
1. Read the child `HEAD` SHA.
1. Run `git status --porcelain` in the child. For a subfolder binding, the run is scoped to the binding's subdir (`status --porcelain -- <subdir>` against its checkout) and remains local-only.
1. Classify:
   - `clean`: child `HEAD` matches the resolved SHA and the worktree is clean.
   - `behind`: child `HEAD` does not match the resolved SHA and the worktree is clean.
   - `local-dirty`: child `HEAD` matches the resolved SHA and the worktree has modifications.
   - `both`: child `HEAD` does not match the resolved SHA and the worktree has modifications.
   - `missing`: the child has no `.gf/git/HEAD`. For a subfolder binding, `missing` means the consumer link or its checkout is absent, or the binding has no recorded URL resolution yet. Drift never resolves a URL.

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

- Unit tests for manifest parsing, URL resolution (local walk-up, `.git` boundary, longest-prefix probe, unresolvable URL), repo/checkout key derivation, ref resolution, path selection, and effective URL/ref merging.
- Integration tests with local bare repositories and multiple parent `git worktree` checkouts.
- End-to-end tests:
  - Fresh parent clone; `gf clone` a git-folder at a custom path; verify `child/.gf/git/config` has a single `origin` remote pointing at the actual URL; verify no `.git` in the child.
  - Edit a git-folder file; `gf status` shows `local-dirty`; `gf sh -c "git diff"` works.
  - Add a parent `git worktree`; `gf pull` in the new worktree; verify the new worktree uses a symlink to the source child and has an independent `HEAD`.
  - Switch a git-folder to a fork via `gf.local.toml`; `gf pull` updates without changing tracked parent files.
  - Clone two subfolders of one repository: one repo store and one checkout exist, the consumer links list only mapped files, and plain `git` from a link resolves to the parent.
  - `gf pull` over several bindings of one repository performs one fetch per store; scoped `status` reports only each binding's subdir; `gf rm` leaves the checkout, its uncommitted work, and the store untouched, and adding the binding back restores the folder.
  - `gf init` then `gf pull` with a subfolder URL converts an empty child to a consumer link and refuses a child that gained files.

## Boundaries

- Windows is not a supported host. `gf` specifies no behavior for Windows process creation, for `.venv/Scripts/python.exe`, or for any other Windows-only mechanism.
- Subfolder bindings require git 2.35 or newer: the mechanism uses `git worktree add --no-checkout`, `git worktree lock --reason`, cone-mode `git sparse-checkout` under `extensions.worktreeConfig`, `git fetch --filter`, and `git ls-remote` for URL resolution. Whole-repo bindings carry no new minimum beyond what they already require.
- The platform surface covers path identity and process replacement only. It does not invoke `git`, read the manifest, or interpret the `.gf` layout; the git backend remains the only caller of `git` and the manifest layer the only reader of `gf.toml`.
- Host support is gained by removing host assumptions, not by adding host-specific command behavior. There is no second git integration and no host-conditioned clone, pull, status, ls, rm, init, or worktree algorithm.
- A virtual environment belongs to one host. `gf` specifies no recovery for a `.venv` shared between hosts with different ABIs; that tree is cleaned and recreated on the host that will run it.