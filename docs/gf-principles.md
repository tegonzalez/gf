---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-engineering-principles]; otherwise, do not modify."
---

# git-folders Engineering Principles

## Git-like UX

`gf` should feel like a natural extension of git. Commands mirror git where possible (`clone`, `init`, `pull`, `status`, `ls`, `rm`), support `-C <path>`, and use git semantics for discovery, refs, and branches.

## Parent/child history isolation

The parent repository must remain independent of git-folders. Git-folder files must not appear in parent history. The child must be a real git repository with its own `.gf` metadata, not a nested `.git` directory that confuses tools.

## Self-contained child gitdirs

Each child has its own full gitdir under `child/.gf/git`. Multiple parent worktrees share a git-folder through relative symlinks to the source child, while each consumer worktree keeps independent `HEAD`, `index`, and branch state.

## Deterministic, hermetic tests

The test suite must run without network access and without touching the real filesystem outside `tests/fixtures`. `pyfakefs` and `MockGitBackend` make the CLI permutation tests fast and deterministic.

## Spec-first, code-second

Behavior lives in `docs/gf-spec.md` before it lives in `src/`. Code changes should be spec-derivable.

## No silent data loss

Commands that mutate the workspace (`rm`, `pull`) must preserve user data. `rm` converts the child back to `.git`; `pull` aborts on dirty worktrees unless the user forces.

## Minimal manifest surface

The manifest carries only what is necessary to bind a git-folder: `name`, `url`, `ref`, `path`. New fields require a strong justification and explicit approval.

## Distinguish name, path, and URL

- `name` is the human identifier and the repo basename.
- `path` is the consumer directory relative to the parent root.
- `url` is the upstream location.

They may coincide, but they are conceptually distinct.

## Local overrides are first-class

`gf.local.toml` lets a developer use a fork or branch without dirtying tracked parent files. The tool must respect and surface overrides without requiring a new manifest schema.