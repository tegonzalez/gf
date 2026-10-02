---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-eng-principles]; otherwise, do not modify."
---

# git-folders Engineering Principles

## Purpose

Aid engineering decisions inside `gf` where the choice between otherwise admissible alternatives is specific to this product. Generic engineering conduct — evidence, authorship, custody, review, and verification criteria — is owned by the suite's common principles corpus and enrolls per task; this document adds only the criteria a `gf` author cannot derive from it.

## Domain

Applicable to design and implementation choices within the `gf` codebase and its tests. Material exclusions: product behavior requirements are owned by [gf-spec.md](gf-spec.md), failure rules by [gf-constraints.md](gf-constraints.md), module and seam structure by [gf-arch.md](gf-arch.md), and test mappings by [gf-testing.md](gf-testing.md). A candidate principle already decided by the suite common corpus is deferred to that corpus rather than restated here.

## Principles

### Git-like UX

Prefer the git-parallel choice: commands mirror git where possible (`clone`, `init`, `pull`, `status`, `ls`, `rm`), support `-C <path>`, and use git semantics for discovery, refs, and branches. Counterpressure: where `gf` semantics genuinely diverge — `rm` converts rather than deletes — the divergence is specified behavior, not a defect.

### Parent/child history isolation

The parent repository must remain independent of git-folders. Git-folder files must not appear in parent history, and no `.git` directory or gitfile may sit where tools scanning for repositories would discover a nested repository. Counterpressure: a parent worktree deliberately shares child files through links; sharing mechanism is not isolation leakage.

### Self-contained storage inside the parent worktree

All git metadata for bindings lives inside the parent worktree — never outside it, and never in a form that masquerades as a nested repository. One repository contributes one object store no matter how many bindings reference it; bindings are views into shared objects, not independent copies. Parent worktrees share one binding's files through links rather than copies, while each worktree keeps its own checkout state.

### Hermetic-first tests

Prefer a test that runs without network and without touching the real filesystem outside its fixture; that is what makes the permutation suite fast and deterministic. Counterpressure: when the claim under test is git's own mechanics — refs, worktrees, filters, symlink resolution — use real git. The per-case mapping is owned by [gf-testing.md](gf-testing.md).

### No silent data loss

Commands that mutate the workspace (`rm`, `pull`) must preserve user data: removal never destroys the user's files or uncommitted work — a whole-repo child becomes a normal repository, and a subfolder binding's checkout is left untouched so re-adding restores it — and update refuses to overwrite uncommitted work unless the user explicitly forces it.

### Minimal manifest surface

The manifest carries only what is necessary to bind a git-folder: `name`, `url`, `ref`, `path`. Prefer carrying a new location dimension inside an existing field — a `url` is a plain path that may reach into a repository — over adding a field; new fields require a strong justification and explicit approval.

### Distinguish name, path, and URL

- `name` is the human identifier and the repo basename.
- `path` is the consumer directory relative to the parent root.
- `url` is the upstream locator: a plain path to a repository or to a folder inside one; `gf` finds the repository boundary.

They may coincide, but they are conceptually distinct. Counterpressure: coincidence is not identity — deriving one from another needs the manifest's derivation rule, not a positional guess.

### Local overrides are first-class

`gf.local.toml` lets a developer use a fork or branch without dirtying tracked parent files. The tool must respect and surface overrides without requiring a new manifest schema.
