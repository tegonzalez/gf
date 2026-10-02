# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

#!/usr/bin/env python3
"""Run a bounded real-Git preservation check through the public gf CLI.

This manual verifier intentionally does not import gf internals. It records
work bytes and semantic Git state through the selected Git executable, then
drives ``python -m gf`` with that same executable first on PATH. All generated
repositories live in a uniquely minted directory below tests/fixtures/tmp.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid


PROJECT = Path(__file__).resolve().parents[1]
FIXTURE_BASE = PROJECT / "tests" / "fixtures" / "tmp"
RETENTION_PREFIX = "refs/worktree/gf-retained-commits/"


class CheckFailure(RuntimeError):
    pass


def require(condition: bool, message: str, checks: list[str]) -> None:
    if not condition:
        raise CheckFailure(message)
    checks.append(message)


def run(argv: list[str | Path], *, env: dict[str, str], cwd: Path | None = None,
        input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    args = [str(item) for item in argv]
    result = subprocess.run(args, cwd=cwd, env=env, input=input_bytes,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise CheckFailure(
            f"command failed rc={result.returncode}: {args!r}\n"
            f"stdout={result.stdout.decode(errors='replace')}\n"
            f"stderr={result.stderr.decode(errors='replace')}"
        )
    return result


def make_environment(git_bin: Path, home: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key not in {"GIT"}}
    env.update({
        "PATH": str(git_bin.parent) + os.pathsep + os.defpath,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "xdg"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "gf-verification",
        "GIT_AUTHOR_EMAIL": "verification@git-folders.invalid",
        "GIT_COMMITTER_NAME": "gf-verification",
        "GIT_COMMITTER_EMAIL": "verification@git-folders.invalid",
        "PYTHONPATH": str(PROJECT / "src"),
        "PYTHONUTF8": "1",
    })
    (home / ".gitconfig").write_text(
        "[init]\n\tdefaultBranch = master\n"
        "[protocol]\n\tallow = never\n"
        '[protocol "file"]\n\tallow = user\n', encoding="utf-8")
    (home / "xdg" / "git").mkdir(parents=True)
    return env


def git(git_bin: Path, env: dict[str, str], *args: str | Path,
        check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return run([git_bin, *args], env=env, cwd=cwd, check=check)


def git_text(git_bin: Path, env: dict[str, str], *args: str | Path,
             cwd: Path | None = None) -> str:
    return git(git_bin, env, *args, cwd=cwd).stdout.decode().strip()


def address(gitdir: Path, worktree: Path) -> list[str]:
    return ["--git-dir", str(gitdir), "--work-tree", str(worktree)]


def hash_worktree(root: Path) -> dict[str, str]:
    """Hash every user-visible file, including ignored and untracked bytes."""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] in {".gf", ".git"}:
            continue
        key = rel.as_posix()
        if path.is_symlink():
            result[key] = "symlink:" + os.readlink(path)
        elif path.is_file():
            result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def probe(git_bin: Path, env: dict[str, str], gitdir: Path, worktree: Path,
          *args: str) -> bytes:
    result = git(git_bin, env, *address(gitdir, worktree), *args, check=False)
    if result.returncode:
        raise CheckFailure(
            f"Git observation failed ({args!r}, rc={result.returncode}): "
            f"{result.stderr.decode(errors='replace')}"
        )
    return result.stdout


def snapshot(git_bin: Path, env: dict[str, str], gitdir: Path,
             worktree: Path, parent: Path | None = None,
             child: Path | None = None) -> dict[str, object]:
    """Capture semantic state; index cache timestamps are deliberately absent."""
    refs = probe(git_bin, env, gitdir, worktree, "for-each-ref",
                 "--format=%(refname) %(objectname)").decode().splitlines()
    local_config = probe(git_bin, env, gitdir, worktree,
                         "config", "--local", "--null", "--list")
    worktree_config = probe(git_bin, env, gitdir, worktree,
                            "config", "--worktree", "--null", "--list")
    symbolic = git(git_bin, env, *address(gitdir, worktree),
                   "symbolic-ref", "-q", "HEAD", check=False)
    if symbolic.returncode not in (0, 1):
        raise CheckFailure("could not inspect HEAD symbolic reference: "
                           + symbolic.stderr.decode(errors="replace"))
    head_oid = probe(git_bin, env, gitdir, worktree, "rev-parse", "HEAD").strip()
    all_index_entries = probe(git_bin, env, gitdir, worktree, "ls-files", "--stage", "-z")
    protected_paths = {b"docs/api/library.txt", b"docs/api/local-commit.txt"}
    protected_index_entries = []
    for entry in all_index_entries.split(b"\0"):
        if b"\t" in entry and entry.split(b"\t", 1)[1] in protected_paths:
            protected_index_entries.append(entry)
    worktrees = probe(git_bin, env, gitdir, worktree, "worktree", "list", "--porcelain")
    worktree_identity = sorted(
        line for line in worktrees.decode(errors="surrogateescape").splitlines()
        if line and not line.startswith("HEAD ")
    )
    return {
        "worktree_bytes": hash_worktree(worktree),
        "head": {"oid": head_oid, "symbolic_ref": symbolic.stdout.strip()
                 if symbolic.returncode == 0 else None},
        "index_entries": all_index_entries,
        "protected_index_entries": protected_index_entries,
        "staged_patch": probe(git_bin, env, gitdir, worktree,
                               "diff", "--cached", "--binary", "HEAD"),
        "unstaged_patch": probe(git_bin, env, gitdir, worktree, "diff", "--binary"),
        "status_including_ignored": probe(git_bin, env, gitdir, worktree,
                                           "status", "--porcelain=v2", "--branch",
                                           "--ignored=matching", "-z"),
        "refs": sorted(line.split(" ", 1) for line in refs if " " in line),
        "stash": probe(git_bin, env, gitdir, worktree, "stash", "list"),
        "local_config": local_config,
        "worktree_config": worktree_config,
        "worktrees": worktrees,
        "worktree_identity": worktree_identity,
        "binding_metadata": binding_metadata(parent, child) if parent and child else {},
    }


def differences(before: dict[str, object], after: dict[str, object]) -> list[str]:
    return sorted(key for key in before if before[key] != after.get(key))


def binding_metadata(parent: Path | None, child: Path | None) -> dict[str, str]:
    """Observe manifests, link identity, and non-Git .gf records semantically."""
    if parent is None or child is None:
        return {}
    observed: dict[str, str] = {}

    def add(path: Path, key: str) -> None:
        if path.is_symlink():
            observed[key] = "symlink:" + os.readlink(path)
        elif path.is_file():
            observed[key] = "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            observed[key] = "directory"
        else:
            observed[key] = "absent"

    for name in ("gf.toml", "gf.local.toml"):
        add(parent / name, name)
    add(child, "consumer_binding")
    roots = {"parent": parent / ".gf", "child": child / ".gf"}
    for label, gf_root in roots.items():
        if not gf_root.is_dir():
            continue
        for path in sorted(gf_root.rglob("*")):
            rel = path.relative_to(gf_root)
            parts = rel.parts
            # These are Git's own object/ref/index/config/worktree records,
            # observed through semantic Git commands and worktree bytes.
            if parts[0] == "git" or parts[0] == "wt":
                continue
            if parts[0] == "repos" and len(parts) > 2 and parts[2] == "git":
                continue
            add(path, f"{label}/.gf/{rel.as_posix()}")
    return observed


def resolve_checkout(git_bin: Path, env: dict[str, str], parent: Path,
                     child: Path, form: str) -> tuple[Path, Path]:
    if form == "whole":
        return child / ".gf" / "git", child
    resolved = child.resolve(strict=True)
    checkout = resolved.parent.parent  # binding path in these scenarios is docs/api
    stores = list((parent / ".gf" / "repos").glob("*/git"))
    if len(stores) != 1:
        raise CheckFailure(f"expected one subfolder repo store, found {stores!r}")
    store = stores[0]
    wt_root = Path(git_text(git_bin, env, "--git-dir", store,
                            "rev-parse", "--git-path", "worktrees"))
    if not wt_root.is_absolute():
        wt_root = (store / wt_root).resolve()
    expected_gitfile = (checkout / ".git").resolve()
    candidates = []
    for record in wt_root.iterdir():
        gitfile = record / "gitdir"
        if gitfile.is_file():
            recorded = Path(gitfile.read_text(encoding="utf-8").strip())
            if recorded.resolve() == expected_gitfile:
                candidates.append(record)
    if len(candidates) != 1:
        raise CheckFailure(f"could not uniquely bind checkout record: {candidates!r}")
    return candidates[0], checkout


def create_upstream(git_bin: Path, env: dict[str, str], root: Path,
                    name: str) -> Path:
    seed = root / f"{name}-seed"
    remote = root / f"{name}.git"
    seed.mkdir()
    git(git_bin, env, "init", "-q", str(seed))
    (seed / "README.md").write_text("upstream base\n", encoding="utf-8")
    (seed / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    (seed / "docs" / "api").mkdir(parents=True)
    (seed / "docs" / "api" / "library.txt").write_text("v1\n", encoding="utf-8")
    git(git_bin, env, "-C", seed, "add", "-A")
    git(git_bin, env, "-C", seed, "commit", "-qm", "upstream base")
    git(git_bin, env, "clone", "-q", "--bare", str(seed), str(remote))
    git(git_bin, env, "-C", seed, "remote", "add", "verify", str(remote))
    return remote


def create_parent(git_bin: Path, env: dict[str, str], root: Path, name: str) -> Path:
    parent = root / name
    parent.mkdir()
    git(git_bin, env, "init", "-q", str(parent))
    (parent / "gf.toml").write_text("git_folder = []\n", encoding="utf-8")
    git(git_bin, env, "-C", parent, "add", "gf.toml")
    git(git_bin, env, "-C", parent, "commit", "-qm", "parent seed")
    return parent


def gf(env: dict[str, str], parent: Path, *args: str | Path,
       check: bool = True) -> subprocess.CompletedProcess:
    return run([sys.executable, "-m", "gf", "-C", parent, *args], env=env,
               cwd=PROJECT, check=check)


def apply_protected_fixture(git_bin: Path, env: dict[str, str], gitdir: Path,
                            worktree: Path, parent: Path, child: Path,
                            checks: list[str]) -> dict[str, str]:
    protected = worktree / "docs" / "api"
    protected.mkdir(parents=True, exist_ok=True)
    (protected / "local-commit.txt").write_text("committed local content\n", encoding="utf-8")
    git(git_bin, env, *address(gitdir, worktree), "add", "docs/api/local-commit.txt")
    git(git_bin, env, *address(gitdir, worktree), "commit", "-qm", "local protected commit")
    head = git_text(git_bin, env, *address(gitdir, worktree), "rev-parse", "HEAD")
    git(git_bin, env, *address(gitdir, worktree), "update-ref", "refs/heads/private-check", head)
    git(git_bin, env, *address(gitdir, worktree), "tag", "private-check-tag", head)
    git(git_bin, env, *address(gitdir, worktree), "config",
        "extensions.worktreeConfig", "true")
    git(git_bin, env, *address(gitdir, worktree), "config", "user.protected-note", "keep-this-config")
    git(git_bin, env, *address(gitdir, worktree), "config", "--worktree",
        "user.protected-worktree-note", "keep-this-worktree-config")
    require(git_text(git_bin, env, *address(gitdir, worktree), "config", "--worktree",
                     "--get", "user.protected-worktree-note") == "keep-this-worktree-config",
            "fixture creates an observable worktree-specific Git config value", checks)
    (protected / "library.txt").write_text("staged then unstaged\n", encoding="utf-8")
    git(git_bin, env, *address(gitdir, worktree), "add", "docs/api/library.txt")
    (protected / "library.txt").write_text("staged plus unstaged\n", encoding="utf-8")
    (protected / "new.txt").write_text("untracked bytes\n", encoding="utf-8")
    (protected / "local.ignored").write_text("ignored bytes\n", encoding="utf-8")
    before_stash = snapshot(git_bin, env, gitdir, worktree, parent, child)
    require(bool(before_stash["staged_patch"]),
            "fixture starts with a staged protected change", checks)
    require(bool(before_stash["unstaged_patch"]),
            "fixture starts with an unstaged protected change", checks)
    git(git_bin, env, *address(gitdir, worktree), "stash", "push", "--all", "-m",
        "preexisting verifier stash", "--", ":(top)", ":(top,exclude).gf")
    stash_entry = git_text(git_bin, env, *address(gitdir, worktree), "stash", "list")
    if not stash_entry:
        raise CheckFailure("could not create preexisting protected stash")
    git(git_bin, env, *address(gitdir, worktree), "stash", "apply", "--index", "stash@{0}")
    after_stash_restore = snapshot(git_bin, env, gitdir, worktree, parent, child)
    for category in ("worktree_bytes", "index_entries", "staged_patch", "unstaged_patch",
                     "status_including_ignored", "local_config", "worktree_config"):
        require(before_stash[category] == after_stash_restore[category],
                f"fixture restores {category} while retaining an existing stash", checks)
    require(after_stash_restore["stash"].decode().strip() == stash_entry,
            "fixture retains the preexisting named stash", checks)
    return {"local_head": head, "dirty_last_line": "staged plus unstaged\n",
            "preexisting_stash": stash_entry}


def advance_upstream(git_bin: Path, env: dict[str, str], remote: Path,
                     root: Path, name: str) -> str:
    work = root / f"{name}-advance"
    git(git_bin, env, "clone", "-q", str(remote), str(work))
    (work / "docs" / "api" / "upstream-added.txt").write_text(
        "upstream advanced\n", encoding="utf-8")
    git(git_bin, env, "-C", work, "add", "docs/api/upstream-added.txt")
    git(git_bin, env, "-C", work, "commit", "-qm", "upstream advance")
    tip = git_text(git_bin, env, "-C", work, "rev-parse", "HEAD")
    git(git_bin, env, "-C", work, "push", "-q", "origin", "master")
    return tip


def run_detector_self_check(git_bin: Path, env: dict[str, str], root: Path,
                            checks: list[str]) -> list[dict[str, object]]:
    repo = root / "detector-fixture"
    repo.mkdir()
    git(git_bin, env, "init", "-q", str(repo))
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(git_bin, env, "-C", repo, "add", "base.txt")
    git(git_bin, env, "-C", repo, "commit", "-qm", "base")
    git(git_bin, env, "-C", repo, "config", "extensions.worktreeConfig", "true")
    git(git_bin, env, "-C", repo, "config", "--worktree",
        "user.controlled-worktree", "baseline")
    initial = snapshot(git_bin, env, repo / ".git", repo)
    evidence: list[dict[str, object]] = []

    # Force a stat-only change, then let Git refresh its index cache. The
    # cache bytes may change even though protected index meaning does not.
    raw_index_before = (repo / ".git" / "index").read_bytes()
    stat = (repo / "base.txt").stat()
    os.utime(repo / "base.txt", ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    git(git_bin, env, *address(repo / ".git", repo), "update-index", "--refresh")
    raw_index_after = (repo / ".git" / "index").read_bytes()
    refreshed = snapshot(git_bin, env, repo / ".git", repo)
    require(raw_index_before != raw_index_after,
            "controlled mtime change caused a benign raw index-cache refresh", checks)
    require(not differences(initial, refreshed),
            "detector accepts benign index stat-cache refresh", checks)
    evidence.append({"stimulus": "git update-index --refresh",
                    "raw_index_changed": raw_index_before != raw_index_after,
                    "changed_categories": differences(initial, refreshed),
                    "verdict": "accepted-benign-refresh"})

    (repo / "base.txt").write_text("staged change\n", encoding="utf-8")
    git(git_bin, env, *address(repo / ".git", repo), "add", "base.txt")
    index_corrupt = snapshot(git_bin, env, repo / ".git", repo)
    require("index_entries" in differences(initial, index_corrupt),
            "detector rejects controlled semantic index corruption", checks)
    evidence.append({"stimulus": "stage controlled content change",
                    "changed_categories": differences(initial, index_corrupt),
                    "verdict": "detected-index-change"})
    git(git_bin, env, *address(repo / ".git", repo), "reset", "-q", "--hard", "HEAD")

    other = git_text(git_bin, env, "-C", repo, "rev-parse", "HEAD")
    git(git_bin, env, "-C", repo, "update-ref", "refs/heads/controlled", other)
    ref_corrupt = snapshot(git_bin, env, repo / ".git", repo)
    require("refs" in differences(initial, ref_corrupt),
            "detector rejects controlled named-ref corruption", checks)
    evidence.append({"stimulus": "create controlled named ref",
                    "changed_categories": differences(initial, ref_corrupt),
                    "verdict": "detected-ref-change"})
    git(git_bin, env, "-C", repo, "update-ref", "-d", "refs/heads/controlled")

    git(git_bin, env, *address(repo / ".git", repo), "config", "user.controlled", "changed")
    config_corrupt = snapshot(git_bin, env, repo / ".git", repo)
    require("local_config" in differences(initial, config_corrupt),
            "detector rejects controlled local-config corruption", checks)
    evidence.append({"stimulus": "change controlled local config",
                    "changed_categories": differences(initial, config_corrupt),
                    "verdict": "detected-config-change"})
    git(git_bin, env, *address(repo / ".git", repo), "config", "--worktree",
        "user.controlled-worktree", "changed")
    worktree_config_corrupt = snapshot(git_bin, env, repo / ".git", repo)
    require("worktree_config" in differences(initial, worktree_config_corrupt),
            "detector rejects controlled worktree-config corruption", checks)
    evidence.append({"stimulus": "change controlled worktree config",
                    "changed_categories": differences(initial, worktree_config_corrupt),
                    "verdict": "detected-worktree-config-change"})
    return evidence


def run_form(git_bin: Path, env: dict[str, str], root: Path, form: str,
             checks: list[str]) -> dict[str, object]:
    remote = create_upstream(git_bin, env, root, f"upstream-{form}")
    parent = create_parent(git_bin, env, root, f"parent-{form}")
    child = parent / "vendor" / "lib"
    url = remote if form == "whole" else Path(str(remote) + "/docs/api")
    result = gf(env, parent, "clone", url, "vendor/lib", check=False)
    stdout = result.stdout.decode(errors="replace")
    stderr = result.stderr.decode(errors="replace")
    if result.returncode:
        raise CheckFailure(f"{form}: clone output:\n{stdout}\n{stderr}")
    require(result.returncode == 0, f"{form}: public CLI clone succeeds", checks)
    status = gf(env, parent, "status", check=False)
    status_text = (status.stdout + status.stderr).decode(errors="replace")
    expected_name = "lib" if form == "whole" else "api"
    require(status.returncode == 0 and expected_name in status_text,
            f"{form}: status reports cloned binding", checks)
    gitdir, worktree = resolve_checkout(git_bin, env, parent, child, form)
    start = apply_protected_fixture(git_bin, env, gitdir, worktree, parent, child, checks)

    # Dirty refusal must preserve all observed categories and return gf's refusal code.
    before_refusal = snapshot(git_bin, env, gitdir, worktree, parent, child)
    refusal = gf(env, parent, "pull", check=False)
    after_refusal = snapshot(git_bin, env, gitdir, worktree, parent, child)
    response = (refusal.stdout + refusal.stderr).decode(errors="replace")
    require(refusal.returncode == 3, f"{form}: dirty pull refusal exits 3", checks)
    require("gf:" in response and "pull" in response and "lib" in response,
            f"{form}: refusal names gf operation and binding", checks)
    require(not differences(before_refusal, after_refusal),
            f"{form}: refusal preserves bytes, index, refs, stash, config, binding metadata", checks)

    # Autostash success preserves bytes and index partition, with no orphaned stash.
    before_auto = snapshot(git_bin, env, gitdir, worktree, parent, child)
    auto = gf(env, parent, "pull", "--autostash", check=False)
    require(auto.returncode == 0, f"{form}: pull --autostash succeeds", checks)
    after_auto = snapshot(git_bin, env, gitdir, worktree, parent, child)
    for category in ("worktree_bytes", "index_entries", "staged_patch", "unstaged_patch"):
        require(before_auto[category] == after_auto[category],
                f"{form}: autostash restores {category}", checks)
    require(before_auto["stash"] == after_auto["stash"],
            f"{form}: autostash leaves no extra stash", checks)
    before_auto_refs = dict(before_auto["refs"])
    after_auto_refs = dict(after_auto["refs"])
    for ref, oid in before_auto_refs.items():
        require(after_auto_refs.get(ref) == oid,
                f"{form}: autostash preserves existing ref {ref}", checks)
    extra_refs = set(after_auto_refs) - set(before_auto_refs)
    require(all(ref.startswith(RETENTION_PREFIX) or ref == "refs/worktree/gf-retained"
                for ref in extra_refs),
            f"{form}: autostash adds only permitted gf retention refs", checks)
    require(before_auto["head"]["oid"].decode() == start["local_head"]
            and after_auto["head"]["oid"] == before_auto["head"]["oid"],
            f"{form}: no-advance autostash leaves HEAD at local commit", checks)
    require(before_auto["local_config"] == after_auto["local_config"],
            f"{form}: autostash preserves local Git config", checks)
    require(before_auto["worktree_config"] == after_auto["worktree_config"],
            f"{form}: autostash preserves worktree-specific Git config", checks)
    require(before_auto["worktree_identity"] == after_auto["worktree_identity"],
            f"{form}: autostash preserves worktree identity and registration", checks)
    protected = worktree / "docs" / "api"
    require((protected / "new.txt").read_text(encoding="utf-8") == "untracked bytes\n",
            f"{form}: untracked bytes restored", checks)
    require((protected / "local.ignored").read_text(encoding="utf-8") == "ignored bytes\n",
            f"{form}: ignored bytes restored", checks)
    require((protected / "library.txt").read_text(encoding="utf-8") == start["dirty_last_line"],
            f"{form}: staged and unstaged bytes restored", checks)

    # Rebase arm exercises the full protected-work fixture before moving HEAD.
    remote2 = create_upstream(git_bin, env, root, f"rebase-upstream-{form}")
    parent2 = create_parent(git_bin, env, root, f"rebase-parent-{form}")
    child2 = parent2 / "vendor" / "lib"
    url2 = remote2 if form == "whole" else Path(str(remote2) + "/docs/api")
    cloned = gf(env, parent2, "clone", url2, "vendor/lib", check=False)
    require(cloned.returncode == 0, f"{form}: rebase fixture clone succeeds", checks)
    gitdir2, worktree2 = resolve_checkout(git_bin, env, parent2, child2, form)
    start2 = apply_protected_fixture(git_bin, env, gitdir2, worktree2,
                                     parent2, child2, checks)
    protected2 = worktree2 / "docs" / "api"
    local_tip = start2["local_head"]
    before_rebase = snapshot(git_bin, env, gitdir2, worktree2, parent2, child2)
    upstream_tip = advance_upstream(git_bin, env, remote2, root, f"rebase-{form}")
    rebased = gf(env, parent2, "pull", "--rebase", "--autostash", check=False)
    require(rebased.returncode == 0, f"{form}: rebase autostash succeeds", checks)
    after_rebase = snapshot(git_bin, env, gitdir2, worktree2, parent2, child2)
    require((protected2 / "upstream-added.txt").is_file(),
            f"{form}: upstream commit integrated by rebase", checks)
    require(probe(git_bin, env, gitdir2, worktree2, "show",
                  "HEAD:docs/api/local-commit.txt").strip() == b"committed local content",
            f"{form}: local commit's blob is present in HEAD", checks)
    require(git(git_bin, env, *address(gitdir2, worktree2), "merge-base", "--is-ancestor",
                upstream_tip, "HEAD", check=False).returncode == 0,
            f"{form}: fetched upstream tip is an ancestor of rebased HEAD", checks)
    refs_before_rebase = dict(before_rebase["refs"])
    refs2 = dict(after_rebase["refs"])
    for ref in ("refs/heads/private-check", "refs/tags/private-check-tag"):
        require(refs2.get(ref) == refs_before_rebase[ref],
                f"{form}: rebase preserves user ref {ref}", checks)
    require(refs2.get(RETENTION_PREFIX + local_tip) == local_tip,
            f"{form}: pre-rebase tip has immutable retention ref", checks)
    require(before_rebase["protected_index_entries"] == after_rebase["protected_index_entries"],
            f"{form}: rebase restores staged protected index entries", checks)
    for category in ("staged_patch", "unstaged_patch"):
        require(before_rebase[category] == after_rebase[category],
                f"{form}: rebase restores {category} partition", checks)
    require(before_rebase["stash"] == after_rebase["stash"],
            f"{form}: rebase preserves preexisting named stash", checks)
    require(before_rebase["local_config"] == after_rebase["local_config"],
            f"{form}: rebase preserves local Git config", checks)
    require(before_rebase["worktree_config"] == after_rebase["worktree_config"],
            f"{form}: rebase preserves worktree-specific Git config", checks)
    require(before_rebase["worktree_identity"] == after_rebase["worktree_identity"],
            f"{form}: rebase preserves worktree identity and registration", checks)
    for rel, expected in (("library.txt", "staged plus unstaged\n"),
                          ("local-commit.txt", "committed local content\n"),
                          ("new.txt", "untracked bytes\n"),
                          ("local.ignored", "ignored bytes\n")):
        require((protected2 / rel).read_text(encoding="utf-8") == expected,
                f"{form}: rebase restores protected bytes for {rel}", checks)
    return {
        "form": form,
        "refusal_snapshot_categories": sorted(before_refusal),
        "refusal_exit": refusal.returncode,
        "autostash_exit": auto.returncode,
        "rebase_exit": rebased.returncode,
        "local_commit": start["local_head"],
        "pre_rebase_tip": local_tip,
    }


def source_test_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for base in (PROJECT / "src", PROJECT / "tests"):
        for path in sorted(base.rglob("*")):
            if not path.is_file() or any(part in {"__pycache__", ".pytest_cache", "tmp"}
                                         for part in path.parts):
                continue
            rel = path.relative_to(PROJECT).as_posix()
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git", required=True, type=Path,
                        help="exact Git binary used by fixtures and production subprocesses")
    args = parser.parse_args()
    git_bin = args.git.resolve(strict=True)
    if not os.access(git_bin, os.X_OK):
        raise SystemExit(f"not executable: {git_bin}")
    FIXTURE_BASE.mkdir(parents=True, exist_ok=True)
    base = FIXTURE_BASE.resolve(strict=True)
    run_root = Path(tempfile.mkdtemp(prefix="verify-preservation-", dir=base)).resolve()
    marker = run_root / ".owned-by-verify-preservation"
    token = uuid.uuid4().hex
    marker.write_text(token, encoding="ascii")
    checks: list[str] = []
    receipt: dict[str, object] = {
        "git_binary": str(git_bin),
        "fixture_root": str(run_root),
    }
    try:
        home = run_root / "home"
        home.mkdir()
        env = make_environment(git_bin, home)
        receipt.update({
            "git_version": subprocess.check_output(
                [str(git_bin), "--version"], env=env, text=True).strip(),
            "git_exec_path": subprocess.check_output(
                [str(git_bin), "--exec-path"], env=env, text=True).strip(),
            "project_head": git_text(git_bin, env, "-C", PROJECT, "rev-parse", "HEAD"),
        })
        require(shutil.which("git", path=env["PATH"]) == str(git_bin),
                "PATH selects requested Git for fixtures and gf subprocesses", checks)
        require(subprocess.check_output(["git", "--version"], env=env, text=True).strip()
                == receipt["git_version"],
                "production PATH Git version matches the recorded binary", checks)
        detector_evidence = run_detector_self_check(git_bin, env, run_root, checks)
        results = [run_form(git_bin, env, run_root, form, checks)
                   for form in ("whole", "subfolder")]
        receipt.update({"checks": checks, "detector": {
                            "categories": ["worktree_bytes", "head", "index_entries",
                                           "staged_patch", "unstaged_patch",
                                           "status_including_ignored", "refs", "stash",
                                           "local_config", "worktree_config", "worktrees"],
                            "evidence": detector_evidence},
                        "scenarios": results,
                        "source_test_sha256": source_test_hashes(), "result": "PASS"})
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        receipt.update({"checks": checks, "result": "FAIL", "error": repr(exc),
                        "source_test_sha256": source_test_hashes()})
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 1
    finally:
        if marker.is_file() and marker.read_text(encoding="ascii") == token:
            resolved = run_root.resolve()
            if resolved.parent == base and resolved.name.startswith("verify-preservation-"):
                shutil.rmtree(resolved)


if __name__ == "__main__":
    raise SystemExit(main())
