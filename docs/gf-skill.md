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

# Sync with upstream (branch refs; resets the local branch to the remote tip)
gf pull

# Sync with upstream while rebasing any local commits
gf pull --rebase

# Remove a child from the manifest and convert it back to a normal `.git` repo
gf rm vendor/libfoo
```

## Commands

| Command                                          | Purpose                                                                           |
| ---                                              | ---                                                                               |
| `gf init [<path>] [-b <ref>]`                    | Create an empty placeholder child and add it to `gf.toml`                         |
| `gf clone <url> [<path>] [-n <name>] [-b <ref>]` | Clone a git-folder and add it to the manifest                                     |
| `gf pull [--rebase] [<path>...]`                 | Update selected children to their effective refs                                  |
| `gf status [<path>...]`                          | Show `git status --porcelain` for selected children                               |
| `gf ls [<path>...]`                              | List git-folders and current refs                                                 |
| `gf rm <path>...`                                | Unregister a git-folder and convert `child/.gf/git` to `child/.git`               |
| `gf git [args...]`                               | Run any `git` command in the current child with `GIT_DIR` and `GIT_WORK_TREE` set |
| `gf sh [-c <cmd>]`                               | Open a shell or run a command with the child git environment                      |

## Rules of thumb

- `gf` never creates commits in the parent for you. Add child paths to the
  parent's `.gitignore` after `clone` or `init`; for a folder binding, add its
  path without a trailing slash and the parent's `.gf/` store directory.
- `gf rm` on a folder binding removes only the link; `gf clone` with the same
  URL brings it back with any uncommitted work.
- `gf ls` and `gf status` do not access the network.
- `gf pull` refuses to run if a child has uncommitted changes.
- `gf pull --rebase` only makes sense for branch refs; for tags or commits it
  falls back to a normal checkout.
- `gf git` is the escape hatch for any git operation `gf` does not wrap.