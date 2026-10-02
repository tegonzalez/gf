---
doc-graph: "When there is an intention to amend this document, first Adhere to [dc-doc-design]; otherwise, do not modify."
dg-limit: words<=15000
---

# git-folders Architecture

## Purpose

Describe the high-level architecture of the `git-folders` tool.

## Overview

`git-folders` is a git-folder repository manager for git. A parent git repository declares git-folders in a tracked manifest (`gf.toml`). Each git-folder is cloned into a user-defined child directory inside the parent workspace. The child is a real git worktree with its own history; its git metadata lives in `.gf/` instead of `.git/`. The child gitdir at `child/.gf/git` is a full, self-contained gitdir with `origin` pointing to the actual git-folder URL.

A binding whose `url` is a plain path to a folder inside a repository maps that subdirectory instead of the whole repository; `gf` finds the repository boundary by URL resolution. Those bindings share a bare repo store per repository under `<root>/.gf` and consume their subdirectory through a sparse linked worktree plus a relative consumer symlink.

`gf` is a POSIX tool. Linux and macOS are supported hosts and behave identically; Windows is not supported. Host adaptation is confined to one module so that no command module carries a host branch.

## Module Inventory

### Modules and responsibilities

### `src/gf/platform.py`

The host adaptation layer. It owns three primitives:

- `logical_cwd()` — the working directory, preferring the `PWD` spelling when `PWD` is absolute and names the same directory as the process working directory. Moved here from `cli.py`, which now calls this module. Spelling is preserved; a command inside a `worktree add` symlink still targets the source child.
- `same_path(a, b)` — path comparison by directory identity, so a symlinked spelling and its target compare equal. When either path does not exist, comparison falls back to resolved spelling so a local placeholder URL can be skipped before the child exists.
- `exec_or_run(...)` — process replacement for the `exec` mode of the shared runner. `runner.py` keeps `capture` and `stream` and delegates only replacement here.

Its inputs are the process environment and the filesystem; its outputs are a path, a comparison result, and a replaced process. It wraps the host, not git: it never invokes `git`, reads `gf.toml`, or interprets the `.gf` layout, so `GitCliBackend` remains the sole git owner and `manifest.py` the sole manifest owner. It does not probe interpreter ABI.

### `src/gf/cli.py`

The command-line entry point and parser. It implements the global `-C <path>` option (only when it appears before the subcommand), `--version` / `-v`, command dispatch, and the project-root discovery protocol. Key helpers:

- `_resolve(cwd)` — central manifest/parent discovery.
- `_align_columns(rows)` — generic column alignment for `ls` and `status`.
- `_normalize_url(url)` / `_name_from_url(url)` — URL shorthand expansion and basename name derivation.
- `_get_version()` — package version from `importlib.metadata` with a `pyproject.toml` fallback.
- `_apply_chdir(argv)` — applies leading global `-C <path>` options and returns the remaining argv; trailing `-C` is left for git passthrough commands.
- `cmd_clone` — supports `--depth <n>` and `--single-branch`.
- `cmd_pull` — supports `--rebase`, `--force`, and `--autostash`.
- `cmd_status` — supports `--remote` drift classification.
- `cmd_ls`, `cmd_rm` (with `--all`), `cmd_init` (with `-n <name>` and `--url <url>`).
- `cmd_diff`, `cmd_log`, `cmd_git` — passthrough commands that forward trailing `argparse.REMAINDER` args to git, with explicit reconstruction to preserve token order.
- `cmd_worktree_add`, `cmd_worktree_list`, `cmd_worktree_remove`.
- `cmd_sh` — runs a shell or command with `GIT_DIR` set to the child gitdir.

Working-directory resolution and path comparison come from `platform.py`. `cli.py` contains no host or interpreter test of its own.

### `src/gf/layout.py`

