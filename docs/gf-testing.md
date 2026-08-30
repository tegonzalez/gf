---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-test-plan]; otherwise, do not modify."
---

# git-folders Testing Plan

## Test command

```bash
uv run pytest -x
```

Run this after every meaningful change. The project uses `pyproject.toml` as the pytest config and `pyfakefs` for filesystem isolation.

## Test layout

| File                             | Responsibility                                                                                                             |
| ---                              | ---                                                                                                                        |
| `tests/conftest.py`              | `tmp_path` override to `tests/fixtures/tmp/<uuid>`, `gf`/`git` shell helpers, `gf_inproc` in-proc runner, `Result` capture |
| `tests/test_cli_permutations.py` | In-process CLI permutation tests using `MockGitBackend` and `pyfakefs`                                                     |
| `tests/test_clone_and_pull.py`   | Real-git integration test for clone, pull, and status                                                                      |
| `tests/test_edge_cases.py`       | Real-git edge cases: multi-worktree, dirty worktree, branch switching, overrides                                           |
| `tests/test_state.py`            | `.gf/state` content and persistence                                                                                        |
| `tests/mock_git.py`              | In-memory git model: repos, refs, commits, `git` command dispatch                                                          |
| `tests/test_runner.py`           | Tests for the shared `capture`/`stream`/`exec` runner                                                                      |

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

- [ ] `uv run pytest -x` passes
- [ ] New behavior has a test in `test_cli_permutations.py` or an integration test
- [ ] `docs/gf-spec.md` matches the new behavior
- [ ] Manual verification matches the expected output for at least one real git repo

## When to use real-git vs mock-git

| Use real-git when...                                | Use mock-git when...                               |
| ---                                                 | ---                                                |
| Testing actual `subprocess.run` git interactions    | Testing CLI argument parsing and branch code paths |
| Testing worktree sharing or cross-worktree symlinks | Testing error handling and edge permutations       |
| Testing refs/tags/branches in real repos            | Testing file-system side effects in `pyfakefs`     |