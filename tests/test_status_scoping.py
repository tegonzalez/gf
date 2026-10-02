# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`gf status`/`gf ls`/drift scoped per binding, local-only.

Exercised against the command surface; the per-binding scoping
behaviors below are what this file pins.

Row scope (verbatim): ``cli.cmd_status``/``cmd_ls``; ``shelf.drift`` +
``_resolve_effective_sha_local``; the porcelain pathspec is the binding's
recorded consumer path resolved through the checkout — never positional
``co.subdir`` (a cwd deeper than the link root would under-scope);
porcelain anchored so cwd cannot skew it; recorded subdirs reach
``--`` literally so pathspec-magic/glob dir names stay scoped; drift
resolves refs in ``co.common_dir``; ``missing`` when
link/checkout/record absent or the mapped subdir is gone from the
store checkout.

Authorities: spec `gf status` (L275-286: scoped porcelain, --remote
local-only drift states, deterministic non-zero on unresolvable ref),
`gf ls` (L288+: `*` scoped to subdir), "Drift algorithm" (L498-502:
local-only, no fetch/remote/ls-remote), "URL resolution" L142 (status/ls
never resolve URLs); constraint "no network in ls/status".
"""

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from conftest import gf, git
from gf import cli, shelf
from gf.backends import GitCliBackend


# ---------------------------------------------------------------------------
# helpers


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} rc={r.returncode}:\n{r.stderr}")
    return r


def _out(*args) -> str:
    return _git(*args).stdout.strip()


class _LogBackend(GitCliBackend):
    """Real-git backend recording every call's argv."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def git(self, *args, **kwargs):
        self.calls.append(tuple(map(str, args)))
        return super().git(*args, **kwargs)

    def git_capture(self, *args, **kwargs):
        self.calls.append(tuple(map(str, args)))
        return super().git_capture(*args, **kwargs)


def _run(parent: Path, capsys, *args, backend=None):
    """In-process `gf -C <parent> <args>`; returns (rc, stdout, stderr)."""
    old = Path.cwd()
    out = err = ""
    try:
        try:
            code = cli.main(["-C", str(parent), *map(str, args)],
                            backend=backend)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    finally:
        os.chdir(old)
        cap = capsys.readouterr()
        out, err = cap.out, cap.err
    return code or 0, out, err


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    git("init", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path) -> Path:
    """Bare upstream: docs/api/a.txt + docs/api/sub/s.txt (deep-cwd dir),
    tools/t.txt, root.txt; tag v1."""
    up = tmp_path / "upstream"
    _git("init", "--bare", str(up))
    work = tmp_path / "_seed"
    _git("clone", str(up), str(work))
    (work / "docs" / "api" / "sub").mkdir(parents=True)
    (work / "docs" / "api" / "a.txt").write_text("api")
    (work / "docs" / "api" / "sub" / "s.txt").write_text("sub")
    (work / "tools").mkdir()
    (work / "tools" / "t.txt").write_text("tool")
    (work / "root.txt").write_text("root")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "init")
    _git("-C", work, "push", "origin", "master")
    _git("-C", work, "tag", "v1")
    _git("-C", work, "push", "origin", "v1")
    return up


def _clone_pair(parent: Path, up: Path) -> None:
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "master")


def _row_blocks(out: str) -> dict[str, str]:
    """name -> header line plus its following porcelain lines."""
    blocks: dict[str, str] = {}
    current = None
    for ln in out.splitlines():
        if re.match(r"^\S+\s{2,}/\S+\s+\[", ln):
            current = ln.split()[0]
            blocks[current] = ln + "\n"
        elif current:
            blocks[current] += ln + "\n"
    return blocks


def _row_line(out: str, name: str) -> str | None:
    return next((ln for ln in out.splitlines()
                 if re.match(rf"^{re.escape(name)}\s", ln)), None)


# ---------------------------------------------------------------------------
# pathspec-magic recorded subdir (F-B)


def _magic_upstream(tmp_path: Path, *subdirs: str) -> Path:
    """Bare upstream whose master tree holds `<dir>/m.txt` for each of
    `subdirs` — dir names meaningful to git pathspec syntax — plus a
    sibling `plain.txt` outside every mapping."""
    up = tmp_path / "upstream"
    _git("init", "--bare", str(up))
    work = tmp_path / "_seed"
    _git("clone", str(up), str(work))
    for subdir in subdirs:
        (work / subdir).mkdir(parents=True)
        (work / subdir / "m.txt").write_text("mapped")
    (work / "plain.txt").write_text("outside")
    _git("-C", work, "add", "-A")
    _git("-C", work, "commit", "-m", "init")
    _git("-C", work, "push", "origin", "master")
    return up


