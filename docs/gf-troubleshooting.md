---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-known-issues]; otherwise, do not modify."
---

# git-folders Troubleshooting

## Purpose

Common issues and resolutions for `git-folders` users.

## Register

| id | datetime | kind | restatement | owner | seed | disposition | retirement |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GF-TRB-1 | 2026-08-30T04:28:41Z | diagnostics-deferral | `gf clone` requires the URL as the first positional argument. | [gf-spec.md](gf-spec.md) | unseeded | documented | absorbed into command parser validation |
| GF-TRB-2 | 2026-08-30T19:28:11Z | diagnostics-deferral | `tests/test_cli_permutations.py` is ~4-5x slower on macOS/c0dev because every `pyfakefs.fake_open` call extracts a stack trace, which triggers `linecache.updatecache` and real `posix.stat` calls; macOS `stat` latency is ~0.7 ms versus ~2.5 microseconds on Linux. | [gf-testing.md](gf-testing.md) | unseeded | documented | absorb by reducing `open()` calls and caching `_get_version()` / `ArgumentParser` |

## `gf clone <url>` says `url` is required

`gf clone` syntax is `gf clone <url> [<path>] [-n <name>] [-b <ref>]`. The first positional is the URL. If you used the old `<name> <url>` order, swap them.

## `gf pull` fails with `fatal: repository 'abc' does not exist`

The git-folder `url` is a local placeholder from `gf init`. `gf pull` skips placeholders; if it tries to clone one, the URL normalization is wrong. Update `gf.toml` `url` to a real upstream.

## `gf ls` shows `[]` for branch

The child is in a detached `HEAD`. This is expected for tag or commit refs. For `latest` or branch refs, ensure `init_child`/`update_child` uses `git checkout -B <branch> origin/<branch>`. If the remote has no default branch, the clone may be empty.

## `gf status` is empty

`status` is a quiet success when no manifest exists or no git-folders match the current context. `ls` is global; use it to verify the manifest is not empty.

## `gf rm` deleted my worktree

`gf rm` must only move `child/.gf/git` to `child/.git`. If it deletes more, file a bug. It should preserve all user files.

## `gf pull` aborts on dirty worktree

`gf pull` aborts by default to avoid overwriting local changes. You can:
- commit or stash the child changes first,
- use `gf pull --autostash` to stash, pull, and restore changes,
- or use `gf pull --force` to discard local worktree changes and check out the resolved ref.

## `gf clone github.com/cursor/plugins` creates `https://github.com/cursor/plugins`

This is correct. Bare host/path URLs are expanded to `https://` for `git clone`. The `name` and consumer `path` are still the repo basename.

## Column alignment looks wrong

`ls` and `status` compute column widths per run. If output still looks unaligned, ensure you are using the current version and the terminal uses a fixed-width font.

## Tests touch files outside `tests/fixtures/tmp/`

The `tmp_path` fixture is overridden in `conftest.py`. If a test writes outside its fixture, it is a test bug. Report it.

## Undoing common operations

`gf` tries to clean up partial side effects when a command fails, but some operations leave the workspace in an intermediate state. The safe undo for each command is:

- `gf clone` that fails before completion: the child directory is removed. If it was not removed, delete `child/` and run `git checkout gf.toml` to restore the manifest.
- `gf init` that fails before completion: the `.gf/` directory is removed from the child. If the manifest was written, run `git checkout gf.toml`.
- `gf rm`: the child is converted back to a normal git worktree (`child/.git` replaces `child/.gf/git`). If the manifest change was not applied, re-add the git-folder.
- `gf worktree add` that fails after the parent worktree is created: the new worktree exists but may be incomplete. Remove it with `git worktree remove <path>` and clean up any leftover symlinked child directories.
- `gf pull` of an existing child that fails mid-checkout: the child worktree may be partially updated. Run `gf sh git status` to inspect, and `gf sh git checkout` to reset.