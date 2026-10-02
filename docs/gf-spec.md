---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-spec]; otherwise, do not modify."
---

# git-folders Specification

## Purpose

This document specifies the `gf` command-line tool and the `git-folders` manifest and child-state model.

## Overview

`git-folders` is a git-folder repository manager for git. A parent git repository declares git-folders in a manifest (`gf.toml`). Each git-folder is cloned into a user-defined child directory inside the parent workspace. The child is a real git worktree with its own history; its git metadata lives in `.gf/` instead of `.git/`, so agentic tools that scan for `.git` do not treat the child as a nested repository. The child gitdir at `child/.gf/git` is a full, self-contained gitdir that uses the actual git-folder URL as its `origin`.

A binding has one of two forms. A **whole-repo binding** maps a complete repository to a consumer path, as described above. A **subfolder binding** maps one subdirectory of a repository to a consumer path: the `url` field is a plain path to a folder inside a repository, and `gf` finds where the repository ends (see [URL resolution](#url-resolution)). Several subfolder bindings of one repository share a single bare repo store and, when their refs resolve to the same branch, a single sparse linked worktree under `<root>/.gf`; a relative symlink at the consumer path exposes only the mapped subdirectory. Both forms are first-class: every command in this specification applies to both.

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

The repo store is created with `git init --bare` and carries `remote.origin.url` and its fetch refspec lines (see [Fetch refspecs](#fetch-refspecs)); fetches use `--filter=blob:none` so objects arrive once per repository. Each checkout is a linked worktree (`git worktree add --no-checkout --detach`), sparse-checked-out in cone mode to the union of its bindings' subdirs. After creation `gf` removes the checkout's `.git` gitfile and runs `git worktree lock`, so plain `git` and `.git`-scanning tools inside a consumer link resolve to the parent repository and `git worktree prune` leaves the checkout alone. `gf` addresses the checkout only through `GIT_DIR`/`GIT_WORK_TREE`, and every `git` process it spawns — backend calls and the `gf sh`/`gf git`/`gf diff`/`gf log` passthroughs alike — runs on an environment scrubbed of ambient `GIT_*` variables — the blocklist enumerated under `gf sh`, config sources and command-path variables included — before `gf`'s own overlays are applied.

Through each consumer link the user sees exactly the contents of the mapped folder. A checkout contains every folder its bindings map, in full, and no other folder trees; in particular it never contains a same-named folder at another depth, such as `lib/src` when `src` is mapped or `vendor/docs/api` when `docs/api` is mapped. Cone-mode sparse checkout also materializes, by design, the files at the repository root and the files directly inside each parent folder of a mapped folder, such as `docs/top.md` when `docs/api` is mapped. A checkout may hold these files; one that lies outside every mapped folder is never visible through a consumer link.

A whole-repo binding and subfolder bindings of the same repository do not share object storage.

#### Fetch refspecs

A repo store created without `--single-branch` gets the wildcard refspec `+refs/heads/*:refs/remotes/origin/*`. A store created with `--single-branch` gets only `+refs/heads/<branch>:refs/remotes/origin/<branch>` for the resolved branch. When a later binding needs a branch that no existing line covers, `gf` appends that branch's line with `git config --add remote.origin.fetch`. A needed tag that no line covers gets the same treatment — `gf` probes upstream with `git ls-remote` and appends `+refs/tags/<ref>:refs/tags/<ref>` so the same fetch lands it — while a needed commit sha, which no refspec line can name, is fetched directly with `git fetch origin <sha>` (the probe covers a whole-repo child's `remote.origin.fetch` the same way). The same coverage runs for the fetch that creates the store — `gf clone`'s create arm and `gf pull`'s store creation alike resolve the binding's effective ref first and feed a non-branch ref into the create step, so a pinned ref the fresh wildcard or `--depth` window would miss is covered before that initial fetch: a needed tag's line lands in the one fetch and a needed sha is fetched directly (`git fetch origin <sha>` is sent without `--depth`; fetching an arbitrary sha requires the server to allow it, e.g. `allow-reachable-sha1-in-want`). `gf` never removes or rewrites an existing refspec line, so every checkout of the store keeps fetching what it needs.

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

Both files are validated on read. `git_folder` and `git_folder_override` must be lists of tables: a `git_folder` entry carries string `name`, `url`, `ref`, and `path` fields; a `git_folder_override` entry's fields are strings where present. A `path` must be a non-empty relative path; normalized, it must not be `.` or `..`, must not begin with `..`, and must contain no `.gf` or `.git` segment. A `gf.toml` that fails validation is refused on read — every `gf` command fails with a clean error naming it, `gf rm` included, so unregistering around a bad manifest is not a workaround; fix or restore the file. A bad `gf.local.toml` fails the commands that read it (`pull`, `status`) the same way. A missing `gf.toml` is not an error: `gf clone`/`gf init` create it and the other commands treat it as an empty manifest.
Every write to `gf.toml` is atomic — the new content goes to a temporary file in the parent repo and replaces the manifest by rename, so a crash leaves the old or the new file, never a torn one. The rename replaces whatever sits at `gf.toml`: a manifest that is a symlink is swapped for the regular file rather than written through.

### URL resolution

A `url` is a plain path: to a repository, or to a folder inside one. There is no separator syntax; `https://github.com/org/repo/docs/api`, `git@github.com:org/repo/docs/api`, and `../other/docs/api` are all valid. `gf` finds where the repository ends:

1. **Local path.** Walk up from the path to the nearest directory that is a repository (a `.git` entry, a bare repository, or a `gf` child with `.gf/git`). That directory is the repo URL; the rest is the subdir. A local path that resolves inside a `.gf` directory — a repo store or hidden checkout, `gf`'s private storage layout — has no repository boundary above it: the walk never treats `.gf`-interior directories as repositories and never climbs out of `.gf`, so unless the path itself is a repository boundary outside `.gf/wt` — `.gf/repos/<repo-key>/git` or a child's `.gf/git` still resolve to themselves, while nothing under `.gf/wt` resolves — resolution fails with a clear error naming the private layout. Remote URLs containing `.gf/` are unaffected — they resolve against the remote tree, not the local filesystem.
1. **Remote URL with a segment ending in `.git`.** The repository ends after the first such segment, e.g. `https://host/org/repo.git/docs/api`. No network is used.
1. **Other remote URL.** Probe with `git ls-remote`: first the full URL, then each shorter prefix, one path segment at a time. The longest prefix that answers is the repo URL; the rest is the subdir. Probes run with terminal prompts disabled (`GIT_TERMINAL_PROMPT=0`), so a wrong prefix never stops to ask for a password; credential helpers and SSH keys still apply.

If the full `url` is itself the repository, the binding is a whole-repo binding. If no remote prefix resolves — for example a private remote that needs an interactive login — `gf` stops with an error that tells the user to mark the boundary by writing `.git` after the repository name. The `.git` hint applies to the remote-probe class; an unresolvable local path reports its own error without it.

Resolution runs only in `gf clone` and in `gf pull` for a binding that has no recorded resolution or whose effective `url` changed since it was recorded. The result is recorded in the per-checkout state. `gf status`, `gf ls`, and the passthrough commands never resolve a URL. A whole-repo child that already exists is never re-resolved.

The subdir is a nonempty repository-relative path in POSIX form; `.`, `..`, and empty segments are rejected. An override may change `url` or `ref`; a changed `url` is re-resolved on the next `gf pull`, and a changed repo URL selects a different repo store. A changed `url` that makes a subfolder binding resolve to a whole repository is refused (see [`gf pull`](#gf-pull---rebase---force---autostash-path)).

## Reference model

A binding `ref` may be one of:

- `commit` — a pinned SHA.
- `tag` — a pinned tag, resolved to its SHA.
- `branch` — a floating reference that resolves to the remote branch tip.
- `latest` — a floating reference that resolves to the remote default branch tip.

`gf` resolves a ref to a concrete SHA before checkout. For floating refs the resolved SHA is recorded in the child state so drift can be reported on subsequent runs.

For subfolder bindings, the ref determines the checkout key:

- checkout key — `latest`/`branch` checkouts: `urllib.parse.quote(resolved branch, safe='')` (one path component by construction); `pinned` checkouts: `'ref=' + quote(ref, safe='')` — `=` never appears in a percent-encoded branch key, so no branch name collides with a pinned key: a pin of `v0.1` keys as `ref=v0.1` while a branch named `ref-v0.1` keys as `ref-v0.1`. A recorded branch ref survives another binding's pin (`gf init -b dev` keeps its checkout on `dev` after a later `gf clone`, which tracks `latest` by default)
- `branch` and `latest` checkouts are attached to their local branch; `tag` and `commit` checkouts are detached.

Bindings that share a repo URL and resolve to the same checkout key share one checkout, and its sparse cone is the union of their subdirs. A second checkout of one branch in one store is never created; `gf` widens the existing checkout instead.

## `.gf` directory

`.gf` is the analog of `.git` for a git-folder worktree, and it exists at two places.

At a whole-repo child root, `child/.gf/` contains at minimum:

- `git/` — the gitdir for this child.
- `state` — git-folders-specific metadata such as the last resolved ref and whether a local override is active.

The child gitdir is a full gitdir with its own `HEAD`, `index`, `refs`, `config`, and `objects` so the child can be on its own branch or commit.

At the parent root, `<root>/.gf/` hosts subfolder-binding storage:

- `repos/<repo-key>/git/` — the bare repo store for one repo URL, where `<repo-key>` is `<basename>-<first 8 hex of sha1(normalized repo URL)>`. Remote spellings still alias a trailing `.git` — `https://host/repo` and `https://host/repo.git` share a key — but a local filesystem path keeps its literal spelling: `/path/repo` and `/path/repo.git` are different repositories with different keys, since on disk they name different directories.
- `wt/<repo-key>/<checkout-key>/` — the sparse linked worktrees of that store.
- `wt/<repo-key>/.<checkout-key>.state` — per-checkout metadata: the resolved SHA, requested ref, repo URL, the bindings served with each binding's effective `url` and subdir (the recorded URL resolution), and whether a local override is active.

An existing subfolder binding's checkout is resolved from the realpath of its consumer link, walking up to the `<root>/.gf/wt/<repo-key>/<checkout-key>` ancestor. This needs no network and no state lookup, and it follows `gf worktree add` symlinks into the source parent's store.

Every `.gf` storage path `gf` populates must resolve to itself — the `.gf`/`repos`/`wt`/`git` components appended below the parent root's realpath (or a whole-repo child's realpath) may not be links, though the anchor leaf itself may legitimately be one. `.gf` is committable content (the `.gitignore` recommendation is advice, not enforcement), so a `parent/.gf` symlink planted in the tree — or a `.gf` link inside checked-out child content — would otherwise redirect repo-store, checkout, and gitdir writes outside the workspace; the write site refuses such a path, and teardown of a store or checkout `gf` itself created skips the recursive remove rather than deleting through the link. Because `.gf` is committable, a bound upstream's own tree is hostile surface too: a `.gf` entry at the root of an upstream's tree — blob, tree, symlink, or gitlink alike — is refused before the tree materializes, since a checked-out `.gf` would occupy the path `gf` anchors its own metadata at; a nested `sub/.gf` entry does not collide with an anchor and is unaffected. Every `git` operation `gf` itself runs against gf-managed gitdirs — the child `.gf/git`, the repo stores, and their checkout gitdirs — runs with `core.hooksPath=/dev/null`, so a hook delivered through committed `.gf` content or a rewritten store config cannot execute during `clone`, `pull`, or `rm`; the `gf sh`/`gf git`/`gf diff`/`gf log` passthroughs keep normal hook resolution — a user's own hooks still run when the user invokes `git`. An existing repo store is trusted only as far as its recorded origin: `gf pull` compares the store's `remote.origin.url` against the binding's recorded URL resolution and refuses on a mismatch rather than fetching from a rewritten origin.

Because `.gf` is a directory in the child worktree, git would normally report it as untracked. `gf clone`/`init` seeds `.gf/git/info/exclude` with the pattern `.gf/` so the child ignores it.

## Parent and child gitignore handling

The parent repo must not track git-folder files. `gf` does not edit the parent `.gitignore`. When a child is created, `gf` prints a one-line recommendation such as `add "vendor/lib/" to .gitignore` so the user can decide how to keep git-folder files out of the parent history.

The child repo must ignore its own `.gf/` directory. `gf clone`/`init` adds `.gf/` to `.gf/git/info/exclude`.

For a subfolder binding, the consumer path is a symlink, and a trailing-slash pattern such as `vendor/api/` does not match a symlink. `gf` therefore recommends `add "vendor/api" to .gitignore`, without the trailing slash. When the first subfolder binding creates the parent-root store, `gf` also prints `add ".gf/" to .gitignore` once. `gf` still does not edit `.gitignore`.

## Public Surface

### Command reference

All commands support a global `-C <path>` option, just like `git -C`. It changes to `<path>` before resolving context and interpreting path arguments. An unusable `<path>` — for example a dangling consumer link — fails deterministically: the command prints `gf: cannot change to '<path>': <reason>` and exits `1`, naming the operand path and the failed operation.

`gf --version` (or `gf -v`) prints the `git-folders` package version (sourced from `pyproject.toml` via `importlib.metadata`, with a `pyproject.toml` fallback when running from source) and exits 0. It is handled before subcommand dispatch, so it works without a parent repo.

#### Global `-C` vs. `git -C` ambiguity

Because `gf` forwards trailing arguments to `git` for the `diff`, `log`, and `git` passthroughs, the global `-C <path>` option and `git`'s own `-C` can appear in similar positions. The rule is: a `-C` that appears **before** the subcommand is the global `gf -C` (it changes `gf`'s working directory); a `-C` that appears **after** the subcommand is passed through to `git`. For example:

- `gf -C vendor/libfoo diff` — `gf` changes to `vendor/libfoo`, then runs `git diff` in that child.
- `gf diff -C vendor/libfoo` — `gf` runs `git diff -C vendor/libfoo` in the current child, forwarding `-C` to `git diff`.

### `gf clone <url> [<path>] [-n <name>] [-b <ref>] [--depth <n>] [--single-branch]`

Add a git-folder to `gf.toml` and create its child worktree. This is the "add" operation: it is non-destructive.

- `<url>` is the repository to clone (remote, local, or bare host/path like `github.com/cursor/plugins`, which is expanded to `https://...`). Bare host/path expansion applies only when neither the first segment nor the spelled path exists under the anchor — a spelled path existing at the anchor is a local path even when its first segment contains a dot (the `<repo>.git/<subdir>` spelling); `./dir/...` and absolute paths were always local, and `https://`/`git@` spellings are unchanged. A path to a folder inside a repository creates a subfolder binding; `gf` finds the repository boundary by [URL resolution](#url-resolution).
- `<path>` is the optional consumer path. If omitted, it defaults to the last path component of `<url>`: the repository name, or the folder name for a subfolder URL.
- A `<path>` leaf of `.` or `..` resolves to the real directory before the containment and occupancy checks — `missing/..` names the parent repository itself and is refused as already occupied — and the recorded `path` and derived `name` use the resolved spelling.
- The git-folder `name` is derived from the basename of `<path>` if given, otherwise from the basename of `<url>` (`.git` stripped). Use `-n <name>` to override it.
- `-b <ref>` sets the branch, tag, or commit to check out, like `git clone -b`. If omitted, it defaults to `latest`.
- `--depth <n>` is passed through to the child fetch as `git fetch --depth=<n> origin`, creating a shallow clone. It only affects the initial fetch; subsequent `gf pull` operations fetch normally. For a subfolder binding it applies only when this clone creates the repo store; bindings joining an existing store ignore it.
- `--single-branch` narrows `remote.origin.fetch` to the resolved branch after the initial fetch, so subsequent fetches only fetch that branch's history. With `latest`, the remote default branch is used. It is ignored for tag/commit refs (which fetch the single ref needed for the checkout). For a subfolder binding it narrows only a repo store that this clone creates; joining an existing store follows [Fetch refspecs](#fetch-refspecs).
- If the path is already a git-folder (a whole-repo child contains `<dir>/.gf/git/`; a subfolder binding path is a consumer link into `<root>/.gf`), the command succeeds after adding the manifest entry; it does not re-clone or overwrite the existing worktree.
- Fail if the path exists, is not empty, and is not a git-folder or a consumer link. A dangling symlink at `<path>` counts as occupied — a path that exists only as a link to nowhere is refused with a clean error rather than treated as empty space.
- Fail if `<path>` lands inside the parent's `.gf` storage or any `.git` tree — spelled there physically or resolved there through a consumer link (`<consumer-link>/<dir>`) or a mid-path symlink — with a clear error; a `<path>` that is itself the consumer link still binds by the already-a-git-folder rule above. The same refusal is enforced at the child-creation layer — ahead of that layer's already-a-git-folder early return, on the spelled path — so no call path can build or re-record a whole-repo child inside `.gf` or `.git`; a subfolder binding's consumer path still legitimately resolves into `.gf/wt`.
- For a whole-repo URL, create `<dir>/.gf/git/` as a full self-contained gitdir, add `origin` pointing to the actual effective URL, and `git fetch origin`.
- For a subfolder URL, create or reuse the repo store for the repo URL (`git init --bare`, `remote.origin.url`, fetch refspec lines per [Fetch refspecs](#fetch-refspecs), `git fetch --filter=blob:none origin`; if the server refuses filtered fetch, fall back to a full fetch with a warning), ensure the checkout for the resolved key (`git worktree add --no-checkout --detach`, `sparse-checkout set --cone` with the union of its bindings' subdirs, `checkout -B` for a branch or detached checkout for a tag/commit, remove the `.git` gitfile, `git worktree lock`), and create the relative consumer link at `<path>` targeting `<checkout>/<subdir>`. A clone that joins an existing repo store fetches it first — one `git fetch --filter=blob:none origin` (or the full-fetch fallback), plus a targeted `git fetch origin <sha>` when a pinned commit is not yet in the store — before the effective ref is resolved; joining does not skip the fetch. Joining an existing checkout re-links only: it never checks anything out, so a binding removed with `gf rm` and added back finds its uncommitted work in place.
- If any step from `git init` through `git checkout` fails, remove the child directory (if it did not exist before the command) or remove the `.gf/` directory (if the child existed but was not yet a git-folder), so no partial state is left behind. For a subfolder binding, remove the consumer link, any checkout this command created, and a repo store this command itself created; a repo store or checkout that was pre-existing or already serving other bindings is always retained.
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
- A binding with no recorded URL resolution, or whose effective `url` changed, is resolved first ([URL resolution](#url-resolution)); local and relative paths resolve against the root that owns the binding's links and `.gf` storage (the root under which the binding's consumer `path` resolves to its checkout) — the source root for `worktree add` link chains — not the invoking root, and a local `gf` child resolves to its inner gitdir; `gf clone` applies the same anchor discipline to its local fetch — a relative local `url` resolves at the parent repo root receiving the manifest, and the anchored repo URL feeds the child's `origin` and fetch — while the manifest records the spelled `url`.
- Selected subfolder bindings are grouped and applied per the update algorithm below: one fetch per repo store (plus a targeted `git fetch origin <sha>` when a pinned commit is not yet in the store — [Fetch refspecs](#fetch-refspecs)), one dirty check and one checkout per shared checkout, sparse-cone union when a binding joins a checkout, and consumer links created or retargeted when an override changes the effective repo URL, subdir, or checkout key. If store creation or the initial fetch fails, `gf` removes only a store it just created — a pre-existing store, and any store already serving other bindings, is retained.
- A shared checkout serves every binding linked into it, so updating it moves all of them. `gf` prints one `Pulled <name>` line per binding the updated checkout serves and marks a binding that was not selected as `(moved with <selected name>)`. `--autostash` and `--force` act on the whole checkout, and the output lists every binding whose files they affected. The `(moved with …)` scan and the vacated-checkout record update read the manifest of the root that owns the checkout; when that manifest is missing, unreadable, or corrupt, the scan reports no siblings and the vacated record is left as is — a still-live binding is never un-materialized on a guess.
- A binding created by `gf init` whose `url` resolves to a subfolder is converted on its first pull: if the child directory has no commits and no files other than `.gf`, `gf` removes it and creates the consumer link; otherwise it stops with an error and deletes nothing.
- A subfolder binding whose effective `url` changes — for example through `gf.local.toml` — so that it resolves to a whole repository is refused. `gf pull` stops with a clear error that names the binding and says that switching a subfolder binding to a whole repository is not supported. It changes nothing: not the shared repo store, its checkout, the binding's consumer link, or the sibling bindings that share the store. `gf` does not convert an established subfolder binding into a whole-repo binding. `gf pull` checks every selected binding for this refusal before it changes anything: when any selected binding is refused, no selected binding is updated, and the command exits with the validation-error code `1`.
- A selected binding whose consumer `path` resolves outside the root that owns its links and `.gf` storage is refused — the resolved child is checked against its owning anchor during routing, before any fetch or write, and the same refusal is enforced again at the child-creation layer. The owning anchor is adopted from the consumer path's realpath only when it proves real: a `*/.gf/wt/<repo-key>/<checkout-key>`-shaped ancestor names the owning root only when the derived root lies outside every `.gf` tree and proves real — either a `.git` entry at that root (directory, gitfile, or symlink) or a `HEAD` in the matching `<root>/.gf/repos/<repo-key>/git` store; `realpath` resolves a nonexistent path lexically, so a committed mid-path symlink spelling a `.gf/wt` shape that exists nowhere on disk cannot claim the anchor — and an unproven or `.gf`-interior shape falls back to the root under which the declared `path` resolves, then the invoking root, where the refusal applies. A consumer link the pull creates or retargets is checked too: the link must sit lexically under its owning root with its parent's realpath inside the root and outside `.gf` and `.git`, so a committed or planted mid-path symlink cannot aim the link step outside the parent or into git metadata.
- For each selected whole-repo child:
  - If the effective URL is a local path that resolves to the child path itself (the placeholder written by `gf init`), the child is skipped until a real upstream is configured, even if the child does not exist yet.
  - Resolve relative local URLs against the root that owns the binding's links and `.gf` storage (the root under which the binding's consumer `path` resolves to its checkout) — the source root for `worktree add` link chains — not the invoking root.
  - If the child path does not exist or has no `.gf/git/`, create a full gitdir, set `origin` to the actual URL, `git fetch origin`, resolve the ref, and check it out.
  - If the child worktree is dirty:
    - Without `--force` or `--autostash`, abort with a clear message.
    - With `--autostash`, run `git stash push -u -m "gf autostash"` first, perform the update, then `git stash pop`. If the pop conflicts, the stash is left in place and `gf` errors clearly so the user can resolve and pop manually. The pop runs on any update failure, including a failed `origin` URL update — a stash taken before the update is never orphaned.
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
- `branch` is the current local branch, or empty brackets for a detached `HEAD` or a child with no commits yet.
- Followed by `git status --porcelain` output for any dirty files in that child. For a subfolder binding, the porcelain run is scoped to the binding's subdir (`status --porcelain -- <subdir>` against the checkout), so only paths inside the mapping are reported.
- A child with no commits yet, for example one just created by `gf init`, is a normal state: its header shows empty brackets and is followed by its porcelain lines. It does not make the command fail, and every other selected git-folder is still listed.
- With `--remote`, append the drift state to the header line: `name url [branch] state`. `state` is one of `clean`, `behind`, `local-dirty`, `both`, or `missing` per the drift algorithm below. `--remote` does **not** access the network; drift is computed from the local remote-tracking refs already present in the child gitdir or repo store (the same refs `git status` compares against after a `git fetch`). Run `gf pull` or `gf git fetch` first to refresh those refs. Without `--remote`, the output is the header and porcelain lines alone. If the effective ref cannot be resolved locally, `gf status --remote` exits with a deterministic non-zero code instead of printing a drift state. A child with no commits yet whose ref cannot be resolved locally gets this same clear unresolvable-ref error and exit code, not a raw git error.

### `gf ls [<path>...]`

Quick list of git-folders. This command does not access the network.

- With no arguments, lists **all** git-folders in the manifest, even when run from inside a child. `ls` is intentionally global; it does not follow the child-context rule used by `status`, `pull`, and `rm`.
- With path arguments, each argument selects the git-folder whose consumer path matches or contains the argument.
- One line per child: `name url [branch] short-hash`. For a subfolder binding, `url` is the full path to the folder and `branch`/`short-hash` describe the shared checkout serving it.
- Columns are aligned using consistent spacing.
- Appends a `*` to the hash when the child has uncommitted worktree changes. For a subfolder binding, only changes inside its subdir mark it dirty.
- `branch` is the current local branch, or empty brackets for a detached `HEAD` or a child with no commits yet.
- A child with no commits yet, for example one just created by `gf init`, is a normal state: it is listed with empty brackets and `?` in the short-hash column, plus the usual `*` when it has uncommitted changes. It does not make the command fail, and every other selected git-folder is still listed.

### `gf init [<path>] [-b <ref>] [-n <name>] [--url <url>]`

Create an empty local child `.gf` gitdir and add it to `gf.toml`.

- Like `git init [<directory>]`, the optional positional argument is the consumer directory.
- If `<path>` is omitted, the current directory becomes the child.
- If `<path>` is given, create the directory if it does not exist — a dangling symlink at `<path>` is refused rather than initialized through, and so is any non-directory occupant — and initialize `.gf` inside it.
- A `<path>` leaf of `.` or `..` resolves to the real directory before the refusal checks the same way, so `missing/..` names the parent repository; the recorded `path` and derived `name` use the resolved spelling.
- Discovers the parent repo by walking from `cwd` up to the top-level `.git` (the first ancestor containing `.git`), then ensures `gf.toml` exists there.
- Adds the git-folder to `gf.toml`.
- The git-folder name is derived from the directory name. Use `-n <name>` to override it.
- `-b <ref>` records the git-folder's ref for later pulls, defaulting to `latest` when omitted.
- Sets the git-folder URL to the local child path spelled relative to the parent repo root; update `gf.toml` before `pull` if an upstream is needed. If `--url <url>` is given, store that URL in `gf.toml` instead of the local placeholder, so a later `gf pull` fetches from the real upstream.
- `init` never resolves the URL and has no special case for subfolder URLs. The binding form is decided on the first `gf pull`, which converts the child to a consumer link when the URL resolves to a subfolder (see `gf pull`).
- Refuse to initialize a directory that already contains a `.git` or `.gf` directory, or a `<path>` that resolves inside the parent's `.gf` storage or any `.git` tree — including a `<path>` that is itself a link into a store checkout. `gf init` never plants a `.gf` gitdir inside `.gf` storage: a link into a recordless store checkout is refused the same way, instead of wedging every later command on `did not create the expected worktree record`. A `<path>` whose normalized spelling carries a `.gf` or `.git` segment (`gf init sub/.gf/x`, `gf init sub/.git/x`) is refused by the same lexical predicate manifest read-validation applies to a recorded `path`, before any directory or `.gf` is created, and the child-creation layer refuses a consumer path inside any `.gf` or `.git` directory as `init_child`/`update_child` do — so `gf init` can never write a manifest binding the reader rejects.
- Print `add "<dir>/" to .gitignore` as a recommendation; do not modify the parent `.gitignore`.

### `gf sh [command...]`

Run a shell or a single command inside a child with the correct git environment.

- The child is selected by the global `-C` option or by the current working directory.
- Only works in directories that contain `.gf/git/` or resolve to a consumer link; it does not fall back to the parent `.git`.
- `GIT_DIR` points to the child's gitdir (`<child>/.gf/git` for a whole-repo binding; `<store>/worktrees/<checkout-key>` for a subfolder binding, with `GIT_WORK_TREE` at the checkout) and `GIT_WORK_TREE` points to the child's working root.
- Ambient `GIT_*` variables are scrubbed first: repository-pointer and index/object variables (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_INDEX_VERSION`, `GIT_OBJECT_DIRECTORY`, `GIT_SHALLOW_FILE`, `GIT_NO_REPLACE_OBJECTS`, and the rest of that family), every config source (`GIT_CONFIG`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_COUNT`/`GIT_CONFIG_PARAMETERS`, and numbered `GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*` injection pairs), init-time content and defaults (`GIT_TEMPLATE_DIR`, `GIT_DEFAULT_HASH`, `GIT_DEFAULT_INITIAL_BRANCH_NAME`), command-path variables (`GIT_SSH`, `GIT_EXTERNAL_DIFF`), the whole `GIT_TEST_*` family, and pathspec-mode switches are all removed before `gf` sets its own values. Identity and transport survive — `GIT_CONFIG_NOSYSTEM`, author/committer, and the remaining transport variables pass through unchanged.
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

This is the supported way to run arbitrary git commands against a child. Direct `gf status`/`push` passthrough is not supported.

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
- For a subfolder binding, `gf` appends `-- .` to the arguments when the trailing args contain no `--` and no operand naming an existing path under the mapped directory or carrying pathspec magic (`*`, `?`, `[`, leading `:`), so the default diff is scoped to the mapping; an explicit `--` or such an operand disables the scope.
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
- For a subfolder binding, `gf` appends `-- .` to the arguments when the trailing args contain no `--` and no operand naming an existing path under the mapped directory or carrying pathspec magic (`*`, `?`, `[`, leading `:`), so the default log follows the mapping; an explicit `--` or such an operand disables the scope. Use `gf git log` for unscoped repository history.
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

- `gf` refuses a `<path>` that resolves at or inside any `.gf` or `.git` directory — the parent's own `.gf` storage or `.git`, another root's `.gf`/`.git`, or a `.gf`/`.git` segment reached through a mid-path symlink — before git sees it.
- `<path>` is passed to `git worktree add` (git itself decides if a path is valid).
- Branch and commit options are passed to `git worktree add`.
- After the parent worktree is created, the current `gf.toml` and `gf.local.toml` are copied to it so all `gf` commands work from the new worktree; a manifest path the checkout materialized as a symlink is unlinked and replaced by a regular file, and a non-regular, non-symlink destination — such as a committed `gf.toml/` directory — is refused — the copy never writes through checked-out content.
- `gf` places a relative symlink at `new-worktree/<git-folder-path>` for each git-folder that is a real child in the source worktree. Placement is refused — rolling the worktree back per the failure contract below — when the link's parent directory resolves outside the new worktree or inside a `.gf`/`.git` tree (for example a mid-path symlink like `vendor -> /abs` committed in the parent), so the add never unlinks or replaces a file outside the tree it just created or inside git metadata.
- The symlink points to the corresponding consumer child in the source worktree. For a subfolder binding, the link targets the source worktree's consumer link (not the resolved checkout), so a later retarget in the source worktree propagates.
- The new worktree shares the git-folder's working tree; uncommitted changes are visible in both worktrees.
- `gf` commands run inside a symlinked child resolve to the source child or checkout and operate on it.
- `gf rm` cannot be run from a symlinked child; remove the git-folder from the owning worktree. For a subfolder binding, the owning worktree is the parent root whose `.gf/wt` contains the link's realpath.
- If a step after `git worktree add` fails — copying the manifests or placing a child link, including a child path that already exists — `gf` removes the worktree it just created (`git worktree remove --force`), so a failed add leaves no registered worktree behind and can be retried directly; when the removal itself fails — a locked worktree needs the force doubled — `gf` retries once, then re-lists the registration and reports the worktree's actual state rather than assuming an orphan: if the worktree is gone, only the original error is reported; if it is still registered but its path is gone, the remedy is `git worktree prune`; if it is still on disk, `git worktree remove --force <path>` as before. `-f` replaces an existing link or file at a child path but never a real directory: a directory already occupying a child path — for example tracked content materialized at a `gf init` consumer path — is refused even under `-f`.

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

- Before calling `git worktree remove`, walk the manifest child paths inside the target worktree. For each child path that is a relative symlink to a git-folder child, or to a subfolder binding's consumer link, outside the worktree, `os.unlink` the symlink so the source is not touched by `git worktree remove`. A child path whose parent directory's realpath escapes the worktree — a mid-path symlink inside the worktree pointing outside it — or lands inside a `.git` tree is skipped rather than unlinked, since the unlink would remove a file outside the worktree or inside git metadata. `git worktree remove` then proceeds normally: removing the worktree removes only the mid-path link itself, never the files it points to.
- Refuse when the current working directory is the target worktree or anywhere inside it: removing a worktree that contains the caller's cwd leaves the process in a deleted directory, so `gf` exits with `<path> contains the current working directory; refusing to remove it` first. The comparison uses the logical cwd (`$PWD`-spelled when it resolves to the physical directory).
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

For subfolder bindings, "inside a child" is decided by realpath matching: a cwd or path argument inside a consumer link (or inside the physical `.gf/wt` checkout it resolves to) selects that binding. When cwd resolves inside more than one binding — for example nested `docs` and `docs/api` bindings sharing one checkout — no-argument selection chooses the innermost binding. When several bindings share that innermost map — two consumer paths mapping the same directory — an argument spelling one of their consumer paths selects that binding. Every path argument identifies by spelled consumer-path identity only after lexical normalization — `.`/`..` segments resolve by directory identity. A physical path under `<root>/.gf` still resolves through the consumer link's realpath, so operating from a `cd -P` physical cwd selects the owning binding rather than nothing. A consumer link is owned by the parent root whose `.gf/wt` contains its realpath; in any other parent worktree it is a linked child.

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

1. Take each binding's repo URL and subdir from its recorded URL resolution, or resolve the effective `url` when there is no record or the `url` changed ([URL resolution](#url-resolution)). Resolve the effective ref and its checkout key. An override that changes the repo URL moves the binding to another store; an override that changes the effective checkout key retargets the consumer link. A subfolder binding whose changed `url` resolves to a whole repository stops `gf pull` with the refusal described under `gf pull`, before any repo store, checkout, consumer link, or state is changed.
1. Group the bindings by repo store; run exactly one `git fetch --filter=blob:none origin` per store (falling back to a full fetch with a warning when the server refuses filters). A store that needs creating gets `git init --bare`, `remote.origin.url`, and its first refspec line first; a binding whose branch or tag no line covers appends one, and a pinned commit the store lacks is fetched directly with `git fetch origin <sha>` ([Fetch refspecs](#fetch-refspecs)).
1. Group the bindings by checkout key within each store. For each checkout:
   - Ensure the checkout exists (`git worktree add --no-checkout --detach`, then verify the worktree record per [Checkout integrity](#checkout-integrity)), widening its sparse cone (`sparse-checkout set --cone <union of subdirs>`) when a new binding joins it. A checkout directory without its worktree record stops this checkout with an error.
   - Run one dirty check (`status --porcelain` over the whole materialized checkout — cone-materialized paths plus physically-present untracked files). If dirty, apply the same `--force`/`--autostash` rules as a whole-repo child to the whole checkout before continuing; without them, abort.
   - Apply the ref once: `checkout -B <branch> origin/<branch>` for branch/`latest`, `checkout <sha>` for tag/commit, `--rebase` honored for branch refs.
   - Remove the `.git` gitfile after creation, and lock the checkout with `git worktree lock` if it is not locked.
1. Create or retarget each binding's consumer link to `<checkout>/<subdir>` — its relative target anchored at the link parent's realpath, so a mid-path symlink inside the root lands a working link — refusing the binding when the link would not sit lexically under the owning root, when its parent's realpath lands outside that root, or when either lands inside `.gf` or `.git` — record per-checkout state at `<root>/.gf/wt/<repo-key>/.<checkout-key>.state`, and print one line per binding the checkout serves, as described under `gf pull`.

## Drift algorithm

For a single child during `gf status --remote`:

1. `gf status --remote` is local-only. It does **not** call `git fetch`, `git remote`, `git ls-remote`, or any other network command. Drift is computed from the local refs already present in the child gitdir, exactly like `git status` compares `refs/heads/<branch>` to `refs/remotes/origin/<branch>` after a `git fetch`. Run `gf pull` or `gf git fetch` to refresh the remote-tracking refs first.
1. If the child is missing (no `child/.gf/git/HEAD`), the state is `missing`. For a subfolder binding, `missing` also covers a dangling consumer link — its mapped subdirectory absent from the checkout.
1. Read the effective ref from `gf.toml` and `gf.local.toml`.
1. Resolve the effective ref to a SHA in the child gitdir or repo store using only local refs:
   - For `branch` refs: `refs/remotes/origin/<branch>`.
   - For `latest`: `refs/remotes/origin/HEAD` if it is a valid symbolic ref, otherwise `refs/remotes/origin/main`, otherwise `refs/remotes/origin/master`. No `git remote set-head` or other network call is made.
   - For `tag` or `commit`: the peeled SHA from local refs.
   - If the ref cannot be resolved locally, raise a clear error (do not report `clean`); `gf status --remote` exits with a deterministic non-zero code. This holds for a child with no commits yet, for example one just created by `gf init`: it has a `HEAD`, so it is not `missing`, and when its ref cannot be resolved locally it gets this error, not a raw git error.
1. Read the child `HEAD` SHA. A child with no commits yet has no `HEAD` SHA; it does not match the resolved SHA.
1. Run `git status --porcelain` in the child. For a subfolder binding, the run is scoped to the binding's subdir (`status --porcelain -- <subdir>` against its checkout) and remains local-only.
1. Classify:
   - `clean`: child `HEAD` matches the resolved SHA and the worktree is clean.
   - `behind`: child `HEAD` does not match the resolved SHA and the worktree is clean.
   - `local-dirty`: child `HEAD` matches the resolved SHA and the worktree has modifications.
   - `both`: child `HEAD` does not match the resolved SHA and the worktree has modifications.
   - `missing`: the child has no `.gf/git/HEAD`. For a subfolder binding, `missing` means the consumer link is absent or dangling (its mapped subdirectory is absent from the checkout), the checkout itself is absent, or the binding has no recorded URL resolution yet. Drift never resolves a URL.

## Security and authority

- No secrets are written to the manifest or logs.
- `gf` does not commit or push on the user’s behalf. git-folder changes happen only when the user runs `git` through `gf sh` or another shell with `GIT_DIR` set.
- Local overrides live in `gf.local.toml`, which is never tracked.
- Parent tracked files change only when `gf.toml` is edited by `gf clone`/`init` or `gf rm`.
- A manifest `path` is trusted only after validation — it must be a relative, normalized, in-parent path free of `.gf` and `.git` segments — and every link `gf` creates, retargets, or unlinks is checked so the link and its parent's realpath remain inside the owning root and outside `.gf` and `.git`; the owning root itself is adopted from a `.gf/wt`-shaped realpath ancestor only when that root proves real (a `.git` entry, or the matching repo store's `HEAD` under it) and does not itself sit inside a `.gf` tree, so committed content cannot forge the anchor. A hostile or hand-edited `gf.toml` therefore cannot aim a `gf` write outside the parent, into `gf`'s storage, or into the parent's `.git` — where a hook-named entry would run on the next `git` operation. Every `.gf`-rooted storage path `gf` populates must additionally resolve to itself: a symlinked `parent/.gf` (committable content) cannot redirect repo-store, checkout, or gitdir writes outside the workspace, and teardown never removes through such a link.

## Error handling

- Exit codes are deterministic:
  - `0`: success or no drift.
  - `1`: general failure or validation error.
  - `2`: network/git failure.
  - `3`: dirty worktree preventing update.
- Error messages include the git-folder name, path, and the operation that failed.
- Manifest and override reads are enveloped: a `gf.toml`/`gf.local.toml` that is unreadable, not valid UTF-8, invalid TOML, or fails the shape validation described under [Manifest](#manifest) fails the command with a clean `gf:` error naming the file and exit `1` — never a Python traceback; a checkout `.state` file that cannot be decoded or parsed is reported as corrupt state the same way. `gf`'s own filesystem reads and mutations carry the same envelope: an `OSError` at a consumer-link unlink or rmdir, a `gf rm` child's link unlink, `.gf/git` move, or `.gf` removal, a checkout-state read or write, or the manifest's atomic temp-write-and-replace exits `1` as a `gf:` error naming the operation and path — a folder-identified site adds the git-folder name through `folder_error` — and `main` catches a residual `OSError` as a last resort for any site a sweep cannot enumerate. `KeyboardInterrupt`, `SystemExit`, and deliberate best-effort swallows such as a failed temp-file cleanup stay untouched. Captured `git` output decodes with replacement, so a malformed byte sequence in a ref name or message surfaces as U+FFFD inside the error instead of a `UnicodeDecodeError` traceback — the same treatment streamed output already had. A checkout's `gitdir` worktree record that is unreadable or not valid UTF-8 is treated as an invalid record — the checkout is rebuilt when its path is free and refused with `refusing to rebuild over existing files` when a populated checkout still occupies it — instead of escaping as a `UnicodeDecodeError`.

## Testing

- Unit tests for manifest parsing, URL resolution (local walk-up, `.git` boundary, longest-prefix probe, unresolvable URL), repo/checkout key derivation, ref resolution, path selection, and effective URL/ref merging.
- Integration tests with local bare repositories and multiple parent `git worktree` checkouts.
- End-to-end tests:
  - Fresh parent clone; `gf clone` a git-folder at a custom path; verify `child/.gf/git/config` has a single `origin` remote pointing at the actual URL; verify no `.git` in the child.
  - Edit a git-folder file; `gf status` shows `local-dirty`; `gf sh -c "git diff"` works.
  - Add a parent `git worktree`; `gf pull` in the new worktree; verify the new worktree uses a symlink to the source child and has an independent `HEAD`.
  - Switch a git-folder to a fork via `gf.local.toml`; `gf pull` updates without changing tracked parent files.
  - Clone three subfolders of one repository: one repo store and one checkout exist, the consumer links list only mapped files, and plain `git` from a link resolves to the parent.
  - `gf pull` over several bindings of one repository performs one fetch per store; scoped `status` reports only each binding's subdir; `gf rm` leaves the checkout, its uncommitted work, and the store untouched, and adding the binding back restores the folder.
  - `gf init` then `gf pull` with a subfolder URL converts an empty child to a consumer link and refuses a child that gained files.

## Boundaries

- Windows is not a supported host. `gf` specifies no behavior for Windows process creation, for `.venv/Scripts/python.exe`, or for any other Windows-only mechanism.
- Subfolder bindings require git 2.35 or newer: the mechanism uses `git worktree add --no-checkout`, `git worktree lock --reason`, cone-mode `git sparse-checkout` under `extensions.worktreeConfig`, `git fetch --filter`, and `git ls-remote` for URL resolution. Whole-repo bindings carry no new minimum beyond what they already require.
- The platform surface covers path identity and process replacement only. It does not invoke `git`, read the manifest, or interpret the `.gf` layout; the git backend remains the only caller of `git` and the manifest layer the only reader of `gf.toml`.
- Host support is gained by removing host assumptions, not by adding host-specific command behavior. There is no second git integration and no host-conditioned clone, pull, status, ls, rm, init, or worktree algorithm.
- A virtual environment belongs to one host. `gf` specifies no recovery for a `.venv` shared between hosts with different ABIs; that tree is cleaned and recreated on the host that will run it.