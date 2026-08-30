---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-constraints]; otherwise, do not modify."
---

# git-folders Constraints

This document addresses the author implementing, extending, or reconsidering `gf`. Constraints are hard negative-knowledge rules. If a proposed change violates one of these, stop and ask.

## Do not place git-folder storage outside the parent worktree

The default object/reference store for git-folders must live inside the discovered parent worktree, under each child's `.gf/git`. The tool must never create a shared object store or other git-folder storage outside the parent worktree.

## Do not add new manifest fields without explicit approval

The manifest schema (`name`, `url`, `ref`, `path`) is intentionally minimal. Reuse existing fields before proposing new ones.

## Do not delete user worktrees

`gf rm` must only unregister the git-folder and convert the child back to a normal git repo by moving `child/.gf/git` to `child/.git`. The rest of the child directory and its contents must remain.

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

`gf ls` and `gf status` must be local-only operations. They may not call `git fetch`, `git remote`, `git ls-remote`, or any other command that requires a network connection. Network access is only allowed in `clone`, `pull`, and `worktree add`.

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