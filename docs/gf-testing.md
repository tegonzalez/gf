---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-test-plan]; otherwise, do not modify."
---

# git-folders Testing Plan

## Purpose

Select and apply this repository's durable testing strategy to the bounded work-preservation Change `DC-DOC-PLAN-007`, and carry that Change's verification obligations through acceptance: preservation-witness oracles, independent authorship, host and Git profiles, and the changed-criterion disposition of every test family.

## Orientation

The receivers are the verification authors who compose and rewrite the executable assets under `tests/`, the architecture owner and coordinator who consume phase evidence, and the independent evaluator of this preparation packet. The user's preservation decisions and the removal of `gf pull --force` are settled inputs from [the plan](../.planning/DC-DOC-PLAN-007-gf-work-preservation.md); this document selects how they are verified and does not restate product meaning.

Scope: every executable test, fixture, oracle, driver, and manual scenario that verifies `gf` under this Change. Non-goals: product semantics ([gf-spec.md](gf-spec.md)), producer design ([gf-arch.md](gf-arch.md)), coordination state ([gf-progress.md](../.planning/gf-progress.md)), and the executable assets themselves — this document owns selection and obligation, not test code, fixtures, run results, or any acceptance decision.

### Receiver contract

The verification author's task is to compose the preservation witnesses and rewrite the mapped families against admitted criteria. Authorized inputs: the product owners' amended documents ([specification](gf-spec.md), [architecture](gf-arch.md), [constraints](gf-constraints.md), [goals](gf-goals.md), [principles](gf-principles.md)), the active plan, and the archived review stimuli under `.trash/planning/2026-09-30-subfolder-remediation/review-evidence/` as donor stimuli only. Authorized actions: create and modify verification assets under `tests/`; the production surfaces `src/gf/` and `bin/gf` are read-only to this assignment. Unresolved bindings: the exact criterion clauses land when the specification, constraints, and architecture owners' amendments are admitted; `rewrite` families re-derive expected values at that admission, not from production output.

## Terminology

| Term                     | Meaning                                                                                                                                                                         |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Protected work           | Staged and unstaged changes, untracked and ignored files, local commits and refs, stashes, detached work, and the configuration needed to use them.                             |
| Provenance               | Usable Git history, refs, index and worktree relationships, plus the retained identity needed to find and reconnect work.                                                       |
| Preservation witness     | A stimulus through a real production path plus before/after observations that distinguish retained work and usable provenance from loss or wrong-target mutation.               |
| Binding form             | Whole-repo (`child/.gf/git`) or subfolder (shared repo store, sparse linked checkout, consumer link); obligations apply to both unless a clause says otherwise.                 |
| Scenario class           | `completion` (the command succeeds), `refusal` (it declines with protected state intact), or `recovery` (failure or interruption leaves retained work and a truthful report).   |
| Regression observer      | A retained family or case whose expectation is unchanged by this Change; it watches for unrelated drift.                                                                        |
| Candidate                | The exact source revision, local changes, inputs, and environment under verification.                                                                                           |
| Verification subject     | The `gf` operation exercised through its public entry point.                                                                                                                    |
| Verification component   | The production seam an obligation's evidence binds to.                                                                                                                          |
| Double boundary          | Where a test double may stand in for git: CLI-shape branches only, never preservation semantics.                                                                                |

## Change under verification

The bounded change under verification is `DC-DOC-PLAN-007`: `gf`'s own mapping, update, unmapping, and recovery operations must preserve user work and usable Git provenance across both binding forms. It supersedes this document's previous selection (repository-subfolder bindings); that selection's durable mechanics remain in force and are re-applied under [Strategy application](#strategy-application).

Admitted starting baseline: local `main` at `8488b74` with the refreshed Linux control `uv run pytest -q -x` at 885 tests, exit 0 in about 109 seconds. Baseline and evidence bindings refresh at each phase admission; a commit identifier or an old green run alone establishes no implementation readiness.

Candidate boundary: production is `src/gf/` and `bin/gf`, read-only to the verification assignment; verification assets are `tests/` — fixtures, drivers, the mock backend, and the executable cases. Host profile: Linux and macOS, each with a virtual environment created on that host. Git profile: floor 2.35 wherever subfolder features are exercised ([gf-spec.md](gf-spec.md) Boundaries: `worktree add --no-checkout`, `worktree lock --reason`, cone-mode sparse checkout, `fetch --filter`, `ls-remote`); whole-repo bindings carry no new minimum.

Affected work-product scope: the `gf` tool's complete public surface — the command set (`clone`, `init`, `pull`, `rm`, `status`, `ls`, `worktree add`/`list`/`remove`, and the `sh`/`git`/`diff`/`log` passthroughs), the manifest and per-checkout state model, and the `.gf` storage layout — plus the `tests/` verification surface.

