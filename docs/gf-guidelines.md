---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-guidelines]; otherwise, do not modify."
---

# git-folders Guidelines

Guidelines are positive instructions for agents working on `git-folders`.

## Start at the spec

When changing behavior, read and update `docs/gf-spec.md` before editing code. The spec is the consumer-safe source of truth for command semantics.

## Reproduce before fixing

For bugs, write a failing test or a manual reproduction first. The test suite is the primary verification surface.

## Keep tests green

Run `uv run pytest -x` after meaningful changes. A red test suite is a blocker.

## Commit by feature

Each commit delivers one feature or one fix, complete: the spec and doc updates, the code, and the tests that prove it land together, across as many files as the feature touches. Do not split one feature into per-file or per-document commits, and do not bundle two unrelated features into one commit.

## Update the spec and tests together

When command behavior changes, update `docs/gf-spec.md`, the relevant tests, and any doc-graph-grounded docs at the same time.

## Use `MockGitBackend` for new CLI permutation tests

Add in-process tests to `tests/test_cli_permutations.py` using the mock backend for CLI-shape behavior. Use real Git when a claim depends on repository state, preservation, worktree or symlink behavior, concurrency, process output, or transport semantics; the test strategy binds each such profile to its fixture-owned process and observation boundary.

## Derive names from the basename

For `gf clone`:
- `name` is the basename of the explicit path or the URL.
- `path` is the consumer directory.
- `url` is the full upstream (local, `https://`, `git@`, or bare host/path expanded to `https://`).

Use `-n <name>` only when the derived name is wrong.

## Centralize manifest discovery

Use `_resolve(cwd)` in every command. Commands must not discover `gf.toml` independently or resolve paths outside the parent root.

## Preserve local placeholders

`gf init` writes a local placeholder `url`. `gf pull` must skip when the effective URL resolves to the child itself, and must not attempt to clone the placeholder as a remote.

## Preserve work first, refuse second

When implementing or extending a `gf`-owned operation, carry the preserve-then-refuse ordering: keep the user's staged, unstaged, untracked, ignored, committed, and stashed work reachable through the operation, and where it cannot be preserved, implement a refusal that reports the true state. Never substitute a destructive or guessing fallback, and never add a flag that waives preservation — `gf` has no discard mode. Verify a preservation claim with the real-git witness style in `docs/gf-testing.md`, not a mock-only claim.

## Make output generic and aligned

For tabular commands (`ls`, `status`), collect all rows first, then align columns with a shared helper. Do not hard-code per-command padding.

## Normalize URLs at clone and pull

Expand bare host/path URLs (e.g. `github.com/cursor/plugins`) to `https://` before storing in `gf.toml` or passing to `init_child`/`update_child`.

## Working on git-folders

When you are asked to add, fix, or refactor behavior:

1. Locate the project root (`projects/gf` under the workspace).
1. Read `docs/gf-spec.md` and `docs/gf-goals.md`.
1. For command behavior, read `docs/gf-spec.md`. For architecture, read `docs/gf-arch.md`. For testing, read `docs/gf-testing.md`.
1. Write or update the relevant failing test.
1. Update `docs/gf-spec.md` if the change affects command semantics.
1. Implement the change in `src/gf/`.
1. Run `uv run pytest -x`.
1. For clone/pull/output changes, run the verification-owned CLI preservation witness from the project root with the exact Git binary for the profile:
   ```bash
   uv run python tests/verify_preservation_cli.py --git /absolute/path/to/git
   ```
   Supply the absolute path to the installed or Git-floor executable under evaluation; the witness returns its assertions and cleans only its own fixture.
1. Stage files by name and commit the feature as one commit with a message that names the feature.