def _cone_skip_checks_supported(tmp_path: Path) -> bool:
    """Whether this git accepts `sparse-checkout set --cone
    --skip-checks` — the admission a `*?[]\\` dir operand needs (git
    2.47.3 does; the spec floor does not)."""
    repo = tmp_path / "_cap"
    _git("init", str(repo))
    return _git(
        "-C", repo, "sparse-checkout", "set", "--cone",
        "--skip-checks", "x", check=False).returncode == 0


def test_pathspec_magic_subdir_name_still_scopes(tmp_path):
    """F-B (round-3): a recorded subdir that is ALSO pathspec magic must
    reach `status --porcelain --` as data. `:(literal)foo` parses as
    magic naming a DIFFERENT path (literal `foo`), silently emptying
    the scope so dirt inside the binding reports as clean (verified on
    git 2.47.3: `-- ':(literal)foo'` on a checkout dirty only under the
    literal dir yields empty porcelain). Pins the scoped-status
    contract on every consumer of that porcelain: `gf status` lists the
    line, `gf ls` appends `*`, `status --remote` reports local-dirty,
    and an unforced `gf pull` aborts with the dirty exit code (spec
    L288 + `gf ls` `*` + Drift step 6 + pull dirty check / exit `3`;
    receiving item)."""
    subdir = ":(literal)foo"
    up = _magic_upstream(tmp_path, subdir)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / subdir), "vendor/magic")
    link = parent / "vendor" / "magic"
    assert (link / "m.txt").is_file()  # cone materialized the literal dir

    (link / "dirty.txt").write_text("edit through the consumer link")

    # `gf status`: the porcelain line appears under the binding's row.
    out = gf("-C", str(parent), "status").stdout
    block = _row_blocks(out)["magic"]
    assert re.search(rf"\?\? {re.escape(subdir)}/dirty\.txt", block)

    # `gf ls`: the `*` marks uncommitted work inside the mapping.
    ls_row = _row_line(gf("-C", str(parent), "ls").stdout, "magic")
    assert ls_row is not None
    assert re.search(r"[0-9a-f]+\*$", ls_row.strip())

    # `gf status --remote`: worktree-dirty within scope → never clean.
    remote_row = _row_line(
        gf("-C", str(parent), "status", "--remote").stdout, "magic")
    assert remote_row is not None
    assert re.search(r"\b(local-dirty|both)\b", remote_row)

    # `gf pull`: the scoped dirty check sees the edit and aborts with
    # the dirty-worktree exit code rather than updating the checkout.
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 3
    assert "dirty" in (r.stdout + r.stderr).lower()
    assert (link / "dirty.txt").is_file()  # the abort changed nothing


def test_glob_char_subdir_name_does_not_widen_scope(tmp_path):
    """F-B same contract, opposite direction: a bound subdir named `*`
    is a pathspec glob matching EVERYTHING (default pathspec `*`
    crosses `/`; verified on git 2.47.3: `-- '*'` reports the whole
    worktree). Without literal passing, dirt under a SIBLING mapping
    leaks into this binding's row — only paths inside the mapping may
    mark it (spec L288 + `gf ls` `*` + Drift step 6; receiving item).
    Cone admission of `*` needs `--skip-checks` (a `*?[]\\` operand)."""
    if not _cone_skip_checks_supported(tmp_path):
        pytest.skip("cone operand admission needs git supporting "
                    "`sparse-checkout set --cone --skip-checks`")
    up = _magic_upstream(tmp_path, "*", "x")
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "*"), "vendor/star")
    gf("-C", str(parent), "clone", str(up / "x"), "vendor/ex")
    assert (parent / "vendor" / "star" / "m.txt").is_file()
    assert (parent / "vendor" / "ex" / "m.txt").is_file()

    # dirt lands ONLY under the sibling's mapping
    (parent / "vendor" / "ex" / "leak.txt").write_text("sibling dirt")

    # `gf status`: the foreign porcelain line must not leak into the
    # `*` binding's row.
    blocks = _row_blocks(gf("-C", str(parent), "status").stdout)
    assert "star" in blocks and "ex" in blocks
    assert "leak.txt" not in blocks["star"]
    assert re.search(r"\?\? x/leak\.txt", blocks["ex"])

    # `gf ls`: `*` only on the sibling row — the star-binding's url
    # column itself ends in `*`, so compare the trailing hash field.
    out_ls = gf("-C", str(parent), "ls").stdout
    star = _row_line(out_ls, "star")
    ex = _row_line(out_ls, "ex")
    assert star is not None and ex is not None
    assert re.search(r"[0-9a-f]$", star.strip())
    assert re.search(r"[0-9a-f]\*$", ex.strip())

    # `gf status --remote`: the `*` binding stays clean; only the
    # sibling is local-dirty.
    out_r = gf("-C", str(parent), "status", "--remote").stdout
    assert re.search(r"\bclean$", _row_line(out_r, "star").strip())
    assert re.search(r"\blocal-dirty\b", _row_line(out_r, "ex"))

    # `gf pull`: the union-cone dirty check still aborts — the sibling
    # dirt is in the cone either way (spec pull dirty check, exit `3`).
    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 3


