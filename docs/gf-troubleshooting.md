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
| GF-TRB-3 | 2026-09-25T20:12:46Z | diagnostics-deferral | A parent-repo `git clean -fdx` removes `<root>/.gf` even when it is ignored, deleting every repo store and checkout with their uncommitted work, unpushed commits, local branches, and stashes. | [gf-spec.md](gf-spec.md) | unseeded | documented | clean with `git clean -fdx -e .gf/` or `git clean -fd`; stores rebuild from remotes on the next `gf pull` |
| GF-TRB-4 | 2026-09-25T20:12:46Z | diagnostics-deferral | A physical-path `cd -P` from a consumer link lands inside `<root>/.gf/wt`, where files appear at repository-relative paths rather than the mapped subdir. | [gf-spec.md](gf-spec.md) | unseeded | documented | covered by realpath target selection back to the owning binding |
| GF-TRB-5 | 2026-09-25T20:12:46Z | diagnostics-deferral | `git worktree unlock` then `prune` run against a repo store can drop a `gf` checkout's worktree record. | [gf-spec.md](gf-spec.md) | unseeded | documented | `gf pull` re-locks unlocked checkouts and reports a checkout without its record as an error with recovery steps |
| GF-TRB-6 | 2026-09-25T20:12:46Z | diagnostics-deferral | A server without `uploadpack.allowFilter` refuses `fetch --filter=blob:none` for a repo store. | [gf-spec.md](gf-spec.md) | unseeded | documented | `gf` falls back to a full fetch with a warning |
| GF-TRB-7 | 2026-09-25T20:12:46Z | diagnostics-deferral | URL resolution cannot find the repository boundary of a private remote that needs an interactive login, because probes run with terminal prompts disabled. | [gf-spec.md](gf-spec.md) | unseeded | documented | write `.git` after the repository name to mark the boundary |
| GF-TRB-8 | 2026-09-25T20:12:46Z | diagnostics-deferral | A trailing-slash `.gitignore` pattern does not match a subfolder binding's consumer symlink, so the parent lists the link as untracked. | [gf-spec.md](gf-spec.md) | unseeded | documented | ignore the consumer path without a trailing slash, as `gf` recommends |

## `gf clone <url>` says `url` is required

`gf clone` syntax is `gf clone <url> [<path>] [-n <name>] [-b <ref>]`. The first positional is the URL. If you used the old `<name> <url>` order, swap them.

## `gf pull` fails with `fatal: repository 'abc' does not exist`

The git-folder `url` is a local placeholder from `gf init`. `gf pull` skips placeholders; if it tries to clone one, the URL normalization is wrong. Update `gf.toml` `url` to a real upstream.

## `gf ls` shows `[]` for branch

The child is in a detached `HEAD`. This is expected for tag or commit refs. For `latest` or branch refs, ensure `init_child`/`update_child` uses `git checkout -B <branch> origin/<branch>`. If the remote has no default branch, the clone may be empty.

## `gf status` is empty

`status` is a quiet success when no manifest exists or no git-folders match the current context. `ls` is global; use it to verify the manifest is not empty.

## `gf rm` deleted my worktree

For a whole-repo child, `gf rm` must only move `child/.gf/git` to `child/.git`. If it deletes more, file a bug. It should preserve all user files.

## My subfolder disappeared after `gf rm`

For a subfolder binding, `gf rm` removes only the link at the consumer path and the manifest entry. The checkout under `<root>/.gf/wt`, including uncommitted work, is untouched. To reconnect that checkout, use its effective URL, effective ref, and consumer path, accounting for both URL and ref overrides in `gf.local.toml`; preserve the original name with `-n` when needed for those overrides. For an original `latest` ref, pass the branch that `latest` resolved to before removal, since the remote default may have changed; for a branch ref, preserve that branch name; for a tag or commit, preserve the pinned ref. For example: `gf clone -b <resolved-branch-or-pinned-ref> -n <original-name> <effective-url> <path>`. Omitting `-b` selects the current default ref and can choose a different checkout.

## `gf pull` recreated everything after `git clean -fdx`

`git clean -fdx` in the parent repo deletes untracked files and ignored files alike, so ignoring `.gf/` does not protect it: every repo store and checkout for subfolder bindings is destroyed. The next `gf pull` rebuilds them from the remotes, but anything that existed only locally is gone — uncommitted changes, unpushed commits, local branches, and stashes. Whole-repo children under ignored paths share the same hazard. Clean with `git clean -fdx -e .gf/` to keep the stores, or with `git clean -fd`, which leaves ignored files alone.

## I ran `cd -P` and landed inside `.gf/wt`

`cd -P` resolves symlinks: from a subfolder consumer link it lands at the physical checkout under `<root>/.gf/wt/<repo-key>/<checkout-key>/<subdir>`. Files there appear at repository-relative layout, not just the mapped subdir. `gf` commands still select the owning binding via realpath matching, so `gf status`/`pull`/`git` work correctly; run them from the consumer path when you want the mapped view.

## `git worktree prune` ran against a repo store

`gf` locks every checkout under `.gf/wt`, so `git worktree prune` skips them and the checkouts survive. If a checkout was unlocked, the next `gf pull` locks it again. If a checkout was unlocked and then pruned, git no longer has its worktree record; `gf pull` stops with an error for that checkout rather than rebuilding over its files. Save any work in that directory, delete it, and run `gf pull` to create a fresh checkout.

## `gf clone <url>/<dir>` fails with `fatal: couldn't fetch ...`

The upstream server refused the partial clone (`fetch --filter=blob:none`) because it lacks `uploadpack.allowFilter`. `gf` falls back to a full fetch and warns; the clone succeeds but transfers all blobs. Enable `uploadpack.allowFilter` on the server, or accept the larger store — it is still shared by all bindings of that repository.

## `gf clone <url>/<dir>` says it cannot find the repository

`gf` finds where the repository ends by asking the server with `git ls-remote`, with password prompts switched off. A private remote that needs an interactive login answers none of those probes. Write `.git` after the repository name, e.g. `https://host/org/repo.git/docs/api`, and `gf` splits there without asking the server.

## A subfolder link shows up in `git status` of the parent

A subfolder binding's consumer path is a symlink, and a `.gitignore` pattern with a trailing slash (`vendor/api/`) does not match a symlink. Use `vendor/api` without the slash. The same applies to links `gf worktree add` places in a second parent worktree.

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