# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`gf pull` rebuilds wiped `.gf` storage (F4 ruling).

Deleting the parent-root `.gf` tree removes the repo stores and shared
checkouts but leaves the manifest and the (now dangling) consumer links
in place. The binding still identifies itself lexically through the
link, so `gf pull` re-resolves the recorded-effective url, recreates the
repo store and shared checkout in place, and reports `Pulled` — the
consumer files match upstream.

Pins:
- one binding: wipe `.gf`, `gf pull` → `Pulled`, store+checkout rebuilt
  under `.gf`, and the link serves the advanced upstream HEAD.
- two bindings on the wiped store, `gf pull` selecting only one: the
  selected binding rebuilds and its files match upstream. (The
  unselected sibling's disposition is deliberately not asserted — it
  may materialize on its own later pull.)

R5-A pair: a pull whose store phase fails removes the repo store it
created (a leaked half-made store keeps `HEAD` on `refs/heads/master`,
which the retry's checkout `worktree add` reports as already used),
while the same failure joining a pre-existing store leaves it in place.

Real git over a local bare upstream via the `gf` subprocess.
Designed-red while the rebuild half of F4 lands.
"""

import shutil
import subprocess
from pathlib import Path

from conftest import gf, git


# ---------------------------------------------------------------------------
# helpers


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} rc={r.returncode}:\n{r.stderr}")
    return r


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str = "upstream") -> Path:
    """Bare upstream with docs/api/x.txt + tools/t.txt on master."""
    up = tmp_path / name
    _git("init", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "x.txt").write_text(f"api on master in {name}")
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text(f"tool on master in {name}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "init")
    _git("-C", work, "push", "origin", "master")
    return up


def _advance(up: Path, tmp_path: Path, tag: str) -> None:
    """Push one more commit to upstream master."""
    work = tmp_path / f"_adv_{tag}"
    _git("clone", str(up), str(work))
    (work / "docs" / "api" / "x.txt").write_text(f"api {tag}")
    (work / "tools" / "t.txt").write_text(f"tool {tag}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", tag)
    _git("-C", work, "push", "origin", "master")


# ---------------------------------------------------------------------------
# F4 — pull rebuilds wiped .gf storage


def test_pull_rebuilds_wiped_store_checkout_and_serves(tmp_path):
    """Wiping the parent's `.gf` leaves a dangling consumer link; `gf
    pull` recreates the repo store + shared checkout in place and the
    link serves the new upstream HEAD (store fetch happened — the files
    could not come from the deleted store)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    _advance(up, tmp_path, "adv")

    shutil.rmtree(parent / ".gf")
    link = parent / "vendor" / "api"
    assert link.is_symlink()            # the link survives the wipe

    r = gf("-C", str(parent), "pull")
    assert "Pulled api" in r.stdout

    # store + checkout rebuilt under .gf; the untouched link resolves
    # back into the checkout's mapped subdir
    assert (parent / ".gf" / "repos").is_dir()
    checkouts = [p for p in (parent / ".gf" / "wt").glob("*/*")
                 if p.is_dir()]
    assert checkouts, "no shared checkout rebuilt under .gf/wt"
    assert link.is_symlink()
    assert link.resolve().is_relative_to(
        (parent / ".gf" / "wt").resolve())
    assert (link / "x.txt").read_text().strip() == "api adv"


def test_pull_selected_binding_rebuilds_wiped_store(tmp_path):
    """With two bindings on the wiped store, `gf pull <path>` selecting
    one rebuilds the selected binding: `Pulled`, the store recreated,
    the consumer files matching upstream. The unselected sibling is out
    of the ruling's scope — it may materialize on its own later pull."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")
    _advance(up, tmp_path, "adv")

    shutil.rmtree(parent / ".gf")

    r = gf("-C", str(parent), "pull", "vendor/api")
    assert "Pulled api" in r.stdout

    link = parent / "vendor" / "api"
    assert link.is_symlink()
    assert (parent / ".gf" / "repos").is_dir()
    assert link.resolve().is_relative_to(
        (parent / ".gf" / "wt").resolve())
    assert (link / "x.txt").read_text().strip() == "api adv"


# ---------------------------------------------------------------------------
# R5-A — a failed pull removes the repo store it created, keeps others'


def test_pull_init_subfolder_retry_after_failed_store_fetch(
        tmp_path, monkeypatch):
    """A binding recorded by `gf init <path> --url <repo>/<subdir>`
    resolves to a subfolder and is converted on its first `gf pull`: the
    placeholder child (only `.gf`, no commits) is removed and the
    consumer link created (spec `gf pull` conversion bullet) through the
    `gf clone` subfolder steps — create or reuse the repo store, fetch,
    ensure the shared checkout, place the link. When the fetch that
    creates the store fails — refused here by git's own
    `protocol.file.allow=never`, which fails the fetch after the bare
    store is initialised — spec Failure cleanup removes "a repo store
    this command itself created", so no `.gf/repos/<repo-key>/git`
    survives the failed pull. Once the transport is allowed again the
    retry creates the store fresh and serves the link; a leaked
    half-made store keeps `HEAD` on `refs/heads/master`, which the
    retry's `worktree add` reports as already used.
    """
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "init", "child", "--url", f"{up}/docs/api")

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")
    first = gf("-C", str(parent), "pull", check=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT")
    monkeypatch.delenv("GIT_CONFIG_KEY_0")
    monkeypatch.delenv("GIT_CONFIG_VALUE_0")

    err = first.stderr + first.stdout
    assert first.returncode != 0, (
        first.returncode, first.stdout, first.stderr)
    # The refusal reached the store fetch, not an earlier phase.
    assert "fetch" in err, err

    # Failure cleanup: the store this pull created is removed — no
    # `.gf/repos/<repo-key>` survives the failed pull.
    repos = parent / ".gf" / "repos"
    leaked = list(repos.iterdir()) if repos.is_dir() else []
    assert leaked == [], f"failed pull leaked repo store(s): {leaked}"

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    link = parent / "child"
    assert link.is_symlink()
    assert (link / "x.txt").read_text().strip() == \
        "api on master in upstream"


def test_pull_failed_fetch_keeps_preexisting_store(
        tmp_path, monkeypatch):
    """Control arm: the same refused store fetch on a repo store the
    pull did NOT create leaves it in place — Failure cleanup retains "a
    repo store or checkout that was pre-existing or already serving
    other bindings". The binding's consumer link is removed first so
    the pull really has a binding to re-serve (the join path) when its
    store fetch fails; the surviving store keeps its gitdir and
    recorded origin and serves the retry.
    """
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api")
    link = parent / "vendor" / "api"
    assert link.is_symlink()
    stores = list((parent / ".gf" / "repos").glob("*/git"))
    assert len(stores) == 1, stores
    store = stores[0]
    origin = _git("--git-dir", store, "config",
                  "remote.origin.url").stdout.strip()

    link.unlink()  # force re-serve of the binding on the next pull

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")
    first = gf("-C", str(parent), "pull", check=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT")
    monkeypatch.delenv("GIT_CONFIG_KEY_0")
    monkeypatch.delenv("GIT_CONFIG_VALUE_0")

    err = first.stderr + first.stdout
    assert first.returncode != 0, (
        first.returncode, first.stdout, first.stderr)
    assert "fetch" in err, err  # refused at the store fetch

    # The pre-existing store survives untouched — same gitdir, same
    # recorded origin — and no consumer link is half-created.
    assert (store / "HEAD").is_file()
    assert _git("--git-dir", store, "config",
                "remote.origin.url").stdout.strip() == origin
    assert not link.exists() and not link.is_symlink()

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert link.is_symlink()
    assert (link / "x.txt").read_text().strip() == \
        "api on master in upstream"
