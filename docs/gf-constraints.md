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

For a whole-repo binding, `gf rm` must only unregister the git-folder and convert the child back to a normal git repo by moving `child/.gf/git` to `child/.git`. The rest of the child directory and its contents must remain. For a subfolder binding, `gf rm` removes only the consumer link and the manifest entry; it must not modify anything under `<root>/.gf` — not the checkout, its uncommitted work, its sparse cone, or the repo store — so adding the binding back restores the folder as it was.

## Do not resolve `gf.toml` outside the project root

All commands must discover the parent repo and `gf.toml` through the centralized `_resolve(cwd)` helper. Do not read or write a manifest above the discovered parent root.

## Do not let integration tests touch `tests/fixtures` outside their own fixture

`conftest.py` overrides `tmp_path` to `tests/fixtures/tmp/<uuid>`. Tests must not create, modify, or delete files elsewhere in `tests/fixtures` or the real filesystem.

## Do not use the real network in tests

Tests must use `MockGitBackend` and `pyfakefs`. Real `git` subprocesses are allowed only in the integration tests under `tests/` that explicitly spin up local bare repos.

## Do not allow `gf` to become a second committer

`gf` may not `git commit` or `git push` on the user’s behalf. Git-folder changes happen only when the user runs `git` through `gf sh` or another shell with `GIT_DIR` set.

## Do not leave a detached `HEAD` when the ref is a branch

For `latest`, branch, and default-branch refs, `init_child` and `update_child` must create a local tracking branch and check it out (`git checkout -B <branch> origin/<branch>`). Only tag and commit refs may be detached.

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

A repo store under `<root>/.gf/repos/<key>/git` is shared by every checkout and binding of that repository. Rewriting or removing one of its `remote.origin.fetch` lines — for example to narrow the store to one branch after it exists — silently changes what every other checkout of that store fetches.

Detection signal: a `config remote.origin.fetch <value>` write without `--add` to a store that already exists, or a `--unset`/`--replace-all` on that key.

Instead: write the first refspec line when the store is created — the wildcard, or the single branch under `--single-branch` — and append a line with `config --add` when a later binding needs a branch no existing line covers.

## Do not create a second checkout of one branch in one repo store

Git permits one checkout of a branch per repository; a second `worktree add` of the same branch in the same store fails (`'main' is already used by worktree at …`). Two bindings of one repository on one branch must therefore be views into one checkout, not two checkouts.

Detection signal: `git worktree add` invoked for a `<repo-key>/<checkout-key>` pair that already exists under `<root>/.gf/wt/`.

Instead: key checkouts by `(repo store, checkout key)` — branch name for branch and `latest` refs, ref string for tag and commit refs — and widen an existing checkout's sparse cone when a new binding joins it; where stored, key = 'ref=' + quote(ref, safe='').

## Do not construct `.gf` layout paths outside the checkout resolver

Both binding forms resolve their `gitdir`, `work_tree`, `common_dir`, `subdir`, and state path through one checkout-resolver function. A second place that spells `".gf" / "git"` or walks `.gf/wt` structure gives the layout two owners, and the owners diverge exactly where whole-repo and subfolder bindings differ.

Detection signal: a `".gf"` or `"git"` layout literal, or a `worktrees/` path join, in `src/gf/` outside the resolver module; `grep -n '"\.gf"' src/gf/` must match only the resolver module.

Instead: put the literal layout in the resolver module (planned as `src/gf/layout.py`) and call it everywhere a path under `.gf` is needed.