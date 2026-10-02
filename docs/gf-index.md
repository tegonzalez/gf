---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-index]; otherwise, do not modify."
---

# git-folders Authority Index

## Purpose

Route readers and agents from the `git-folders` repository root to the unique durable owners of outcomes, behavior, architecture, verification strategy, negative guidance, decision criteria, and change history without turning this index into a summary of those owners. In-flight coordination and execution selection are outside this durable index.

Reader navigation here consults current durable owners for product and maintenance questions; it performs no operation dispatch. Start with the [README](../README.md) for install and usage and the [agent workflow](gf-guidelines.md#working-on-git-folders) for the numbered change procedure. This index does not select in-flight coordination.

## Authority Basis

| Authority ID  | Priority | Class                   | Surface                                    | Scope                                                                                  | Dimensions                                                  | Disposition                                  | Closure                                               | Expansion                                                   |
| ------------- | -------- | ----------------------- | ------------------------------------------ | -------------------------------------------------------------------------------------- | ----------------------------------------------------------- | -------------------------------------------- | ----------------------------------------------------- | ----------------------------------------------------------- |
| `GF-AUTH-001` | 90       | `dc-doc-goals`          | [Project goals](gf-goals.md)               | Durable outcomes, priorities, success criteria, and their tradeoffs                    | alignment; goal-priority; outcome-tradeoffs                 | noncompeting · priority source               | `GF-AUTH-001`                                         | new durable outcome or priority relation                    |
| `GF-AUTH-002` | 60       | `dc-doc-spec`           | [Product specification](gf-spec.md)        | Current command semantics, manifest model, physical layout, algorithms, public surface | behavior; command-semantics; manifest-model; layout         | noncompeting · behavioral owner              | `GF-AUTH-002`; mechanism: `GF-AUTH-002 → GF-AUTH-003` | new consumer-visible behavior or command surface            |
| `GF-AUTH-003` | 45       | `dc-doc-design`         | [Architecture](gf-arch.md)                 | Module inventory, seams and contracts, data flow, and the decision register            | architecture; modules; seams; rationale                     | noncompeting · mechanism owner               | `GF-AUTH-002 → GF-AUTH-003`                           | new module, seam, or load-bearing decision                  |
| `GF-AUTH-004` | 95       | `dc-doc-constraints`    | [Constraints](gf-constraints.md)           | Hard negative rules with detection signals and replacements; the stop-and-ask boundary | negative-guidance; failure-detection; replacement           | noncompeting · negative-guidance owner       | `GF-AUTH-004`                                         | new reusable failure pattern with detection and replacement |
| `GF-AUTH-005` | 95       | `dc-doc-guidelines`     | [Guidelines](gf-guidelines.md)             | Positive workflow and authoring guidance for agents working on `gf`                    | positive-conduct; workflow                                  | noncompeting · positive-guidance owner       | `GF-AUTH-005`                                         | new reusable workflow rule                                  |
| `GF-AUTH-006` | 80       | `dc-doc-eng-principles` | [Engineering principles](gf-principles.md) | Source-independent engineering decision criteria and tradeoff basis                    | engineering-criteria; tradeoff-basis                        | noncompeting · decision-criteria owner       | `GF-AUTH-001 → GF-AUTH-006`                           | new reusable decision criterion or counterpressure          |
| `GF-AUTH-007` | 20       | `dc-doc-test-strategy`  | [Testing strategy](gf-testing.md)          | Reusable verification model, acceptance mapping and harness boundaries                 | verification-strategy; evidence-mapping; harness-boundaries | noncompeting · verification owner            | `GF-AUTH-002 → GF-AUTH-007`                           | new verification boundary or evidence-mapping obligation    |
| `GF-AUTH-008` | 40       | `dc-doc-known-issues`   | [Known issues](gf-troubleshooting.md)      | Admitted residual issues, diagnostics deferrals, and undo procedures                   | residual-issues; diagnostics; recovery                      | noncompeting · residual owner                | `GF-AUTH-002 → GF-AUTH-008`                           | new admitted residual or diagnostics deferral               |
| `GF-AUTH-009` | 15       | `dc-doc-release-notes`  | [Changelog](gf-changelog.md)               | Accepted release and change history                                                    | change-record; release-history                              | noncompeting · change recorder               | `GF-AUTH-009`                                         | new accepted change entry                                   |
| `GF-AUTH-010` | 20       | `dc-doc-readme`         | [README](../README.md)                     | Install, quickstart, and feature and command summary                                   | reader-entry; quickstart                                    | noncompeting · reader help                   | `GF-AUTH-010 → GF-AUTH-002`                           | new supported install or usage path                         |
| `GF-AUTH-013` | 10       | `implementation`        | `src/gf/` · `tests/`                       | Delivered product artifacts: realized command behavior and the suite that exercises it | realized-behavior; suite-evidence                           | referent · evaluated against admitted owners | `GF-AUTH-002 → GF-AUTH-003 → GF-AUTH-013`             | new realized behavior against admitted criteria             |

`Priority` is a declared integer `0` through `100` owned by this index's Authority Basis; `100` is more governing. Comparison of competing claims with matching scope and applicability uses that declared number; equal integers escalate rather than select an owner. Class, surface, path, recency, table position, prose confidence, token count, implementation behavior, and test output do not rank. First-fill seeds come from the Authority Basis reader policy's seed table: `GF-AUTH-001` through `GF-AUTH-005`, `GF-AUTH-007`, and `GF-AUTH-013` use seeds. Declared non-seed priorities: `GF-AUTH-006` at 80 because reusable decision criteria govern tradeoffs below outcomes and above behavior; `GF-AUTH-008` at 40 because admitted residuals remain subordinate to specified behavior and design; `GF-AUTH-009` at 15 because a change record reports accepted work and governs nothing; `GF-AUTH-010` at 20 because it is subordinate reader help.

A missing owner gates only an authority question routed by this index; a nearby or historical document is not a substitute.

The owner roster preserves each listed responsibility and declared priority. This index asserts owner coverage, not product conformance or completed preparation. Goals-to-principles, specification-to-design, and specification-to-test or known-issues sequences are dependency closures, not competing owners. An unequal-integer content conflict returns a separate lower-owner follow-up without editing the higher owner, changing a declared Priority, or rewriting this index as a conflict-resolution side effect.

This table is an authority-question roster, not a file-membership or repository-retention rule. Absence from it produces no conclusion about a file's existence, mutation, retention, or deletion.

## Authority Tree

| Scope         | Parent | Kind | Authority index |
| ------------- | ------ | ---- | --------------- |
| `git-folders` | none   | tool | this index      |

This index is the single root `dc-doc-index` for the `git-folders` work-product scope. No subordinate component index exists.

## Query Routes

| Question                                     | Kind    | Ordered owners                                                 | Output                                                               | Missing-input behavior                        |
| -------------------------------------------- | ------- | -------------------------------------------------------------- | -------------------------------------------------------------------- | --------------------------------------------- |
| What outcome must `gf` achieve?              | direct  | project goals                                                  | Desired outcome, priority, and success criterion                     | Report the missing goals owner                |
| What must a `gf` command do?                 | direct  | product specification                                          | Command semantics, layout, manifest model, algorithm, or error rule  | Report the missing specification owner        |
| How is that behavior produced?               | derived | product specification → architecture                           | Mechanism, module, and rationale linked to named behavior            | Gate on the first missing owner               |
| What is prohibited, and what replaces it?    | direct  | constraints                                                    | Failure pattern, detection signal, and replacement                   | Report that no negative-guidance owner exists |
| How do I work on `git-folders`?              | direct  | guidelines                                                     | Numbered workflow and positive conduct                               | Route exact obligations to the owning surface |
| Which criterion decides this tradeoff?       | direct  | engineering principles                                         | Applicable criterion, rationale, and counterpressure                 | Report the missing principles owner           |
| How is behavior verified?                    | derived | product specification → test strategy → verification assets    | Test levels, profiles, acceptance mapping, and harness boundaries    | Gate on the first missing owner               |
| Is this symptom a known residual?            | direct  | known-issues register                                          | Admitted residual, restatement, owner, and disposition               | Report that no register entry exists          |
| What changed and when?                       | direct  | release notes                                                  | Accepted change record and release history                            | Report the missing release-notes owner          |
| What does the delivered code actually do?    | derived | product specification → architecture → implementation referent | Realized behavior as evidence evaluated against the governing owners | Evidence, never a source of intended behavior |

## Durable Boundary

The authority basis consists only of the listed durable owners and the artifacts they route to. `docs/gf-skill.md` is host skill packaging without doc-graph guard intent: it remains a reader aid outside this basis, and its location gives it no lifecycle value or authority. Active-work detail — implementation order, in-flight findings, per-run evidence — has no durable owner in this index.
