---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-known-issues]; otherwise, do not modify."
---

# git-folders Troubleshooting

## Purpose

Common issues and resolutions for `git-folders` users.

## Register

| id       | datetime             | kind                 | restatement                                                                                                                                                                                                                                                                                              | owner                          | seed     | disposition | retirement                                                                                                              |
| -------- | -------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ | -------- | ----------- | ----------------------------------------------------------------------------------------------------------------------- |
| GF-TRB-1 | 2026-08-30T04:28:41Z | diagnostics-deferral | `gf clone` requires the URL as the first positional argument.                                                                                                                                                                                                                                            | [gf-spec.md](gf-spec.md)       | unseeded | documented  | absorbed into command parser validation                                                                                 |
| GF-TRB-2 | 2026-08-30T19:28:11Z | diagnostics-deferral | `tests/test_cli_permutations.py` is ~4-5x slower on macOS/c0dev because every `pyfakefs.fake_open` call extracts a stack trace, which triggers `linecache.updatecache` and real `posix.stat` calls; macOS `stat` latency is ~0.7 ms versus ~2.5 microseconds on Linux.                                   | [gf-testing.md](gf-testing.md) | unseeded | documented  | absorb by reducing `open()` calls and caching `_get_version()` / `ArgumentParser`                                       |
| GF-TRB-3 | 2026-09-25T20:12:46Z | diagnostics-deferral | A parent-repo `git clean -fdx` removes `<root>/.gf` even when it is ignored, deleting every repo store and checkout with their uncommitted work, unpushed commits, local branches, and stashes.                                                                                                          | [gf-spec.md](gf-spec.md)       | unseeded | documented  | dry-run exact targets and matches; exclude anything inside or containing `<root>/.gf` or a consumer path                |
| GF-TRB-4 | 2026-09-25T20:12:46Z | diagnostics-deferral | A physical-path `cd -P` from a consumer link lands inside `<root>/.gf/wt`, where files appear at repository-relative paths rather than the mapped subdir.                                                                                                                                                | [gf-spec.md](gf-spec.md)       | unseeded | documented  | covered by realpath target selection back to the owning binding                                                         |
| GF-TRB-5 | 2026-09-25T20:12:46Z | diagnostics-deferral | `git worktree unlock` then `prune` run against a repo store can drop a `gf` checkout's worktree record.                                                                                                                                                                                                  | [gf-spec.md](gf-spec.md)       | unseeded | documented  | `gf pull` re-locks unlocked checkouts and reports a checkout without its record as an error with recovery steps         |
| GF-TRB-6 | 2026-09-25T20:12:46Z | diagnostics-deferral | A server without `uploadpack.allowFilter` refuses `fetch --filter=blob:none` for a repo store.                                                                                                                                                                                                           | [gf-spec.md](gf-spec.md)       | unseeded | documented  | `gf` falls back to a full fetch with a warning                                                                          |
| GF-TRB-7 | 2026-09-25T20:12:46Z | diagnostics-deferral | URL resolution cannot find the repository boundary of a private remote that needs an interactive login, because probes run with terminal prompts disabled.                                                                                                                                               | [gf-spec.md](gf-spec.md)       | unseeded | documented  | write `.git` after the repository name to mark the boundary                                                             |
| GF-TRB-8 | 2026-09-25T20:12:46Z | diagnostics-deferral | A trailing-slash `.gitignore` pattern does not match a subfolder binding's consumer symlink, so the parent lists the link as untracked.                                                                                                                                                                  | [gf-spec.md](gf-spec.md)       | unseeded | documented  | ignore the consumer path without a trailing slash, as `gf` recommends                                                   |
| GF-TRB-9 | 2026-09-30T03:56:00Z | diagnostics-deferral | `gf clone` of a subfolder whose directory name starts with `!` (e.g. `app/!foo`) died on git ≥ 2.36 — `git sparse-checkout set --cone` rejected a `!`-leading operand while `gf` added `--skip-checks` only for names containing `*?[]\`; git 2.35's cone mode has no operand check and accepts the name. | [gf-spec.md](gf-spec.md)       | unseeded | **fixed** | absorbed: the member predicate now flags a `!`-bearing operand (operand-leading or a `!`-leading `/` segment) for `--skip-checks`, covering git's full sanitize set |
| GF-TRB-10 | 2026-09-30T06:22:35Z | diagnostics-deferral | `gf log`/`gf diff` suppress the subfolder scope when a separately spelled option value looks pathspec-like — `--grep fix.*` (a space between flag and value) is mistaken for a path operand and drops the `-- .` default, while `--grep=fix.*` stays scoped; there is no option-arity table for passthrough commands | [gf-spec.md](gf-spec.md) | unseeded | documented | spell values carrying `*?[`, a leading `:`, or naming an existing path with `=` (`--grep=fix.*`), or separate options from paths with an explicit `--` (`gf log -- <paths>`) |
| GF-TRB-11 | 2026-09-30T12:22:52Z | diagnostics-deferral | `gf` took no inter-process lock: concurrent invocations on one parent interleaved store, manifest, and state writes last-writer-wins — a completed clone could leave its store, checkout, and consumer link fully materialized while its `git_folder` entry was silently dropped, invisible to `ls`/`status`/`rm`. | [gf-spec.md](gf-spec.md) | unseeded | documented | closed by the admitted `GF-D20` design ([gf-arch.md](gf-arch.md#decision-register)): mutating commands serialize on `flock` of `<git common dir>/gf.lock` — the lock lands with the P3 phase; until it does, run no concurrent mutating `gf` commands on one parent |
| GF-TRB-12 | 2026-09-30T23:51:04Z | diagnostics-deferral | A parent repository that tracks `.gf` lets the parent's own `git pull` rewrite gf-managed store plumbing beyond hooks and origin — committed `.gf` content lands over repo-store config, refs, or objects, which the bound-upstream root-`.gf` refusal, `core.hooksPath=/dev/null`, and the store-origin match do not cover. | [gf-spec.md](gf-spec.md) | unseeded | documented | the precondition is the documented must-not — do not track `.gf` in the parent (keep the `.gf/` `.gitignore` recommendation); full closure needs a tracked-`.gf` refusal, e.g. a `git ls-files .gf` probe at parent operations |
| GF-TRB-13 | 2026-10-01T02:28:05Z | diagnostics-deferral | A whole-repo `gf rm` reconciles each registered linked-worktree gitfile to the moved `child/.git` before the move and refuses when one cannot be reconciled; a post-move rewrite failure reports the exact path and leaves a straggler gitfile pointing at the old `.gf/git` location. | [gf-spec.md](gf-spec.md) | unseeded | documented | recover a straggler with `git worktree repair <worktree-path>` in the converted child; retires if reconciliation covers post-move failures |
| GF-TRB-14 | 2026-10-01T02:28:05Z | diagnostics-deferral | A pre-existing `+`-forced `remote.origin.fetch` line whose destination lands outside `refs/remotes/origin/*` — legacy state an older `gf` could write, such as a forced tag line — refuses a fetch or store operation as unsafe rather than being rewritten or executed. | [gf-spec.md](gf-spec.md) | unseeded | documented | the user repairs the line deliberately with their own `git config` in that gitdir; retires if a `gf` repair path is admitted |

## `gf clone <url>` says `url` is required

`gf clone` syntax is `gf clone <url> [<path>] [-n <name>] [-b <ref>]`. The first positional is the URL. If you used the old `<name> <url>` order, swap them.

## `gf pull` fails with `fatal: repository 'abc' does not exist`

The git-folder `url` is a local placeholder from `gf init`. `gf pull` skips placeholders; if it tries to clone one, the URL normalization is wrong. Update `gf.toml` `url` to a real upstream.

## `gf ls` shows `[]` for branch

The child is in a detached `HEAD`. This is expected for tag or commit refs. A child with no commits yet, for example one just created by `gf init`, also shows `[]`; that is a normal state, not a detached `HEAD`. For `latest` or branch refs the checkout should be attached to a local tracking branch — `gf` creates it at `origin/<branch>` when absent and integrates `origin/<branch>` fast-forward-only when it exists; `gf` never uses `checkout -B` or `checkout -f`, so a branch ref left detached is a defect to report. If the remote has no default branch, the clone may be empty.

## `gf status` is empty

`status` is a quiet success when no manifest exists or no git-folders match the current context. `ls` is global; use it to verify the manifest is not empty.

## `gf rm` deleted my worktree

For a whole-repo child, `gf rm` must only move `child/.gf/git` to `child/.git` — an immediately usable repository with `HEAD`, index, refs, config, and `origin` intact, and any `git worktree` entries registered in it reconciled to the new path. If it deletes more, file a bug. It should preserve all user files.

## `gf rm` left a `.gf` directory behind

On a whole-repo `gf rm`, `gf` removes only provably `gf`-owned `.gf` content — its `state` bookkeeping — after moving `child/.gf/git` to `child/.git`, and removes `.gf` itself only when nothing remains. Anything else under `.gf` is foreign content: it is retained and reported, never swept. Inspect what remains and delete it yourself once it is unwanted.

## `gf rm` refuses over a registered worktree

A whole-repo child's gitdir can have `git worktree` entries registered under `worktrees/<n>/`. Before moving `child/.gf/git` to `child/.git`, `gf` reconciles each record's external `.git` gitfile — it must be writable and currently name the old `.gf/git/worktrees/<n>` path — and refuses before the move when a record cannot be reconciled, leaving the binding gf-managed and nothing moved. Make the named gitfile writable or remove the stale registration (`gf git worktree prune`, or `gf git worktree remove` — plain `git` inside the child sees the parent, not the `.gf/git` gitdir), then re-run `gf rm`. If the move itself succeeded but a post-move rewrite failed, `gf` reports the exact unresolved path; run `git worktree repair <worktree-path>` in the converted child — now an ordinary repository — to fix the straggler.

## My subfolder disappeared after `gf rm`

For a subfolder binding, `gf rm` removes only the link at the consumer path and the manifest entry. The checkout under `<root>/.gf/wt`, including uncommitted work, is untouched. To reconnect that checkout, use its effective URL, effective ref, and consumer path, accounting for both URL and ref overrides in `gf.local.toml`; preserve the original name with `-n` when needed for those overrides. For an original `latest` ref, pass the branch that `latest` resolved to before removal, since the remote default may have changed; for a branch ref, preserve that branch name; for a tag or commit, preserve the pinned ref. For example: `gf clone -b <resolved-branch-or-pinned-ref> -n <original-name> <effective-url> <path>`. Omitting `-b` selects the current default ref and can choose a different checkout.

## `gf pull` recreated everything after `git clean -fdx`

`git clean -fdx` in the parent repo deletes untracked files and ignored files alike, so ignoring `.gf/` does not protect it: every repo store and checkout for subfolder bindings is destroyed. The next `gf pull` can rebuild them from the remotes, but anything that existed only locally is gone — uncommitted changes, unpushed commits, local branches, and stashes. A pull rebuilds the sparse cone for the selected bindings' subdirs only; a binding outside that selection rematerializes on its own later pull. Whole-repo children under ignored paths share the same hazard. Clean only explicitly reviewed generated paths. Neither a target nor any expanded match may be inside or contain the root `.gf` storage or any consumer path. For example, first run `git clean -ndx -- <generated-paths>` and inspect each reported target and all its descendants (Git can summarize an ancestor as `Would remove vendor/`); only repeat with `git clean -fdx -- <same-generated-paths>` after confirming every target and match is disposable. A bare parent-root `git clean -fdx` or `git clean -fd` is not safe: either may remove consumer work, and `-fd` also removes unignored paths.

## I ran `cd -P` and landed inside `.gf/wt`

`cd -P` resolves symlinks: from a subfolder consumer link it lands at the physical checkout under `<root>/.gf/wt/<repo-key>/<checkout-key>/<subdir>`. Files there appear at repository-relative layout, not just the mapped subdir. `gf` commands still select the owning binding via realpath matching, so `gf status`/`pull`/`git` work correctly; run them from the consumer path when you want the mapped view.

## `git worktree prune` ran against a repo store

`gf` locks every checkout under `.gf/wt`, so `git worktree prune` skips them and the checkouts survive. If a checkout was unlocked, the next `gf pull` locks it again. If a checkout was unlocked and then pruned, git no longer has its worktree record; `gf pull` stops with an error for that checkout rather than rebuilding over its files. Save any work in that directory, delete it, and run `gf pull` to create a fresh checkout.

## `gf` refuses an unsafe fetch refspec

A fetch may move only `gf`'s remote-tracking mirror: force (`+`) is permitted only on a `remote.origin.fetch` line whose destination is `refs/remotes/origin/*`. A pre-existing `+` line landing outside that namespace — a forced tag or branch line an older `gf` could write into a child gitdir (`<child>/.gf/git/config`) or a repo store (`.gf/repos/<repo-key>/git/config`) — would move user-owned refs on every fetch, so `gf` refuses before fetching and reports the unsafe line. `gf` never rewrites the line itself: repair it deliberately with your own `git config` in that gitdir — for example `git --git-dir <gitdir> config --unset-all remote.origin.fetch`, then `--add` the replacement coverage (`+refs/heads/*:refs/remotes/origin/*` or a per-branch mirror line; tag coverage carries no `+`: `refs/tags/<t>:refs/tags/<t>`) — and re-run the command.

## `gf clone <url>/<dir>` fails with `fatal: couldn't fetch ...`

The upstream server refused the partial clone (`fetch --filter=blob:none`) because it lacks `uploadpack.allowFilter`. `gf` falls back to a full fetch and warns; the clone succeeds but transfers all blobs. Enable `uploadpack.allowFilter` on the server, or accept the larger store — it is still shared by all bindings of that repository.

## `gf clone <url>/<dir>` says it cannot find the repository

`gf` finds where the repository ends by asking the server with `git ls-remote`, with password prompts switched off. A private remote that needs an interactive login answers none of those probes. Write `.git` after the repository name, e.g. `https://host/org/repo.git/docs/api`, and `gf` splits there without asking the server.

## `gf clone` of a `!`-leading subfolder dies on git ≥ 2.36 (fixed)

Was: on git 2.36 and newer, `git sparse-checkout set --cone` refused a directory operand that starts with `!` and died with `specify directories rather than patterns. If your directory starts with a '!', pass --skip-checks`. `gf` added `--skip-checks` only when a member name contains glob characters (`*?[]\`), so a subfolder binding with an operand-leading `!` like `!foo` got no flag and the clone aborted with that raw git error (a mid-path `!` segment such as `app/!foo` was accepted natively); on git 2.35 cone mode has no operand check and the same clone succeeds. The same predicate gated every cone update, so a `gf pull` widening a shared checkout to such a member failed the same way.

Fixed: the member predicate now flags a `!`-bearing operand — operand-leading or any `!`-leading `/` segment — for `--skip-checks` exactly as glob characters do, so `!foo` and `app/!foo` mappings clone and widen cleanly. The git 2.35 retry-without-flag path is unchanged. This entry is retained as history; it no longer needs a workaround.

## `gf log`/`gf diff` option values can suppress the subfolder scope

For a subfolder binding, `gf` appends `-- .` to `log`/`diff` unless the trailing args contain `--` or a pathspec-like operand (an existing path under the mapped directory, or one carrying `*`, `?`, `[`, or a leading `:`). The scan reads tokens, not options: a separately spelled value such as `gf log --grep 'fix.*'` occupies its own token, carries `*`, and disables the default scope, so the command runs over the whole repository. The `=` spelling (`--grep=fix.*`) keeps the value inside the option token and stays scoped. There is deliberately no option-arity table for passthrough commands — when a value must be spelled separately and looks pathspec-like, end options with an explicit `--` (`gf log --grep 'fix.*' -- <paths>`), or scope explicitly with `gf log -- <paths>`.

## `gf` commands run concurrently on one parent (documented)

Admitted fix, landing with the P3 phase: mutating `gf` commands — `clone`, `init`, `pull`, `rm`, `worktree add`, `worktree remove` — serialize on a `flock` of `<git common dir>/gf.lock` (`GF-D20`), held exclusively from before the first planning read through command end. One lock covers every worktree of the parent. A second writer waits on the blocking lock; when the lock cannot be acquired at all, the command fails fast and names the contended resource. The lock is descriptor-held, so a killed `gf` releases it — no stale lock file survives. Read commands (`status`, `ls`, `worktree list`) and the passthroughs (`sh`, `git`, `diff`, `log`) take no lock.

Was: `gf` took no inter-process lock, so two invocations on one parent interleaved store, manifest, and state writes last-writer-wins; a completed clone could leave its repo store, checkout, and consumer link fully materialized while its `git_folder` entry was silently dropped — invisible to `gf ls`, `gf status`, and `gf rm`. That remains the installed baseline until P3 lands the lock: the workaround is to run no concurrent mutating `gf` commands on one parent. A binding orphaned by a pre-lock race is still recovered by re-running its `gf clone`/`gf init`: the add rejoins the orphaned storage with uncommitted work intact; if the orphan is unwanted instead, remove `.gf/repos/<repo-key>` and `.gf/wt/<repo-key>` by hand.

## A subfolder link shows up in `git status` of the parent

A subfolder binding's consumer path is a symlink, and a `.gitignore` pattern with a trailing slash (`vendor/api/`) does not match a symlink. Use `vendor/api` without the slash. The same applies to links `gf worktree add` places in a second parent worktree.

## `gf pull` aborts on dirty worktree

`gf pull` refuses to update over uncommitted work — the refusal exits `3` and discards nothing. You can:
- commit the child changes first, or stash them yourself (`gf -C <child> git commit`, `gf -C <child> git stash`),
- use `gf pull --autostash` to `stash push -u`, update, and `stash pop --index` the changes back — the index partition (staged versus unstaged) is restored too — the restore runs after a successful update and after every failure that follows the stash,
- or, when the changes are genuinely unwanted, discard them yourself with `gf git`/`gf sh`. `gf` has no discard mode: there is no `gf pull --force`.

## `gf pull` refuses a diverged branch

When the local tracking branch and `origin/<branch>` each carry commits the other lacks, `gf pull` refuses (exit `1`) without moving anything and reports the truthful ahead/behind state. Resolve the diverged histories with an explicit `gf pull --rebase` or with Git integration you control; Git's rebase conflict handling applies, and `git rebase --abort` unwinds a rebase while retaining the original local history. Committing or stashing prepares dirty worktrees, but does not resolve already diverged history. A strictly-ahead branch is not a refusal: `gf pull` leaves its local commits untouched. `--rebase` is meaningless for a tag or commit ref and is ignored there.

## `gf pull --autostash` could not restore my changes

`--autostash` stashes with `git stash push -u -m "gf autostash"` before the update and restores with `git stash pop --index` afterwards — on success and on every update failure that follows the stash, so a taken stash is never orphaned. When the pop itself conflicts or fails, `gf` keeps the named stash entry and reports the state rather than dropping the work. Recover inside the child: when the pop applied with conflicts, resolve the conflicted paths and run `gf git stash drop` once the content is landed; when the pop never applied, clear the obstruction and run `gf git stash pop --index`.

## `gf pull` reports commits would be stranded

Before any checkout that would move `HEAD` off commits the new position cannot reach — attaching or switching branches, detaching at a tag or commit, a rebase — `gf` records the outgoing `HEAD` under the checkout's `refs/worktree/gf-retained` ref so those commits stay durably reachable, never only through the reflog. When `gf` cannot establish that durable reachability it refuses before the checkout and reports the commits that would be stranded. To recover commits a transition left there, inspect `gf git log refs/worktree/gf-retained` and keep them under an ordinary ref — `gf git branch <name> refs/worktree/gf-retained` — or cherry-pick them.

## `gf pull` was interrupted

A killed or failed `gf pull` retains its partial effects instead of sweeping them: children already updated stay updated, manifest and state writes are atomic renames so nothing is torn, and the error report names which bindings were updated, skipped, or refused. Re-running `gf pull` is the recovery — the next run classifies what it finds: a missing link or checkout is re-materialized under the binding's recorded identity, a mid-operation merge or rebase refuses a stacked operation until you finish or abort it (`gf git rebase --continue`/`--abort`, `gf git merge --abort`), and a `gf autostash` entry left in the stash list is restored with `gf git stash pop --index`.

## `gf clone github.com/cursor/plugins` creates `https://github.com/cursor/plugins`

This is correct. Bare host/path URLs are expanded to `https://` for `git clone`. The `name` and consumer `path` are still the repo basename.

## Column alignment looks wrong

`ls` and `status` compute column widths per run. If output still looks unaligned, ensure you are using the current version and the terminal uses a fixed-width font.

## Tests touch files outside `tests/fixtures/tmp/`

The `tmp_path` fixture is overridden in `conftest.py`. If a test writes outside its fixture, it is a test bug. Report it.

## Undoing common operations

`gf` keeps partial side effects recoverable when a command fails: rollback removes only artifacts that invocation provably created, and anything pre-existing, foreign, or unprovable is retained and reported. The safe undo for each command is:

- `gf clone` that fails before completion: `gf` removes only what that invocation provably created — a fresh child directory, or the `.gf/` it added to an existing child. Anything pre-existing or unprovable is retained and reported; if a leftover child directory is unwanted, delete `child/` and run `git checkout gf.toml` to restore the manifest.
- `gf init` that fails before completion: the `.gf/` directory is removed from the child. If the manifest was written, run `git checkout gf.toml`.
- `gf rm`: a whole-repo child is converted back to a normal git worktree (`child/.git` replaces `child/.gf/git`, with `HEAD`, index, refs, `origin`, and registered `git worktree` entries intact); a subfolder binding loses only its consumer link. If the manifest change was not applied, re-add the git-folder.
- `gf worktree add` that fails after the parent worktree is created: the just-created worktree is removed automatically (`git worktree remove --force`, retried as `--force --force` on failure — a locked worktree needs the doubled force); fix the reported cause and re-run. When even the doubled removal reports failure, `gf` re-checks the worktree's registration and prints the true remedy — nothing extra when the worktree is actually gone, `git worktree prune` when it is still registered but its path is gone, or `git worktree remove --force <path>` when it is still on disk. `-f` replaces a leftover link or file at a child path but refuses a real directory — such as tracked content materialized at a `gf init` consumer path — which must be moved, untracked, or given another `path` before the add can succeed.
- `gf pull` of an existing child that fails mid-checkout: partial effects are retained, not swept — the child worktree may be partially updated, an in-progress merge or rebase is left for you to finish or abort (`gf git rebase --abort`, `gf git merge --abort`), a stranded `gf autostash` stays in the stash list until `gf git stash pop --index`, and commits a transition would have orphaned remain reachable at `refs/worktree/gf-retained`. Inspect with `gf git status` and `gf git log`, then re-run `gf pull` to let it classify the state.
- A `gf.toml` that fails validation — unreadable, invalid TOML, a wrong-shaped entry, or a `path` that violates the manifest path rules (must be relative, non-empty, normalized not `.`/`..`/`../`-leading, no `.gf` or `.git` segment) — blocks every `gf` command, `gf rm` included: the check fails closed. Restore the manifest with `git checkout gf.toml` or fix the offending entry, then re-run; `gf rm` cannot unregister around it.
