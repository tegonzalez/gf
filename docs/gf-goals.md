---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-goals]; otherwise, do not modify."
---

# git-folders Goals

## Purpose

Define the durable outcomes and priorities for `git-folders`, a tool that lets a parent git repository manage git-folder git repositories as user-defined subfolders of the parent workspace. Each git-folder is a real git repository with its own history; the parent does not embed git-folder files in its own history.

## Terminology

| Term                | Meaning                                                                                                                                                                     |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Consumer workspace  | A parent repository worktree in which git-folders are used.                                                                                                                 |
| Git-folder          | A git-tracked corpus that is not part of the parent repository.                                                                                                             |
| Child               | A git-folder checkout in the consumer workspace.                                                                                                                            |
| Workspace binding   | A declaration in the parent manifest that maps a git-folder to a consumer path and a reference.                                                                             |
| Subfolder binding   | A workspace binding whose URL is a path to a folder inside a git-folder rather than to the whole repository.                                                                |
| Protected work      | The user's staged and unstaged changes, untracked and ignored files, local commits and refs, stashes, detached work, and the configuration and metadata needed to use them. |
| Provenance          | Usable git history, refs, index, and worktree relationships, plus the retained identity needed to find and reconnect work.                                                  |

## Goals

| ID       | Priority   | Desired outcome                                                      | Success criterion                                                                                                                                                                                                                                                                                                      |
| -------- | ---------- | -------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GF-G1`  | highest    | Parent repository history remains independent of git-folders         | The parent repository can be cloned, shared, and committed to upstream without carrying git-folder files; each git-folder keeps its own git history.                                                                                                                                                                   |
| `GF-G2`  | highest    | Git-folder storage is local and explicit                             | A git-folder's git objects and refs live inside the parent worktree: under `child/.gf/git` for a whole-repo binding, and under `<root>/.gf` for subfolder bindings. No git-folder storage is created outside the parent worktree.                                                                                      |
| `GF-G3`  | high       | Shared git-folders remain available across consumer worktrees        | Multiple `git worktree` checkouts of the same parent repository see the same git-folders through relative symlinks to the source child, without conflating each other’s in-progress changes.                                                                                                                           |
| `GF-G4`  | high       | Developers can maintain independent git-folder branches and forks    | A developer can bind a git-folder to a personal fork or branch through local configuration; the parent repository’s tracked files do not change when the binding is updated.                                                                                                                                           |
| `GF-G5`  | high       | Git-folder bindings support stable and floating references           | A workspace binding can declare a commit, tag, branch, or `latest` reference, and the tool can report drift between the bound state and the declared reference.                                                                                                                                                        |
| `GF-G6`  | medium     | Git-folders keep ordinary git branching and worktree capabilities    | A team can branch, `git worktree add`, merge, and roll back changes in a git-folder on its own cadence, independent of the parent project.                                                                                                                                                                             |
| `GF-G7`  | medium     | Composition tooling is read-only and authority-preserving by default | The tool can list, update, and report status of git-folders; it does not write to a shared git-folder except through that git-folder’s own git workflow and governance.                                                                                                                                                |
| `GF-G8`  | high       | A developer gets the same `gf` on every supported host               | The same commands and verification control produce the same results on Linux and macOS. No command behaves differently because of its host, no host requires a second git integration, and Windows is an explicit non-goal.                                                                                            |
| `GF-G9`  | high       | A repository subfolder binds as a first-class git-folder             | A workspace binding can name one subfolder of a git-folder; the consumer sees that subfolder's files at the bound path, every `gf` command behaves correctly for both binding forms, and bindings that share one repository share that repository's one object store — never a copy per binding.                       |
| `GF-G10` | highest    | `gf` operations preserve protected work and usable provenance        | After any `gf` operation — map, update, unmap, remap, or an interrupted one — every piece of the user's protected work is still present and reachable with usable git provenance; when an operation cannot complete while preserving work, `gf` refuses and reports the true state rather than discarding or guessing. |

## Goal Relationships

`GF-G1` and `GF-G2` are co-equal foundations: a parent repository cannot remain independent if it carries git-folder files, and it cannot silently scatter git-folder storage outside the parent worktree. `GF-G3` and `GF-G4` depend on those foundations because worktree isolation and per-user forks require both independent history and local, explicit storage. `GF-G5` and `GF-G6` apply the same model to versioning and git-folder-side collaboration. `GF-G7` preserves the boundary between the composition tool and the git-folders it composes. `GF-G8` is subordinate to `GF-G1` and `GF-G2`: a host is gained by removing host assumptions from the tool, never by moving git-folder storage outside the parent worktree or by carrying git-folder files in parent history. It serves `GF-G3` and `GF-G6`, because a developer who moves between hosts keeps one worktree layout and one branching workflow rather than learning a second. `GF-G9` extends the same foundations to repository subfolders: a subfolder binding carries the parent-independence and local-storage outcomes of `GF-G1` and `GF-G2`, applies the versioning model of `GF-G5`, and keeps the ordinary git workflow of `GF-G6`. `GF-G10` bounds the whole set: no outcome below it — sharing, convenience, drift reporting, or host coverage — is earned by losing protected work or provenance, so an operation that cannot preserve them refuses instead of proceeding.

## Success Criteria

- A consumer can identify every git-folder and its bound reference from one workspace manifest without reading execution history.
- A fresh agent can edit a git-folder’s files as ordinary files in the consumer workspace.
- Adding a `git worktree` to the parent repository does not duplicate or conflate git-folder content.
- A developer can switch a git-folder to a fork or branch without a commit in the parent repository.
- The tool reports drift for pinned and floating references without mutating the git-folder.
- After any `gf` operation — including a refused or interrupted one — the user's staged, unstaged, untracked, ignored, committed, and stashed work and the refs needed to reach it are all still present; no `gf` flag or mode discards user work, and unmapping then rebinding restores the same work with usable provenance.
- A git-folder can be branched and merged independently of the parent repository.
- The composition tool remains a deterministic, non-authoritative inspector by default.
- The same commands and the same verification run produce the same results on each supported host.
- A consumer can bind one repository subfolder to a path and use ordinary git workflows through it, with repository storage shared by that repository's other subfolder bindings.

## Priorities And Tradeoffs

Preservation of protected work and usable provenance outranks every convenience below it: an update, unmap, or remap that cannot complete while keeping the user's work refuses and reports rather than discarding, and disposal of work is always the user's own deliberate git action — `gf` offers no discard mode. Parent-repository independence outranks version flexibility because an upstreamable parent is the reason to avoid embedding git-folders. Local and explicit storage outranks shared-object convenience; no git-folder objects or refs are placed outside the parent worktree. Git-folder mutability stays with the git-folder; the composition tool never becomes a second committer. Host coverage ranks below all of these: `gf` earns a host by removing an assumption, not by adding a host-specific command path or a second git integration, and a host that cannot be served that way stays unsupported. Breadth of hosts is deliberately bounded — POSIX hosts are supported, Windows is not — because one implementation that both supported hosts exercise is worth more than a wider matrix in which each host exercises only its own half. Subfolder bindings rank equal to whole-repo bindings as an outcome, but not as a storage model: sharing one object store per repository outranks per-binding isolation because it keeps storage local to the parent worktree while fetching objects once per repository.