# ---------------------------------------------------------------------------
# per-binding scoping


def test_status_edit_scoped_to_own_binding(tmp_path):
    """An edit under one binding appears ONLY in its row — the porcelain
    run is scoped to the binding's subdir, not the whole shared checkout
    (spec L283; receiving item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "vendor" / "api" / "dirty.txt").write_text("edit under api")

    r = gf("-C", str(parent), "status")
    blocks = _row_blocks(r.stdout)
    assert "api" in blocks and "tools" in blocks
    assert "dirty.txt" in blocks["api"]
    assert "dirty.txt" not in blocks["tools"]
    assert re.search(r"\?\? .*docs/api/dirty\.txt", blocks["api"])


def test_status_deep_cwd_scopes_to_recorded_subdir(tmp_path):
    """A cwd deeper than the link root still scopes to the binding's
    RECORDED subdir (never positional co.subdir): an edit at the link
    root is reported from a cwd inside `link/sub` (row verbatim)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "vendor" / "api" / "dirty.txt").write_text("at link root")

    deep = parent / "vendor" / "api" / "sub"
    assert deep.is_dir()  # cone materialized the nested dir
    r = gf("-C", str(deep), "status")
    blocks = _row_blocks(r.stdout)
    # context selects the api binding only (cwd inside a child)
    assert "api" in blocks and "tools" not in blocks
    # and the run is scoped to docs/api — NOT narrowed to docs/api/sub
    assert "dirty.txt" in blocks["api"]


def test_ls_dirty_star_scoped_to_own_binding(tmp_path):
    """`gf ls` appends `*` only for the binding whose subdir has
    uncommitted changes — a sibling sharing the checkout is not marked
    (spec `gf ls`; receiving item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "vendor" / "api" / "dirty.txt").write_text("edit under api")

    out = gf("-C", str(parent), "ls").stdout
    api = _row_line(out, "api")
    tools = _row_line(out, "tools")
    assert api is not None and tools is not None
    assert re.search(r"[0-9a-f]+\*$", api.strip())
    assert re.search(r"[0-9a-f]+$", tools.strip())
    assert "*" not in tools


def test_status_remote_local_dirty_scoped_to_binding(tmp_path):
    """`status --remote` drift's worktree-dirty half is scoped the same
    way: the sibling checkout-shared binding stays `clean`
    (spec L283-286 + Drift algorithm step 6; receiving item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "vendor" / "api" / "dirty.txt").write_text("edit under api")

    out = gf("-C", str(parent), "status", "--remote").stdout
    api = _row_line(out, "api")
    tools = _row_line(out, "tools")
    assert api is not None and "local-dirty" in api
    assert tools is not None and re.search(r"\bclean\b", tools)


# ---------------------------------------------------------------------------
# missing / drift / local-only


