# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`gf pull` per-binding recorded resolutions (`binding_urls`).

Receiving test for the F-D ruling ("shared checkouts with differing URL
spellings or identities"): two bindings of ONE repository reached via
different spellings — `file://…/upstream.git/docs/api` (the `.git`
segment marks the repo boundary) and `file://…/upstream/tools`, unified
by `repo_key` into a single store via an `upstream.git` symlink.

The defect: the store's `remote.origin.url` keeps only the CREATING
spelling, so a sibling binding spelled differently reconstructed
`origin + "/" + co.subdir` that never equaled its manifest url — every
`gf pull` re-ran `resolve_repo_url` (ls-remote probes) for it. The fix
records each served binding's effective url in the checkout state's
`binding_urls` map keyed by manifest `path` and compares THAT entry, so
an unchanged binding never re-resolves (spec URL resolution; ruling
F-D).

Observables, per the ruling's preference order:
  (a) behavioral — an in-process `resolve_repo_url` spy (monkeypatched
      module attribute, the seam `cmd_pull` calls through) plus an
      `ls-remote`-counting `GitCliBackend` wrapper: an unchanged binding
      costs ZERO resolutions/probes on pull;
  (b) record-level — `wt/<rk>/.<key>.state` carries `binding_urls` with
      each binding's own spelling keyed by manifest `path`;
  (c) regression direction — an override that changes a binding's
      effective url still resolves exactly once and re-records the map.
"""

import os
import subprocess
import tomllib
from pathlib import Path

from conftest import gf, git
from gf import cli, layout, shelf
from gf.backends import GitCliBackend


# ---------------------------------------------------------------------------
# helpers (same in-process-on-real-fs harness as test_pull_grouped)


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} rc={r.returncode}:\n{r.stderr}")
    return r


class _LogBackend(GitCliBackend):
    """Real-git backend recording (argv, kwargs) of every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict]] = []

    def git(self, *args, **kwargs):
        self.calls.append((tuple(map(str, args)), kwargs))
        return super().git(*args, **kwargs)

    def ls_remote_calls(self) -> list:
        return [c for c in self.calls if "ls-remote" in c[0]]


