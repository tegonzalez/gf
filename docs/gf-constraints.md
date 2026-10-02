---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-constraints]; otherwise, do not modify."
---

# git-folders Constraints

This document addresses the author implementing, extending, or reconsidering `gf`. Constraints are hard negative-knowledge rules. If a proposed change violates one of these, stop and ask.

## Do not place git-folder storage outside the parent worktree

The object/reference store for a git-folder must live inside the discovered parent worktree: under each child's `.gf/git` for a whole-repo binding, and under `<root>/.gf` (repo stores and checkouts) for subfolder bindings. The tool must never create a shared object store or other git-folder storage outside the parent worktree.

## Do not add new manifest fields without explicit approval

The manifest schema (`name`, `url`, `ref`, `path`) is intentionally minimal. Reuse existing fields before proposing new ones.

## Do not delete user worktrees

For a whole-repo binding, `gf rm` must only unregister the git-folder and convert the child back to a normal git repo by moving `child/.gf/git` to `child/.git`. The rest of the child directory and its contents must remain — every staged, unstaged, untracked, ignored, committed, and stashed change included. The moved gitdir must be an immediately usable ordinary `.git`: `HEAD`, index, refs, and config intact, `origin` still pointing at the real URL, and any `git worktree` entries registered in that gitdir still resolving after the move — `gf` rewrites their path references for the new location or refuses with instructions when a record cannot be reconciled; it never leaves a `.git` that loses or misdirects a registered worktree. The remaining `child/.gf` content is `gf`'s own bookkeeping (such as `state`) and is removed with the binding's registration. For a subfolder binding, `gf rm` removes only the consumer link and the manifest entry; it must not modify anything under `<root>/.gf` — not the checkout, its uncommitted work, its sparse cone, its per-checkout state record, or the repo store — so adding the binding back restores the folder as it was. A dangling or absent consumer link still unregisters cleanly: missing linkage is never a reason to touch retained storage.

The same rule bounds `gf worktree remove`: staged, unstaged, untracked and ignored work inside the target requires refusal before unlinking or removal, even when ordinary status appears clean and even with `--force`; only verified linked-out child symlinks are excluded. Private detached history without a surviving shared name, per-worktree refs and unproven-disposable configuration/metadata also require refusal, because deleting the worktree registration would destroy their provenance. A target worktree that owns `.gf` storage — repo stores, checkouts, binding state, or a child directory carrying `.gf/git` — must be refused, since removing the worktree deletes the storage inside it and unlinking protects only children linked out. `gf` owns no discard of that storage; retiring the worktree is the user's own `git worktree remove`.

Detection signal: a removal that proceeds over protected user files, an ignored file lost on apparently clean removal, or a `worktree remove` path that proceeds while the target's `.gf` holds stores, checkouts, or state, or while a declared child inside the target carries a `.gf` gitdir — or one that unlinks through a target to reach a source.

## Do not resolve `gf.toml` outside the project root

All commands must discover the parent repo and `gf.toml` through the centralized `_resolve(cwd)` helper. Do not read or write a manifest above the discovered parent root.

## Keep test writes inside an owned fixture

`conftest.py` overrides `tmp_path` to a unique directory under `tests/fixtures/tmp/`; this is the writable root for each test by default. Tests must not create, modify, or delete files in another fixture's tree, user data, or an arbitrary filesystem location. The raw-byte filename capability case in `tests/test_runner_non_utf8.py` may allocate the unique directory returned by `tmp_path_factory.mktemp("raw-bytes")` as a candidate fallback. It may use that exact returned directory for raw-byte fixture data only when the default `tmp_path` fails its byte-preservation probe; pytest owns cleanup of the allocated candidate. No other case may use it, and a caller-supplied `--basetemp` or another unowned path is not an admitted fixture root.

Detection signal: a test writes or deletes outside its returned fixture root; the raw-byte case uses the candidate fallback for fixture data when the default root preserves the raw filename; or a test uses an unowned base such as a caller-supplied `--basetemp`.

