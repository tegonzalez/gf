# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""P3 concurrency witnesses — the per-parent `gf.lock` serialization
contract (Change DC-DOC-PLAN-007, obligation O-concurrency; GF-D20;
GF-TRB-11 closure).

Expected behavior derives only from the admitted documents:

- gf-arch.md GF-D20 + "Serialization and locking": mutating commands
  (`clone`, `init`, `pull`, `rm`, `worktree add`, `worktree remove`)
  serialize on a blocking `flock` of `<git common dir>/gf.lock`, held
  from before the first planning read through command end. Read
  commands and passthroughs take no lock. One lock per parent
  repository covers every worktree of that parent. The descriptor is
  the lock — a kill or crash releases it, so no welded lock file can
  block the next writer.
- gf-spec.md "Security and authority": mutations of shared state are
  serialized per parent root; a second writer either waits or fails
  fast naming the contended resource — never a lost manifest update.

Lock-side probes use `fcntl.flock` on the contract-named path —
`<common dir>` is resolved with real `git rev-parse --git-common-dir`,
never by reading `src/gf/`.
"""

import fcntl
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from conftest import gf, git


# ---------------------------------------------------------------------------
# helpers — real-git fixtures, same style as test_preservation_unmap


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        pytest.fail(
            f"setup: git {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stderr}")
    return r


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", "-q", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root\n")
    git("add", "README", cwd=parent)
    git("commit", "-qm", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str = "upstream") -> Path:
    up = tmp_path / name
    _git("init", "-q", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", "-q", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text(f"api in {name}\n")
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text(f"tool in {name}\n")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")
    return up


def _common_dir(root: Path) -> Path:
    """The parent's git common dir — the `gf.lock` domain (GF-D20)."""
    out = _out("-C", root, "rev-parse", "--git-common-dir")
    p = Path(out)
    return p if p.is_absolute() else (root / p).resolve()


def _out(*args) -> str:
    return _git(*args).stdout.strip()


def _manifest_names(root: Path) -> set[str]:
    data = tomllib.loads((root / "gf.toml").read_text())
    return {f["name"] for f in data.get("git_folder", [])}


def _lockfile(root: Path) -> Path:
    return _common_dir(root) / "gf.lock"


def _spawn_gf(*args) -> subprocess.Popen:
    """A real `python -m gf` process, unmonitored."""
    return subprocess.Popen(
        [sys.executable, "-m", "gf", *map(str, args)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


# ---------------------------------------------------------------------------
# locking — a held lock blocks a mutating writer until release


def test_mutating_command_waits_on_held_gf_lock(tmp_path):
    """concurrency · locking: a mutating `gf` on a parent whose
    `<common dir>/gf.lock` is flock-held blocks until the holder
    releases, then completes — blocking acquisition, never a lost
    update or a crash (GF-D20)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "vendor/lib")

    lockpath = _lockfile(parent)
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        proc = _spawn_gf("-C", str(parent), "pull")
        try:
            # The writer must still be waiting on the lock.
            time.sleep(2.5)
            assert proc.poll() is None, (
                "mutating gf completed while gf.lock was held: "
                f"rc={proc.returncode}")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            fd = -1
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, (proc.returncode, out, err)
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        if proc.poll() is None:
            proc.kill()
    # consistent end state: the binding is still registered and usable
    assert _manifest_names(parent) == {"lib"}
    assert (parent / "vendor" / "lib" / ".gf" / "git" / "HEAD").is_file()


def test_gf_lock_domain_is_parent_common_dir_across_worktrees(tmp_path):
    """concurrency · locking: one `gf.lock` per parent repository —
    a mutating command run inside a LINKED parent worktree serializes
    on the main root's common-dir lock, not a per-worktree file
    (GF-D20 "covers every worktree of that parent")."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    wt2 = tmp_path / "wt2"
    gf("-C", str(parent), "worktree", "add", str(wt2))
    assert (wt2 / "vendor" / "api").is_symlink()
    # the linked worktree's common dir IS the parent's — one domain
    assert _common_dir(wt2) == _common_dir(parent)

    lockpath = _lockfile(parent)
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        proc = _spawn_gf("-C", str(wt2), "pull")
        try:
            time.sleep(2.5)
            assert proc.poll() is None, (
                "gf run in a linked worktree completed while the "
                "parent-domain gf.lock was held")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            fd = -1
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, (proc.returncode, out, err)
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        if proc.poll() is None:
            proc.kill()


def test_reads_and_passthroughs_not_blocked_by_held_lock(tmp_path):
    """concurrency · locking: read commands and passthroughs take no
    lock — `status`, `ls`, and `gf git` all complete while the parent
    `gf.lock` is held by a writer (GF-D20 "read commands and
    passthroughs take none")."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "vendor/lib")

    lockpath = _lockfile(parent)
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for args in (("-C", str(parent), "status"),
                     ("-C", str(parent), "ls"),
                     ("-C", str(parent / "vendor" / "lib"),
                      "git", "status")):
            r = gf(*args, check=False)
            assert r.returncode == 0, (
                f"read/passthrough {args} blocked or failed under a "
                f"held lock: rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_killed_lock_holder_leaves_no_weld(tmp_path):
    """concurrency · locking: the lock is fd-held — a process killed
    while holding `gf.lock` releases it; the next mutating command
    proceeds without waiting on or breaking a stale lock file
    (GF-D20 "no welded lock file"; a lock FILE may remain but cannot
    block)."""
    parent = _parent(tmp_path)
    lockpath = _lockfile(parent)
    lockpath.parent.mkdir(parents=True, exist_ok=True)

    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\n"
         "time.sleep(60)\n",
         str(lockpath)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=15)
    finally:
        if holder.poll() is None:
            holder.kill()

    # the next mutating command acquires immediately — no stale lock
    up = _upstream(tmp_path)
    t0 = time.monotonic()
    r = gf("-C", str(parent), "clone", str(up), "vendor/lib",
           check=False)
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert elapsed < 30, f"clone waited {elapsed:.1f}s on a dead holder"
    assert _manifest_names(parent) == {"lib"}


def test_concurrent_writers_serialize_manifest_consistent(tmp_path):
    """concurrency · two writers: concurrent `gf clone`s on disjoint
    bindings of one parent both complete and the manifest records BOTH
    — no last-writer-wins torn record (GF-TRB-11; GF-D20)."""
    up_a = _upstream(tmp_path, "upa")
    up_b = _upstream(tmp_path, "upb")
    parent = _parent(tmp_path)

    pa = _spawn_gf("-C", str(parent), "clone", str(up_a), "vendor/a")
    pb = _spawn_gf("-C", str(parent), "clone", str(up_b), "vendor/b")
    out_a, err_a = pa.communicate(timeout=120)
    out_b, err_b = pb.communicate(timeout=120)

    assert pa.returncode == 0, (pa.returncode, out_a, err_a)
    assert pb.returncode == 0, (pb.returncode, out_b, err_b)
    # the serialized result keeps BOTH bindings — a lost update drops
    # one side's manifest entry
    assert _manifest_names(parent) == {"a", "b"}, (
        (parent / "gf.toml").read_text())
    for name in ("a", "b"):
        child = parent / "vendor" / name
        assert (child / ".gf" / "git" / "HEAD").is_file()
        assert _git("-C", child, "--git-dir", ".gf/git",
                    "rev-parse", "HEAD",
                    check=False).returncode == 0
