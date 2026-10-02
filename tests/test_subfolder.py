# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Gap pins for subfolder bindings.

This file holds only the subfolder-binding pins not covered elsewhere,
adapted from the donor `test_subfolder.py` at `b3df631` (the rejected
first attempt) with non-cone sparse assertions and ref-<slug> key pins
stripped:

  - the POSITIVE `git worktree prune` arm — locked gf checkouts survive
    prune (test_store_checkout.py proves the lock happens and that an
    unlocked record is pruned; the survival arm itself was unpinned);
  - plain-path URL forms end-to-end at real-git CLI level — whole repo,
    nested local path, remote `.git` marker, longest-prefix ls-remote
    probe over `file://`, and the unresolvable error surface (unit-level
    resolution lives in test_url_resolution.py);
  - suite invariants: manifest schema keys and the
    no-host-test-outside-platform.py invariant (the .gf-literal pin
    already lives at test_layout.py::test_gf_layout_literals_live_only_in_layout_module).

Authorities: spec URL resolution (L132-144), Checkout integrity (L103);
docs/gf-constraints.md ("Do not add new manifest fields", "Do not
branch on the host platform in command logic"); arch GF-D8/D12. Real
git over local bare upstreams;
`file://localhost` exercises the remote resolution rules over git's
local transport (allowed by the conftest protocol guard — no network).
"""

import os
import re
import subprocess
import tomllib
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
    up = tmp_path / name
    _git("init", "--bare", str(up))
    work = tmp_path / f"_seed_{name}"
    _git("clone", str(up), str(work))
    (work / "docs" / "api").mkdir(parents=True)
    (work / "docs" / "api" / "reference.md").write_text(f"ref in {name}")
    (work / "docs" / "api" / "spec.md").write_text(f"spec in {name}")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-qm", "init")
    _git("-C", work, "push", "-q", "origin", "master")
    return up


# ---------------------------------------------------------------------------
# A5 — `git worktree prune` keeps checkouts


def test_worktree_prune_keeps_locked_checkouts(tmp_path):
    """A gf checkout is a locked, gitfile-less linked worktree of its
    store — `git worktree prune` through the store's gitdir leaves it,
    and prune inside the parent repo cannot even see it (spec L91/L101
    Checkout integrity; GF-D8; plan A5 bullet)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs" / "api"), "vendor/api")

    store = next((parent / ".gf" / "repos").glob("*/git"))
    wt = next((parent / ".gf" / "wt").glob("*/master"))
    admin = store / "worktrees" / "master"
    assert (admin / "locked").is_file()  # gf locked it at ensure time

    # Prune through the store gitdir (the checkout is its linked
    # worktree) and through the parent repo (which must not see it).
    _git("--git-dir", str(store), "worktree", "prune", "--expire", "now")
    git("worktree", "prune", cwd=parent)

    assert wt.is_dir() and admin.is_dir() and (admin / "locked").is_file()
    assert (wt / "docs" / "api" / "reference.md").is_file()
    # the consumer link still serves the mapped subdir end to end
    assert sorted(os.listdir(parent / "vendor" / "api")) == [
        "reference.md", "spec.md"]
    r = gf("-C", str(parent), "status")
    assert re.search(r"^api\s", r.stdout, re.M)


# ---------------------------------------------------------------------------
# A5 — plain-path URL forms resolve end to end


def test_plain_path_url_forms_resolve(tmp_path):
    """Real-git URL forms: bare local path → whole repo; local path
    inside a repo → nested subfolder binding; remote `.git` segment →
    lexical split, no probe; remote without `.git` → longest-prefix
    ls-remote resolution (spec L132-144; plan A5 bullet)."""
    up = _upstream(tmp_path)
    dotgit = _upstream(tmp_path, "repo.git")
    parent = _parent(tmp_path)

    # Whole repo: the URL is the repository itself.
    gf("-C", str(parent), "clone", str(up), "vendor/lib")
    lib = parent / "vendor" / "lib"
    assert lib.is_dir() and not lib.is_symlink()
    assert (lib / ".gf" / "git" / "HEAD").is_file()
    assert (lib / "docs" / "api" / "reference.md").is_file()

    # Nested folder: local path inside the repository → walk-up
    # boundary → subfolder binding (consumer link).
    gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api")
    link = parent / "vendor" / "api"
    assert link.is_symlink()
    assert sorted(os.listdir(link)) == ["reference.md", "spec.md"]

    # `.git` marker on a remote URL: lexical split after the first
    # `.git`-ending segment, no probe (file://localhost runs the remote
    # rules over git's local transport — no network).
    gf("-C", str(parent), "clone",
       f"file://localhost{dotgit}/docs/api", "vendor/marked")
    marked = parent / "vendor" / "marked"
    assert marked.is_symlink()
    assert sorted(os.listdir(marked)) == ["reference.md", "spec.md"]
    manifest = tomllib.loads((parent / "gf.toml").read_text())
    entry = next(e for e in manifest["git_folder"]
                 if e["name"] == "marked")
    assert entry["url"] == f"file://localhost{dotgit}/docs/api"

    # Longest-prefix probe: remote URL with no `.git` segment — the full
    # URL fails ls-remote, the repo prefix answers, rest is the subdir.
    gf("-C", str(parent), "clone",
       f"file://localhost{up}/docs/api", "vendor/probed")
    probed = parent / "vendor" / "probed"
    assert probed.is_symlink()
    assert sorted(os.listdir(probed)) == ["reference.md", "spec.md"]

    # Resolved-identity pins (spec URL resolution: the longest answering
    # prefix is the repo URL). A `file://` fixture URL under the test
    # temp directory can misresolve to the gf repository itself — the
    # suite lives under it, so a probe that climbed too far would still
    # answer. Each binding must serve its own repository's content, and
    # every repo store must record exactly the repository its binding's
    # url resolved to — a resolution to any other repo fails here.
    assert (link / "reference.md").read_text() == "ref in upstream"
    assert (marked / "reference.md").read_text() == "ref in repo.git"
    assert (probed / "reference.md").read_text() == "ref in upstream"
    origins = {
        _git("--git-dir", store, "config", "remote.origin.url")
        .stdout.strip()
        for store in (parent / ".gf" / "repos").glob("*/git")
    }
    assert origins == {
        str(up.resolve()),                 # vendor/api — local walk-up
        f"file://localhost{dotgit}",       # vendor/marked — .git split
        f"file://localhost{up}",           # vendor/probed — prefix probe
    }


def test_unresolvable_url_fails_with_clear_enveloped_error(tmp_path):
    """An unresolvable URL reaches the CLI as the RESOLVER's error —
    spec L142 mandates the hint "mark the repository boundary by writing
    '.git'" for the all-probes-fail case (its own example is
    whole-repo-intent). The pin asserts that hint text plus the §Error
    handling envelope fields (folder name+path+operation), a deterministic
    non-zero exit, no traceback, and no child left behind. The
    resolver-level wording twin is pinned by test_url_resolution.py::
    test_unresolvable_url_errors_naming_the_dot_git_hint."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs" / "api"), "vendor/api")

    # The prefix must have no repository ancestor — a tmp_path-local
    # `file://` URL would longest-prefix resolve to the gf repo itself
    # (tests/fixtures/tmp lives under it). `/tmp` is repo-free.
    bad = "file://localhost/tmp/definitely-not-a-repo-gf/sub/dir"
    r = gf("-C", str(parent), "clone", bad, "vendor/bad", check=False)
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    err = r.stderr + r.stdout
    assert "bad" in err and "vendor/bad" in err and "clone" in err
    # the resolver's mandated hint reaches the user (spec L140)
    assert "could not resolve" in err
    assert "mark the repository boundary by writing '.git'" in err
    assert not os.path.lexists(parent / "vendor" / "bad")


# ---------------------------------------------------------------------------
# A8 — manifest schema + no host tests


def test_manifest_schema_keys_are_exactly_the_documented_four(tmp_path):
    """Plan D2 + A8 + docs/gf-constraints.md "Do not add new manifest
    fields": a `[[git_folder]]` entry carries exactly
    {name, url, ref, path}; the subfolder rides in the plain `url`
    string — no separator syntax (donor pin, adapted)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "vendor/lib")
    gf("-C", str(parent), "clone", f"{up}/docs/api", "vendor/api")

    manifest = tomllib.loads((parent / "gf.toml").read_text())
    folders = manifest["git_folder"]
    assert len(folders) == 2
    for entry in folders:
        assert set(entry) == {"name", "url", "ref", "path"}, entry
    sub = next(e for e in folders if e["name"] == "api")
    assert sub["url"] == f"{up}/docs/api"


def test_no_host_platform_test_outside_platform_module():
    """A8 + docs/gf-constraints.md "Do not branch on the host platform in
    command logic": no `sys.platform`, `os.name`, `platform.system`, or
    `os.uname` outside `src/gf/platform.py`, and no host-conditioned
    skip/xfail in the test suite (donor pin, adapted)."""
    root = Path(__file__).resolve().parent.parent
    src = root / "src" / "gf"
    host_re = re.compile(
        r"sys\.platform|os\.name|platform\.system|os\.uname")
    offenders = []
    for py in sorted(src.glob("*.py")):
        if py.name == "platform.py":
            continue
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            if host_re.search(line):
                offenders.append(
                    f"src/gf/{py.name}:{lineno}: {line.strip()}")
    skip_re = re.compile(
        r"(skipif|xfail)[^\n]*(sys\.platform|platform\.system|os\.name|"
        r"darwin|linux|win32|windows)")
    for py in sorted((root / "tests").glob("*.py")):
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            if skip_re.search(line):
                offenders.append(
                    f"tests/{py.name}:{lineno}: {line.strip()}")
    assert offenders == [], f"host test outside platform.py: {offenders}"