def _run(parent: Path, capsys, *args, backend=None):
    """In-process `gf -C <parent> <args>` on the real fs.

    Returns (exit_code, stdout). `-C` chdirs inside cli.main; restore.
    """
    old = Path.cwd()
    try:
        try:
            code = cli.main(["-C", str(parent), *map(str, args)],
                            backend=backend)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    finally:
        os.chdir(old)
    return code or 0, capsys.readouterr().out


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path, name: str = "upstream",
              marker: str = "api on master") -> Path:
    """Bare upstream with docs/api/x.txt + tools/t.txt and a dev branch."""
    up = tmp_path / name
    _git("init", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", str(up), str(work))
    (work / "docs/api").mkdir(parents=True)
    (work / "docs/api/x.txt").write_text(marker)
    (work / "tools").mkdir()
    (work / "tools/t.txt").write_text(f"tool in {name}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "init")
    _git("-C", work, "push", "origin", "master")
    _git("-C", work, "checkout", "-q", "-b", "dev")
    (work / "docs/api/dev.txt").write_text(f"dev in {name}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "dev")
    _git("-C", work, "push", "origin", "dev")
    return up


def _co(parent: Path, repo_url: str, key: str,
        subdir: str) -> layout.Checkout:
    """The `<key>` checkout of `repo_url`'s store under `parent`.

    `repo_url` is a spelling (the store key digests the normalized
    spelling): `file://{up}` and `file://{up}.git` share a key, a local
    path does not.
    """
    return layout.subfolder_checkout(parent, repo_url, key, subdir)


def _state(parent: Path, repo_url: str, key: str) -> dict:
    co = _co(parent, repo_url, key, "docs/api")
    return tomllib.loads(co.state.read_text())


def _spellings(tmp_path: Path) -> tuple[Path, str, str]:
    """One bare `upstream` reached by two spellings sharing one store key.

    Returns (up, api_url, tools_url): `api_url` spells the repo through
    an `upstream.git` symlink (the `.git` boundary resolves lexically —
    no probes); `tools_url` spells it without the suffix (repo_key
    strips `.git`, so both bindings land in ONE store while
    `remote.origin.url` keeps whichever spelling created it).
    """
    up = _upstream(tmp_path)
    os.symlink("upstream", tmp_path / "upstream.git")
    api_url = f"file://{up}.git/docs/api"
    tools_url = f"file://{up}/tools"
    return up, api_url, tools_url


def _clone_spellings(parent: Path, api_url: str, tools_url: str) -> None:
    gf("-C", str(parent), "clone", api_url, "vendor/api")
    gf("-C", str(parent), "clone", tools_url, "vendor/tools")


def _resolve_spy(monkeypatch) -> list[str]:
    """Wrap `shelf.resolve_repo_url`, recording the url of every call.

    `cmd_pull` reaches the resolver through the module attribute, so the
    monkeypatched wrapper observes routing-phase resolutions; the real
    function still runs underneath.
    """
    calls: list[str] = []
    orig = shelf.resolve_repo_url

    def _spy(url, parent_root=None, backend=None):
        calls.append(str(url))
        return orig(url, parent_root, backend)

    monkeypatch.setattr(shelf, "resolve_repo_url", _spy)
    return calls


# ---------------------------------------------------------------------------
# (a) behavioral: unchanged bindings never re-resolve


def test_pull_differing_url_spellings_never_resolve_again(
        tmp_path, capsys, monkeypatch):
    """F-D: two subfolder bindings of one repository reached through
    `upstream.git` and `upstream` spellings share one store whose
    `remote.origin.url` keeps the creating (`.git`) spelling. An
    unchanged `gf pull` must consult `resolve_repo_url` for NEITHER
    binding and issue ZERO `ls-remote` probes — the sibling's recorded
    resolution is its own `binding_urls` entry, not a re-derived
    `origin + "/" + subdir` that mismatched every pull."""
    up, api_url, tools_url = _spellings(tmp_path)
    parent = _parent(tmp_path)
    _clone_spellings(parent, api_url, tools_url)

    # setup sanity: one store serves both spellings, and its origin
    # keeps only the creating `.git` spelling — the mismatch seed.
    store = layout.repo_store(parent, f"file://{up}")
    assert store == layout.repo_store(parent, f"file://{up}.git")
    origin = subprocess.run(
        ["git", "--git-dir", str(store), "config", "remote.origin.url"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert origin == f"file://{up}.git"
    assert origin != f"file://{up}", "setup needs differing spellings"

    calls = _resolve_spy(monkeypatch)
    rec = _LogBackend()

    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert calls == [], f"unchanged bindings re-resolved: {calls}"
    assert rec.ls_remote_calls() == [], \
        f"unchanged bindings probed: {rec.ls_remote_calls()}"

    # pin "every pull", not just the first one after the fix lands
    rec.calls.clear()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert calls == []
    assert rec.ls_remote_calls() == []

    # both bindings still serve their files
    assert (parent / "vendor/api/x.txt").read_text().strip() == (
        "api on master")
    assert (parent / "vendor/tools/t.txt").read_text().strip() == (
        "tool in upstream")


def test_pull_differing_url_spellings_dead_resolver(
        tmp_path, capsys, monkeypatch):
    """F-D, strong form: with `resolve_repo_url` rigged to raise, a pull
    of the unchanged differently-spelled bindings still succeeds — no
    code path may consult the resolver for them."""
    up, api_url, tools_url = _spellings(tmp_path)
    parent = _parent(tmp_path)
    _clone_spellings(parent, api_url, tools_url)

    def _dead(url, parent_root=None, backend=None):
        raise AssertionError(
            f"resolve_repo_url invoked for recorded binding: {url}")

    monkeypatch.setattr(shelf, "resolve_repo_url", _dead)
    code, out = _run(parent, capsys, "pull")
    assert code == 0, out
    assert "Pulled api" in out and "Pulled tools" in out


# ---------------------------------------------------------------------------
# (b) record-level: each binding's own spelling, keyed by manifest path


def test_checkout_state_binding_urls_carry_each_spelling(
        tmp_path, capsys):
    """The shared checkout's state records `binding_urls` keyed by the
    manifest `path` with each binding's OWN effective url — `vendor/api`
    keeps its `.git` spelling and `vendor/tools` the plain one — while
    the scalar `url` is retained (last-served spelling). The map
    survives an unchanged pull byte-identical (no re-derivation)."""
    up, api_url, tools_url = _spellings(tmp_path)
    parent = _parent(tmp_path)
    _clone_spellings(parent, api_url, tools_url)

    st = _state(parent, f"file://{up}", "master")
    assert st["binding_urls"] == {
        "vendor/api": api_url,
        "vendor/tools": tools_url,
    }
    # scalar url retained: it names one of the served spellings
    assert st["url"] in (api_url, tools_url)

    code, _ = _run(parent, capsys, "pull")
    assert code == 0
    st = _state(parent, f"file://{up}", "master")
    assert st["binding_urls"] == {
        "vendor/api": api_url,
        "vendor/tools": tools_url,
    }


# ---------------------------------------------------------------------------
# (c) regression direction: a changed effective url resolves once


def test_pull_changed_binding_url_resolves_once_and_rerecords(
        tmp_path, capsys, monkeypatch):
    """An override that moves `vendor/api` to a DIFFERENT repo changes
    its effective url: the binding's own `binding_urls` entry no longer
    matches, so it resolves exactly once (`resolve_repo_url` called with
    the new spelling), is re-recorded on the new store's checkout map,
    and drops out of the vacated checkout's map — while the unchanged
    sibling still resolves zero times."""
    up_a, api_url, tools_url = _spellings(tmp_path)
    up_b = _upstream(tmp_path, "up_b", "api in B")
    parent = _parent(tmp_path)
    _clone_spellings(parent, api_url, tools_url)

    new_url = str(up_b / "docs/api")
    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\n'
        f'url = "{new_url}"\n')

    calls = _resolve_spy(monkeypatch)
    rec = _LogBackend()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out

    # exactly one resolution: the changed binding, spelled anew — the
    # unchanged sibling never re-resolves
    resolved = str((up_b / "docs/api").resolve())
    assert calls == [resolved], calls

    # the moved binding re-records on the new store's checkout map —
    # the store is keyed by the RESOLVED local repo path, not file://
    st_b = _state(parent, str(up_b), "master")
    assert st_b["binding_urls"]["vendor/api"] == resolved
    assert "docs/api" in st_b["bindings"]

    # ... and vacates the old checkout's map + bindings
    st_a = _state(parent, f"file://{up_a}", "master")
    assert set(st_a["binding_urls"]) == {"vendor/tools"}
    assert st_a["binding_urls"]["vendor/tools"] == tools_url
    assert set(st_a["bindings"]) == {"tools"}

    # steady state restored: the next pull resolves nothing again —
    # the re-recorded local spelling matches by path identity
    calls.clear()
    rec.calls.clear()
    code, out = _run(parent, capsys, "pull", backend=rec)
    assert code == 0, out
    assert calls == []
    assert rec.ls_remote_calls() == []
    assert (parent / "vendor/api/x.txt").read_text().strip() == (
        "api in B")