The checkout resolver: the single owner of the `.gf` physical layout for both binding forms. It maps a binding to its `gitdir`, `work_tree`, `common_dir`, `subdir`, and state path — `child/.gf/git` for a whole-repo binding; `<root>/.gf/repos/<repo-key>/git` plus the `<root>/.gf/wt/<repo-key>/<checkout-key>` worktree for a subfolder binding. It derives `repo-key` and `checkout-key`, resolves an existing binding's checkout from its consumer link's realpath, and is filesystem-only: it performs no git calls, which keeps it callable from discovery and selection code that must not touch git. All `".gf"`-layout literals live here.

### `src/gf/shelf.py`

The git operations layer. It manages child gitdir creation, ref resolution, checkout, and drift classification.

- `_init_child_gitdir` — creates `.gf/git` as a full, self-contained gitdir.
- URL resolution — finds where the repository ends inside an effective `url`: a filesystem walk-up for local paths, a `.git` segment boundary, otherwise a longest-prefix `git ls-remote` probe with terminal prompts disabled. It runs only for `clone` and `pull`, and records its result through the layout module's per-checkout state.
- Repo-store and checkout management for subfolder bindings: ensure the bare store (`init --bare`, `remote.origin.*`, `fetch --filter=blob:none`), ensure the sparse linked checkout (`worktree add --no-checkout --detach`, `sparse-checkout set --cone`, gitfile removal, `worktree lock`), and create or retarget consumer links.
- `_set_child_origin` — configures `origin` to point to the actual git-folder URL.
- `resolve_ref` — resolves `latest`, branch, tag, and commit refs to a SHA in the child gitdir.
- `_resolve_remote_branch` — resolves a remote tracking branch (`refs/remotes/origin/<branch>`) directly, avoiding ambiguity with local branches or tags.
- `_effective_branch` — maps `latest` to the remote default branch and recognizes branch refs.
- `_fetch_and_checkout` / `_fetch_and_rebase` — fetch `origin` and check out or rebase the resolved ref; support `--force`, `--depth`, and `--single-branch` where applicable.
- `init_child` / `update_child` — initialize or update a child; `update_child` supports `--rebase`, `--force`, and `--autostash`. For branch refs it fetches `origin` and checks out a local tracking branch (`git checkout -B <branch> origin/<branch>`). If child creation fails, `_cleanup_new_child` removes the partial `.gf/git` and the child directory (only what the tool created).
- `drift` — classifies a child as `clean`, `behind`, `local-dirty`, or `both` against its effective ref, local-only: it reads the local remote-tracking refs already present and makes no network calls.
- `list_parent_worktrees` — parses `git worktree list --porcelain` into worktree records.
- `linked_git_folders_in_worktree` / `unlink_linked_git_folders_in_worktree` — detect and guard relative symlinks to git-folder children inside a parent worktree.
- `remove_child` — unregisters a whole-repo child by moving `.gf/git` to `.git`; for a subfolder binding it removes only the consumer link and never touches `<root>/.gf`.
- `select_children` — selects git-folders from the manifest by context and path arguments, with a fallback to matching by git-folder name.

### `src/gf/manifest.py`

Manifest reading, writing, and discovery. Resolves the parent repo context, reads `gf.toml`, `gf.local.toml` overrides, and computes the effective URL/ref for each git-folder. It does not interpret the URL; URL resolution belongs to `shelf.py` because it may call git.

### `src/gf/state.py`

Per-child metadata stored at `.gf/state`. Records the resolved SHA, requested ref, effective URL, and whether a local override is active. For subfolder bindings the state lives per checkout at `<root>/.gf/wt/<repo-key>/.<checkout-key>.state` and additionally records the bindings served, with each binding's effective `url` and subdir (the recorded URL resolution).

### `src/gf/backends.py`

- `GitBackend` protocol for spawning or mocking git.
- `GitCliBackend` — default real git backend.

### `src/gf/runner.py`

- Shared command runner with three modes: `capture`, `stream`, and `exec`.
- `capture` collects output for parsing.
- `stream` echoes stdout/stderr live while collecting it for error messages.
- `exec` replaces the process when stdout is a terminal so interactive programs (e.g. pagers) work. It delegates the replacement itself to `platform.py`; `runner.py` owns mode selection and the `capture` and `stream` implementations.

