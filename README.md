---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-readme]; otherwise, do not modify."
---

# git-folders

`gf` manages git-folder git repositories inside a parent git workspace without polluting the parent history.

A parent repo declares git-folders in `gf.toml`. Each git-folder is cloned into a child directory. The child has its own git history, and its metadata lives under `.gf/` instead of `.git/`. The child gitdir at `.gf/git` is a full, self-contained gitdir with `origin` pointing to the actual git-folder URL.

```text
parent/
├── .git
├── gf.toml
└── vendor/libfoo/           # child worktree
    ├── .gf/git/             # child gitdir
    └── ...                  # git-folder files
```

## Features

| Feature                              | What it gives you                                                                   |
| ------------------------------------ | ----------------------------------------------------------------------------------- |
| Nested git repos as ordinary folders | Clone external repos into the parent workspace without polluting the parent history |
| Self-contained child gitdir          | Each child has its own gitdir under `.gf/git/` instead of `.git/`                   |
| Tracked manifest + local overrides   | Canonical bindings in `gf.toml`; per-user overrides in `gf.local.toml`              |
| Pinned or floating refs              | Use a commit, tag, branch, or `latest`; `gf` resolves the ref                       |
| Parent worktree support              | Add a parent git worktree with git-folders symlinked from the source                |
| Run git in a child                   | Run git commands in a child with the right `GIT_DIR`                                |

## Install

`git-folders` requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

### Run from source (no install)

```bash
uv run gf --help
```

### Run the test suite

```bash
uv run pytest -x
```

### Install as a uv tool

```bash
uv tool install .
```

This builds `git-folders` and creates the `gf` executable in uv's tool bin directory. Normally that is `~/.local/bin`; if your environment sets `XDG_DATA_HOME`, it will be `$XDG_DATA_HOME/../bin`. Make sure that directory is on your `PATH`.

### Install to a chosen bin directory

```bash
UV_TOOL_BIN_DIR=~/bin uv tool install .
```

### Uninstall

```bash
uv tool uninstall git-folders
```

## Quickstart

Run `gf` from the parent Git repository you want to manage. `gf init` creates an empty local child and records it in `gf.toml`; set that entry's upstream URL before running `gf pull`.

After installing the package with `uv tool install .`, initialize a child from the parent repository:

```bash
gf init vendor/libfoo
gf ls
```

The first command prints `Initialized libfoo in vendor/libfoo` and creates the child without fetching a remote.

From a source checkout, target the parent repository with `-C`:

```bash
uv run gf -C /path/to/parent init vendor/libfoo
```

## Commands

All commands accept a global `-C <path>` option, like `git -C`, to run from another directory. `gf --version` (or `gf -v`) prints the package version and exits.

| Command                                                                                      | Purpose                                                              |
| -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `gf clone <url> [<path>] [-n <name>] [-b <ref>] [--depth <n>] [--single-branch]`                    | Add a git-folder to the manifest and clone its child worktree        |
| `gf init [<path>] [-b <ref>] [-n <name>] [--url <url>]`                                | Create an empty `.gf` child and add it to the manifest               |
| `gf pull [--rebase] [--force] [--autostash] [<path>...]`                                      | Update selected children to their effective refs                     |
| `gf status [<path>...] [--remote]`                                                         | Show git status for selected children; `--remote` classifies drift against local remote-tracking refs (no network) |
| `gf ls [<path>...]`                                                                          | List all git-folders in the manifest                                 |
| `gf rm <path>... [--all]`                                                                    | Unregister git-folders and convert children back to `.git`           |
| `gf diff [args...]`                                                                          | Run `git diff` in a child                                            |
| `gf log [args...]`                                                                           | Run `git log` in a child                                             |
| `gf worktree add <path> [<commit-ish>] [-b <new-branch>] [-B <new-or-existing-branch>] [-f]` | Add a parent git worktree with git-folders symlinked from the source |
| `gf worktree list [--porcelain] [--verbose]`                                                | List parent git worktrees and the git-folders linked into each        |
| `gf worktree remove <path> [--force]`                                                       | Remove a parent worktree, guarding linked git-folder children        |
| `gf git [args...]`                                                                           | Run an arbitrary `git` command in a child                            |
| `gf sh [command...]`                                                                         | Run a shell or command inside a child                                |

The full command syntax and examples are in `docs/gf-spec.md`.

## Configuration

`gf.toml` is the tracked manifest that declares the git-folders in the parent repo.

```toml
[[git_folder]]
name = "libfoo"
url = "https://github.com/foo/libfoo.git"
ref = "main"
path = "vendor/libfoo"
```

Local overrides live in `gf.local.toml` and are never tracked.

`gf` does not edit the parent `.gitignore`. When a child is created it prints a recommendation such as `add "vendor/libfoo/" to .gitignore` so the user can keep git-folder files out of the parent history.

## Documentation

- `docs/gf-index.md` — documentation routing surface
- `docs/gf-spec.md` — full command and algorithm reference
- `docs/gf-arch.md` — architecture and data flow
- `docs/gf-testing.md` — test plan
- `docs/gf-troubleshooting.md` — common issues and undo procedures

## License

Licensed under the [MIT License](LICENSE).
SPDX identifier: `MIT`.
