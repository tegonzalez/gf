---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-readme]; otherwise, do not modify."
---

# git-folders

`gf` manages external Git repositories as folders inside a parent Git workspace. The parent tracks the bindings in `gf.toml`; each child keeps its own Git history.

A **whole-repo binding** keeps a full, self-contained Git directory in `child/.gf/git`, including its own objects, refs, index, and history; `origin` points to the actual upstream URL. A **subfolder binding** exposes one repository folder through a relative symlink to a shared sparse checkout under the parent's `.gf/`. Subfolder bindings of the same repository and resolved branch share a checkout and object store.

```text
parent/
├── .git
├── gf.toml
└── <chosen-folder>/         # created by gf clone
    ├── .gf/git/             # child Git metadata
    └── ...                  # git-folder files
```

`gf` preserves staged and unstaged changes, untracked and ignored files, local commits and refs, stashes, and usable Git provenance. An operation that cannot preserve them refuses and reports the state.

## Features

| Feature                               | Purpose                                                                      |
| ------------------------------------- | ---------------------------------------------------------------------------- |
| Whole repositories and subfolders     | Map a complete repository or one of its folders into the workspace           |
| Shared local storage                  | Fetch once per repository store and keep related subfolder bindings coherent |
| Tracked bindings and local overrides  | Share `gf.toml` while keeping personal forks and refs in `gf.local.toml`     |
| Branches, tags, commits, and `latest` | Follow a branch or bind a pinned revision                                    |
| Work-preserving updates and unmapping | Retain files, local history, index state, and Git provenance                 |
| Parent worktrees                      | Link bindings from the source workspace into another parent worktree         |
| Child Git commands and local status   | Use the child's Git context and inspect drift without fetching               |

## Install