def test_status_remote_missing_when_link_or_checkout_absent(tmp_path):
    """Absent consumer link → `missing`; absent checkout+record →
    `missing` for every binding it served (spec drift `missing`; row
    item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)

    (parent / "vendor" / "api").unlink()
    out = gf("-C", str(parent), "status", "--remote").stdout
    assert "missing" in _row_line(out, "api")
    assert re.search(r"\bclean\b", _row_line(out, "tools"))

    # checkout + worktree record gone → every binding it served is missing
    import shutil
    wt = parent / ".gf" / "wt"
    rk = next(p for p in wt.iterdir() if p.is_dir())
    store = parent / ".gf" / "repos" / rk.name / "git"
    shutil.rmtree(wt)
    shutil.rmtree(store / "worktrees")
    out2 = gf("-C", str(parent), "status", "--remote").stdout
    assert "missing" in _row_line(out2, "tools")


def test_status_remote_missing_when_only_checkout_dir_absent(tmp_path):
    """F3: deleting ONLY the checkout dir under `.gf/wt/<rk>/<ck>` — the
    worktree record under `<store>/worktrees/<ck>` and the per-checkout
    `.ck.state` survive, the repo store is intact — reports `missing`
    for the binding it served while sibling bindings on OTHER checkouts
    still report. `status` and `ls` render their rows without dying:
    no raw 'work tree' git failure escapes, and every command exits 0."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    gf("-C", str(parent), "clone", str(up / "tools"), "vendor/tools",
       "-b", "v1")

    import shutil
    wt = parent / ".gf" / "wt"
    rk = next(p for p in wt.iterdir() if p.is_dir())
    shutil.rmtree(rk / "master")   # the checkout dir only
    # the rest of the checkout's machinery survives
    assert (rk / ".master.state").is_file()
    store = parent / ".gf" / "repos" / rk.name / "git"
    assert (store / "worktrees" / "master" / "gitdir").is_file()

    r = gf("-C", str(parent), "status", "--remote", check=False)
    assert r.returncode == 0
    assert "work tree" not in (r.stdout + r.stderr)
    api = _row_line(r.stdout, "api")
    tools = _row_line(r.stdout, "tools")
    assert api is not None and "missing" in api
    # the sibling on the surviving ref=v1 checkout still reports
    assert tools is not None and "missing" not in tools
    assert re.search(r"\bclean\b", tools)

    # the non-remote row renderers degrade identically
    for args in (("status",), ("ls",)):
        r = gf("-C", str(parent), *args, check=False)
        assert r.returncode == 0, (
            f"gf {' '.join(args)} rc={r.returncode}:\n{r.stderr}")
        assert "work tree" not in (r.stdout + r.stderr)
        assert _row_line(r.stdout, "api") is not None
        assert _row_line(r.stdout, "tools") is not None


def test_status_remote_missing_when_upstream_deletes_mapped_subdir(
        tmp_path):
    """R5-D: upstream `git rm -r` of the mapped directory, then `gf
    pull`, applies the removal to the shared checkout — the binding's
    content is gone and its consumer link dangles while the checkout
    root and its sibling mappings survive, so `status --remote` reports
    `missing`, not `clean` (spec drift `missing` L525: the consumer
    link is absent — a dangling link resolves to nothing; Drift step 2;
    receiving item). `status`/`ls` still exit 0 listing every binding —
    the same degradation as the absent-checkout rows above."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    link = parent / "vendor" / "api"

    # upstream deletes the whole `docs` tree the api binding maps into
    work = tmp_path / "_rm"
    _git("clone", str(up), str(work))
    _git("-C", work, "rm", "-r", "docs")
    _git("-C", work, "commit", "-m", "remove docs")
    _git("-C", work, "push", "origin", "master")

    r = gf("-C", str(parent), "pull", check=False)
    assert r.returncode == 0, (
        f"gf pull rc={r.returncode}:\n{r.stdout}\n{r.stderr}")

    # the consumer link survives but dangles; the sibling mapping on
    # the same shared checkout is intact
    assert link.is_symlink() and os.path.lexists(link)
    assert not link.exists()
    wt = parent / ".gf" / "wt"
    rk = next(p for p in wt.iterdir() if p.is_dir())
    ck = rk / "master"  # both bindings share the one branch checkout
    assert not (ck / "docs" / "api").exists()
    assert (ck / "tools" / "t.txt").is_file()

    # `status --remote`: the binding whose mapped dir is gone is
    # `missing`; the sibling on the same checkout stays `clean`.
    out = gf("-C", str(parent), "status", "--remote").stdout
    api = _row_line(out, "api")
    tools = _row_line(out, "tools")
    assert api is not None and re.search(r"\bmissing$", api.strip())
    assert tools is not None and re.search(r"\bclean$", tools.strip())

    # the non-remote row renderers degrade identically
    for args in (("status",), ("ls",)):
        r = gf("-C", str(parent), *args, check=False)
        assert r.returncode == 0, (
            f"gf {' '.join(args)} rc={r.returncode}:\n{r.stderr}")
        assert _row_line(r.stdout, "api") is not None
        assert _row_line(r.stdout, "tools") is not None


def test_status_and_ls_make_no_network_calls(tmp_path, capsys):
    """status / status --remote / ls are local-only: zero fetch, ls-remote,
    or `git remote` calls on the recorded log (spec L283/288 + Drift
    algorithm step 1; constraint "no network in ls/status"; row item)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)

    rec = _LogBackend()
    for args in (("status",), ("status", "--remote"), ("ls",)):
        code, _, _ = _run(parent, capsys, *args, backend=rec)
        assert code == 0
    net = [c for c in rec.calls
           if c[0] in ("fetch", "ls-remote", "remote", "pull", "push")]
    assert net == [], f"network-class calls: {net}"


