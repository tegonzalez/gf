"""Mock git backend for in-process CLI permutation tests.

This backend does not spawn git. It records every git call and maintains a
simplified in-memory git model (commits, refs, worktree) so `gf` can still
read and write files in a `pyfakefs` filesystem.

The model also covers the shared-store shape behind subfolder bindings: a
bare common gitdir holds refs and objects, and linked worktrees addressed
through their admin dir ``<store>/worktrees/<name>`` get per-worktree HEAD,
index, worktree, sparse-cone and stash state while sharing the store's
refs and commits.
"""
# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import re
import shutil
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
    # Linked-worktree state (shared repo store). Seeded worktrees leave
    # these at their defaults and only appear in `worktree list/remove`.
    admin_dir: str | None = None  # <store>/worktrees/<name>
    locked: str | None = None     # None = unlocked, "" = locked w/o reason
    cone: frozenset[str] | None = None
    files: dict[str, str] = field(default_factory=dict)      # full commit tree
    indexed: dict[str, str] = field(default_factory=dict)    # cone-filtered
    worktree: dict[str, str] = field(default_factory=dict)   # disk model
    stashes: list[dict] = field(default_factory=list)
    # Per-worktree refs — the refs/worktree/* hierarchy (e.g.
    # refs/worktree/gf-retained) is per-worktree in a linked-checkout
    # store, shared-dir refs stay on Repo.
    refs: dict[str, str] = field(default_factory=dict)
    checked_out: bool = False