### `bin/gf`

The development launcher. If `.venv/bin/python` exists it re-execs that interpreter; if not, it continues under the current interpreter with `src/` prepended to `sys.path`. It carries no host test and no ABI probe. A developer must remove a virtual environment left from another host and recreate it with `uv sync`. The installed console script does not use this launcher.

### `tests/conftest.py`

The test-environment seam. It owns fixture isolation and the host inputs the suite must not inherit: hermetic git identity plus `init.defaultBranch = master` through `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_NOSYSTEM`. The pin controls real Git without wrapping it or changing product behavior.

### `tests/mock_git.py`

In-memory mock git backend used by `test_cli_permutations.py`. It simulates `init`, `remote add/set-url`, `remote set-head`, `fetch`, `checkout`, `merge --ff-only`, `rev-parse`, `show-ref`, `symbolic-ref`, `status --porcelain`, and `config` commands against a `pyfakefs` filesystem. It models the initial branch as `master`, matching the real-Git pin.

## Architecture

### Data flow

```text
parent repo
├── gf.toml            # canonical manifest
├── gf.local.toml      # untracked local overrides
├── <child>/               # whole-repo binding
│   ├── .gf/git/       # child gitdir
│   │   ├── HEAD
│   │   ├── objects/       # self-contained object store
│   │   ├── refs/
│   │   └── config         # origin = actual git-folder URL
│   ├── .gf/state      # resolved ref metadata
│   └── ...                # git-folder worktree
├── .gf/                   # subfolder-binding storage at the parent root
│   ├── repos/<repo-key>/git/          # bare repo store: objects, refs, origin
│   └── wt/<repo-key>/<checkout-key>/  # sparse linked worktree + .<checkout-key>.state
└── <subfolder path>       # consumer link → .gf/wt/<repo-key>/<checkout-key>/<subdir>
```

## Design Decisions

### Seams and contracts