def test_status_remote_unresolvable_ref_deterministic_nonzero(
        tmp_path, capsys):
    """When the effective ref cannot be resolved locally, `status
    --remote` exits with a deterministic non-zero code instead of
    printing a drift state (spec L286 + Drift algorithm)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "gf.local.toml").write_text(
        '[[git_folder_override]]\nname = "api"\nref = "no-such-ref"\n')
    r = gf("-C", str(parent), "status", "--remote", check=False)
    assert r.returncode != 0
    assert "no-such-ref" in (r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# whole-repo regression / detached


def test_whole_repo_row_fields_unchanged_with_subfolder_siblings(
        tmp_path):
    """A whole-repo child's `status`/`ls` rows keep their shape when
    subfolder bindings share the manifest (spec formats; row item —
    byte-identity is guarded by the untouched existing status/ls tests;
    this pin asserts field equality under sibling presence)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up), "lib")
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")

    out = gf("-C", str(parent), "status").stdout
    lib = _row_line(out, "lib")
    assert lib is not None
    assert re.fullmatch(
        rf"lib\s+{re.escape(str(up))}\s+\[master\]", lib.strip())

    out_ls = gf("-C", str(parent), "ls").stdout
    lib_ls = _row_line(out_ls, "lib")
    assert re.fullmatch(
        rf"lib\s+{re.escape(str(up))}\s+\[master\]\s+[0-9a-f]+\*?",
        lib_ls.strip())


def test_ls_detached_binding_shows_empty_brackets(tmp_path):
    """A tag/checkout detached shared checkout prints `[]` for branch in
    `gf ls` and `gf status` (spec `gf ls`: empty brackets for detached
    HEAD; `ref=` checkout-key surface)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api",
       "-b", "v1")
    out = gf("-C", str(parent), "ls").stdout
    api = _row_line(out, "api")
    assert api is not None and "[]" in api
    out2 = gf("-C", str(parent), "status").stdout
    assert "[]" in _row_line(out2, "api")


# ---------------------------------------------------------------------------
# physical `.gf/wt` checkout cwd selection (R5-B + same-class sparse-cwd fix)
#
# spec "Target selection rules" (L473): a `cd -P` physical cwd inside
# `<root>/.gf/wt/<rk>/<ck>` resolves through the consumer link's realpath
# and selects the owning binding rather than nothing; no-arg selection is
# "all children at or below cwd" for a cwd above bindings and "the
# innermost binding" for a cwd inside nested maps. The synthesized no-arg
# operand must resolve independently of the process cwd.
#
# Pre-fix signature (verified against HEAD's src/gf via a PYTHONPATH
# overlay): every no-arg invocation below printed NOTHING and exited 0 —
# a silent empty selection — and `pull` was a no-op. With selection
# repaired but the cone operand unanchored (fix A only), `pull` from
# `<ck>/docs` rewrote the sparse cone to `docs/docs/api` + `docs/tools`,
# dematerialized `<ck>/docs/api` and left both consumer links dangling.


def _repo_key_dir(parent: Path) -> Path:
    """The single repo-key dir under `<root>/.gf/wt/` (one store in play)."""
    wt = parent / ".gf" / "wt"
    entries = [p for p in wt.iterdir() if p.is_dir()]
    assert len(entries) == 1, f"expected one repo store, found {entries}"
    assert re.fullmatch(r"upstream-[0-9a-f]{8}", entries[0].name)
    return entries[0]


def _sparse_cone(parent: Path, rk: Path, key: str) -> str:
    """The checkout's cone patterns from the store's sparse-checkout info."""
    f = (parent / ".gf" / "repos" / rk.name / "git" / "worktrees" / key
         / "info" / "sparse-checkout")
    return f.read_text() if f.is_file() else ""


def test_status_from_physical_checkout_key_dir(tmp_path):
    """`gf -C <root>/.gf/wt/<rk>/<ck> status` (cwd = the checkout key dir
    itself, the `cd -P` spelling) selects every binding whose map sits at
    or below it — here both bindings on the `master` checkout (spec L473;
    pre-fix: empty output)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    ck = _repo_key_dir(parent) / "master"
    assert ck.is_dir()

    blocks = _row_blocks(gf("-C", str(ck), "status").stdout)
    assert "api" in blocks and "tools" in blocks