@dataclass
class Repo:
    bare: bool = False
    mirror: bool = False
    commits: dict[str, Commit] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    symrefs: dict[str, str] = field(default_factory=dict)
    remotes: dict[str, str] = field(default_factory=dict)
    remote_fetch: dict[str, str] = field(default_factory=dict)
    config: dict[str, list[str]] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    worktree: dict[str, str] = field(default_factory=dict)
    indexed: dict[str, str] = field(default_factory=dict)
    cone: frozenset[str] | None = None
    head_ref: str | None = None
    head_sha: str | None = None
    worktrees: list[Worktree] = field(default_factory=list)
    stashes: list[dict] = field(default_factory=list)
    # Poison pin: when True, `ls-tree <sha> .gf` on this repo reports a
    # root `.gf` entry — a committed blob/tree/symlink/gitlink alike.
    # `fetch` carries the flag across so an upstream's poison reaches
    # the store/child repo the entry guard probes.
    gf_entry: bool = False

    def resolve(self, ref: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", ref):
            return ref

        # peel tag
        if ref.endswith("^{}"):
            ref = ref[:-3]

        # follow symrefs
        if ref in self.symrefs:
            return self.resolve(self.symrefs[ref])

        # HEAD resolves through this checkout's own state before a literal
        # refs["HEAD"] entry left behind by an earlier fetch.
        if ref in ("HEAD", ""):
            if self.head_ref and self.head_ref in self.refs:
                return self.refs[self.head_ref]
            if self.head_sha:
                return self.head_sha

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
        self.envs: list[dict | None] = []
        self.repos: dict[str, Repo] = {}

    def _path(self, cwd: Any, git_dir: Any, work_tree: Any) -> str:
        return str(Path(git_dir or cwd or work_tree or Path.cwd()).resolve())

    def _repo(self, path: str) -> Repo:
        if path not in self.repos:
            self.repos[path] = Repo()
        return self.repos[path]

    def _repo_for(self, path: str) -> tuple[Repo, Worktree | None]:
        """Resolve a command address to (repo, linked worktree | None).

        A git_dir of the form ``<store>/worktrees/<name>`` selects the
        linked worktree registered under that admin dir; the returned repo
        is the shared store. Any other path selects (or creates) the repo
        keyed by that path.
        """
        if path in self.repos:
            return self.repos[path], None
        for repo in self.repos.values():
            for wt in repo.worktrees:
                if wt.path == path:
                    return repo, wt
        p = Path(path)
        if p.parent.name == "worktrees" and str(p.parent.parent) in self.repos:
            repo = self.repos[str(p.parent.parent)]
            for wt in repo.worktrees:
                if wt.admin_dir == path:
                    return repo, wt
            raise GitError(f"fatal: '{path}' is not a git repository")
        return self._repo(path), None

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

    def _ancestors(self, sha: str) -> set[str]:
        """All commits reachable from `sha`, including itself."""
        seen: set[str] = set()
        stack = [sha]
        while stack:
            s = stack.pop()
            if s in seen:
                continue
            try:
                commit = self._find_commit(s)
            except GitError:
                continue
            seen.add(s)
            stack.extend(commit.parents)
        return seen

    def _is_ancestor(self, maybe_ancestor: str, tip: str) -> bool:
        if not maybe_ancestor or not tip:
            return False
        return maybe_ancestor in self._ancestors(tip)

    def _ref_value(self, repo: Repo, wt: Worktree | None, ref: str) -> str | None:
        """Look up a ref, consulting per-worktree refs first for a
        worktree-bound call (refs/worktree/* lives on the checkout)."""
        if wt is not None and ref in wt.refs:
            return wt.refs[ref]
        if ref in repo.symrefs:
            resolved = repo.symrefs[ref]
            if resolved in repo.refs:
                return repo.refs[resolved]
        return repo.refs.get(ref)

    def _find_worktree(self, repo: Repo, target: str) -> Worktree | None:
        for wt in repo.worktrees:
            if wt.path == target:
                return wt
        return None

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
        env: dict | None = None,
    ) -> GitResult:
        args = tuple(str(a) for a in args)
        path = self._path(cwd, git_dir, work_tree)
        self.calls.append((args, path))
        self.envs.append(env)

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
        env: dict | None = None,
    ) -> str:
        return self.git(
            *args, cwd=cwd, git_dir=git_dir, work_tree=work_tree,
            check=True, env=env,
        ).stdout

    # -- shared-store helpers -------------------------------------------------

    def _head_branch(self, repo: Repo, wt: Worktree | None) -> str | None:
        """Short name of the branch HEAD is attached to, if any."""
        if wt is not None:
            return wt.branch
        if repo.head_ref and repo.head_ref.startswith("refs/heads/"):
            return repo.head_ref.split("/", 2)[2]
        return None

    def _head_sha(self, repo: Repo, wt: Worktree | None) -> str | None:
        """Current HEAD sha; attached worktrees resolve through the common dir."""
        if wt is not None:
            if wt.branch:
                return repo.refs.get(f"refs/heads/{wt.branch}", wt.head)
            return wt.head
        return repo.head_sha

    def _branch_in_use(self, repo: Repo, branch: str, exclude: Worktree | None = None) -> str | None:
        """Path of the checkout already using `branch`, or None.

        Git allows each branch to be checked out in at most one worktree of
        a common dir. The (non-bare) repo's own checkout counts too.
        """
        for w in repo.worktrees:
            if w is not exclude and w.branch == branch:
                return w.path
        if not repo.bare and repo.head_ref == f"refs/heads/{branch}":
            return next((k for k, v in self.repos.items() if v is repo), "")
        return None

    def _cone_includes(self, name: str, cone: frozenset[str] | None) -> bool:
        """Cone-mode membership: root files plus the listed directories."""
        if cone is None or "/" not in name:
            return True
        return any(name == d or name.startswith(d + "/") for d in cone)

    def _cone_filter(self, files: dict[str, str], cone: frozenset[str] | None) -> dict[str, str]:
        return {n: c for n, c in files.items() if self._cone_includes(n, cone)}

    def _work_root(self, wt: Worktree | None, work_tree: Any) -> Path | None:
        if work_tree:
            return Path(work_tree)
        if wt is not None:
            return Path(wt.path)
        return None

    def _apply_commit(
        self,
        repo: Repo,
        wt: Worktree | None,
        commit: Commit,
        sha: str,
        branch: str | None,
        git_dir: Any,
        work_root: Path | None,
    ) -> None:
        """Attach `branch` (detach when None) at `sha` and write the tree."""
        cone = wt.cone if wt is not None else repo.cone
        written = self._cone_filter(commit.files, cone)
        if wt is not None:
            wt.head = sha
            wt.branch = branch
            wt.files = dict(commit.files)
            wt.indexed = dict(written)
            wt.worktree = dict(written)
            wt.checked_out = True
        else:
            repo.head_sha = sha
            repo.head_ref = f"refs/heads/{branch}" if branch else None
            repo.files = dict(commit.files)
            repo.indexed = dict(written)
            repo.worktree = dict(written)
        if branch:
            repo.refs[f"refs/heads/{branch}"] = sha
        if git_dir:
            self._write_head(
                Path(git_dir),
                ref=f"refs/heads/{branch}" if branch else None,
                sha=sha,
            )
        if work_root is not None:
            self._write_worktree(Path(work_root), written)

    def _remote_url(self, repo: Repo, name: str) -> str | None:
        vals = repo.config.get(f"remote.{name}.url")
        if vals:
            return vals[0]
        return repo.remotes.get(name)

    def _set_config(self, repo: Repo, key: str, value: str, append: bool = False) -> None:
        if append:
            repo.config.setdefault(key, []).append(value)
        else:
            repo.config[key] = [value]
        # Keep the remote model in step with `config remote.<name>.*` writes.
        parts = key.split(".", 2)
        if parts[0] == "remote" and len(parts) == 3:
            name, field_name = parts[1], parts[2]
            if field_name == "url":
                repo.remotes[name] = value
            elif field_name == "fetch":
                repo.remote_fetch[name] = "\n".join(repo.config[key])

    @staticmethod
    def _positionals(args: tuple[str, ...], value_flags: tuple[str, ...] = ()) -> list[str]:
        """Non-option args, skipping flags that consume a value."""
        pos: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in value_flags:
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            pos.append(a)
            i += 1
        return pos

    def _pathspec_match(self, name: str, specs: list[str]) -> bool:
        included = []
        excluded = []
        for spec in specs:
            flags = []
            if spec.startswith(":("):
                magic, spec = spec[2:].split(")", 1)
                flags = magic.split(",")
            spec = spec.strip("/")
            match = spec in ("", ".") or name == spec or name.startswith(spec + "/")
            (excluded if "exclude" in flags else included).append(match)
        return (not included or any(included)) and not any(excluded)

    def _status_lines(self, repo: Repo, wt: Worktree | None, work_root: Path | None) -> list[str]:
        indexed = wt.indexed if wt is not None else repo.indexed
        workmap = wt.worktree if wt is not None else repo.worktree
        cone = wt.cone if wt is not None else repo.cone
        lines: list[str] = []
        if work_root is not None:
            for name, expected in indexed.items():
                work_path = work_root / name
                actual = work_path.read_text() if work_path.exists() else None
                if actual != expected:
                    lines.append(f" M {name}")
        for name in workmap:
            if name not in indexed:
                lines.append(f"?? {name}")
        # Detect untracked files actually present on disk in the worktree.
        if work_root is not None and work_root.is_dir():
            known = set(indexed.keys()) | set(workmap.keys())
            if wt is not None:
                known |= set(wt.files.keys())
            for entry in work_root.rglob("*"):
                if not entry.is_file():
                    continue
                rel = entry.relative_to(work_root).as_posix()
                if rel.startswith(".gf/"):
                    continue
                if wt is not None and (rel == ".git" or rel.startswith(".git/")):
                    continue
                if not self._cone_includes(rel, cone):
                    continue
                if rel not in known:
                    lines.append(f"?? {rel}")
        # Deduplicate while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for line in lines:
            if line not in seen:
                seen.add(line)
                deduped.append(line)
        return deduped

    def _worktree_dirty(self, wt: Worktree) -> bool:
        """Whether a linked worktree has modified or untracked files."""
        if wt.admin_dir is None:
            # Seeded worktrees carry no checkout state; keep them removable.
            return False
        root = Path(wt.path)
        for name, expected in wt.indexed.items():
            p = root / name
            if not p.exists() or p.read_text() != expected:
                return True
        if root.is_dir():
            known = set(wt.indexed.keys()) | set(wt.worktree.keys()) | set(wt.files.keys())
            for entry in root.rglob("*"):
                if not entry.is_file():
                    continue
                rel = entry.relative_to(root).as_posix()
                if rel.startswith(".gf/") or rel == ".git" or rel.startswith(".git/"):
                    continue
                if not self._cone_includes(rel, wt.cone):
                    continue
                if rel not in known:
                    return True
        return False

    def _dispatch(self, args: tuple[str, ...], path: str, cwd: Any, git_dir: Any, work_tree: Any) -> GitResult:
        repo, wt = self._repo_for(path)
        cmd = args[0]

        if cmd == "init":
            if "--bare" in args:
                repo.bare = True
            repo.head_ref = "refs/heads/master"
            if git_dir:
                self._write_head(Path(git_dir), ref=repo.head_ref)
            return GitResult(0, "", "")

        if cmd == "ls-remote":
            rest = [a for a in args[1:] if not a.startswith("-")]
            url = rest[0] if rest else None
            if url is None:
                raise GitError("usage: git ls-remote <url>")
            source = self._target_repo(url)
            # Trailing non-flag args are ref patterns: git matches a ref
            # when it equals the pattern or ends with `/<pattern>` (a
            # tail match beginning at a / boundary). HEAD lists only
            # when it matches a pattern too.
            patterns = rest[1:]

            def _wants(ref: str) -> bool:
                return not patterns or any(
                    ref == p or ref.endswith("/" + p)
                    for p in patterns)

            lines = []
            head_sha = source.refs.get(source.head_ref, source.head_sha or "")
            if head_sha and _wants("HEAD"):
                lines.append(f"{head_sha}\tHEAD")
            for ref in sorted(source.refs):
                if _wants(ref):
                    lines.append(f"{source.refs[ref]}\t{ref}")
            return GitResult(0, "\n".join(lines) + "\n" if lines else "", "")

        if cmd == "ls-tree":
            # `ls-tree <sha> [<path>]` — the `.gf` root-entry guard's
            # probe. Trees carry no root `.gf` by default; `Repo.gf_entry`
            # poisons a repo's trees so the refusal can be exercised.
            # Only a root `.gf` pathspec reports the entry — a nested
            # `sub/.gf` stays legal, as in real git.
            pos = self._positionals(args[1:])
            sha = pos[0] if pos else ""
            specs = pos[1:]
            if (
                sha and repo.gf_entry
                and any(s.rstrip("/") == ".gf" for s in specs)
            ):
                return GitResult(0, f"040000 tree {sha}\t.gf\n", "")
            return GitResult(0, "", "")

        if cmd == "config":
            if len(args) >= 3 and args[1] in ("--get", "--get-all"):
                key = f"{args[2]}.{args[3]}" if len(args) == 4 else args[2]
                if key.startswith("remote.") and key.endswith(".url"):
                    # `config --get remote.<name>.url` answers the remote
                    # URL wherever the model recorded it — a `config`
                    # write or a seeded `remotes` entry alike.
                    url = self._remote_url(repo, key[7:-4])
                    if url:
                        return GitResult(0, url + "\n", "")
                    return GitResult(1, "", "")
                vals = repo.config.get(key)
                if vals:
                    return GitResult(0, "\n".join(vals) + "\n", "")
                return GitResult(1, "", "")
            if len(args) >= 4 and args[1] == "--add":
                # Fetch refspecs are append-only: --add keeps existing lines.
                self._set_config(repo, args[2], args[3], append=True)
                return GitResult(0, "", "")
            # config key value
            if len(args) == 3:
                self._set_config(repo, args[1], args[2])
                return GitResult(0, "", "")

        if cmd == "fetch":
            # fetch [--no-tags|--filter=...|--depth=N|--prune] <remote> [<ref-or-sha>...]
            pos = [a for a in args[1:] if not a.startswith("-")]
            remote_name = pos[0] if pos else ""
            targeted = pos[1:]
            remote_url = self._remote_url(repo, remote_name)
            if remote_url:
                source = self._target_repo(remote_url)
                if source.gf_entry:
                    # A poisoned root entry travels with the fetched
                    # objects; the guard's `ls-tree` on THIS repo must
                    # see it too.
                    repo.gf_entry = True
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
                for spec in targeted:
                    # Targeted fetch — `fetch --no-tags origin <sha>`
                    # lands the named commit's history without writing
                    # a tracking ref (a commit sha is what no refspec
                    # line can name).
                    try:
                        sha = source.resolve(spec)
                    except GitError:
                        sha = spec
                    if sha not in source.commits:
                        raise GitError(
                            f"fatal: couldn't find remote ref {spec}")
                    for anc in self._ancestors(sha):
                        repo.commits[anc] = self._find_commit(anc)
            return GitResult(0, "", "")

        if cmd == "remote":
            if args[1] == "get-url":
                name = args[2]
                url = self._remote_url(repo, name)
                if url:
                    return GitResult(0, url + "\n", "")
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
                    self._set_config(repo, f"remote.{name}.url", url)
                    self._set_config(repo, f"remote.{name}.fetch", "+refs/heads/*:refs/remotes/origin/*")
                return GitResult(0, "", "")
            if args[1] == "set-head":
                # remote set-head <name> -a
                name = args[2]
                remote_url = self._remote_url(repo, name)
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
            if "--absolute-git-dir" in args:
                key = next((k for k, r in self.repos.items() if r is repo), path)
                gitpath = (wt.admin_dir if wt is not None and wt.admin_dir else
                           key if repo.bare or git_dir else str(Path(key) / ".git"))
                return GitResult(0, gitpath + "\n", "")
            if "--git-common-dir" in args:
                # The one gitdir every worktree shares. Real git answers
                # the absolute common dir from a linked worktree and
                # `<root>/.git` (or the dir itself, when bare) from a
                # normal repo — production absolutizes a relative reply,
                # so return the absolute form for either shape.
                key = next(
                    (k for k, r in self.repos.items() if r is repo), path)
                common = key if repo.bare else str(Path(key) / ".git")
                return GitResult(0, common + "\n", "")
            if "HEAD" in args:
                # An unborn HEAD (a child with no commits yet) resolves
                # to no commit: real git dies rc=128 on EVERY HEAD form —
                # --verify, --short, bare HEAD, and --abbrev-ref alike.
                # Only once HEAD resolves do the per-form outputs apply.
                try:
                    sha = (self._head_sha(repo, wt) if wt is not None
                           else repo.resolve("HEAD"))
                except GitError:
                    sha = None
                if not sha:
                    raise GitError(
                        "fatal: ambiguous argument 'HEAD': unknown "
                        "revision or path not in the working tree")
                if "--abbrev-ref" in args:
                    branch = self._head_branch(repo, wt)
                    return GitResult(0, (branch or "HEAD") + "\n", "")
                out = sha[:12] if "--short" in args else sha
                return GitResult(0, out + "\n", "")
            ref = args[-1]
            # strip trailing ^{} for tag peel display; resolve handles it
            ref = ref.replace("^{}", "")
            wt_sha = wt.refs.get(ref) if wt is not None else None
            sha = wt_sha or repo.resolve(ref)
            out = sha[:12] if "--short" in args else sha
            return GitResult(0, out + "\n", "")

        if cmd == "symbolic-ref":
            ref = args[1]
            if wt is not None and ref == "HEAD":
                if wt.branch:
                    return GitResult(0, f"refs/heads/{wt.branch}\n", "")
                return GitResult(1, "", "ref HEAD is not a symbolic ref")
            if ref in repo.symrefs:
                return GitResult(0, repo.symrefs[ref] + "\n", "")
            if ref == "HEAD" and repo.head_ref:
                return GitResult(0, repo.head_ref + "\n", "")
            return GitResult(1, "", f"ref {ref} is not a symbolic ref")

        if cmd == "show-ref":
            if "--verify" in args:
                ref = args[-1]
                wt_sha = wt.refs.get(ref) if wt is not None else None
                if wt_sha:
                    return GitResult(0, wt_sha + " " + ref + "\n", "")
                resolved = None
                if ref in repo.symrefs:
                    resolved = repo.symrefs[ref]
                if resolved and resolved in repo.refs:
                    return GitResult(0, repo.refs[resolved] + " " + ref + "\n", "")
                if ref in repo.refs:
                    return GitResult(0, repo.refs[ref] + " " + ref + "\n", "")
                return GitResult(1, "", "")

        if cmd == "update-ref":
            # update-ref [-d] <ref> [<sha>]
            pos = self._positionals(args[1:], value_flags=())
            if not pos:
                raise GitError("usage: git update-ref <ref> [<sha>]")
            ref = pos[0]
            target = (
                wt.refs
                if wt is not None and ref.startswith("refs/worktree/")
                else repo.refs)
            if "-d" in args:
                target.pop(ref, None)
                return GitResult(0, "", "")
            if len(pos) < 2:
                raise GitError("usage: git update-ref <ref> <sha>")
            sha = repo.resolve(pos[1].replace("^{}", ""))
            if len(pos) > 2:
                expected = pos[2]
                existing = target.get(ref)
                if ((set(expected) == {"0"} and existing is not None) or
                    (set(expected) != {"0"} and existing != expected)):
                    raise GitError(f"cannot lock ref '{ref}': unexpected existing value")
            target[ref] = sha
            return GitResult(0, "", "")

        if cmd == "for-each-ref":
            refs = dict(repo.refs)
            if wt is not None:
                refs.update(wt.refs)
            contains = None
            if "--contains" in args:
                contains = args[args.index("--contains") + 1]
            pattern = next((a.split("=", 1)[1] for a in args if a.startswith("--format=")),
                           "%(objectname) %(refname)")
            names = [ref for ref in sorted(refs) if contains is None or
                     self._is_ancestor(contains, refs[ref]) or contains == refs[ref]]
            return GitResult(0, "".join(
                pattern.replace("%(refname)", ref).replace("%(objectname)", refs[ref]) + "\n"
                for ref in names), "")

        if cmd == "rev-list" and "--reverse" in args:
            left, right = args[-1].split("..", 1)
            excluded = self._ancestors(repo.resolve(left))
            ordered = []
            seen = set()
            def visit(sha):
                if sha in seen or sha in excluded:
                    return
                seen.add(sha)
                for parent in self._find_commit(sha).parents:
                    visit(parent)
                ordered.append(sha)
            visit(repo.resolve(right))
            return GitResult(0, "".join(sha + "\n" for sha in ordered), "")

        if cmd == "rev-list" and "--parents" in args:
            sha = repo.resolve(args[-1])
            parents = self._find_commit(sha).parents
            return GitResult(0, " ".join([sha, *parents]) + "\n", "")

        if cmd == "rev-list" and "--left-right" in args:
            # rev-list --left-right --count <a>...<b> — the drift
            # algorithm's pair-count (commits each side has that the
            # other lacks).
            spec = next((a for a in args[1:] if "..." in a), None)
            if spec is None:
                raise GitError("usage: rev-list --left-right <a>...<b>")
            a_ref, b_ref = spec.split("...", 1)
            a_sha = repo.resolve(a_ref.replace("^{}", ""))
            b_sha = repo.resolve(b_ref.replace("^{}", ""))
            left = self._ancestors(a_sha) - self._ancestors(b_sha)
            right = self._ancestors(b_sha) - self._ancestors(a_sha)
            return GitResult(0, f"{len(left)}\t{len(right)}\n", "")

        if cmd == "merge-base" and "--is-ancestor" in args:
            pos = self._positionals(args[1:], value_flags=())
            if len(pos) < 2:
                raise GitError("usage: merge-base --is-ancestor <a> <b>")
            a_sha = repo.resolve(pos[0].replace("^{}", ""))
            b_sha = repo.resolve(pos[1].replace("^{}", ""))
            if self._is_ancestor(a_sha, b_sha):
                return GitResult(0, "", "")
            return GitResult(1, "", "")

        if cmd == "checkout":
            if "-B" in args:
                # `checkout -B` stays in the vocabulary only so CLI-shape
                # tests can assert no producer issues it — under GF-D16
                # a producer emitting it is a defect.
                branch = args[args.index("-B") + 1]
                i = args.index("-B") + 2
                start_ref = args[i] if i < len(args) else "HEAD"
                sha = repo.resolve(start_ref.replace("^{}", ""))
            elif "-b" in args:
                branch = args[args.index("-b") + 1]
                i = args.index("-b") + 2
                start_ref = args[i] if i < len(args) else "HEAD"
                sha = repo.resolve(start_ref.replace("^{}", ""))
            else:
                branch = None
                # Skip flags like -f to find the commit-ish, then
                # resolve it — a plain `checkout <branch>` ATTACHES to
                # an existing local branch (it does not detach).
                target = next(
                    (a for a in args[1:] if not a.startswith("-")), None)
                if target is None:
                    raise GitError("fatal: checkout requires a target")
                sha = repo.resolve(target.replace("^{}", ""))
                if (
                    "--detach" not in args and "-d" not in args
                    and f"refs/heads/{target}" in repo.refs
                ):
                    branch = target
            if branch and wt is not None:
                # One checkout per branch per common dir.
                conflict = self._branch_in_use(repo, branch, exclude=wt)
                if conflict:
                    raise GitError(
                        f"fatal: '{branch}' is already used by worktree at '{conflict}'"
                    )
            commit = self._find_commit(sha)
            repo.commits[sha] = commit
            self._apply_commit(
                repo, wt, commit, sha, branch, git_dir, self._work_root(wt, work_tree),
            )
            return GitResult(0, f"HEAD is now at {sha}\n", "")

        if cmd == "merge" and "--ff-only" in args:
            ref = args[args.index("--ff-only") + 1]
            target_sha = repo.resolve(ref)
            cur_sha = self._head_sha(repo, wt) if wt is not None else repo.head_sha
            # Real ancestry: an up-to-date or strictly-ahead HEAD is a
            # no-op ("Already up to date"), a strictly-behind HEAD
            # fast-forwards, and anything else refuses.
            if target_sha == cur_sha or self._is_ancestor(
                    target_sha, cur_sha):
                return GitResult(0, "Already up to date.\n", "")
            commit = self._find_commit(target_sha)
            repo.commits[target_sha] = commit
            if cur_sha and not self._is_ancestor(cur_sha, target_sha):
                return GitResult(1, "", "merge: not fast-forward")
            self._apply_commit(
                repo, wt, commit, target_sha, self._head_branch(repo, wt),
                git_dir, self._work_root(wt, work_tree),
            )
            return GitResult(0, f"Updating {target_sha}\n", "")

        if cmd == "rebase":
            target_ref = args[-1]
            target_sha = repo.resolve(target_ref)
            commit = self._find_commit(target_sha)
            repo.commits[target_sha] = commit
            branch = self._head_branch(repo, wt)
            self._apply_commit(
                repo, wt, commit, target_sha, branch, git_dir, self._work_root(wt, work_tree),
            )
            return GitResult(0, f"Successfully rebased and updated refs/heads/{branch or 'HEAD'}.\n", "")

        if cmd == "ls-files" and "-v" in args:
            indexed = wt.indexed if wt is not None else repo.indexed
            return GitResult(0, "".join(f"H {name}\0" for name in sorted(indexed)), "")

        if cmd == "status" and "--porcelain" in args:
            lines = self._status_lines(repo, wt, self._work_root(wt, work_tree))
            if "--" in args:
                specs = list(args[args.index("--") + 1:])
                lines = [l for l in lines if self._pathspec_match(l[3:], specs)]
            return GitResult(0, ("\n".join(lines) + "\n") if lines else "", "")

        if cmd == "sparse-checkout":
            # sparse-checkout set --cone <dirs...>
            if len(args) > 1 and args[1] == "set":
                cone = frozenset(
                    a.strip("/") for a in args[2:] if not a.startswith("-") and a.strip("/")
                )
                if wt is not None:
                    self._set_cone(repo, wt, cone, self._work_root(wt, work_tree))
                else:
                    repo.cone = cone
                    new_map = self._cone_filter(repo.files or repo.indexed, cone)
                    if repo.indexed and work_tree:
                        self._resparsify_map(repo.indexed, new_map, Path(work_tree))
                    repo.indexed = dict(new_map)
                    repo.worktree = dict(new_map)
                return GitResult(0, "", "")
            raise GitError(f"unmocked git command: {' '.join(args)}")

        if cmd == "worktree":
            sub = args[1] if len(args) > 1 else ""
            if sub == "list" and "--porcelain" in args:
                out = []
                if repo.bare:
                    out.append(f"worktree {path}")
                    out.append("bare")
                    out.append("")
                for w in repo.worktrees:
                    out.append(f"worktree {w.path}")
                    if w.head:
                        out.append(f"HEAD {w.head}")
                    if w.branch:
                        out.append(f"branch refs/heads/{w.branch}")
                    elif w.head:
                        out.append("detached")
                    if w.locked is not None:
                        out.append(f"locked {w.locked}".rstrip())
                    out.append("")
                return GitResult(0, "\n".join(out) + "\n" if out else "", "")

            if sub == "add":
                # worktree add [--no-checkout] [--detach] [-b|-B <br>] <path> [<ref>]
                pos = self._positionals(args[2:], value_flags=("-b", "-B"))
                if not pos:
                    raise GitError("fatal: worktree add requires a path")
                target = str(Path(pos[0]).resolve())
                ref = pos[1] if len(pos) > 1 else "HEAD"
                new_branch = None
                for flag in ("-b", "-B"):
                    if flag in args:
                        new_branch = args[args.index(flag) + 1]
                sha = repo.resolve(ref)
                commit = self._find_commit(sha)
                if self._find_worktree(repo, target) is not None:
                    raise GitError(f"fatal: '{target}' already exists")
                made_branch = False
                if new_branch:
                    branch = new_branch
                    made_branch = True
                elif "--detach" in args or "-d" in args:
                    branch = None
                elif f"refs/heads/{ref}" in repo.refs:
                    branch = ref
                else:
                    # git names the new branch after the path basename.
                    branch = Path(target).name
                    made_branch = True
                if branch:
                    conflict = self._branch_in_use(repo, branch)
                    if conflict:
                        raise GitError(
                            f"fatal: '{branch}' is already used by worktree at '{conflict}'"
                        )
                    if f"refs/heads/{branch}" not in repo.refs:
                        repo.refs[f"refs/heads/{branch}"] = sha
                wt_path = Path(target)
                admin = Path(path) / "worktrees" / wt_path.name
                new_wt = Worktree(
                    path=target,
                    head=sha,
                    branch=branch,
                    admin_dir=str(admin),
                    files=dict(commit.files),
                    indexed=dict(commit.files),
                    worktree=dict(commit.files),
                )
                repo.worktrees.append(new_wt)
                wt_path.mkdir(parents=True, exist_ok=True)
                (wt_path / ".git").write_text(f"gitdir: {admin}\n")
                admin.mkdir(parents=True, exist_ok=True)
                (admin / "commondir").write_text("../..\n")
                (admin / "gitdir").write_text(f"{wt_path / '.git'}\n")
                self._write_head(
                    admin,
                    ref=f"refs/heads/{branch}" if branch else None,
                    sha=sha,
                )
                if "--no-checkout" not in args:
                    self._write_worktree(wt_path, commit.files)
                    new_wt.checked_out = True
                if not branch:
                    what = f"detached HEAD {sha[:7]}"
                elif made_branch:
                    what = f"new branch '{branch}'"
                else:
                    what = f"checking out '{branch}'"
                return GitResult(0, "", f"Preparing worktree ({what})\n")

            if sub == "lock":
                # worktree lock [--reason <r>] <path>
                reason = ""
                if "--reason" in args:
                    reason = args[args.index("--reason") + 1]
                for a in args[2:]:
                    if a.startswith("--reason="):
                        reason = a.split("=", 1)[1]
                pos = self._positionals(args[2:], value_flags=("--reason",))
                if not pos:
                    raise GitError("fatal: worktree lock requires a path")
                target = str(Path(pos[-1]).resolve())
                target_wt = self._find_worktree(repo, target)
                if target_wt is None:
                    raise GitError(f"fatal: '{target}' is not a working tree")
                if target_wt.locked is not None:
                    raise GitError(
                        f"fatal: '{target}' is already locked, reason: {target_wt.locked}"
                    )
                target_wt.locked = reason
                return GitResult(0, "", "")

            if sub == "unlock":
                pos = self._positionals(args[2:])
                if not pos:
                    raise GitError("fatal: worktree unlock requires a path")
                target = str(Path(pos[-1]).resolve())
                target_wt = self._find_worktree(repo, target)
                if target_wt is None:
                    raise GitError(f"fatal: '{target}' is not a working tree")
                if target_wt.locked is None:
                    raise GitError(f"fatal: '{target}' is not locked")
                target_wt.locked = None
                return GitResult(0, "", "")

            if sub == "remove":
                # worktree remove [--force] <path>
                pos = self._positionals(args[2:])
                if not pos:
                    raise GitError("fatal: worktree remove requires a path")
                target = str(Path(pos[-1]).resolve())
                target_wt = self._find_worktree(repo, target)
                if target_wt is None:
                    raise GitError(f"fatal: '{target}' is not a working tree")
                if target_wt.locked is not None and args.count("--force") < 2:
                    raise GitError(
                        f"fatal: cannot remove a locked working tree, lock reason: "
                        f"{target_wt.locked}\nuse 'remove -f -f' to override or unlock first"
                    )
                if "--force" not in args and self._worktree_dirty(target_wt):
                    raise GitError(
                        f"fatal: '{target}' contains modified or untracked files, "
                        "use --force to delete it"
                    )
                repo.worktrees = [w for w in repo.worktrees if w.path != target]
                shutil.rmtree(target, ignore_errors=True)
                if target_wt.admin_dir:
                    shutil.rmtree(target_wt.admin_dir, ignore_errors=True)
                return GitResult(0, "", "")

            raise GitError(f"unmocked git command: {' '.join(args)}")

        if cmd == "stash":
            sub = args[1] if len(args) > 1 else ""
            indexed = wt.indexed if wt is not None else repo.indexed
            workmap = wt.worktree if wt is not None else repo.worktree
            stashes = wt.stashes if wt is not None else repo.stashes
            cone = wt.cone if wt is not None else repo.cone
            root = self._work_root(wt, work_tree)
            if sub == "list":
                out = "".join(
                    f"stash@{{{i}}}: On {self._head_branch(repo, wt) or 'HEAD'}:"
                    f" {entry.get('message', 'WIP')}\n"
                    for i, entry in enumerate(stashes))
                return GitResult(0, out, "")
            if sub == "push":
                # stash push -u -m <msg>
                # Snapshot only the dirty/untracked files from disk so the
                # pop reapplies local changes on top of the updated worktree
                # without clobbering files the update changed.
                msg = (args[args.index("-m") + 1] if "-m" in args
                       else "WIP")
                snapshot: dict[str, str] = {}
                if root is not None:
                    for name in indexed:
                        p = root / name
                        if p.exists() and p.read_text() != indexed[name]:
                            snapshot[name] = p.read_text()
                    known = set(indexed.keys()) | set(workmap.keys())
                    if wt is not None:
                        known |= set(wt.files.keys())
                    for entry in root.rglob("*"):
                        if not entry.is_file():
                            continue
                        rel = entry.relative_to(root).as_posix()
                        if rel.startswith(".gf/"):
                            continue
                        if wt is not None and (rel == ".git" or rel.startswith(".git/")):
                            continue
                        if not self._cone_includes(rel, cone):
                            continue
                        if rel not in known:
                            snapshot[rel] = entry.read_text()
                stashes.append({"worktree": snapshot,
                                "indexed": dict(indexed),
                                "message": msg})
                # Reset tracked files to indexed (clean) state and remove the
                # now-stashed untracked files.
                workmap.clear()
                workmap.update(indexed)
                if root is not None:
                    self._write_worktree(root, indexed)
                    for name in list(snapshot.keys()):
                        if name not in indexed:
                            p = root / name
                            if p.exists():
                                p.unlink()
                return GitResult(0, "Saved working directory and index state\n", "")
            if sub == "pop":
                if not stashes:
                    return GitResult(1, "", "No stash entries found.")
                entry = stashes.pop()
                # Reapply stashed files on top of the current worktree.
                workmap.update(entry["worktree"])
                if root is not None:
                    for name, content in entry["worktree"].items():
                        p = root / name
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text(content)
                if "--index" in args:
                    # `pop --index` also restores the index partition —
                    # the staged/unstaged split is part of the work.
                    indexed.update(entry["indexed"])
                return GitResult(0, "", "")
            raise GitError(f"unmocked git command: {' '.join(args)}")

        raise GitError(f"unmocked git command: {' '.join(args)}")

    def _set_cone(self, repo: Repo, wt: Worktree, cone: frozenset[str], root: Path | None) -> None:
        """Apply `sparse-checkout set --cone` to a linked worktree."""
        source = wt.files or wt.indexed
        wt.cone = cone
        new_map = self._cone_filter(source, cone)
        if wt.checked_out and root is not None:
            self._resparsify_map(wt.indexed, new_map, root)
        wt.indexed = dict(new_map)
        wt.worktree = dict(new_map)

    def _resparsify_map(self, old_map: dict[str, str], new_map: dict[str, str], root: Path) -> None:
        """Bring the disk in line with a narrowed or widened cone."""
        for gone in set(old_map) - set(new_map):
            p = root / gone
            if p.exists():
                p.unlink()
        self._write_worktree(root, {n: c for n, c in new_map.items() if n not in old_map})

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