1. **Manifest contract** — `gf.toml` is the single source of truth; `gf.local.toml` may override `url` and `ref` by git-folder `name`. Owner: `src/gf/manifest.py`; the direction is one-way — command modules consume the effective `url`/`ref` through it and never parse the manifest themselves; its test seam is `tests/test_edge_cases.py` (override cases) and `tests/test_state.py` (the recorded override).
1. **Gitdir contract** — a whole-repo child's `.gf/git` is a full gitdir, not bare; all objects and refs live in the child. Owner: `src/gf/shelf.py` (`_init_child_gitdir` creates it; `src/gf/layout.py` spells the path); its test seam is `tests/test_clone_and_pull.py`.
1. **Layout contract** — `src/gf/layout.py` is the only module that spells `.gf` layout paths; every consumer of `gitdir`, `work_tree`, `common_dir`, `subdir`, or state paths goes through the checkout resolver, and the resolver makes no git calls. Its test seam is `tests/test_layout.py`.
1. **Checkout-polymorphism contract** — every git operation exists once and works on the `Checkout` the resolver returns: plumbing operations (fetch, refspec config, `remote set-head`, `ls-remote`, store-level ref resolution) and `worktree add`/`lock` address `common_dir`, and worktree operations (checkout and `checkout -B`, rebase, stash, status, `rev-parse HEAD`, sparse checkout) address `gitdir` and `work_tree`. A git call whose operands are worktree-relative must pin the subprocess `cwd` to `work_tree`, or the operands resolve against the caller's process cwd instead. `src/gf/layout.py` owns the derivation of a binding's `Checkout` (`whole_repo_checkout`, `subfolder_checkout`, `resolve_checkout`) and its one form property, `Checkout.is_store_checkout`; consumers read fields and that property and never re-derive the form. The places where the two forms may differ are fixed by `GF-D15`. Review witnesses, run at every review of `src/gf/`: `grep -rn 'def .*subfolder\|_at(' src/gf/` shows no twin operation (the resolver's `subfolder_checkout` constructor is its expected match), and `grep -rn '"\.gf"' src/gf/` matches only `src/gf/layout.py`. Its test seam is `tests/test_checkout_replumb.py` and `tests/test_layout.py`.
1. **Store and checkout contract** — one bare repo store per normalized repo URL under `<root>/.gf/repos/<repo-key>/git` whose fetch refspec lines are only ever appended, never rewritten or removed; one sparse linked worktree per `(store, checkout key)` under `<root>/.gf/wt`, addressed only through `GIT_DIR`/`GIT_WORK_TREE`, its `.git` gitfile removed and the checkout locked; each consumer path holds a relative symlink into the checkout's mapped subdir, owned by the parent root whose `.gf/wt` contains its realpath. Owner: `src/gf/shelf.py` for creation and `src/gf/layout.py` for the keys and paths; the direction is one-way — consumers see only the relative link and never address `.gf` directly; its test seam is `tests/test_store_checkout.py` and `tests/test_checkout_key.py`.
1. **URL resolution contract** — a `url` is a plain path; resolution runs only in `clone` and `pull`, and its result is recorded so `status`, `ls`, and passthrough commands stay local-only. Owner: `src/gf/shelf.py`; the direction is one-way — `status`, `ls`, and the passthrough commands read the recorded result rather than resolving; its test seam is `tests/test_url_resolution.py`.
1. **Origin contract** — child `origin` fetches from and pushes to the actual git-folder URL. Owner: `src/gf/shelf.py` (`_set_child_origin`); its test seam is `tests/test_clone_and_pull.py` and the recorded `remote.origin.url` pins in `tests/test_subfolder.py`.
1. **Ref contract** — `latest` resolves to the remote default branch; branch refs track `origin/<branch>`; tag/commit refs resolve to a SHA and may detach. Owner: `src/gf/shelf.py` (`resolve_ref`); its test seam is `tests/test_clone_and_pull.py`, `tests/test_clone_subfolder.py`, and `tests/test_checkout_key.py` (ref → key).
1. **Discovery contract** — `_resolve(cwd)` walks up from `cwd` to find the parent `.git` and `gf.toml`. Owner: `src/gf/cli.py`; the direction is one-way — every command resolves its context through it; its test seam is `tests/test_realpath_discovery.py`.
1. **Platform contract** — `platform.py` owns path identity and process replacement, and the direction is one-way: command and backend modules consume the primitives, and none of them reads host identity. Its unit seam is `tests/test_platform.py`; runner failure translation remains owned by `tests/test_runner.py`.
1. **Host contract** — Linux and macOS are supported and produce identical command behavior. Windows is a declared gap, not an unstated one: `gf` carries no Windows process-creation path and no `Scripts/python.exe` lookup. Owner: `src/gf/platform.py`; the direction is one-way — the supported hosts share the one implementation and an unsupported host gets no code path; its check is the per-host control, `uv run pytest -x` with a virtual environment created on each host.
1. **Test-environment contract** — `tests/conftest.py` pins the real-Git initial branch to `master`, `tests/test_host_inputs.py` proves the pin through `git init`, and `tests/test_runner.py` invokes `sys.executable` instead of a host-resolved `python`. The control is `uv run pytest -x` on each supported host with a virtual environment created on that host.

### Decision register

Each entry is cited by identifier instead of restated. A `current` entry is design authority; a superseded entry would name its outcome and successor here.

| ID       | Decision                                                                                                                                                                                     | Rationale                                                                                                                                                   | Alternatives rejected                                                                                                                                                                                                                                                                                | Affected surfaces                                                   | Status  |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ------- |
| `GF-D1`  | Confine host adaptation to one module, `src/gf/platform.py`, and give every other module host-independent primitives.                                                                        | One owner gives one place to state, read, and verify host behavior, and each command keeps a single execution path that both supported hosts run.           | Host tests inside `cli.py` and `shelf.py`: each branch adds a per-command path that only one host executes, so the other host's path is never exercised by the suite that gates the change. A full abstraction layer that also wraps git: duplicates `GitCliBackend` and creates a second git owner. | `src/gf/platform.py`, `src/gf/cli.py`, `src/gf/runner.py`, `bin/gf` | current |
| `GF-D2`  | `bin/gf` re-execs a present `.venv/bin/python`; a developer removes and recreates a foreign `.venv` instead of probing it.                                                                   | ABI fallback would hide a dirty worktree and can continue under an interpreter without the environment's dependencies.                                      | ABI-matching fallback to `sys.executable`; always skipping re-exec even when the host-owned environment is valid.                                                                                                                                                                                    | `bin/gf`                                                            | current |
| `GF-D3`  | Support POSIX hosts and declare Windows out of scope rather than unstated.                                                                                                                   | Linux and macOS share process replacement and path semantics, so one implementation serves both; a declared gap keeps the boundary reviewable.              | Adding Windows: needs a separate process-creation path, a `Scripts/python.exe` layout, and a second set of expected outputs, none of which the supported hosts exercise. Leaving host scope unstated: every portability question reopens the same argument with no recorded outcome.                 | `src/gf/platform.py`, `bin/gf`, `docs/gf-spec.md`                   | current |
| `GF-D4`  | Pin the Git initial branch and runner interpreter instead of adapting expected results to the host.                                                                                          | Fixed inputs preserve one expected result across supported hosts, so failures identify product drift rather than machine defaults.                          | Reading host defaults into expectations; repeating `git init` flags in every fixture; skipping tests on a divergent host.                                                                                                                                                                            | `tests/conftest.py`, `test_host_inputs.py`, `test_runner.py`        | current |
| `GF-D5`  | Subfolder-binding storage lives under `<root>/.gf`: one bare repo store per repo URL plus sparse linked checkouts.                                                                 | Storage stays inside the parent worktree and objects are fetched once per repository rather than once per binding.                                          | Per-binding full clones under each consumer path.                                                                                                                                                                                                                                                    | `src/gf/layout.py`, `src/gf/shelf.py`                               | current |
| `GF-D6` | A subfolder is a plain path in `url` (`https://host/org/repo/docs/api`, `../other/docs/api`); `gf` resolves the repository boundary. The manifest schema stays `name`, `url`, `ref`, `path`. | URLs are paths; no separator syntax or new field is needed, and the default name stays the URL basename. Resolution needs the network only where it is already allowed. | A `//` separator; a new `subdir` field; a `#subdirectory=` fragment; host-specific `/tree/<ref>/<path>` URLs. | `gf.toml` grammar, `src/gf/shelf.py`, `src/gf/layout.py` | current |
| `GF-D7`  | One checkout per `(repo store, checkout key)`: `Checkout` (spec Terminology "Checkout") names it: `latest` → `quote(resolved_branch)`; `ref` → `ref= + quote(ref)` — `=` can't occur in a percent-encoding, so the `ref=` prefix cannot collide with any branch name. Tag and commit checkouts detach. | Git allows one checkout per branch; overlapping subdirs stay coherent, one commit can span bindings, and there is one status and one checkout per key.      | One checkout per binding on gf-namespaced branches: duplicates the same files and needs explicit push refspecs.                                                                                                                                                                                      | `src/gf/layout.py`, `src/gf/shelf.py`                               | current |
| `GF-D8`  | Remove each checkout's `.git` gitfile and `git worktree lock` it; `gf` addresses checkouts through `GIT_DIR`/`GIT_WORK_TREE`.                                                      | Plain `git` and `.git`-scanning tools inside a consumer link resolve to the parent repository, matching whole-repo child behavior; the lock prevents prune. | Keeping the gitfile: plain `git` from the consumer stops at the hidden checkout instead of the parent.                                                                                                                                                                                               | `src/gf/shelf.py`, `src/gf/layout.py`                               | current |
| `GF-D9` | `gf rm` on a subfolder binding removes only the consumer link and the manifest entry; the checkout, its uncommitted work, its sparse cone, and the repo store are untouched. | Removal must not touch the repository; adding the binding back restores the folder with its uncommitted work. | Copying the files into a plain directory; removing the checkout when unused and clean; narrowing the sparse cone. | `src/gf/shelf.py`, `src/gf/cli.py` | current |
| `GF-D10` | `gf git` and `gf sh` are pure passthroughs with cwd at the mapped subdir; `gf diff` and `gf log` append `-- .` only when the user passes no pathspec.                              | Relative paths behave naturally, the default view matches the mapping, and `gf git` still reaches whole-repo history.                                       | Always scoping (surprising for `gf git`); never scoping (`log` shows unrelated repository history by default).                                                                                                                                                                                       | `src/gf/cli.py`                                                     | current |
| `GF-D11` | `gf init` has no subfolder special case; the first `gf pull` resolves the `url` and converts an empty `init` child to a consumer link, refusing a child that gained files. | The binding form is decided by URL resolution, not by `init`, which stays local-only. | Rejecting subfolder URLs in `init`; resolving the URL during `init`. | `src/gf/cli.py`, `src/gf/shelf.py` | current |
| `GF-D12` | `--single-branch` narrows only a store it creates; later bindings append refspec lines with `config --add`; lines are never rewritten or removed. `--depth` applies at store creation; stores fetch with `--filter=blob:none`. | A per-branch line added beside the wildcard would fetch every branch, making `--single-branch` a no-op; append-only keeps every checkout of a shared store fetching what it needs. | Always keeping the wildcard plus a per-branch line; rewriting the refspec; full-blob fetches. | `src/gf/shelf.py`, `src/gf/cli.py` | current |
| `GF-D13` | Creation verifies the worktree record is `<store>/worktrees/<checkout-key>`; every checkout `pull` touches is locked idempotently; a checkout directory without its worktree record is an error, never rebuilt over existing files. | Gitdir resolution stays deterministic and uncommitted work in a checkout is never overwritten. | Trusting git's admin-dir naming; silently recreating a checkout. | `src/gf/layout.py`, `src/gf/shelf.py` | current |
| `GF-D14` | A consumer link is owned by the parent root whose `.gf/wt` contains its realpath; `gf worktree add` links to the source worktree's consumer link, not the resolved checkout. | `rm` must distinguish an owned link from a linked child, and a source retarget must propagate to other parent worktrees. | Linking to the resolved checkout; allowing `rm` from any parent worktree. | `src/gf/layout.py`, `src/gf/cli.py`, `src/gf/shelf.py` | current |
| `GF-D15` | Every git operation exists once and works on the `Checkout` from the resolver; there are no per-form twin operations. For a whole-repo binding `common_dir == gitdir`, so the operation vocabulary contains no form test. The whole-repo and subfolder forms may differ only at these places, exhaustively: (i) which `Checkout` constructor a creation flow calls; (ii) the environment-creation step (`_init_child_gitdir` versus ensuring the repo store, checkout, and consumer link); (iii) `pull`'s grouping composition and its `(moved with <name>)` output; (iv) the teardown branch inside `remove_child`; (v) the `.gitignore` recommendation text; (vi) the `-- .` scoping condition in `diff` and `log`; (vii) the per-binding `status --porcelain`/`ls` porcelain scope and `status --remote` drift (`binding_scope`): a store-linked binding scopes to the recorded subdir; a whole-repo binding is unscoped. The single permitted form property is `Checkout.is_store_checkout` in `src/gf/layout.py`. The different step order of the specification's two update algorithms is composition, not a duplicated operation; both pull flows share the ref-application operation (`_apply_ref`) — only fetch scheduling and step order differ. | One implementation of each operation serves both forms, so a fix or a test of that operation covers both, and the form question is answered in one place. | Per-form twin operations, such as `_at`-suffixed copies of whole-repo operations: a parallel subsystem whose two halves each get only their own fixes and tests, and drift apart. Re-deriving the form in each consumer by comparing `gitdir` with `common_dir`: spreads the form test across modules. | `src/gf/layout.py`, `src/gf/shelf.py`, `src/gf/cli.py` | current |