def test_status_from_mid_checkout_dir_narrows_to_below(tmp_path):
    """`gf -C <ck>/docs status` selects only bindings at or below
    `<ck>/docs` — `api` (map `docs/api`) but not the `tools` sibling —
    while a physical cwd deep inside the binding's map
    (`<ck>/docs/api/sub`) still selects the binding itself (spec L473
    both arms; pre-fix: empty output)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    ck = _repo_key_dir(parent) / "master"
    mid = ck / "docs"
    deep = ck / "docs" / "api" / "sub"
    assert mid.is_dir() and deep.is_dir()  # cone materialized both

    blocks = _row_blocks(gf("-C", str(mid), "status").stdout)
    assert "api" in blocks
    assert "tools" not in blocks  # `tools`' map is NOT below `<ck>/docs`

    blocks = _row_blocks(gf("-C", str(deep), "status").stdout)
    assert "api" in blocks
    assert "tools" not in blocks


def test_status_innermost_of_nested_bindings_from_physical_cwd(tmp_path):
    """Nested `docs` + `docs/api` bindings on one checkout (the spec's
    innermost example): from the physical `<ck>/docs/api/sub` the
    innermost binding `api` selects; from `<ck>/docs` — the `docs`
    binding's own map — `docs` alone; from `<ck>` both (spec L473;
    pre-fix: empty output)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    gf("-C", str(parent), "clone", str(up / "docs"), "vendor/docs")
    gf("-C", str(parent), "clone", str(up / "docs/api"), "vendor/api")
    ck = _repo_key_dir(parent) / "master"
    deep = ck / "docs" / "api" / "sub"
    assert deep.is_dir()

    blocks = _row_blocks(gf("-C", str(deep), "status").stdout)
    assert "api" in blocks and "docs" not in blocks

    blocks = _row_blocks(gf("-C", str(ck / "docs"), "status").stdout)
    assert "docs" in blocks and "api" not in blocks

    blocks = _row_blocks(gf("-C", str(ck), "status").stdout)
    assert "docs" in blocks and "api" in blocks


def test_pull_from_mid_checkout_dir_keeps_cone_and_links(tmp_path):
    """`gf -C <ck>/docs pull` operates on the `api` binding (`Pulled
    api`; the sibling sharing the checkout reports `(moved with api)`)
    AND the sparse cone survives intact: `<ck>/docs/api` stays
    materialized and the `vendor/api` consumer link still resolves
    (spec L473 + Update algorithm "widening its sparse cone"; receiving
    pin for the `sparse-checkout` operand anchoring — pre-fix-B the cone
    was re-anchored at the process cwd to `docs/docs/api` and the link
    dangled)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    rk = _repo_key_dir(parent)
    ck = rk / "master"
    mid = ck / "docs"

    r = gf("-C", str(mid), "pull")
    assert "Pulled api" in r.stdout
    assert "Pulled tools (moved with api)" in r.stdout

    # fix-B witnesses: the mapped subdir stays materialized and every
    # consumer link still resolves to its checkout subdir.
    assert (ck / "docs" / "api" / "a.txt").is_file()
    assert (ck / "tools" / "t.txt").is_file()
    assert (parent / "vendor" / "api" / "a.txt").is_file()
    assert (parent / "vendor" / "tools" / "t.txt").is_file()
    # and the cone itself was not re-anchored at the caller's cwd.
    cone = _sparse_cone(parent, rk, "master")
    assert "/docs/api/" in cone and "/tools/" in cone
    assert "/docs/docs/" not in cone


def test_status_from_consumer_link_cwd_unchanged(tmp_path):
    """Control: no-arg `status` through the consumer link's own spelling
    keeps selecting exactly its binding — the pre-change behavior this
    remediation must not disturb (spec L473)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)

    blocks = _row_blocks(
        gf("-C", str(parent / "vendor" / "api"), "status").stdout)
    assert "api" in blocks and "tools" not in blocks


