---
name: gf
description: How to use the `gf` git-folder manager inside a parent git repo.
---

# gf quick reference

`gf` manages git-folder git repositories as children of a parent git repo.
Each child keeps its own gitdir under `child/.gf/git`, so the parent repo
does not track the child's contents. Add child paths to the parent
`.gitignore`. A URL that is a path to a folder inside a repository binds just
that folder through a shared sparse checkout under the parent's `.gf/`; add
`.gf/` and the folder path (no trailing slash) to `.gitignore`.

## Typical workflow

```bash
# See what is already managed
gf ls
gf status

# Clone a git-folder into the parent
gf clone https://github.com/foo/libfoo vendor/libfoo

# Bind just one repository subdirectory
gf clone https://github.com/foo/libfoo/docs/api vendor/libfoo-api

# Move into a child and use git normally
gf -C vendor/libfoo git status
gf -C vendor/libfoo git log --oneline
gf -C vendor/libfoo git commit -m "local work"

# Sync with upstream: attach the local tracking branch, then fast-forward it;
# local commits and worktree changes are preserved, never reset
gf pull

# Sync with upstream while rebasing any local commits
gf pull --rebase

# Remove a child from the manifest and convert it back to a normal `.git` repo
gf rm vendor/libfoo
```

## Commands

| Command                                          | Purpose                                                                           |
| ---                                              | ---                                                                               |
| `gf init [<path>] [-b <ref>] [-n <name>] [--url <url>]`                    | Create an empty placeholder child and add it to `gf.toml`                         |
| `gf clone <url> [<path>] [-n <name>] [-b <ref>] [--depth <n>] [--single-branch]` | Clone a git-folder and add it to the manifest                                     |
| `gf pull [--rebase] [--autostash] [<path>...]`                 | Update selected children to their effective refs                                  |
| `gf status [<path>...] [--remote]`                          | Show `git status --porcelain` for selected children                               |
| `gf ls [<path>...]`                              | List git-folders and current refs                                                 |
| `gf rm <path>... [--all]`                                | Unregister a git-folder; a whole-repo child becomes a usable `.git` repo (registered worktrees included), a folder binding loses only its link |
| `gf git [args...]`                               | Run any `git` command in the current child with `GIT_DIR` and `GIT_WORK_TREE` set |
| `gf sh [command...]`                               | Open a shell or run a command with the child git environment                      |

## Rules of thumb

- `gf` never creates commits in the parent for you. Add child paths to the
  parent's `.gitignore` after `clone` or `init`; for a folder binding, add its
  path without a trailing slash and the parent's `.gf/` store directory.
- `gf rm` on a whole-repo child converts `child/.gf/git` into a usable
  `child/.git` — history, index, refs, and registered `git worktree`s included;
  on a folder binding it removes only the link, and `gf clone` with the same
  URL brings it back with any uncommitted work.
- `gf ls` and `gf status` do not access the network.
- `gf pull` refuses to run if a child has uncommitted changes; pass
  `--autostash` to stash, update, and restore them.
- `gf` has no force or discard mode. A diverged branch refuses and reports the
  ahead/behind state — commit, stash, or `gf pull --rebase` are the deliberate
  recoveries; deleting work is your own `git` action.
- `gf pull --rebase` only makes sense for branch refs; for tags or commits it
  is ignored.
- `gf git` is the escape hatch for any git operation `gf` does not wrap.