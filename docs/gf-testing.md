---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-test-plan]; otherwise, do not modify."
---

# git-folders Testing Plan

## Test command

```bash
uv run pytest -x
```

Run this after every meaningful change. The project uses `pyproject.toml` as the pytest config and `pyfakefs` for filesystem isolation.

The bounded change under verification is the POSIX platform seam and the host-independent inputs needed for the same suite to retain one expected result on Linux and macOS.

### Running the control

Run the control on each supported host, with a virtual environment created on that host. The dual-host requirement, the per-host virtual environment, and the pins that keep expected results host-independent are the test-seam contract in [gf-arch.md](gf-arch.md); this plan runs against that contract rather than restating it.

| Check              | Host  | Requirement                                          |
| ---                | ---   | ---                                                  |
| `uv run pytest -x` | Linux | Exit 0.                                              |
| `uv run pytest -x` | macOS | Exit 0 using a virtual environment created on macOS. |
| `gf` CLI scenario  | Linux | A local bare repository as the upstream; no network. |
| `gf` CLI scenario  | macOS | A local bare repository as the upstream; no network. |

The CLI scenario is: `gf -C <parent> clone <local-upstream> vendor/lib`, then `gf -C <parent> status`, against a local bare repository. It passes when both commands exit 0 and `status` reports the new child.

### Reading a failure

An expected result is admitted as correct because the test-seam contract fixes it, never because the machine running the suite fixes it. When a run is red, separate the two causes before investigating the product.

A host whose git defaults to `main`, with the default-branch pin absent or overridden, fails the real-Git tests whose golden names `master` while the mock-backend and runner tests stay green. Read that signature as a leaked host input and check the pin before treating it as a product regression.

## Test layout

| File                             | Responsibility                                                                                                                                                            |
| ---                              | ---                                                                                                                                                                       |
| `tests/conftest.py`              | `tmp_path` override to `tests/fixtures/tmp/<uuid>`, hermetic git identity and the host-input pins, `gf`/`git` shell helpers, `gf_inproc` in-proc runner, `Result` capture |
| `tests/test_cli_permutations.py` | In-process CLI permutation tests using `MockGitBackend` and `pyfakefs`                                                                                                    |
| `tests/test_clone_and_pull.py`   | Real-git integration test for clone, pull, and status                                                                                                                     |
| `tests/test_edge_cases.py`       | Real-git edge cases: multi-worktree, dirty worktree, branch switching, overrides                                                                                          |
| `tests/test_state.py`            | `.gf/state` content and persistence                                                                                                                                       |
| `tests/mock_git.py`              | In-memory git model: repos, refs, commits, `git` command dispatch                                                                                                         |
| `tests/test_runner.py`           | Tests for the shared `capture`/`stream`/`exec` runner                                                                                                                     |
| `tests/test_platform.py`         | Tests for `logical_cwd`, `same_path`, and `exec_or_run`                                                                                                                   |
| `tests/test_host_inputs.py`      | Real-Git proof that the configured initial branch is `master`                                                                                                             |

## Mock backend contract

`MockGitBackend` in `tests/mock_git.py` supports the commands `gf` uses in CLI permutation tests:

- `init` / `init --bare`
- `remote add/set-url`
- `remote set-head`
- `fetch`
- `config --get remote.origin.fetch`
- `config remote.origin.fetch <refspec>`
- `rev-parse HEAD/--short/--abbrev-ref`
- `show-ref --verify`
- `symbolic-ref`
- `checkout` / `checkout -B <branch> <start>` / `checkout -f ...`
- `merge --ff-only`
- `status --porcelain`
- `stash push -u -m <msg>` / `stash pop`
- `worktree list --porcelain`
- `worktree remove [--force] <path>`

New git commands used by `gf` must be added to the mock before they can be tested by CLI permutation tests.

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
| ---                                                 | ---                                                |
| Testing actual `subprocess.run` git interactions    | Testing CLI argument parsing and branch code paths |
| Testing worktree sharing or cross-worktree symlinks | Testing error handling and edge permutations       |
| Testing refs/tags/branches in real repos            | Testing file-system side effects in `pyfakefs`     |