def test_noarg_selection_operand_resolves_independent_of_cwd(
        tmp_path, capsys, monkeypatch):
    """CLI-level pin on the synthesized operand: when no path args are
    given and cwd sits inside a child, `select_children` must receive an
    operand whose resolution cannot re-anchor at a physical `.gf/wt` cwd
    — `cwd / <arg>` collapses to the absolute operand itself, spelling
    the context child under every cwd (spec L473; R5-B). A
    parent-relative spelling doubles onto the checkout dir and selects
    nothing — the HEAD signature."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    ck = _repo_key_dir(parent) / "master"
    mid = ck / "docs"

    seen: list[list[str]] = []
    real_select = shelf.select_children

    def spy(parent_root, cwd, args, manifest_data):
        seen.append(list(args))
        return real_select(parent_root, cwd, args, manifest_data)

    monkeypatch.setattr(shelf, "select_children", spy)
    code, out, _ = _run(mid, capsys, "status")
    assert code == 0 and "api" in out

    assert len(seen) == 1 and len(seen[0]) == 1
    operand = Path(seen[0][0])
    # `mid / operand` collapses to `operand`: the join cannot double the
    # selection target onto the physical checkout cwd.
    assert operand.is_absolute()
    assert mid / operand == operand
    assert Path(os.path.realpath(operand)) == Path(os.path.realpath(mid))


# ---------------------------------------------------------------------------
# aliased bindings: two consumer paths on one map (R7-A ruling)
#
# Two bindings cloned onto the SAME upstream subdir share one checkout and
# one map — identical realpath target and identical depth. Spec "Target
# selection rules" + the `gf rm`/`gf status`/`gf ls` argument rows
# (gf-spec L271/L286/L298): each argument "selects the git-folder whose
# consumer path matches or contains the argument" — a spelled operand names
# its own binding lexically, so the aliases stay distinct under selection.
#
# Pre-fix signature (verified): `gf rm one` removed BOTH links and manifest
# entries; `gf status one` / `gf ls one` printed both rows.


def _clone_alias_pair(parent: Path, up: Path) -> None:
    """`one` and `two` bindings onto the same `docs/api` map of the
    shared `master` checkout."""
    gf("-C", str(parent), "clone", str(up / "docs/api"), "one")
    gf("-C", str(parent), "clone", str(up / "docs/api"), "two")


def _manifest_entries(parent: Path) -> list[tuple[str, str]]:
    """(name, path) pairs recorded in `<parent>/gf.toml`."""
    data = tomllib.loads((parent / "gf.toml").read_text())
    return [(f["name"], f["path"]) for f in data.get("git_folder", [])]


def test_spelled_alias_arg_scopes_status_and_ls_rows(tmp_path):
    """`gf status one` / `gf ls one` print exactly the spelled binding's
    row — never both aliases (spec arg rows: the operand names the
    consumer path it spells). The no-arg parent-root form still lists
    every binding below cwd, and a physical `.gf/wt` cwd — naming no
    consumer path — keeps selecting every alias on the map (spec L473
    documented residual; pre-fix: both rows for every form)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_alias_pair(parent, up)
    ck = _repo_key_dir(parent) / "master"

    # the spelled operand selects its own binding only
    assert set(_row_blocks(gf(
        "-C", str(parent), "status", "one").stdout)) == {"one"}
    assert set(_row_blocks(gf(
        "-C", str(parent), "status", "two").stdout)) == {"two"}
    assert set(_row_blocks(gf(
        "-C", str(parent), "ls", "one").stdout)) == {"one"}
    assert set(_row_blocks(gf(
        "-C", str(parent), "ls", "two").stdout)) == {"two"}

    # a cwd spelled through one consumer link selects that binding
    blocks = _row_blocks(
        gf("-C", str(parent / "one"), "status").stdout)
    assert set(blocks) == {"one"}

    # controls: below-the-parent selects both; a physical checkout cwd
    # names no consumer path, so every alias on the map still selects
    assert set(_row_blocks(
        gf("-C", str(parent), "status").stdout)) == {"one", "two"}
    assert set(_row_blocks(
        gf("-C", str(ck), "status").stdout)) == {"one", "two"}