Supported hosts are Linux and macOS. Python 3.13+, Git, and [uv](https://docs.astral.sh/uv/) are required; subfolder bindings require Git 2.35 or newer.

Run these commands from the `git-folders` source checkout.

### Install in this container

The supported installation in this target container places the executable in `~/bin`. Run this command from the source checkout:

```bash
UV_TOOL_BIN_DIR=~/bin uv tool install .
```

### Install with uv's default directory

For other environments using uv's default executable directory:

```bash
uv tool install .
uv tool dir --bin
```

The second command prints that environment's executable directory. Ensure it is on `PATH`.

### Run from source

```bash
uv run gf --help
```

Use the global `-C` option before the subcommand to target another parent repository from the source checkout.

### Uninstall

```bash
uv tool uninstall git-folders
```

## Quickstart

After installing `gf`, run this Bash example from the parent Git repository where you want to add a binding. Enter your upstream repository URL or local repository path, then choose a new child folder relative to the parent:

```bash
read -r -p "Repository URL or local path: " repo_url
read -r -p "New child folder: " child_path
gf clone "$repo_url" "$child_path"
gf ls
gf status "$child_path"
gf pull "$child_path"
```

`gf clone` creates the chosen folder and records the binding in `gf.toml`; no predefined folder layout is required. It prints `Cloned <binding-name> into <chosen-folder>`, with the name derived from the folder's basename. Status and pull select that child explicitly.

From the `git-folders` source checkout, the same mapping operation uses the source entrypoint and an explicit parent repository:

```bash
read -r -p "Parent repository path: " parent_repo
read -r -p "Repository URL or local path: " repo_url
read -r -p "New child folder: " child_path
uv run gf -C "$parent_repo" clone "$repo_url" "$child_path"
```

## Usage

### Map an upstream

The repository input in Quickstart may be an HTTPS or SSH URL, a local repository path, or a scheme-less host/repository path that `gf` expands to HTTPS.

To bind one repository subfolder, enter the full URL or local path to that subfolder and choose its consumer folder in your parent repository:

```bash
read -r -p "Repository subfolder URL or local path: " subfolder_url
read -r -p "New consumer folder: " consumer_path
gf clone "$subfolder_url" "$consumer_path"
```

The consumer folder is a symlink that `gf` creates. For remote URLs, writing the repository's `.git` suffix makes the repository boundary explicit before the subfolder path. `-b <ref>` accepts a branch, tag, or commit; omitting it selects `latest`, which follows the remote default branch. See the [reference model](docs/gf-spec.md#reference-model) for checkout sharing and pins.

### Initialize a local child

For a local repository with no upstream yet, choose an unused child folder from your parent repository:

```bash
read -r -p "New local child folder: " local_child
gf init "$local_child"
gf ls
```

`gf init` creates that folder and records its binding without fetching. Set its upstream URL before pulling from a remote; until then, pull skips the local placeholder.

### Inspect and update

From the same parent repository, using the `child_path` selected in Quickstart:

```bash
gf status --remote "$child_path"
gf pull "$child_path"
gf -C "$child_path" git status
```

`status --remote` compares local refs without fetching. Pull advances an existing branch fast-forward-only and leaves a strictly-ahead branch intact. Dirty work refuses an update; add `--autostash` to carry it through the update and restore its staged and unstaged state. Diverged histories require explicit `--rebase` or your own Git integration; committing or stashing alone does not resolve divergence. There is no `gf pull --force`.

Address a child's Git repository through `gf git` or `gf sh`. For a subfolder binding, `gf diff` and `gf log` scope to the mapped folder by default, while `gf git` exposes the shared repository's ordinary Git commands.

### Unmap and reconnect

`gf rm` converts a whole-repo child to an ordinary `.git` repository and keeps its folder and work. For a subfolder binding, it removes the consumer link and manifest entry while retaining the shared checkout and store.

To reconnect a removed subfolder binding, restore its effective URL, original ref or resolved branch, and consumer path. An original `latest` binding needs its previously resolved branch if the remote default has changed. Account for local overrides; [troubleshooting](docs/gf-troubleshooting.md#my-subfolder-disappeared-after-gf-rm) gives the restoration command.

`gf worktree remove` refuses a target containing protected work, private Git provenance, or owned `.gf` storage, including under `--force`.

## Commands

Place global `-C <path>` before the subcommand. `gf --version` or `gf -v` prints the package version. The [command reference](docs/gf-spec.md#command-reference) provides complete options and selection rules.

| Command                                                                                      | Purpose                                               |
| -------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| `gf clone <url> [<path>] [-n <name>] [-b <ref>] [--depth <n>] [--single-branch]`             | Add a whole repository or subfolder binding           |
| `gf init [<path>] [-b <ref>] [-n <name>] [--url <url>]`                                      | Create an empty local child and record its binding    |
| `gf pull [--rebase] [--autostash] [<path>...]`                                               | Update selected bindings while preserving work        |
| `gf status [<path>...] [--remote]`                                                           | Inspect selected bindings and optional local drift    |
| `gf ls [<path>...]`                                                                          | List bindings; no arguments lists all bindings        |
| `gf rm <path>... [--all]`                                                                    | Unregister bindings while retaining their work        |
| `gf diff [args...]`                                                                          | Run Git diff, scoped to a mapped subfolder by default |
| `gf log [args...]`                                                                           | Run Git log, scoped to a mapped subfolder by default  |
| `gf worktree add <path> [<commit-ish>] [-b <new-branch>] [-B <new-or-existing-branch>] [-f]` | Create a parent worktree with linked bindings         |
| `gf worktree list [--porcelain] [--verbose]`                                                 | List parent worktrees and their linked bindings       |
| `gf worktree remove <path> [--force]`                                                        | Remove a parent worktree after preservation checks    |
| `gf git [args...]`                                                                           | Run an arbitrary Git command in the child repository  |
| `gf sh [command...]`                                                                         | Run a shell or command in the child's Git context     |

## Configuration

`gf clone` and `gf init` record your selected bindings in the parent's tracked `gf.toml`. This entry template shows the fields; replace the angle-bracketed values with your binding's name, repository URL, and chosen relative folder:

```toml
[[git_folder]]
name = "<binding-name>"
url = "<repository-url>"
ref = "latest"
path = "<chosen-folder>"
```

Keep per-user overrides in an untracked `gf.local.toml`. Match the existing binding name and supply your fork URL and branch:

```toml
[[git_folder_override]]
name = "<binding-name>"
url = "<fork-repository-url>"
ref = "<branch-name>"
```

`gf` recommends ignore patterns but does not edit the parent's `.gitignore`. Ignore each whole-repo consumer directory using its chosen path with a trailing slash; ignore each subfolder consumer link using its chosen path without a trailing slash; and ignore parent-root `.gf/` storage. Keep child files and metadata out of parent history.

Ignoring a path does not protect it from `git clean -fdx`. Cleanup targets must neither contain nor sit inside `.gf` storage or any consumer path; inspect dry runs before deleting generated output. See [cleanup guidance](docs/gf-troubleshooting.md#gf-pull-recreated-everything-after-git-clean--fdx).

## Documentation

Start at the [documentation index](docs/gf-index.md) for current requirements, design, verification, and recovery guidance.

- [Release notes](RELEASE_NOTES.md) — dated releases and upgrade actions
- [Specification](docs/gf-spec.md) — command behavior and reference models
- [Architecture](docs/gf-arch.md) — storage, shared checkouts, and data flow
- [Test strategy](docs/gf-testing.md) — verification model and harness boundaries
- [Troubleshooting](docs/gf-troubleshooting.md) — known issues and recovery

## Development

Source lives in `src/gf/`; verification assets live in `tests/`. From the source checkout, run:

```bash
uv run pytest -x
uv run python tests/verify_preservation_cli.py --git /absolute/path/to/git
```

For the CLI witness, replace the Git path with the executable under evaluation. Follow the [contribution workflow](docs/gf-guidelines.md#working-on-git-folders) for changes.

## License

Licensed under the [MIT License](LICENSE). SPDX identifier: `MIT`.