Receiving decisions this plan serves: phase admission (architecture owner), preservation per increment (architecture owner and coordinator at receipt), integrated supported hosts (coordinator, with user-supplied macOS evidence), and whole-Change acceptance (user). Every selected obligation binds one criterion of an amended product owner; criterion provenance uses the keys in [Changed criteria](#changed-criteria).

## Changed criteria

| Criterion    | Admitted direction under governing preparation (receiving owner)                                                                                                                                                          |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `C-preserve` | `gf`-owned operations preserve protected work and usable provenance; uncertain ownership refuses (goals, constraints, principles).                                                                                        |
| `C-pull`     | Pull integrates upstream while preserving local work; `--force` is removed; fast-forward-only default; complete autostash restoration; recoverable explicit rebase (specification, constraints, architecture).            |
| `C-unmap`    | Whole-repo unmap yields an ordinary working repository; subfolder unmap retains the shared checkout and edits; re-add reconnects identified retained work without silently choosing latest (specification, architecture). |
| `C-inspect`  | Status, ls, and drift truthfully distinguish requested upstream, actual `HEAD`, ahead/behind/diverged, and dirty state; the unborn-child behavior survives (specification, architecture).                                 |
| `C-identity` | Every mutation binds a verified repository/worktree/index; only ownership-proven disposal occurs (specification, architecture).                                                                                           |
| `C-recovery` | Shared mutation is serialized; interruption retains uncertain partial work with truthful partial-effect and recovery reporting (architecture, constraints, specification error handling).                                 |
| `C-ops`      | Provision, map/remap, integrate, retarget, and unmap stay separate meanings across both forms; purge remains a deliberate user act (specification, architecture).                                                         |

These keys name the admitted directions from the plan's settled inputs. The exact clause identities bind when the specification, constraints, and architecture owners land their amendments; until then a `rewrite` family's expected values are gated on that admission.

## Test command

```bash
uv run pytest -x
```

Run this after every meaningful change. The project uses `pyproject.toml` as the pytest config and `pyfakefs` for filesystem isolation. The retained POSIX platform seam and host-independent inputs remain in force so the same suite keeps one expected result on Linux and macOS.

### Running the control

Run the control on each supported host, with a virtual environment created on that host. The dual-host requirement, the per-host virtual environment, and the pins that keep expected results host-independent are the test-seam contract in [gf-arch.md](gf-arch.md); this plan runs against that contract rather than restating it.

| Check                            | Host    | Requirement                                                                              |
| -------------------------------- | ------- | ---------------------------------------------------------------------------------------- |
| `uv run pytest -x`               | Linux   | Exit 0.                                                                                  |
| `uv run pytest -x`               | macOS   | Exit 0 using a virtual environment created on macOS.                                     |
| `gf` CLI scenario                | Linux   | A local bare repository as the upstream; no network.                                     |
| `gf` CLI scenario                | macOS   | A local bare repository as the upstream; no network.                                     |
| `gf` subfolder CLI scenario      | Linux   | A local bare repository with subdirectories as the upstream; no network.                 |
| `gf` subfolder CLI scenario      | macOS   | A local bare repository with subdirectories as the upstream; no network.                 |
| `gf` preservation CLI scenario   | Linux   | A local bare upstream; committed and dirty local work in the child; no network.          |
| `gf` preservation CLI scenario   | macOS   | A local bare upstream; committed and dirty local work in the child; no network.          |

The CLI scenario is: `gf -C <parent> clone <local-upstream> vendor/lib`, then `gf -C <parent> status`, against a local bare repository. It passes when both commands exit 0 and `status` reports the new child.

The subfolder CLI scenario is: `gf -C <parent> clone <local-upstream>/docs/api vendor/api`, then `gf -C <parent> status`, against a local bare repository containing `docs/api`. It passes when both commands exit 0, `status` reports the new binding, and `vendor/api` resolves through its consumer link to the mapped subdirectory.

The preservation CLI scenario is: `gf -C <parent> clone <local-upstream> vendor/lib`; commit local work inside `vendor/lib` and leave one dirty edit; then `gf -C <parent> pull`. It passes when a successful pull leaves the local commit reachable from the child's history and the dirty file's bytes unchanged, and when a refusal exits nonzero inside a `gf:` envelope with the worktree byte-identical. Both arms are preservation invariants whichever pull semantics land.

### Reading a failure

An expected result is admitted as correct because the test-seam contract fixes it, never because the machine running the suite fixes it. When a run is red, separate the two causes before investigating the product.

A host whose git defaults to `main`, with the default-branch pin absent or overridden, fails the real-Git tests whose golden names `master` while the mock-backend and runner tests stay green. Read that signature as a leaked host input and check the pin before treating it as a product regression.

A preservation witness red on a `preserved` expectation reads as `work-loss`, `wrong-target`, or `unusable-provenance` per [the oracle](#preservation-witness-oracle). Any of those findings blocks acceptance; it does not degrade to a documented residual.

## Independent authorship

- Expected behavior derives from the admitted product owners — the amended specification, constraints, architecture, goals, and principles — plus fixture-controlled values the witness itself minted. Implementation output, candidate-generated goldens, and archived probe results never supply an expectation.
- The acceptance-test author differs from the production author: product implementation binds to `impl-role` and acceptance assets to `verification-impl-role`. Production surfaces stay read-only to the verification assignment; every executable asset lives under `tests/`.
- Rewritten families re-derive expectations from the amended clauses. Retained families — and retained cases inside rewritten families — stay regression observers; they are not silently deleted or declared covered.
- The archived review-evidence scripts and outputs are donor stimuli: they motivate witness shapes and candidate cases, they are revalidated against the current candidate, and their outputs supply no intended behavior. Several issues they exposed have already received fixes.
- Author verification (the document writer's own conformance check), the coordinator's independent mutation evaluation, executable-code review of the candidate, and receiving acceptance are separate surfaces; none substitutes for another.

## Preservation witness oracle

### What a witness observes

A preservation witness snapshots the protected surface before the stimulus, drives the `gf` operation through its real production entry (`python -m gf` subprocess; in-proc only where the branch is CLI-shape), then re-observes. Five observation categories, all against real `git` state:

| Category                  | Observed before and after                                                                                                                                                            | Instruments                                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| Protected bytes           | Content hash and existence of every fixture-authored file — staged, unstaged, untracked, ignored.                                                                                    | File hashing (`git hash-object` or equivalent), existence checks                                  |
| Refs and reachability     | Each fixture-minted commit stays reachable from a live ref — `HEAD`, a local branch, or a tag. Reflog-only survival is loss.                                                         | `git rev-parse`, `git branch --contains`, `git tag --contains`, `git merge-base --is-ancestor`    |
| Index and stash           | The staged/unstaged partition survives; a taken autostash is restored rather than orphaned.                                                                                          | `git status --porcelain`, `git diff --cached --name-only`, `git stash list`                       |
| Repository identity       | Mutations land only in the bound repository, store, checkout, and worktree — not a sibling alias, an ambient `GIT_*` target, or another parent root.                                 | `remote.origin.url`, per-repo `git worktree list`, recorded repo keys, link realpaths             |
| Provenance usability      | Retained work is reachable through the declared mechanism: an unmapped whole-repo child is an ordinary working repository; a re-added binding serves the identified retained work.   | `git status`/`log`/`commit` on the result; retained edits visible through the consumer link       |

### Verdicts

| Verdict                 | Meaning                                                                                                                                                                                             |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `preserved`             | Every required observation holds and the command's intended effect occurred.                                                                                                                        |
| `work-loss`             | Protected bytes differ or vanish, a minted commit is reached by no live ref, an autostash is orphaned, or the index partition is destroyed.                                                         |
| `wrong-target`          | The mutation or observation landed in a repository, store, checkout, or worktree other than the bound one.                                                                                          |
| `unusable-provenance`   | Bytes survive but the declared reconnection path fails — the unmapped child is not an ordinary working repo, or re-add silently serves latest upstream rather than the identified retained work.    |

`work-loss`, `wrong-target`, and `unusable-provenance` are blocking findings: they block acceptance and cannot be downgraded to documented residuals.

### Required observations per scenario class

| Scenario class   | Required observations                                                             | Additional requirement                                                                                                                                  |
| ---------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `completion`     | All five categories.                                                              | The intended effect is observed too — a green exit that did nothing is not preservation evidence.                                                       |
| `refusal`        | Bytes, refs/reachability, and index/stash identical to the pre-state; identity.   | Nonzero exit inside a `gf:` envelope naming the folder's name, path, and operation; every mutation outside declared record updates is rolled back.      |
| `recovery`       | All five categories.                                                              | The induced failure or interruption leaves retained work and a truthful partial-effect report, and the named recovery path reaches a `preserved` end.   |

Every witness whose criterion applies to both binding forms runs against both. Permitted variation: fixture-minted SHAs and file bytes compare exactly; command output wording, ordering, timing, and `.gf`-internal paths are evaluated through their governed behavior rather than pinned literals.

## Scenario coverage

Each selected obligation binds a goal, subject, component, profile, fixtures, allowed double boundary, expected evidence handle, and delivery phase. The independent verification owner for every obligation is the `verification-impl-role` author, distinct from the production author.

| Obligation        | Goal                                                                                     | Kind         | Subject · component                                               | Profile · double boundary                                               | Fixtures · data                                               | Evidence handle                                  | Phase   |
| ----------------- | ---------------------------------------------------------------------------------------- | ------------ | ----------------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------ | ------- |
| `O-update`        | `gf pull` integrates upstream while protected work survives.                             | integrated   | `gf pull`; `update_child`, `_apply_ref`, fetch producers          | real-git subprocess; no double                                          | local commits plus staged/unstaged/untracked/ignored work     | before/after witness capture per the verdicts    | P1      |
| `O-refusal`       | Mutating commands refuse on uncertain ownership or dirty state with protection intact.   | integrated   | all mutating commands; preflight, dirty, and identity checks      | real-git; in-proc mock only for refusal routing shape                   | uncertain-ownership and dirty fixtures                        | refusal envelope plus unchanged-state capture    | P1      |
| `O-inspect`       | `status`/`ls`/drift truthfully distinguish upstream, actual `HEAD`, divergence, dirty.   | integrated   | `gf status`/`gf ls`; `drift`                                      | real-git                                                                | local-ahead, diverged, pinned, dirty, and unborn children     | classified output against `rev-list` truth       | P1      |
| `O-identity`      | Every mutation binds a verified repository, worktree, and index.                         | integrated   | `_resolve`, checkout resolver, `clean_environ`                    | real-git; ambient `GIT_*` injected as controlled fixture environment    | sibling stores, aliased URLs, ambient targets                 | identity observations                            | P1      |
| `O-fetch`         | Fetch refspecs and pinned refs land without rewriting existing lines or user refs.       | integrated   | store/child fetch and refspec producers                           | real-git                                                                | pinned tag/commit/branch upstreams; pre-existing user refs    | store config and ref observations                | P1      |
| `O-unmap`         | `gf rm` retains work and leaves usable provenance.                                       | integrated   | `gf rm`; `remove_child`, `_vacate_checkout`, `_siblings_served`   | real-git                                                                | dirty shared checkouts, unmapped siblings, linked worktrees   | provenance-usability observations                | P2      |
| `O-reconnect`     | A re-added binding reconnects identified retained work, never a silent latest.           | integrated   | `gf clone`/`gf pull` re-add paths; `ensure_*` producers           | real-git                                                                | removed-then-readded bindings carrying retained work          | consumer-link visibility and checkout identity   | P2      |
| `O-worktree`      | Parent-worktree add/remove preserves linked children and source state.                   | integrated   | `gf worktree add`/`list`/`remove`; link guard                     | real-git                                                                | parent worktrees, linked children, dirty targets              | registrations plus bytes and reachability        | P2–P3   |
| `O-recovery`      | Interruption or failure leaves retained work and a truthful partial-effect report.       | integrated   | rollback/cleanup producers; error envelopes                       | real-git plus bounded fault injection inside fixture trees              | mid-operation failure fixtures                                | retained-work capture plus report                | P3      |
| `O-concurrency`   | Concurrent updates serialize; no torn state.                                             | integrated   | lock producers; shared store/manifest writers                     | real-git parallel subprocesses                                          | two writers on one store/manifest                             | one winner, one clean loser, no torn state       | P3      |
| `O-records`       | Manifest and checkout-state writes serialize correctly under compound operations.        | isolated     | `manifest.py`, `state.py` writers/readers                         | in-proc or real-git                                                     | compound manifests, overrides, checkout state                 | record-level before/after comparison             | P3      |
| `O-resolver`      | Checkout resolution proves identity beyond locators and live links.                      | isolated     | `layout.py` checkout resolver                                     | seeded layouts; real-git where identity requires git                    | forged, aliased, and relocated layouts                        | resolution verdicts                              | P1–P2   |

The Change's acceptance coverage expands those obligations along these axes:

| Coverage axis                            | Witness shape                                                                                 | Scenario classes              | Phase   |
| ---------------------------------------- | --------------------------------------------------------------------------------------------- | ----------------------------- | ------- |
| Staged/unstaged/untracked/ignored work   | Each work kind authored, including an ignored file; bytes and index partition held.           | completion/refusal/recovery   | P1–P3   |
| Local-ahead, diverged, pinned history    | Fixture mints each history relation; reachability and truthful drift observed.                | completion                    | P1      |
| Aliases, shared and unmapped siblings    | Two bindings share a store; unmapping one leaves the sibling serving and untouched.           | completion/refusal            | P2      |
| Missing links                            | Dangling or absent consumer link; the command refuses rather than rebuilding over it.         | refusal/recovery              | P2      |
| Multiple retained refs                   | Several local branches, tags, and stashes; all still reachable after the operation.           | completion                    | P2      |
| Ordinary linked `git worktree`s          | A user's own `git worktree add` of a child and `gf worktree add` links both survive.          | completion                    | P2–P3   |
| Safe parent-worktree removal             | `gf worktree remove` unlinks only its own links; source children and stores persist.          | completion/refusal            | P3      |
| Conflicting ambient Git context          | Injected `GIT_DIR`/`GIT_WORK_TREE`/index variables aim at another repository.                 | completion/refusal            | P1      |
| Corrupt state                            | Malformed or non-UTF-8 manifest, checkout state, or worktree record.                          | refusal/recovery              | P3      |
| Permissions                              | Unreadable or unwritable fixture paths at mutation sites.                                     | refusal/recovery              | P3      |
| Concurrent updates                       | Parallel `gf` writers on one store or manifest serialize; no torn state.                      | recovery                      | P3      |
| Interruption                             | A signal or injected fault lands mid-operation; retained work plus a truthful report.         | recovery                      | P3      |

## Strategy application

This Change applies the durable testing strategy the suite already carries: pytest with in-process (`gf_inproc`) and subprocess (`python -m gf`) drivers, real `git` against local bare upstreams, `pyfakefs` plus `MockGitBackend` for CLI permutations, fixture isolation under `tests/fixtures/tmp/`, the transport-denial guard, and the per-host control on Linux and macOS. No separate durable test-strategy document exists today; the reusable mechanics live in this document and in the architecture's seam contracts, and they transfer to the verification owner's successor surface at this Change's succession.

### Boundary comparison

| Dimension                                      | Durable boundary                                                                                          | This Change                                                                                   | Disposition   |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ------------- |
| Execution mechanism                            | pytest; in-proc `gf_inproc` and subprocess `python -m gf`                                                 | Same; preservation witnesses take the subprocess/real-git path.                               | equal         |
| Process                                        | Real `git` subprocesses on fixture repositories                                                           | Same; interruption witnesses may signal their own spawned processes.                          | equal         |
| Network                                        | None — `protocol.allow = never`; local transport only                                                     | Same.                                                                                         | equal         |
| External or autonomous runtime                 | None                                                                                                      | None added.                                                                                   | equal         |
| Credential/environment/home/external config    | `HOME`-redirected `.gitconfig`, emptied `XDG_CONFIG_HOME`, `GIT_CONFIG_NOSYSTEM`; product `GIT_*` scrub   | Same; witnesses may inject ambient `GIT_*` as controlled fixture input to prove the scrub.    | equal         |
| Fixture, data, write, cleanup                  | `tmp_path` → `tests/fixtures/tmp/<uuid>`; no writes outside                                               | Same; fault injection and planted corrupt state stay inside fixture trees.                    | equal         |
| Oracle                                         | Expectations from admitted product owners plus fixture-controlled values                                  | Same, extended by the preservation-witness model above.                                       | equal         |
| Semantic observation                           | Exit codes, `gf:` envelopes, filesystem effects, real-`git` queries                                       | Same, plus reachability, index/stash, and repository-identity observations.                   | equal         |

Temporal narrowing admitted for this Change: family expectation re-derivation narrows to the `rewrite` rows of the disposition map — retained families revalidate but do not re-derive. Owner: the verification owner; reason: the bounded Change alters only preservation-adjacent criteria; affected acceptance: family-level expectations; expiry: Change succession; receiving disposition: the coordinator at phase receipt. Every other dimension is equal. A broader proposal — new runtimes, network access, host-conditioned paths — requires an independently authorized durable-strategy amendment at the architecture owner before dispatch.

### Durable versus Change-scoped

Reconciliation for `DC-DOC-PLAN-007`: the durable strategy is the selection above — control, isolation, mock boundary, profiles, scenario style — and stays reusable across Changes as this document's transferable content. Change-scoped and ending with the Change: the preservation-oracle bindings to amended clauses, the per-phase obligations, the family disposition map, and the criterion-resolution gate. Splitting a separate durable strategy document now would orphan the suite's verification conventions mid-Change, so they remain here marked as the durable selection; at Change acceptance or re-scope they transfer to the verification owner's successor surface — a durable test-strategy owner if one is admitted, else the next verification-planning surface — and this document's temporal selection withdraws per [Applicability end](#applicability-end).

## Phase obligations

Every affected public observer and its cooperating producers receive an admitted criterion and a witness before code dispatch. The phase rows bind the plan's admission requirements to verification obligations; the plan owns the outcomes and admission authority.

| Phase   | Admitted outcome                                                                                                                              | Verification obligations                                                                                                                                                                                                                                                      | Admission evidence                                                                                   |
| ------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `P1`    | Safe refusals, verified repository identity/context, protected fetch refs, history-preserving integration and inspection across both forms.   | `O-update`, `O-refusal`, `O-inspect`, `O-identity`, `O-fetch`, `O-resolver` witnessed green; `C-pull`/`C-inspect`/`C-identity` family rewrites landed; retained guard families revalidated; every unsafe transition not yet implemented witnessed as a refusal.               | Real candidate and custody; preservation witnesses; accepted producers and test obligations.         |
| `P2`    | Retained mapping lifetime and whole/subfolder unmap/remap, with usable linked-worktree provenance.                                            | `O-unmap`, `O-reconnect`, `O-worktree` add-side witnessed; `C-unmap` rewrites landed — aliases, shared and unmapped siblings, missing links, multiple retained refs, identified reconnection.                                                                                 | Retention schema and migration accepted; ambiguity and missing-link contracts executable.            |
| `P3`    | Safe compound transitions, parent-worktree operations, concurrency, interruption, and recovery over the complete command surface.             | `O-worktree` remove-side, `O-recovery`, `O-concurrency`, `O-records` witnessed; rollback, envelope, and compound-record rewrites landed; concurrent-update, permission-denial, and corrupt-state witnesses bounded and green; unsafe interim paths still witnessed refused.   | Lock/recovery protocol accepted; bounded failure and concurrency witnesses; interim paths refused.   |

## Test layout

Complete disposition of the suite's families for this Change; the `C-*` keys are defined under [Changed criteria](#changed-criteria). `rewrite` re-derives the mapped expectations from the changed criterion; retained cases inside a rewritten family stay regression observers. `retain` marks a regression observer revalidated against the admitted candidate. Retirements are case-level — for example `pull --force` expectations die with the flag; no whole family retires, and no family is unavailable or unresolved. Dispositions of `rewrite` gate on criterion admission: expected values bind only when the owning amendment lands.

### Rewritten families

| Family                                             | Responsibility                                                                       | Changed criterion · regression residue                                                                                                             |
| -------------------------------------------------- | ------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/conftest.py`                                | `tmp_path` fixture, hermetic git env, `gf`/`git`/`gf_inproc` helpers                 | `C-preserve` — witness helpers (protected-surface capture, reachability probes) land here under verification ownership; isolation pins unchanged   |
| `tests/mock_git.py`                                | In-memory git model                                                                  | `C-ops` — mock verbs track admitted CLI-shape changes only; the mock is never an oracle for a preservation claim                                   |
| `tests/test_mock_git.py`                           | Mock self-tests                                                                      | Follows `mock_git.py` verb changes                                                                                                                 |
| `tests/test_clone_and_pull.py`                     | Whole-repo clone/pull integration                                                    | `C-pull`, `C-ops` — pull expectations re-derived; gitdir/origin/ref pins remain observers                                                          |
| `tests/test_pull_grouped.py`                       | Grouped subfolder pull                                                               | `C-pull` — shared-checkout dirty rules and `(moved with …)` reporting re-derived                                                                   |
| `tests/test_pull_autostash_recovery.py`            | Autostash recovery                                                                   | `C-pull` — complete stash restoration and index partition under every failure arm                                                                  |
| `tests/test_pull_recovery.py`                      | Pull-side rebuild/recovery                                                           | `C-recovery`, `C-identity` — wiped or unknown storage is retained or proven-owned, never rebuilt over                                              |
| `tests/test_pull_binding_urls.py`                  | Recorded `binding_urls`                                                              | `C-unmap`, `C-identity` — resolution records under retarget/remap lifetime                                                                         |
| `tests/test_state.py`                              | Checkout state records                                                               | `C-unmap`, `C-recovery` — record lifetime under the retained-state schema                                                                          |
| `tests/test_status_scoping.py`                     | `status`/`ls`/drift scoping                                                          | `C-inspect` — ahead/diverged/pinned/dirty classification re-derived; unborn-child pins carry forward                                               |
| `tests/test_edge_cases.py`                         | Multi-worktree, dirty, overrides, passthrough                                        | `C-pull`, `C-inspect` — dirty-abort and drift cases re-derived; passthrough/override cases remain observers                                        |
| `tests/test_rm_subfolder.py`                       | `gf rm` semantics                                                                    | `C-unmap` — retained lifetime and identified reconnection                                                                                          |
| `tests/test_clone_subfolder.py`                    | Subfolder clone/init through the seam                                                | `C-unmap`, `C-ops` — re-add reconnection and init-conversion cases re-derived; containment/refusal pins remain observers                           |
| `tests/test_worktree.py`                           | `gf worktree add` integration                                                        | `C-identity`, `C-recovery` — parent-worktree provenance and rollback under preservation                                                            |
| `tests/test_worktree_links.py`                     | Worktree consumer links                                                              | `C-unmap` — link provenance across parent worktrees                                                                                                |
| `tests/test_layout.py`                             | Checkout resolver                                                                    | `C-identity` — identity proven beyond locators and live links                                                                                      |
| `tests/test_url_resolution.py`                     | URL resolution units                                                                 | `C-identity` — recorded resolution lifetime; re-resolution under overrides                                                                         |
| `tests/test_store_checkout.py`                     | Store/checkout/link primitives                                                       | `C-identity`, `C-unmap` — creation separated from reconnection; existing refs and work preserved                                                   |
| `tests/test_cli_permutations.py`                   | Mock-driven CLI permutations                                                         | `C-ops` — flag surface (`--force` removal) and refusal routing; CLI-shape only                                                                     |
| `tests/test_hardening_regressions.py`              | Defect regression pins                                                               | All changed criteria — per-case criterion comparison; pins whose criterion changed are re-derived, fixed-issue pins stay green                     |
| `tests/test_worktree_add_rollback.py`              | `worktree add` rollback                                                              | `C-recovery` — failure witnesses observe retained work, not just absence of new state                                                              |
| `tests/test_worktree_add_rollback_unexpected.py`   | Rollback on unexpected errors                                                        | `C-recovery` — same coverage over unexpected exception types                                                                                       |
| `tests/test_joined_checkout_rollback.py`           | Joined-checkout rollback                                                             | `C-recovery` — rollback removes only self-created state; foreign work retained                                                                     |
| `tests/test_manifest_atomic.py`                    | Atomic manifest writes                                                               | `C-recovery` — compound record serialization across manifest and checkout state                                                                    |
| `tests/test_manifest_errors.py`                    | Manifest error envelopes                                                             | `C-recovery`, `C-identity` — corrupt retained state reported truthfully                                                                            |
| `tests/test_oserror_envelope.py`                   | `OSError` envelopes                                                                  | `C-recovery` — partial-effect truthfulness at filesystem failure sites                                                                             |
| Manual CLI scenarios                               | Host CLI checks under Running the control                                            | `C-pull`, `C-unmap` — the preservation scenario is added; existing scenarios unchanged                                                             |

### Retained families

| Family                                      | Responsibility                                                          | Revalidation note                                                                                             |
| ------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `tests/test_ambient_env_scrub.py`           | `GIT_*` scrub pins                                                      | Revalidated per affected producer; ambient context is itself a wrong-target witness vector                    |
| `tests/test_consumer_path_containment.py`   | `.gf`/`.git` segment refusals                                           | Observer                                                                                                      |
| `tests/test_forged_root_adoption.py`        | Forged owning-root refusal                                              | Observer; feeds identity witnesses                                                                            |
| `tests/test_symlinked_gf_storage.py`        | `.gf` resolve-to-self                                                   | Observer                                                                                                      |
| `tests/test_subfolder.py`                   | Gap pins: prune survival, URL forms, schema keys, no host branch        | Observer                                                                                                      |
| `tests/test_checkout_key.py`                | `ref=` checkout keys                                                    | Observer; revalidate only if the admitted retention schema re-keys checkouts                                  |
| `tests/test_checkout_replumb.py`            | Spec-derived whole-repo seam pins                                       | Observer; re-derived only where an amended clause changes the pinned clause                                   |
| `tests/test_passthrough.py`                 | `gf sh`/`git`/`diff`/`log` boundary                                     | Observer; passthrough stays a deliberate-user surface whose context binding `C-identity` revalidates          |
| `tests/test_realpath_discovery.py`          | Seeded-layout discovery/selection                                       | Observer                                                                                                      |
| `tests/test_chdir_logical_cwd.py`           | `-C` logical cwd                                                        | Observer                                                                                                      |
| `tests/test_env_guard.py`                   | Transport guard                                                         | Observer                                                                                                      |
| `tests/test_error_fields.py`                | Error name/path/operation fields                                        | Observer; refusal witnesses reuse the envelope contract                                                       |
| `tests/test_host_inputs.py`                 | Default-branch pin proof                                                | Observer                                                                                                      |
| `tests/test_platform.py`                    | Platform primitives                                                     | Observer                                                                                                      |
| `tests/test_runner.py`                      | Runner modes                                                            | Observer                                                                                                      |
| `tests/test_runner_non_utf8.py`             | Runner decode pins                                                      | Observer                                                                                                      |
| `tests/test_non_utf8_envelope.py`           | Non-UTF-8 envelopes                                                     | Observer; corrupt-state witnesses extend it                                                                   |
| `tests/test_non_utf8_worktree_record.py`    | Corrupt worktree record                                                 | Observer; extended by `O-recovery`                                                                            |
| `tests/test_sparse_dir_names.py`            | Sparse-cone name collisions                                             | Observer                                                                                                      |
| `tests/test_upstream_gf_tree.py`            | Hostile upstream `.gf` pins                                             | Observer; revalidated against rewritten `_apply_ref`/`ensure_*` producers                                     |
| `tests/fixtures/tmp/`                       | Per-test scratch root                                                   | Mechanism unchanged; witness fixtures live here                                                               |

### Candidate new families

Gaps the current families do not cover are assigned to new preservation families, created under verification ownership; the split below is indicative, not contractual:

| Candidate family                        | Obligations covered                                                                                               |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `tests/test_preservation_update.py`     | `O-update`, `O-refusal`, `O-fetch` — update integration, refusal, and fetch witnesses in both forms               |
| `tests/test_preservation_unmap.py`      | `O-unmap`, `O-reconnect` — unmap lifetime and reconnection witnesses in both forms                                |
| `tests/test_preservation_identity.py`   | `O-identity`, `O-resolver` — wrong-target and ambient-context witnesses                                           |
| `tests/test_preservation_recovery.py`   | `O-recovery`, `O-concurrency`, `O-records` — interruption, concurrency, corrupt-state, and permission witnesses   |

## Mock backend contract

`MockGitBackend` in `tests/mock_git.py` supports the commands `gf` uses in CLI permutation tests:

- `init` / `init --bare`
- `ls-remote <url>` answering for repository URLs and failing for other prefixes
- `remote add/set-url`
- `remote set-head`
- `fetch` / `fetch --filter=<filter>`
- `config --get remote.origin.fetch`
- `config remote.origin.fetch <refspec>`
- `rev-parse HEAD/--short/--abbrev-ref`
- `show-ref --verify`
- `symbolic-ref`
- `checkout` (`checkout -B`/`-f` spellings remain in the mock's vocabulary only so CLI-shape tests can assert no producer issues them — under GF-D16 a producer emitting either is a defect)
- `merge --ff-only`
- `status --porcelain`, including a pathspec scope after `--`
- `stash push -u -m <msg>` / `stash pop --index`
- `worktree list --porcelain`
- `worktree add [--no-checkout] [--detach] <path> <ref>`
- `worktree lock [--reason <r>] <path>` / `worktree unlock <path>`
- `worktree remove [--force] <path>`
- `sparse-checkout set --cone <dirs...>`

The mock also models the shared-store semantics the feature depends on: refs are shared across worktrees of one common dir, a second checkout of one branch in one store fails as git does, and a fetch call log is kept so tests can assert one fetch per repo store. New git commands used by `gf` must be added to the mock before they can be tested by CLI permutation tests.

Under this Change the mock is `rewrite` disposition: new verbs land only where an admitted CLI-shape change requires them, under verification ownership. A preservation claim never rests on `MockGitBackend` — its in-memory model cannot prove ref reachability, stash restoration, or index partition — so the double boundary stops at CLI-shape branches that do not touch preservation semantics.

## Fixture isolation

`conftest.py` overrides `tmp_path` so every test creates files under `tests/fixtures/tmp/<uuid>`. `tests/fixtures/tmp/` is in `.gitignore`. No test may write to `/tmp` or the real filesystem outside its fixture.

Preservation witnesses keep the same boundary: fixtures mint real repositories, commits, and files inside the fixture root, and the fixture-minted SHAs and bytes are the exact-comparison source for the oracle. Fault injection — signals to spawned subprocesses, planted corrupt state, permission denial, parallel writers — stays inside fixture-owned trees and processes. `deny_file_transport` pins transport denial wherever a case needs it.

## Verification checklist

- [ ] `uv run pytest -x` passes on Linux
- [ ] `uv run pytest -x` passes on macOS with a virtual environment created on that host
- [ ] The CLI, subfolder, and preservation CLI scenarios pass on each supported host
- [ ] Every affected public observer has an admitted criterion and a witness before its code dispatch
- [ ] Each preservation witness observes the required categories for its scenario class on real git, in both binding forms where its criterion applies
- [ ] Each `rewrite` family's expectations derive from the admitted amended clauses, not from implementation output or archived probe output
- [ ] New behavior has an assertion-coupled test at its owning implementation boundary
- [ ] `docs/gf-spec.md` matches the new behavior
- [ ] Manual verification matches the expected output for at least one real git repo

## When to use real-git vs mock-git

| Use real-git when...                                                              | Use mock-git when...                                                 |
| --------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Testing actual `subprocess.run` git interactions                                  | Testing CLI argument parsing and branch code paths                   |
| Testing worktree sharing or cross-worktree symlinks                               | Testing error handling and edge permutations                         |
| Testing refs/tags/branches in real repos                                          | Testing file-system side effects in `pyfakefs`                       |
| Testing repo stores, sparse checkouts, and links                                  | Testing grouped pull ordering and command routing                    |
| Testing any preservation claim: bytes, reachability, stash/index, repo identity   | Testing CLI-shape refusal routing that never touches git semantics   |

## Applicability end

This plan applies while Change `DC-DOC-PLAN-007` is active. It ends when the Change is accepted with succession complete, or when it is explicitly abandoned or re-scoped. At succession: the durable strategy sections (control, isolation, mock boundary, profiles, scenario style) transfer to the verification owner's successor surface; the oracle bindings, phase obligations, family dispositions, and criterion gate retire with the Change; and the index owner withdraws or redirects this document's temporal route (`GF-AUTH-007` in [gf-index.md](gf-index.md)). Known `work-loss`, `wrong-target`, or `unusable-provenance` findings block acceptance and never downgrade to residuals. Byte disposition is a separate authorized action.
