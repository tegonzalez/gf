---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-test-plan]; otherwise, do not modify."
---

# git-folders Testing Plan

## Test command

```bash
uv run pytest -x
```

Run this after every meaningful change. The project uses `pyproject.toml` as the pytest config and `pyfakefs` for filesystem isolation.

The bounded change under verification is repository-subfolder bindings alongside whole-repo bindings. The retained POSIX platform seam and host-independent inputs remain in force so the same suite keeps one expected result on Linux and macOS.

### Running the control

Run the control on each supported host, with a virtual environment created on that host. The dual-host requirement, the per-host virtual environment, and the pins that keep expected results host-independent are the test-seam contract in [gf-arch.md](gf-arch.md); this plan runs against that contract rather than restating it.

| Check                       | Host  | Requirement                                                              |
| --------------------------- | ----- | ------------------------------------------------------------------------ |
| `uv run pytest -x`          | Linux | Exit 0.                                                                  |
| `uv run pytest -x`          | macOS | Exit 0 using a virtual environment created on macOS.                     |
| `gf` CLI scenario           | Linux | A local bare repository as the upstream; no network.                     |
| `gf` CLI scenario           | macOS | A local bare repository as the upstream; no network.                     |
| `gf` subfolder CLI scenario | Linux | A local bare repository with subdirectories as the upstream; no network. |
| `gf` subfolder CLI scenario | macOS | A local bare repository with subdirectories as the upstream; no network. |

The CLI scenario is: `gf -C <parent> clone <local-upstream> vendor/lib`, then `gf -C <parent> status`, against a local bare repository. It passes when both commands exit 0 and `status` reports the new child.

The subfolder CLI scenario is: `gf -C <parent> clone <local-upstream>/docs/api vendor/api`, then `gf -C <parent> status`, against a local bare repository containing `docs/api`. It passes when both commands exit 0, `status` reports the new binding, and `vendor/api` resolves through its consumer link to the mapped subdirectory.

### Reading a failure

An expected result is admitted as correct because the test-seam contract fixes it, never because the machine running the suite fixes it. When a run is red, separate the two causes before investigating the product.

A host whose git defaults to `main`, with the default-branch pin absent or overridden, fails the real-Git tests whose golden names `master` while the mock-backend and runner tests stay green. Read that signature as a leaked host input and check the pin before treating it as a product regression.

## Test layout

