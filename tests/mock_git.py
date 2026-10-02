"""Mock git backend for in-process CLI permutation tests.

This backend does not spawn git. It records every git call and maintains a
simplified in-memory git model (commits, refs, worktree) so `gf` can still
read and write files in a `pyfakefs` filesystem.
"""
# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gf.backends import GitBackend, GitResult
from gf.exceptions import GitError


@dataclass
class Commit:
    sha: str
    parents: list[str]
    message: str
    files: dict[str, str]


@dataclass
class Worktree:
    path: str
    head: str = ""
    branch: str | None = None  # None = detached HEAD


@dataclass
class Repo:
    bare: bool = False
    mirror: bool = False
    commits: dict[str, Commit] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    symrefs: dict[str, str] = field(default_factory=dict)
    remotes: dict[str, str] = field(default_factory=dict)
    remote_fetch: dict[str, str] = field(default_factory=dict)
    worktree: dict[str, str] = field(default_factory=dict)
    indexed: dict[str, str] = field(default_factory=dict)
    head_ref: str | None = None
    head_sha: str | None = None
    worktrees: list[Worktree] = field(default_factory=list)
    stashes: list[dict] = field(default_factory=list)

    def resolve(self, ref: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", ref):
            return ref

        # peel tag
        if ref.endswith("^{}"):
            ref = ref[:-3]

        # follow symrefs
        if ref in self.symrefs:
            return self.resolve(self.symrefs[ref])

        candidates = [
            ref,
            f"refs/heads/{ref}",
            f"refs/remotes/{ref}",
            f"refs/remotes/origin/{ref}",
            f"refs/tags/{ref}",
        ]
        for c in candidates:
            if c in self.refs:
                return self.refs[c]

        if (ref in ("HEAD", "") or ref == "latest") and self.head_ref:
            if self.head_ref in self.refs:
                return self.refs[self.head_ref]
            if self.head_sha:
                return self.head_sha

        raise GitError(f"unknown ref {ref}")


class MockGitBackend(GitBackend):
    def __init__(self):
        self.calls: list[tuple] = []
        self.repos: dict[str, Repo] = {}

    def _path(self, cwd: Any, git_dir: Any, work_tree: Any) -> str:
        return str(Path(cwd or git_dir or work_tree or Path.cwd()).resolve())

    def _repo(self, path: str) -> Repo:
        if path not in self.repos:
            self.repos[path] = Repo()
        return self.repos[path]

    def _target_repo(self, url: str) -> Repo:
        """Find the repo at a URL (which is a local path in tests)."""
        path = str(Path(url).resolve())
        if path not in self.repos:
            raise GitError(f"no repo at {url}")
        return self.repos[path]

    def _find_commit(self, sha: str) -> Commit:
        for repo in self.repos.values():
            if sha in repo.commits:
                return repo.commits[sha]
        raise GitError(f"unknown sha {sha}")

    def seed(self, path: str | Path, bare: bool = False, mirror: bool = False, head: str = "master") -> Repo:
        path = str(Path(path).resolve())
        repo = Repo(bare=bare, mirror=mirror)
        self.repos[path] = repo
        repo.head_ref = f"refs/heads/{head}"
        return repo

    def add_commit(self, repo: Repo, sha: str, files: dict[str, str], parents: list[str] | None = None, message: str = "") -> Commit:
        commit = Commit(sha=sha, parents=parents or [], message=message, files=files)
        repo.commits[sha] = commit
        return commit

    def tag(self, repo: Repo, name: str, sha: str) -> None:
        repo.refs[f"refs/tags/{name}"] = sha

    def add_worktree(self, repo: Repo, path: str | Path, head: str = "", branch: str | None = "master") -> Worktree:
        wt = Worktree(path=str(Path(path).resolve()), head=head, branch=branch)
        repo.worktrees.append(wt)
        return wt

    def git(
        self,
        *args: Any,
        cwd: Path | None = None,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
        check: bool = True,
        stream: bool = False,
    ) -> GitResult:
        args = tuple(str(a) for a in args)
        path = self._path(cwd, git_dir, work_tree)
        self.calls.append((args, path))

        try:
            return self._dispatch(args, path, cwd, git_dir, work_tree)
        except GitError as e:
            if check:
                raise
            return GitResult(returncode=1, stdout="", stderr=str(e))

    def git_capture(
        self,
        *args: Any,
        cwd: Path | None = None,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
    ) -> str:
        return self.git(*args, cwd=cwd, git_dir=git_dir, work_tree=work_tree, check=True).stdout

    def _dispatch(self, args: tuple[str, ...], path: str, cwd: Any, git_dir: Any, work_tree: Any) -> GitResult:
        repo = self._repo(path)
        cmd = args[0]

        if cmd == "init":
            if "--bare" in args:
                repo.bare = True
            repo.head_ref = "refs/heads/master"
            if git_dir:
                self._write_head(Path(git_dir), ref=repo.head_ref)
            return GitResult(0, "", "")

        if cmd == "config":
            if args[1] == "--get":
                key = f"{args[2]}.{args[3]}" if len(args) == 4 else args[2]
                if key == "remote.origin.fetch":
                    val = repo.remote_fetch.get("origin")
                    if val:
                        return GitResult(0, val + "\n", "")
                return GitResult(1, "", "")
            # config key value
            if len(args) == 3:
                repo.remote_fetch["origin"] = args[2]
                return GitResult(0, "", "")

        if cmd == "fetch":
            # fetch origin  (or fetch --prune origin)
            remote_name = args[-1]
            remote_url = repo.remotes.get(remote_name)
            if remote_url:
                source = self._target_repo(remote_url)
                if repo.mirror:
                    # mirror copies refs one-to-one
                    for ref, sha in source.refs.items():
                        repo.refs[ref] = sha
                    if source.head_ref:
                        repo.refs["HEAD"] = source.refs.get(source.head_ref, source.head_sha or "")
                else:
                    # child maps source heads to remote refs
                    repo.commits.update(source.commits)
                    for ref, sha in source.refs.items():
                        if ref.startswith("refs/heads/"):
                            repo.refs[ref.replace("refs/heads/", f"refs/remotes/{remote_name}/")] = sha
                        else:
                            repo.refs[ref] = sha
                    if source.head_ref:
                        head_target = source.refs.get(source.head_ref, source.head_sha or "")
                        if "HEAD" not in repo.refs:
                            repo.refs["HEAD"] = head_target
                        repo.refs[f"refs/remotes/{remote_name}/HEAD"] = head_target
            return GitResult(0, "", "")

        if cmd == "remote":
            if args[1] == "get-url":
                name = args[2]
                if name in repo.remotes:
                    return GitResult(0, repo.remotes[name] + "\n", "")
                return GitResult(1, "", "")
            if args[1] in ("add", "set-url"):
                is_push = "--push" in args
                if is_push:
                    name = args[args.index("--push") + 1]
                    url = args[args.index("--push") + 2]
                    repo.remotes[name + "-push"] = url
                else:
                    name = args[2]
                    url = args[3]
                    repo.remotes[name] = url
                    repo.remote_fetch[name] = "+refs/heads/*:refs/remotes/origin/*"
                return GitResult(0, "", "")
            if args[1] == "set-head":
                # remote set-head <name> -a
                name = args[2]
                remote_url = repo.remotes.get(name)
                if not remote_url:
                    return GitResult(1, "", f"remote {name} not found")
                source = self._target_repo(remote_url)
                head_branch = None
                if source.head_ref and source.head_ref.startswith("refs/heads/"):
                    head_branch = source.head_ref.split("/")[-1]
                if not head_branch:
                    head_branch = "master"
                head_sha = source.refs.get(source.head_ref, source.head_sha or "")
                repo.symrefs[f"refs/remotes/{name}/HEAD"] = f"refs/remotes/{name}/{head_branch}"
                repo.refs[f"refs/remotes/{name}/HEAD"] = head_sha
                repo.refs[f"refs/remotes/{name}/{head_branch}"] = head_sha
                return GitResult(0, f"{name}/HEAD set to {head_branch}\n", "")

        if cmd == "rev-parse":
            if "HEAD" in args:
                if "--abbrev-ref" in args:
                    if repo.head_ref and repo.head_ref.startswith("refs/heads/"):
                        short = repo.head_ref.split("/")[2]
                        return GitResult(0, short + "\n", "")
                    return GitResult(0, "HEAD\n", "")
                sha = repo.resolve("HEAD")
                out = sha[:12] if "--short" in args else sha
                return GitResult(0, out + "\n", "")
            ref = args[-1]
            # strip trailing ^{} for tag peel display; resolve handles it
            sha = repo.resolve(ref.replace("^{}", ""))
            out = sha[:12] if "--short" in args else sha
            return GitResult(0, out + "\n", "")

        if cmd == "symbolic-ref":
            ref = args[1]
            if ref in repo.symrefs:
                return GitResult(0, repo.symrefs[ref] + "\n", "")
            if ref == "HEAD" and repo.head_ref:
                return GitResult(0, repo.head_ref + "\n", "")
            return GitResult(1, "", f"ref {ref} is not a symbolic ref")

        if cmd == "show-ref":
            if "--verify" in args:
                ref = args[-1]
                resolved = None
                if ref in repo.symrefs:
                    resolved = repo.symrefs[ref]
                if resolved and resolved in repo.refs:
                    return GitResult(0, repo.refs[resolved] + " " + ref + "\n", "")
                if ref in repo.refs:
                    return GitResult(0, repo.refs[ref] + " " + ref + "\n", "")
                return GitResult(1, "", "")

        if cmd == "checkout":
            if "-B" in args:
                branch = args[args.index("-B") + 1]
                start_ref = args[args.index("-B") + 2]
                sha = repo.resolve(start_ref.replace("^{}", ""))
            elif "-b" in args:
                branch = args[args.index("-b") + 1]
                start_ref = args[args.index("-b") + 2]
                sha = repo.resolve(start_ref.replace("^{}", ""))
            else:
                branch = None
                # Skip flags like -f to find the commit-ish.
                sha = next(a for a in args[1:] if not a.startswith("-"))
            commit = self._find_commit(sha)
            repo.commits[sha] = commit
            repo.head_sha = sha
            if branch:
                repo.head_ref = f"refs/heads/{branch}"
                repo.refs[repo.head_ref] = sha
            else:
                repo.head_ref = None
            repo.indexed = dict(commit.files)
            repo.worktree = dict(commit.files)
            if git_dir:
                self._write_head(Path(git_dir), ref=repo.head_ref, sha=sha)
            if work_tree:
                self._write_worktree(Path(work_tree), commit.files)
            return GitResult(0, f"HEAD is now at {sha}\n", "")

        if cmd == "merge" and "--ff-only" in args:
            ref = args[args.index("--ff-only") + 1]
            target_sha = repo.resolve(ref)
            if target_sha == repo.head_sha:
                return GitResult(0, "Already up to date.\n", "")
            commit = self._find_commit(target_sha)
            repo.commits[target_sha] = commit
            if repo.head_sha and repo.head_sha not in commit.parents:
                return GitResult(1, "", "merge: not fast-forward")
            repo.head_sha = target_sha
            if repo.head_ref:
                repo.refs[repo.head_ref] = target_sha
            else:
                repo.head_ref = None
            repo.indexed = dict(commit.files)
            repo.worktree = dict(commit.files)
            if git_dir:
                self._write_head(Path(git_dir), ref=repo.head_ref, sha=target_sha)
            if work_tree:
                self._write_worktree(Path(work_tree), commit.files)
            return GitResult(0, f"Updating {target_sha}\n", "")

        if cmd == "rebase":
            target_ref = args[-1]
            target_sha = repo.resolve(target_ref)
            commit = self._find_commit(target_sha)
            repo.commits[target_sha] = commit
            repo.head_sha = target_sha
            if repo.head_ref:
                repo.refs[repo.head_ref] = target_sha
            repo.indexed = dict(commit.files)
            repo.worktree = dict(commit.files)
            if git_dir:
                self._write_head(Path(git_dir), ref=repo.head_ref, sha=target_sha)
            if work_tree:
                self._write_worktree(Path(work_tree), commit.files)
            return GitResult(0, f"Successfully rebased and updated refs/heads/{repo.head_ref.split('/')[-1] if repo.head_ref else 'HEAD'}.\n", "")

        if cmd == "status" and "--porcelain" in args:
            lines = []
            for name in repo.indexed:
                work_path = Path(work_tree) / name
                actual = work_path.read_text() if work_path.exists() else None
                expected = repo.indexed[name]
                if actual != expected:
                    lines.append(f" M {name}")
            for name in repo.worktree:
                if name not in repo.indexed:
                    lines.append(f"?? {name}")
            # Detect untracked files actually present on disk in the worktree.
            if work_tree:
                wt_path = Path(work_tree)
                if wt_path.is_dir():
                    known = set(repo.indexed.keys()) | set(repo.worktree.keys())
                    for entry in wt_path.rglob("*"):
                        if not entry.is_file():
                            continue
                        rel = entry.relative_to(wt_path).as_posix()
                        if rel.startswith(".gf/"):
                            continue
                        if rel not in known:
                            lines.append(f"?? {rel}")
            # Deduplicate while preserving order.
            seen = set()
            deduped = []
            for line in lines:
                if line not in seen:
                    seen.add(line)
                    deduped.append(line)
            return GitResult(0, ("\n".join(deduped) + "\n") if deduped else "", "")

        if cmd == "worktree":
            sub = args[1] if len(args) > 1 else ""
            if sub == "list" and "--porcelain" in args:
                out = []
                for wt in repo.worktrees:
                    out.append(f"worktree {wt.path}")
                    if wt.head:
                        out.append(f"HEAD {wt.head}")
                    if wt.branch:
                        out.append(f"branch refs/heads/{wt.branch}")
                    out.append("")
                return GitResult(0, "\n".join(out) + "\n" if out else "", "")
            if sub == "remove":
                # worktree remove [--force] <path>
                rest = [a for a in args[2:] if a != "--force"]
                target = str(Path(rest[-1]).resolve())
                repo.worktrees = [w for w in repo.worktrees if w.path != target]
                return GitResult(0, "", "")
            raise GitError(f"unmocked git command: {' '.join(args)}")

        if cmd == "stash":
            sub = args[1] if len(args) > 1 else ""
            if sub == "push":
                # stash push -u -m <msg>
                # Snapshot only the dirty/untracked files from disk so the
                # pop reapplies local changes on top of the updated worktree
                # without clobbering files the update changed.
                snapshot: dict[str, str] = {}
                if work_tree:
                    wt_path = Path(work_tree)
                    for name in repo.indexed:
                        p = wt_path / name
                        if p.exists() and p.read_text() != repo.indexed[name]:
                            snapshot[name] = p.read_text()
                    known = set(repo.indexed.keys()) | set(repo.worktree.keys())
                    for entry in wt_path.rglob("*"):
                        if not entry.is_file():
                            continue
                        rel = entry.relative_to(wt_path).as_posix()
                        if rel.startswith(".gf/"):
                            continue
                        if rel not in known:
                            snapshot[rel] = entry.read_text()
                repo.stashes.append({"worktree": snapshot, "indexed": dict(repo.indexed)})
                # Reset tracked files to indexed (clean) state and remove the
                # now-stashed untracked files.
                repo.worktree = dict(repo.indexed)
                if work_tree:
                    self._write_worktree(Path(work_tree), repo.indexed)
                    for name in list(snapshot.keys()):
                        if name not in repo.indexed:
                            p = Path(work_tree) / name
                            if p.exists():
                                p.unlink()
                return GitResult(0, "Saved working directory and index state\n", "")
            if sub == "pop":
                if not repo.stashes:
                    return GitResult(1, "", "No stash entries found.")
                entry = repo.stashes.pop()
                # Reapply stashed files on top of the current worktree.
                repo.worktree.update(entry["worktree"])
                if work_tree:
                    wt_path = Path(work_tree)
                    for name, content in entry["worktree"].items():
                        p = wt_path / name
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text(content)
                return GitResult(0, "", "")
            raise GitError(f"unmocked git command: {' '.join(args)}")

        raise GitError(f"unmocked git command: {' '.join(args)}")

    def _write_worktree(self, work_tree: Path, files: dict[str, str]) -> None:
        for name, content in files.items():
            path = work_tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def _write_head(self, git_dir: Path, ref: str | None = None, sha: str | None = None) -> None:
        head = git_dir / "HEAD"
        head.parent.mkdir(parents=True, exist_ok=True)
        if ref:
            head.write_text(f"ref: {ref}\n")
        elif sha:
            head.write_text(f"{sha}\n")