def test_rm_spelled_alias_removes_only_that_binding(tmp_path):
    """`gf rm one` removes ONLY `one`: its consumer link and manifest
    entry. `two`'s link still serves the shared checkout's mapped
    content, its manifest entry survives, `gf status`/`gf ls` list it,
    and the checkout itself is untouched (spec `gf rm` arg row + L274:
    the consumer link and manifest entry are all a subfolder binding
    loses). Pre-fix: both links and both manifest entries were removed.
    """
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_alias_pair(parent, up)
    ck = _repo_key_dir(parent) / "master"
    link_two = parent / "two"

    r = gf("-C", str(parent), "rm", "one")
    assert r.stdout.splitlines() == ["Removed one"]

    # `one` loses only its consumer link; `two` is fully intact
    assert not os.path.lexists(parent / "one")
    assert link_two.is_symlink()
    assert link_two.resolve() == (ck / "docs" / "api").resolve()
    assert (link_two / "a.txt").read_text().strip() == "api"

    # the manifest keeps `two`'s entry only
    assert _manifest_entries(parent) == [("two", "two")]

    # the shared checkout and its mapped content are untouched
    assert (ck / "docs" / "api" / "a.txt").is_file()

    # `two` is still a working selected binding
    assert set(_row_blocks(
        gf("-C", str(parent), "status").stdout)) == {"two"}
    assert set(_row_blocks(
        gf("-C", str(parent), "ls").stdout)) == {"two"}


# ---------------------------------------------------------------------------
# dot-segment path operands (R8-B)
#
# spec "Target selection rules" (L473): "Every path argument identifies by
# spelled consumer-path identity only after lexical normalization — `.`/`..`
# segments resolve by directory identity." `sub/../vendor` names `vendor`, so
# `gf status`/`gf ls`/`gf pull` on that spelling select every binding below
# `vendor` — identically to the plain `vendor` operand.
#
# Pre-fix signature (verified against HEAD's src/gf via a PYTHONPATH overlay):
# `_below` held the raw operand in its lexical compare, so `status`,
# `ls`, and `pull` each saw an empty selection — printed nothing, exited 0.


def test_status_ls_pull_dotdot_spelling_selects_below_parent(tmp_path):
    """`gf status sub/../vendor` prints the same rows as `gf status
    vendor`; `gf ls` and `gf pull` on the same spelling operate on the
    same bindings (`Pulled api` / `Pulled tools`). The cwd-relative
    spelling `../vendor` from inside `sub/` is the same operand."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)
    (parent / "sub").mkdir()

    want = {"api", "tools"}
    assert set(_row_blocks(
        gf("-C", str(parent), "status", "vendor").stdout)) == want
    assert set(_row_blocks(gf(
        "-C", str(parent), "status", "sub/../vendor").stdout)) == want
    assert set(_row_blocks(gf(
        "-C", str(parent), "ls", "sub/../vendor").stdout)) == want

    pull = gf("-C", str(parent), "pull", "sub/../vendor")
    assert "Pulled api" in pull.stdout
    assert "Pulled tools" in pull.stdout

    # the same operand spelled from inside `sub/` — cwd joins `../vendor`
    assert set(_row_blocks(gf(
        "-C", str(parent / "sub"), "status", "../vendor").stdout)) == want


def test_status_dotdot_popping_own_link_selects_lexical_parent(tmp_path):
    """`gf status vendor/api/..` lists every `vendor` descendant — the
    operand normalizes lexically to `vendor`, so below-selection answers
    both bindings even though the operand's realpath pops the
    `vendor/api` link into `docs/`, containing only the `api` map
    (pre-fix: the realpath arm alone answered — `[api]` row only)."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)
    _clone_pair(parent, up)

    assert set(_row_blocks(gf(
        "-C", str(parent), "status", "vendor/api/..").stdout)
    ) == {"api", "tools"}