| File                             | Responsibility                                                                                                                                                            |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/conftest.py`              | `tmp_path` override to `tests/fixtures/tmp/<uuid>`, hermetic git identity and the host-input pins, `gf`/`git` shell helpers, `gf_inproc` in-proc runner, `Result` capture |
| `tests/test_cli_permutations.py` | In-process CLI permutation tests using `MockGitBackend` and `pyfakefs`                                                                                                    |
| `tests/test_clone_and_pull.py`   | Real-git integration test for clone, pull, and status                                                                                                                     |
| `tests/test_edge_cases.py`       | Real-git edge cases: multi-worktree, dirty worktree, branch switching, overrides                                                                                          |
| `tests/test_state.py`            | `.gf/state` content and persistence                                                                                                                                       |
| `tests/mock_git.py`              | In-memory git model: repos, refs, commits, `git` command dispatch                                                                                                         |
| `tests/test_runner.py`           | Tests for the shared `capture`/`stream`/`exec` runner                                                                                                                     |
| `tests/test_platform.py`         | Tests for `logical_cwd`, `same_path`, and `exec_or_run`                                                                                                                   |
| `tests/test_host_inputs.py`      | Real-Git proof that the configured initial branch is `master`                                                                                                             |
| `tests/test_subfolder.py`        | Real-git subfolder-binding scenarios: URL resolution, shared stores and checkouts, consumer links, scoped status, `rm`, worktree commands, `init` conversion      |
| `tests/test_url_resolution.py`   | URL resolution unit tests: local walk-up, `.git` boundary, longest-prefix probe, unresolvable URL                                                                          |
| `tests/test_layout.py`           | The checkout resolver in `src/gf/layout.py`: `Checkout` derivation for both binding forms                                                                                 |
| `tests/test_checkout_replumb.py` | Checkout-resolver re-plumb: every consumer reaches git state through the `Checkout` seam                                                                                  |
| `tests/test_checkout_key.py`     | The `ref=` checkout-key contract: collision-free pinned keys                                                                                                              |
| `tests/test_store_checkout.py`   | Shared store, checkout, and consumer-link creation primitives                                                                                                             |
| `tests/test_clone_subfolder.py`  | `gf clone` and `gf init` creating subfolder bindings through the seam                                                                                                     |
| `tests/test_pull_grouped.py`     | `gf pull` grouped composition and init-child first-pull conversion                                                                                                        |
| `tests/test_status_scoping.py`   | `gf status`/`gf ls` and drift scoped per binding, local-only                                                                                                              |
| `tests/test_rm_subfolder.py`     | `gf rm` subfolder semantics                                                                                                                                                |
| `tests/test_passthrough.py`      | `gf sh`/`git`/`diff`/`log` passthrough on resolved checkouts                                                                                                              |
| `tests/test_worktree.py`         | `gf worktree add` integration                                                                                                                                              |
| `tests/test_worktree_links.py`   | `gf worktree` consumer-link semantics                                                                                                                                     |
| `tests/test_realpath_discovery.py` | Realpath-aware discovery and binding selection                                                                                                                          |
| `tests/test_error_fields.py`     | The error-message contract: name, path, and operation fields                                                                                                              |
| `tests/test_env_guard.py`        | Environment guard: git itself refuses non-local transports in the suite                                                                                                   |
| `tests/test_mock_git.py`         | Self-tests for the in-memory git model in `tests/mock_git.py`                                                                                                             |
| `tests/test_hardening_regressions.py` | Regression pins for confirmed defects, strict-xfail until each fix lands                                                                                               |
| `tests/test_consumer_path_containment.py` | Consumer-path containment: `.gf`/`.git` segment refusals, mid-path symlink anchoring, hostile-manifest read gates                                                     |
| `tests/test_forged_root_adoption.py` | Owning-root adoption: forged `.gf/wt`-shaped realpaths cannot redirect storage outside the workspace                                                                      |
| `tests/test_manifest_atomic.py`    | Atomic `gf.toml` writes: temp-write-and-replace, no partial manifests                                                                                                     |
| `tests/test_manifest_errors.py`    | Manifest and override error envelopes: shape validation, clean `gf:` failures                                                                                             |
| `tests/test_non_utf8_envelope.py`  | Non-UTF-8 `gf.toml`/`gf.local.toml`/`.state` files die inside the envelope, never a `UnicodeDecodeError` traceback                                                        |
| `tests/test_non_utf8_worktree_record.py` | A corrupt or non-UTF-8 checkout `gitdir` record counts as invalid — `gf pull` dies `refusing to rebuild over existing files` over a populated checkout, never a traceback |
| `tests/test_oserror_envelope.py`   | `OSError` at filesystem sites (unlink/rmdir/mkdir/replace/state IO) exits `1` as a `gf:` error                                                                            |
| `tests/test_pull_recovery.py`      | Pull-side recovery: wiped `.gf` store/checkout rebuilt on pull; store retention/removal on rollback; retry after a failed creating fetch                                    |
| `tests/test_pull_binding_urls.py`  | Recorded `binding_urls`: per-binding effective URL resolution in checkout state                                                                                           |
| `tests/test_pull_autostash_recovery.py` | `gf pull --autostash` restores the stash on every update failure, including a failed `origin` update                                                              |
| `tests/test_sparse_dir_names.py`   | Sparse-cone member names needing `--skip-checks` (`*?[]\`, leading `!` segments)                                                                                          |
| `tests/test_symlinked_gf_storage.py` | `.gf`-rooted storage must resolve to itself: symlinked components refuse writes and never rmtree through                                                                |
| `tests/test_ambient_env_scrub.py`  | Ambient `GIT_*` scrubbing: repo-pointer/index/config-injection variables cannot retarget spawned `git`                                                                    |
| `tests/test_chdir_logical_cwd.py`  | `gf -C` preserves the operand's logical spelling in `PWD`; lexical binding tiebreak sees it                                                                               |
| `tests/test_joined_checkout_rollback.py` | Joined-checkout rollback removes only checkouts the invocation created                                                                                              |
| `tests/test_runner_non_utf8.py`    | Captured `git` output decodes with replacement; malformed bytes surface as U+FFFD inside the error                                                                        |
| `tests/test_worktree_add_rollback.py` | `gf worktree add` removes the new worktree on post-add failure                                                                                                       |
| `tests/test_worktree_add_rollback_unexpected.py` | Rollback covers unexpected exceptions, not just `gf` and OS errors                                                                                         |

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
- `checkout` / `checkout -B <branch> <start>` / `checkout -f ...`
- `merge --ff-only`
- `status --porcelain`, including a pathspec scope after `--`
- `stash push -u -m <msg>` / `stash pop`
- `worktree list --porcelain`
- `worktree add [--no-checkout] [--detach] <path> <ref>`
- `worktree lock [--reason <r>] <path>` / `worktree unlock <path>`
- `worktree remove [--force] <path>`
- `sparse-checkout set --cone <dirs...>`

The mock also models the shared-store semantics the feature depends on: refs are shared across worktrees of one common dir, a second checkout of one branch in one store fails as git does, and a fetch call log is kept so tests can assert one fetch per repo store. New git commands used by `gf` must be added to the mock before they can be tested by CLI permutation tests.

## Fixture isolation

`conftest.py` overrides `tmp_path` so every test creates files under `tests/fixtures/tmp/<uuid>`. `tests/fixtures/tmp/` is in `.gitignore`. No test may write to `/tmp` or the real filesystem outside its fixture.

## Verification checklist

- [ ] `uv run pytest -x` passes on Linux
- [ ] `uv run pytest -x` passes on macOS with a virtual environment created on that host
- [ ] The CLI scenario passes on each supported host
- [ ] New behavior has an assertion-coupled test at its owning implementation boundary
- [ ] `docs/gf-spec.md` matches the new behavior
- [ ] Manual verification matches the expected output for at least one real git repo

## When to use real-git vs mock-git

| Use real-git when...                                | Use mock-git when...                               |
| --------------------------------------------------- | -------------------------------------------------- |
| Testing actual `subprocess.run` git interactions    | Testing CLI argument parsing and branch code paths |
| Testing worktree sharing or cross-worktree symlinks | Testing error handling and edge permutations       |
| Testing refs/tags/branches in real repos            | Testing file-system side effects in `pyfakefs`     |
| Testing repo stores, sparse checkouts, and links    | Testing grouped pull ordering and command routing  |