Instead: use the per-case `tmp_path` root. For the raw-byte capability case only, allow pytest to allocate its exact `tmp_path_factory` candidate and write the raw-byte fixture there only when the byte-preservation probe fails for `tmp_path`; keep every created repository and file under that returned directory, and leave its lifecycle to pytest.

## Do not use external or unowned networks in tests

Tests must not connect to an external host or an unowned service. Real Git subprocesses may use only repositories created inside an owned test fixture. The sole protocol exception is the raw-byte refname case in `tests/test_runner_non_utf8.py`, which starts its own `git-daemon` on `127.0.0.1` with an ephemeral port to verify captured ref bytes; it creates the daemon's repositories and log inside its fixture, terminates only its daemon, and uses no credentials or Internet connection. One raw-byte refname case in `tests/test_runner_non_utf8.py` may start its own `git-daemon` on `127.0.0.1` with an ephemeral port and use Git's loopback protocol to observe the byte-preserving ref response; the case creates the daemon's repositories and log under its owned fixture root, terminates only its own daemon, and uses no Internet connection or credentials.

Detection signal: a test resolves or contacts an external host, uses a service it did not create, binds beyond loopback, reuses a fixed listener port, or leaves its daemon running or its log outside its fixture.

Instead: use fixture-local repository paths for Git integration; when the malformed-byte response itself is the criterion, use the bounded loopback profile in [the test strategy](gf-testing.md#harness-boundaries).

## Do not allow `gf` to become a second committer

`gf` may not `git commit` or `git push` on the user’s behalf. Git-folder changes happen only when the user runs `git` through `gf sh` or another shell with `GIT_DIR` set.

## Do not leave a detached `HEAD` when the ref is a branch

For `latest`, branch, and default-branch refs, `init_child` and `update_child` must leave the checkout attached to a local tracking branch. Only tag and commit refs may be detached. `checkout -B` is prohibited against an existing ref: recreating or resetting a local branch discards its local commits and any work keyed to its tip.

Detection signal: a `checkout -B`, `checkout -f -B`, `reset`, `branch -f`, or `update-ref` call that creates-or-resets or moves an existing local branch during clone, init, pull, or shared-checkout ref application — or a branch ref left detached.

Instead: when the local tracking branch does not exist, create it at `origin/<branch>` and check it out; when it exists, integrate `origin/<branch>` fast-forward-only (`merge --ff-only` semantics); when the histories have diverged, refuse and report the state. Resolve divergence with an explicit `gf pull --rebase` or Git integration controlled by the user. Committing or stashing prepares a dirty worktree; it does not resolve diverged history.

## Do not fetch the network in `ls` or `status`

`gf ls` and `gf status` must be local-only operations. They may not call `git fetch`, `git remote`, `git ls-remote`, or any other command that requires a network connection, and they never run URL resolution; they read the recorded resolution. Network access is only allowed in `clone`, `pull`, and `worktree add`.

## Do not display the consumer path as the URL in `ls` or `status`

The second column of `ls`/`status` is the git-folder `url` from `gf.toml`. The `name` is the basename; the `path` is not shown unless it is also the `url`.

## Do not make `ls` context-aware

`gf ls` lists all git-folders when run without arguments, even from inside a child. `status`, `pull`, and `rm` are context-aware; `ls` is intentionally global.

## Do not clone a local placeholder URL

`gf pull` must skip a git-folder whose effective URL resolves to the child path itself, even if the child does not exist yet. It must not create a partial child by trying to clone the placeholder.

## Do not branch on the host platform in command logic

`src/gf/shelf.py`, `src/gf/cli.py`, and `src/gf/backends.py` must contain no `sys.platform`, `os.name`, `platform.system()`, or equivalent host test. A host branch inside command logic gives that command two execution paths of which each host runs only one, so the path belonging to the other host is never exercised by the suite that gates the change. It then drifts silently until a developer on that host reports it, and the report arrives without the failing test that would have located it.

Detection signal: a match for `sys.platform`, `os.name`, or `platform.system` anywhere outside `src/gf/platform.py`. A second signal is any test that is skipped or xfailed because of the host.

Instead: express the host-dependent behavior once in `src/gf/platform.py` as a primitive with one contract — path identity or process replacement — and let the command call it unconditionally. If a needed behavior cannot be stated as one host-independent primitive, stop and ask rather than adding the branch.

## Do not wrap git, the manifest, or the `.gf` layout in the platform module

`src/gf/platform.py` adapts the host and nothing else. It must not invoke `git`, read or write `gf.toml` or `gf.local.toml`, or interpret the `.gf` layout. A platform module that grows a git call becomes a second git owner beside `GitCliBackend`, and two owners disagree about quoting, environment, and error handling exactly where behavior is hardest to test. A platform module that grows manifest or layout knowledge splits parent discovery across two modules, so `_resolve(cwd)` stops being the single answer to "which parent am I in".

Detection signal: `platform.py` importing `gf.backends`, `gf.manifest`, `gf.shelf`, or `tomllib`, or naming `git`, `gf.toml`, or `.gf` in any string it builds.

Instead: keep `GitCliBackend` the only caller of `git` and `manifest.py` the only reader of the manifest, and have them call platform primitives. A portability problem that looks like it needs a git wrapper is usually a missing primitive; add the primitive, not the wrapper.

## Do not re-root a repository subdirectory by rewriting or copying history

A subfolder binding must map the repository's real subdirectory; it must not manufacture one. Git cannot re-root a subdirectory as a worktree root, and sparse checkout filters paths without relocating them. `git subtree split`, `read-tree --prefix` exports, file copy-sync, and bind mounts all fabricate a different history or a second file population, so the consumer's `git log`, `status`, and commits would stop matching the upstream repository.

Detection signal: a `subtree`, `read-tree`, `filter-branch`, or bulk copy producing a binding's file view, or a second physical copy of one repository subdirectory under the parent worktree.

Instead: keep the subdirectory inside one sparse linked worktree of the repository's store and publish it through the consumer link.

## Do not leave a `.git` gitfile in a `gf` checkout

A linked worktree under `<root>/.gf/wt` must not keep its `.git` gitfile. A surviving gitfile lets plain `git` inside a consumer link stop at the checkout instead of resolving to the parent repository, and it lets tools that scan for `.git` treat the hidden checkout as a nested repository — the confusion `.gf` exists to prevent. `gf` addresses the checkout through `GIT_DIR`/`GIT_WORK_TREE`, so the gitfile carries no needed function.

Detection signal: a `.git` file existing at the root of any directory under `<root>/.gf/wt/`.

Instead: remove the gitfile after `git worktree add` and lock the checkout with `git worktree lock` so `git worktree prune` does not reap it; pass `--git-dir`/`--work-tree` (or `GIT_DIR`/`GIT_WORK_TREE`) on every git call that touches the checkout.

## Do not rewrite or remove fetch refspec lines on a shared repo store

A repo store under `<root>/.gf/repos/<key>/git` is shared by every checkout and binding of that repository. Rewriting or removing one of its `remote.origin.fetch` lines — for example to narrow the store to one branch after it exists — silently changes what every other checkout of that store fetches. Force (`+`) is permitted only on a line whose destination is `gf`'s own remote-tracking mirror (`:refs/remotes/origin/*`); a line landing in a user-owned namespace — `refs/heads/*` or `refs/tags/*` — carries no `+`, so a fetch never moves a local branch or tag, and a tag upstream moved surfaces as divergence instead of overwriting the user's copy.

Detection signal: a `config remote.origin.fetch <value>` write without `--add` to a store that already exists, a `--unset`/`--replace-all` on that key, or a `+`-prefixed refspec line whose destination is outside `refs/remotes/origin/` — the last applies to a whole-repo child's gitdir as well as to a repo store.

Instead: write the first refspec line when the store is created — the wildcard, or the single branch under `--single-branch` — and append a line with `config --add` when a later binding needs a ref no existing line covers: `+refs/heads/<branch>:refs/remotes/origin/<branch>` for a branch, and the non-forced `refs/tags/<ref>:refs/tags/<ref>` for a tag.

## Do not create a second checkout of one branch in one repo store

Git permits one checkout of a branch per repository; a second `worktree add` of the same branch in the same store fails (`'main' is already used by worktree at …`). Two bindings of one repository on one branch must therefore be views into one checkout, not two checkouts.

Detection signal: `git worktree add` invoked for a `<repo-key>/<checkout-key>` pair that already exists under `<root>/.gf/wt/`.

Instead: key checkouts by `(repo store, checkout key)` — branch name for branch and `latest` refs, ref string for tag and commit refs — and widen an existing checkout's sparse cone when a new binding joins it; where stored, key = 'ref=' + quote(ref, safe='').

## Do not construct `.gf` layout paths outside the checkout resolver

Both binding forms resolve their `gitdir`, `work_tree`, `common_dir`, `subdir`, and state path through one checkout-resolver function. A second place that spells `".gf" / "git"` or walks `.gf/wt` structure gives the layout two owners, and the owners diverge exactly where whole-repo and subfolder bindings differ.

Detection signal: a `".gf"` or `"git"` layout literal, or a `worktrees/` path join, in `src/gf/` outside the resolver module; `grep -rn '"\.gf"' src/gf/` must match only the resolver module.

Instead: put the literal layout in the resolver module, `src/gf/layout.py`, and call it everywhere a path under `.gf` is needed.

## Do not duplicate a git operation per binding form

Every git operation exists once and works on the `Checkout` the resolver returns; `GF-D15` in [gf-arch.md](gf-arch.md#decision-register) fixes the only places where the whole-repo and subfolder forms may differ. A second implementation of an operation for one form, such as an `_at`-suffixed twin, or a form test outside those places builds a parallel subsystem: each fix and each test reaches only one form's copy, and the copies drift apart exactly where the forms meet.

Detection signal: two functions that perform the same git operation, one per binding form; or, outside `src/gf/layout.py` and the places `GF-D15` lists, a conditional that chooses behavior by binding form, however it is spelled — for example reading `Checkout.is_store_checkout` or comparing `gitdir` with `common_dir`. A secondary text signal is a match for `grep -rn 'def .*subfolder\|_at(' src/gf/` other than the resolver's `subfolder_checkout` constructor.

Instead: write the operation once against the `Checkout` fields — `common_dir` for plumbing and for `worktree add`/`lock`, `gitdir` and `work_tree` for worktree operations — and read the form only through `Checkout.is_store_checkout` at a place `GF-D15` lists. If a needed difference fits none of those places, stop and ask.

## Do not implicitly discard or overwrite user work

Every `gf`-owned operation preserves the user's staged and unstaged changes, untracked and ignored files, local commits and refs, stashes, detached work, and the config and metadata needed to use them. `gf` must not destroy or overwrite any of it as a side effect, and must not offer a flag that does: `gf pull` has no `--force`, because discarding work is the user's own deliberate git action — `git` run through `gf sh`/`gf git`, or files deleted by hand — never a `gf` mode. The same bar covers cleanup and rollback: an operation removes only artifacts it provably created itself; unknown or ambiguous state is retained and reported. A placeholder's unborn `HEAD` and empty visible worktree do not establish disposable Git metadata: private refs, dangling objects, changed config, index or stash state must remain intact. A verified initialization snapshot, or equivalent complete pristine proof, is required before placeholder conversion.

Detection signal: `checkout -f`, `reset --hard`, `clean`, `stash drop`/`clear`, `branch -f`/`tag -f`/`update-ref` deletions or forced writes, or a `checkout -B`/`rebase` that can strand or overwrite existing work issued by `gf`-owned code; a remove/rmtree/unlink whose target was not provably created by that invocation; a pull path that proceeds over uncommitted work or a divergence without the user's explicit `--autostash` or `--rebase`.

Instead: refuse when the operation cannot complete while preserving work and report the truthful state — divergence counts, dirty files, and what exists — with condition-appropriate recoveries. For dirty work, name commit, stash, or `gf pull --autostash`; for diverged histories, name explicit `gf pull --rebase` or Git integration controlled by the user. A transition that cannot be made safe refuses rather than exposing a destructive legacy path.

## Do not erase a binding's identity when its links are missing

A dangling consumer link, a missing checkout, or a missing `.gf` anchor does not end the binding: `gf ls`/`gf status` degrade to truthful `missing`/`?` reporting, `gf rm` still unregisters the binding cleanly, and `gf pull` re-materializes the missing link or checkout or refuses per the identity rules. Missing linkage is never license to drop the manifest entry, a repo store, a checkout, or its records, and never a reason to guess which retained storage is the binding's.

Detection signal: a code path that deletes or silently skips a manifest entry, checkout record, repo store, or checkout because a consumer link or `.gf` anchor is absent or dangling; an `rm` that cannot unregister a binding whose link is missing; a read path that omits a bound child instead of reporting `missing`/`?`.

Instead: keep the manifest entry and the binding's recorded identity authoritative until `gf rm` unregisters it; re-derive links from records, never records from links.

## Do not force-move a user-owned ref

Local branches and local tags belong to the user. `gf` may fetch into its own remote-tracking mirror (`refs/remotes/origin/*`). An existing local branch may advance by the specified fast-forward integration, or by rebase only when the user explicitly requests `gf pull --rebase`; the original local history remains durably retained across rebase. Tags remain at their existing values: when upstream has moved a tag that exists locally, `gf` reports the divergence instead of overwriting the user's tag. `gf` creates immutable retention refs for protected commits before transitions that would otherwise strand them.

Detection signal: an effect that resets, clobbers, or deletes an existing local branch or tag; a tag update from a `+`-prefixed fetch refspec; or a forced fetch destination outside `refs/remotes/origin/*`. Judge the effect, not the helper's spelling: the specified fast-forward branch advance and explicitly requested rebase with retained original history are permitted; creating an immutable `refs/worktree/gf-retained-commits/<sha>` ref for a protected tip is permitted, but overwriting or deleting that ref is not; attaching `HEAD` symbolically to a branch by itself does not clobber that branch.

Instead: create user-visible local branch or tag refs only when absent, advance an existing branch by the specified fast-forward or by an explicitly requested rebase, and surface a moved tag or diverged branch as state the user resolves deliberately. Before a rebase, retain the outgoing tip under an immutable retention ref so its original history remains reachable. Do not move an existing branch outside those integration cases or in a way that discards its retained history; do not move or delete a tag.

## Do not mutate a repository other than the verified binding target

Every `gf` write must land on storage proven to belong to the selected binding: the resolved checkout's `.gf` components must resolve to themselves, a repo store's `remote.origin.url` must key-match the binding's recorded URL resolution, and a consumer path must resolve inside its owning root. A `.gf` anchor that resolves through a link, a store whose origin no longer keys to the recorded resolution, or a consumer path that escapes its owner names another repository's storage — `gf` refuses rather than reading or writing through it.

Detection signal: a `git` write (`config`, `fetch`, `checkout`, `worktree`, a ref update, `stash`) whose `git_dir`/`GIT_DIR`/`GIT_WORK_TREE` was not first verified against the binding's recorded identity, a mutation under a lock belonging to another parent's common directory, or a mutation that proceeds when an identity check cannot be proven — for example treating an unresolvable realpath as the local target.

Instead: run the resolve-to-self and recorded-identity checks and verify that the storage owner shares the invoking parent's Git common directory before any mutation and refuse on a mismatch; where the true target cannot be established, stop and report rather than operate on a guess.
