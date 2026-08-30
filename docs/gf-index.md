---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-index]; otherwise, do not modify."
---

# git-folders Documentation Index

This index routes the durable and temporal documentation for the `git-folders` git-folder repository manager.

## Durable product docs

| Path                                           | Class                           | What it covers                                         |
| ---                                            | ---                             | ---                                                    |
| [README.md](../README.md)                      | `dc-doc-readme`                 | Quickstart and common commands                         |
| [gf-goals.md](gf-goals.md)                     | `dc-doc-goals`                  | Durable outcomes and priorities (GF-G1..GF-G7)         |
| [gf-spec.md](gf-spec.md)                       | `dc-doc-spec`                   | Canonical command reference and algorithms             |
| [gf-arch.md](gf-arch.md)                       | `dc-doc-design`                 | Architecture: cli, manifest, state, backends, mock git |
| [gf-constraints.md](gf-constraints.md)         | `dc-doc-constraints`            | Hard "do not" rules for agents and contributors        |
| [gf-guidelines.md](gf-guidelines.md)           | `dc-doc-guidelines`             | Positive workflow guidance for agents                  |
| [gf-principles.md](gf-principles.md)           | `dc-doc-engineering-principles` | Source-independent engineering decision criteria       |
| [gf-testing.md](gf-testing.md)                 | `dc-doc-test-plan`              | Verification strategy and mock-backend contract        |
| [gf-troubleshooting.md](gf-troubleshooting.md) | `dc-doc-known-issues`           | Common issues and resolutions                          |
| [gf-changelog.md](gf-changelog.md)             | `dc-doc-release-notes`          | Release and change history                             |

## Temporal docs

| Path                                          | Class             | What it covers                                  |
| ---                                           | ---               | ---                                             |
| [gf-progress.md](../.planning/gf-progress.md) | `dc-doc-progress` | Handoff document for the next agent; start here |

## Agent workflow

- The numbered agent workflow is in [gf-guidelines.md#working-on-git-folders](gf-guidelines.md).
- Workspace-level agent conduct lives in `projects/devin/.devin/roles/constraints.md` and `projects/devin/.devin/roles/guidelines.md`.
- git-folders-specific agent guidance is in [gf-constraints.md](gf-constraints.md), [gf-guidelines.md](gf-guidelines.md), and [gf-principles.md](gf-principles.md).