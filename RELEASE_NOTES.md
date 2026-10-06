---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-release-notes]; otherwise, do not modify."
---

<!-- SPDX-FileCopyrightText: 2026 Tomas Gonzalez -->
<!-- SPDX-License-Identifier: MIT -->

# git-folders Release Notes

## 2026-10-06

### Breaking Changes

- [D!] `gf pull --force` is removed; use `--autostash` to preserve dirty work, or perform any intentional discard through your own Git commands.
- [M!] Pull no longer resets existing branches to the remote tip; diverged histories refuse fast-forward integration, so request `--rebase` explicitly or integrate with Git before retrying.
- [M!] Scripts parsing `gf status --remote` must handle `ahead`, `diverged`, and dirty variants such as `behind-dirty` instead of treating every different revision as `behind` or `both`.
- [M!] `gf worktree remove --force` no longer bypasses preservation checks; retain or relocate protected work, private provenance, and owned GF storage before removing a parent worktree.
- [M!] Updates refuse legacy forced fetch refspecs outside the origin mirror; repair branch coverage to target origin's remote-tracking refs and make tag coverage non-forced before retrying.
- [M!] Managed operations scrub ambient Git repository, index, and config-source overrides and disable repository hooks; configure bindings and child repositories directly, and use explicit `gf git` or `gf sh` workflows when hooks are required.
- [M!] Upstream trees containing a root `.gf` entry are refused to protect managed metadata; choose an upstream revision without that reserved entry.
- [M!] Legacy or modified `gf init` placeholders cannot convert to subfolder bindings without proof that all Git metadata is pristine; keep their local work and map the subfolder at a new empty consumer path.

### Complete

- [N] Map repository subfolders through relative consumer links to shared sparse checkouts and one local object store per repository; subfolder bindings require Git 2.35 or newer.
- [N] Related subfolder bindings share grouped updates and per-folder status, diff, and log views, while ordinary child Git commands operate on the shared repository.
- [M] Preserve staged, unstaged, untracked, and ignored work during updates through autostash with index restoration and durable retention of outgoing history; a failed restoration keeps the stash for recovery.
- [M] Preserve Git provenance when unmapping by converting whole-repo children to ordinary repositories and retaining shared subfolder checkouts; reconnect retained subfolder work using its original effective URL and ref.
- [M] Parent worktree creation keeps bindings linked to their source and rolls back failed setup, so later source retargets propagate to linked workspaces.
- [M] Serialize mutations across a parent's worktrees and retain recoverable partial effects after interruption, preventing concurrent manifest or shared-checkout writes from dropping work.
- [M] Validate manifests, target paths, and repository identity before mutation, and report missing bindings, unborn children, and failures with actionable binding and operation context.

### In Progress

- None recorded.

### Known Issues

- Parent Git operations can still damage GF storage if metadata or consumer files are tracked or included in cleanup; keep them outside parent history and reviewed deletion targets.
- Separately spelled `gf log` or `gf diff` option values that look like paths can suppress subfolder scoping; use joined option values or an explicit `--` path separator.
- A linked-worktree metadata rewrite that fails after whole-repo unmapping can require `git worktree repair` in the converted child.
- CLI permutation tests remain slower on macOS because fixture file interception adds overhead.

## 2026-10-01

### Complete

- [N] Manage whole Git repositories as ordinary child folders through clone, local initialization, update, and unmapping, with each child keeping its own history and `.gf/git` metadata.
- [N] Version canonical bindings in `gf.toml` and use `gf.local.toml` for personal forks and refs without changing the shared manifest.
- [N] Bind branches, tags, commits, or the remote default through `latest`, with shallow and single-branch cloning options and bare host/path URL expansion.
- [N] Inspect child status and local remote-ref drift, and run diff, log, arbitrary Git commands, or a shell in the child's Git context.
- [N] Create, list, and remove parent worktrees with child folders linked from the source workspace.
- [N] Install `gf` as a uv tool or run it from source with Python 3.13 or newer on Linux and macOS, with MIT licensing and SPDX attribution.

### In Progress

- None recorded.

### Known Issues

- CLI permutation tests are slower on macOS because fixture file interception adds overhead.
