# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Self-tests for the in-memory git model in tests/mock_git.py.

These tests pin the mock contract documented in docs/gf-testing.md
("Mock backend contract"), which the subfolder-binding work relies on:

- the command surface `gf` drives: ``init``/``init --bare``,
  ``ls-remote <url>``, ``remote add``/``set-url``/``set-head``,
  ``fetch``/``fetch --filter=<filter>``,
  ``config remote.origin.fetch <refspec>`` / ``config --get`` /
  ``config --add``, ``rev-parse HEAD``/``--short``/``--abbrev-ref``,
  ``show-ref --verify``, ``symbolic-ref``,
  ``checkout``/``checkout -B <b> <start>``/``checkout -f``,
  ``merge --ff-only``, ``status --porcelain`` including a ``-- <path>``
  scope, ``stash push -u -m <msg>``/``stash pop``,
  ``worktree list --porcelain``,
  ``worktree add [--no-checkout] [--detach] <path> <ref>``,
  ``worktree lock [--reason <r>] <path>``/``unlock``,
  ``worktree remove [--force]``, and
  ``sparse-checkout set --cone <dirs...>``;
- the shared-store semantics: refs are shared across linked worktrees
  of one common dir, a second checkout of one branch in one store fails
  the way git does, and a fetch call log exists so tests can assert one
  fetch per repo store.

Checkout commands are addressed the way `gf` addresses them
(docs/gf-spec.md "Subfolder-binding layout"): ``git_dir`` is the
linked-worktree admin dir ``<store>/worktrees/<name>`` and
``work_tree`` is the checkout path — the GIT_DIR/GIT_WORK_TREE pair.
"""

from pathlib import Path

SHA1 = "1111111111111111111111111111111111111111"
SHA2 = "2222222222222222222222222222222222222222"
SHA3 = "3333333333333333333333333333333333333333"

WILDCARD = "+refs/heads/*:refs/remotes/origin/*"
FEATURE_REF = "+refs/heads/feature:refs/remotes/origin/feature"

UPSTREAM_FILES = {
    "top.txt": "root",
    "docs/api/a.txt": "api",
    "docs/other/b.txt": "other",
    "src/c.txt": "src",
}


def _resolve(path) -> str:
    return str(Path(path).resolve())


def _seed_upstream(backend, path="/upstream", files=None, refs=None, head="master"):
    """Seed a bare upstream repo; returns the mock Repo model."""
    repo = backend.seed(path, bare=True, head=head)
    backend.add_commit(repo, SHA1, files or {"a.txt": "hello"})
    for ref, sha in (refs or {f"refs/heads/{head}": SHA1}).items():
        repo.refs[ref] = sha
    return repo


def _repo_store(backend, store="/store/git", upstream="/upstream"):
    """Create a bare repo store the way the spec's verified mechanism does:
    `init --bare`, `remote.origin.url`, the first fetch refspec line, then a
    filtered fetch."""
    backend.git("init", "--bare", git_dir=store)
    backend.git("config", "remote.origin.url", upstream, git_dir=store)
    backend.git("config", "remote.origin.fetch", WILDCARD, git_dir=store)
    backend.git("fetch", "--filter=blob:none", "origin", git_dir=store)
    return Path(store)


def _admin_dir(store, wt) -> Path:
    """The linked-worktree admin dir git creates: <store>/worktrees/<basename>."""
    return Path(store) / "worktrees" / Path(wt).name


def _add_checkout(backend, store, wt, ref="origin/master", check=True):
    """`worktree add --no-checkout --detach <wt> <ref>` — the gf creation flow."""
    return backend.git(
        "worktree", "add", "--no-checkout", "--detach", str(wt), ref,
        git_dir=store, check=check,
    )


def _checkout(backend, store, wt, branch="main", start="origin/master", check=True):
    """`checkout -B <branch> <start>` inside a linked checkout."""
    return backend.git(
        "checkout", "-B", branch, start,
        git_dir=_admin_dir(store, wt), work_tree=wt, check=check,
    )


def _worktree_stanzas(out: str) -> dict:
    """Parse `worktree list --porcelain` into {path: [attribute lines]}."""
    stanzas = {}
    cur = None
    for line in out.splitlines():
        if not line.strip():
            cur = None
            continue
        if line.startswith("worktree "):
            cur = line.split(" ", 1)[1]
            stanzas[cur] = []
        elif cur is not None:
            stanzas[cur].append(line)
    return stanzas


class TestInit:
    def test_init_writes_master_head(self, fs, mock_backend):
        r = mock_backend.git("init", git_dir="/child/.gf/git", work_tree="/child")
        assert r.returncode == 0
        head = Path("/child/.gf/git/HEAD").read_text()
        assert head.strip() == "ref: refs/heads/master"
        sym = mock_backend.git("symbolic-ref", "HEAD", git_dir="/child/.gf/git")
        assert sym.stdout.strip() == "refs/heads/master"

    def test_init_bare_creates_a_bare_store(self, fs, mock_backend):
        r = mock_backend.git("init", "--bare", git_dir="/store/git")
        assert r.returncode == 0
        repo = mock_backend.repos[_resolve("/store/git")]
        assert repo.bare is True


class TestLsRemote:
    def test_ls_remote_answers_for_a_repository_url(self, fs, mock_backend):
        _seed_upstream(mock_backend, files={"a.txt": "x"})
        r = mock_backend.git("ls-remote", "/upstream", check=False)
        assert r.returncode == 0
        assert SHA1 in r.stdout
        assert "refs/heads/master" in r.stdout
        assert "HEAD" in r.stdout

    def test_ls_remote_fails_for_a_path_below_the_repo(self, fs, mock_backend):
        """URL resolution probes longest prefixes: a subdir of a repo is not
        a repository URL and must fail."""
        _seed_upstream(mock_backend)
        r = mock_backend.git("ls-remote", "/upstream/docs/api", check=False)
        assert r.returncode != 0

    def test_ls_remote_fails_for_an_unknown_url(self, fs, mock_backend):
        r = mock_backend.git("ls-remote", "/no-such-repo", check=False)
        assert r.returncode != 0


class TestRemoteAndConfig:
    def test_remote_add_then_get_url(self, fs, mock_backend):
        mock_backend.git("init", "--bare", git_dir="/store/git")
        mock_backend.git("remote", "add", "origin", "/upstream", git_dir="/store/git")
        r = mock_backend.git("remote", "get-url", "origin", git_dir="/store/git")
        assert r.stdout.strip() == "/upstream"

    def test_remote_set_url_changes_the_url(self, fs, mock_backend):
        mock_backend.git("init", "--bare", git_dir="/store/git")
        mock_backend.git("remote", "add", "origin", "/upstream", git_dir="/store/git")
        mock_backend.git("remote", "set-url", "origin", "/other", git_dir="/store/git")
        r = mock_backend.git("remote", "get-url", "origin", git_dir="/store/git")
        assert r.stdout.strip() == "/other"

    def test_remote_set_head_points_origin_head_at_the_default_branch(self, fs, mock_backend):
        _seed_upstream(mock_backend)
        mock_backend.git("init", "--bare", git_dir="/store/git")
        mock_backend.git("remote", "add", "origin", "/upstream", git_dir="/store/git")
        r = mock_backend.git("remote", "set-head", "origin", "-a", git_dir="/store/git")
        assert r.returncode == 0
        sym = mock_backend.git(
            "symbolic-ref", "refs/remotes/origin/HEAD", git_dir="/store/git",
        )
        assert sym.stdout.strip() == "refs/remotes/origin/master"

    def test_config_set_then_get_refspec(self, fs, mock_backend):
        mock_backend.git("init", "--bare", git_dir="/store/git")
        mock_backend.git(
            "config", "remote.origin.fetch", WILDCARD, git_dir="/store/git",
        )
        r = mock_backend.git(
            "config", "--get", "remote.origin.fetch", git_dir="/store/git",
        )
        assert r.returncode == 0
        assert WILDCARD in r.stdout

    def test_config_add_appends_refspec_lines(self, fs, mock_backend):
        """Fetch refspecs are append-only (spec "Fetch refspecs"): `config --add`
        keeps the existing line. `--get` is the only read the contract lists, so
        it must expose every stored line for the coverage check to see."""
        mock_backend.git("init", "--bare", git_dir="/store/git")
        mock_backend.git(
            "config", "remote.origin.fetch", WILDCARD, git_dir="/store/git",
        )
        mock_backend.git(
            "config", "--add", "remote.origin.fetch", FEATURE_REF,
            git_dir="/store/git",
        )
        r = mock_backend.git(
            "config", "--get", "remote.origin.fetch", git_dir="/store/git",
        )
        assert r.returncode == 0
        assert FEATURE_REF in r.stdout
        assert WILDCARD in r.stdout

    def test_config_keys_are_independent(self, fs, mock_backend):
        """`config remote.origin.url` (the store creation in the spec's verified
        mechanism) must not land in `remote.origin.fetch`."""
        mock_backend.git("init", "--bare", git_dir="/store/git")
        mock_backend.git(
            "config", "remote.origin.url", "/upstream", git_dir="/store/git",
        )
        r = mock_backend.git(
            "config", "--get", "remote.origin.url", git_dir="/store/git",
        )
        assert r.returncode == 0
        assert r.stdout.strip() == "/upstream"
        missing = mock_backend.git(
            "config", "--get", "remote.origin.fetch",
            git_dir="/store/git", check=False,
        )
        assert missing.returncode != 0


class TestFetchAndCallLog:
    def test_fetch_maps_remote_heads_to_origin_refs(self, fs, mock_backend):
        _seed_upstream(mock_backend)
        store = _repo_store(mock_backend)
        r = mock_backend.git(
            "show-ref", "--verify", "refs/remotes/origin/master",
            git_dir=store, check=False,
        )
        assert r.returncode == 0
        sha = mock_backend.git(
            "rev-parse", "refs/remotes/origin/master", git_dir=store,
        )
        assert sha.stdout.strip() == SHA1

    def test_fetch_accepts_a_filter_option(self, fs, mock_backend):
        """`fetch --filter=blob:none origin` is how repo stores fetch."""
        _seed_upstream(mock_backend)
        mock_backend.git("init", "--bare", git_dir="/store/git")
        mock_backend.git("remote", "add", "origin", "/upstream", git_dir="/store/git")
        r = mock_backend.git(
            "fetch", "--filter=blob:none", "origin", git_dir="/store/git",
        )
        assert r.returncode == 0
        verify = mock_backend.git(
            "show-ref", "--verify", "refs/remotes/origin/master",
            git_dir="/store/git", check=False,
        )
        assert verify.returncode == 0

    def test_fetch_uses_config_remote_url(self, fs, mock_backend):
        """The verified mechanism stores the URL via `config remote.origin.url`;
        `fetch origin` must resolve it."""
        _seed_upstream(mock_backend)
        store = Path("/store/git")
        mock_backend.git("init", "--bare", git_dir=store)
        mock_backend.git(
            "config", "remote.origin.url", "/upstream", git_dir=store,
        )
        mock_backend.git("config", "remote.origin.fetch", WILDCARD, git_dir=store)
        mock_backend.git("fetch", "origin", git_dir=store)
        r = mock_backend.git(
            "show-ref", "--verify", "refs/remotes/origin/master",
            git_dir=store, check=False,
        )
        assert r.returncode == 0

    def test_fetch_calls_are_logged_per_store(self, fs, mock_backend):
        """The call log must let a test assert one fetch per repo store."""
        _seed_upstream(mock_backend)
        store_a = _repo_store(mock_backend, store="/store-a/git")
        store_b = _repo_store(mock_backend, store="/store-b/git")
        fetches = [c for c in mock_backend.calls if c[0][0] == "fetch"]
        assert len(fetches) == 2
        by_path = {_resolve(store_a): 0, _resolve(store_b): 0}
        for args, path in fetches:
            assert path in by_path
            by_path[path] += 1
            assert "--filter=blob:none" in args
            assert args[-1] == "origin"
        assert by_path == {_resolve(store_a): 1, _resolve(store_b): 1}


class TestRefReads:
    def _child(self, backend):
        """A non-bare repo with origin fetched, the whole-repo child shape."""
        _seed_upstream(backend)
        backend.git("init", git_dir="/child/.gf/git", work_tree="/child")
        backend.git(
            "remote", "add", "origin", "/upstream", git_dir="/child/.gf/git",
        )
        backend.git("fetch", "origin", git_dir="/child/.gf/git")
        backend.git(
            "checkout", "-B", "master", "origin/master",
            git_dir="/child/.gf/git", work_tree="/child",
        )
        return Path("/child/.gf/git")

    def test_rev_parse_head(self, fs, mock_backend):
        gitdir = self._child(mock_backend)
        r = mock_backend.git("rev-parse", "HEAD", git_dir=gitdir, work_tree="/child")
        assert r.stdout.strip() == SHA1

    def test_rev_parse_short(self, fs, mock_backend):
        gitdir = self._child(mock_backend)
        r = mock_backend.git(
            "rev-parse", "--short", "HEAD", git_dir=gitdir, work_tree="/child",
        )
        assert r.stdout.strip() == SHA1[:12]

    def test_rev_parse_abbrev_ref(self, fs, mock_backend):
        gitdir = self._child(mock_backend)
        r = mock_backend.git(
            "rev-parse", "--abbrev-ref", "HEAD",
            git_dir=gitdir, work_tree="/child",
        )
        assert r.stdout.strip() == "master"

    def test_show_ref_verify_found_and_missing(self, fs, mock_backend):
        gitdir = self._child(mock_backend)
        ok = mock_backend.git(
            "show-ref", "--verify", "refs/heads/master",
            git_dir=gitdir, check=False,
        )
        assert ok.returncode == 0
        assert SHA1 in ok.stdout
        missing = mock_backend.git(
            "show-ref", "--verify", "refs/heads/nope",
            git_dir=gitdir, check=False,
        )
        assert missing.returncode != 0

    def test_symbolic_ref_detached_head_fails(self, fs, mock_backend):
        gitdir = self._child(mock_backend)
        mock_backend.git(
            "checkout", SHA1, git_dir=gitdir, work_tree="/child",
        )
        r = mock_backend.git(
            "symbolic-ref", "HEAD", git_dir=gitdir, check=False,
        )
        assert r.returncode != 0


class TestCheckoutMergeStatusStash:
    def _child(self, backend):
        _seed_upstream(backend)
        backend.git("init", git_dir="/child/.gf/git", work_tree="/child")
        backend.git(
            "remote", "add", "origin", "/upstream", git_dir="/child/.gf/git",
        )
        backend.git("fetch", "origin", git_dir="/child/.gf/git")
        return Path("/child/.gf/git")

    def test_checkout_b_creates_branch_and_writes_files(self, fs, mock_backend):
        gitdir = self._child(mock_backend)
        mock_backend.git(
            "checkout", "-B", "master", "origin/master",
            git_dir=gitdir, work_tree="/child",
        )
        assert Path("/child/a.txt").read_text() == "hello"
        ok = mock_backend.git(
            "show-ref", "--verify", "refs/heads/master", git_dir=gitdir,
            check=False,
        )
        assert ok.returncode == 0

    def test_checkout_f_restores_dirty_files(self, fs, mock_backend):
        gitdir = self._child(mock_backend)
        mock_backend.git(
            "checkout", "-B", "master", "origin/master",
            git_dir=gitdir, work_tree="/child",
        )
        Path("/child/a.txt").write_text("dirty")
        mock_backend.git(
            "checkout", "-f", "-B", "master", "origin/master",
            git_dir=gitdir, work_tree="/child",
        )
        assert Path("/child/a.txt").read_text() == "hello"

    def test_merge_ff_only_advances_head(self, fs, mock_backend):
        gitdir = self._child(mock_backend)
        upstream = mock_backend.repos[_resolve("/upstream")]
        mock_backend.git(
            "checkout", "-B", "master", "origin/master",
            git_dir=gitdir, work_tree="/child",
        )
        mock_backend.add_commit(
            upstream, SHA2, {"a.txt": "v2"}, parents=[SHA1],
        )
        upstream.refs["refs/heads/master"] = SHA2
        mock_backend.git("fetch", "origin", git_dir=gitdir)
        r = mock_backend.git(
            "merge", "--ff-only", "origin/master",
            git_dir=gitdir, work_tree="/child",
        )
        assert r.returncode == 0
        head = mock_backend.git(
            "rev-parse", "HEAD", git_dir=gitdir, work_tree="/child",
        )
        assert head.stdout.strip() == SHA2
        assert Path("/child/a.txt").read_text() == "v2"

    def test_merge_ff_only_refuses_non_fast_forward(self, fs, mock_backend):
        gitdir = self._child(mock_backend)
        upstream = mock_backend.repos[_resolve("/upstream")]
        mock_backend.git(
            "checkout", "-B", "master", "origin/master",
            git_dir=gitdir, work_tree="/child",
        )
        # Unrelated line of history: SHA2 is not a descendant of HEAD.
        mock_backend.add_commit(upstream, SHA2, {"a.txt": "v2"})
        upstream.refs["refs/heads/master"] = SHA2
        mock_backend.git("fetch", "origin", git_dir=gitdir)
        r = mock_backend.git(
            "merge", "--ff-only", "origin/master",
            git_dir=gitdir, work_tree="/child", check=False,
        )
        assert r.returncode != 0

    def test_status_porcelain_reports_modified_and_untracked(self, fs, mock_backend):
        gitdir = self._child(mock_backend)
        mock_backend.git(
            "checkout", "-B", "master", "origin/master",
            git_dir=gitdir, work_tree="/child",
        )
        assert mock_backend.git(
            "status", "--porcelain", git_dir=gitdir, work_tree="/child",
        ).stdout.strip() == ""
        Path("/child/a.txt").write_text("dirty")
        Path("/child/new.txt").write_text("new")
        out = mock_backend.git(
            "status", "--porcelain", git_dir=gitdir, work_tree="/child",
        ).stdout
        assert " M a.txt" in out.splitlines()
        assert "?? new.txt" in out.splitlines()

    def test_stash_push_pop_roundtrip(self, fs, mock_backend):
        gitdir = self._child(mock_backend)
        mock_backend.git(
            "checkout", "-B", "master", "origin/master",
            git_dir=gitdir, work_tree="/child",
        )
        Path("/child/a.txt").write_text("dirty")
        Path("/child/new.txt").write_text("local work")
        mock_backend.git(
            "stash", "push", "-u", "-m", "wip",
            git_dir=gitdir, work_tree="/child",
        )
        assert mock_backend.git(
            "status", "--porcelain", git_dir=gitdir, work_tree="/child",
        ).stdout.strip() == ""
        assert not Path("/child/new.txt").exists()
        mock_backend.git(
            "stash", "pop", git_dir=gitdir, work_tree="/child",
        )
        assert Path("/child/a.txt").read_text() == "dirty"
        assert Path("/child/new.txt").read_text() == "local work"


class TestSharedStoreWorktrees:
    """The linked-worktree shared-store model behind subfolder bindings."""

    def _store(self, backend):
        _seed_upstream(backend, files=UPSTREAM_FILES)
        return _repo_store(backend)

    def test_worktree_add_no_checkout_detach_registers_the_checkout(
        self, fs, mock_backend
    ):
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        r = _add_checkout(mock_backend, store, wt)
        assert r.returncode == 0
        stanzas = _worktree_stanzas(
            mock_backend.git(
                "worktree", "list", "--porcelain", git_dir=store,
            ).stdout
        )
        assert str(wt) in stanzas
        attrs = stanzas[str(wt)]
        assert f"HEAD {SHA1}" in attrs
        assert "detached" in attrs
        assert not any(a.startswith("branch ") for a in attrs)
        # --no-checkout: git creates the directory but writes no files.
        assert wt.is_dir()
        assert not (wt / "top.txt").exists()

    def test_worktree_add_detach_without_no_checkout_writes_files(
        self, fs, mock_backend
    ):
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        mock_backend.git(
            "worktree", "add", "--detach", str(wt), "origin/master",
            git_dir=store,
        )
        assert (wt / "top.txt").read_text() == "root"
        stanzas = _worktree_stanzas(
            mock_backend.git("worktree", "list", "--porcelain", git_dir=store).stdout
        )
        assert "detached" in stanzas[str(wt)]

    def test_admin_dir_addressing_shares_store_refs(self, fs, mock_backend):
        """Commands run with GIT_DIR=<store>/worktrees/<name> see the common
        dir's refs."""
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        _add_checkout(mock_backend, store, wt)
        admin = _admin_dir(store, wt)
        ok = mock_backend.git(
            "show-ref", "--verify", "refs/remotes/origin/master",
            git_dir=admin, work_tree=wt, check=False,
        )
        assert ok.returncode == 0
        sha = mock_backend.git(
            "rev-parse", "origin/master", git_dir=admin, work_tree=wt,
        )
        assert sha.stdout.strip() == SHA1

    def test_checkout_b_attaches_the_branch_in_this_worktree(self, fs, mock_backend):
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        _add_checkout(mock_backend, store, wt)
        admin = _admin_dir(store, wt)
        r = _checkout(mock_backend, store, wt, "main")
        assert r.returncode == 0
        assert (wt / "top.txt").read_text() == "root"
        abbrev = mock_backend.git(
            "rev-parse", "--abbrev-ref", "HEAD",
            git_dir=admin, work_tree=wt,
        )
        assert abbrev.stdout.strip() == "main"
        sym = mock_backend.git(
            "symbolic-ref", "HEAD", git_dir=admin, work_tree=wt,
        )
        assert sym.stdout.strip() == "refs/heads/main"
        # The new branch ref lives in the common dir, visible at the store.
        ok = mock_backend.git(
            "show-ref", "--verify", "refs/heads/main",
            git_dir=store, check=False,
        )
        assert ok.returncode == 0

    def test_second_checkout_of_one_branch_in_one_store_fails(self, fs, mock_backend):
        """Git allows one checkout per branch per store."""
        store = self._store(mock_backend)
        wt1 = Path("/wt/main")
        wt2 = Path("/wt/other")
        _add_checkout(mock_backend, store, wt1)
        _checkout(mock_backend, store, wt1, "main")
        _add_checkout(mock_backend, store, wt2)
        r = _checkout(mock_backend, store, wt2, "main", check=False)
        assert r.returncode != 0
        assert "already used by worktree" in r.stderr
        assert "main" in r.stderr
        # A plain `worktree add <path> <branch>` of the checked-out branch
        # fails the same way.
        dup = mock_backend.git(
            "worktree", "add", "/wt/dup", "main", git_dir=store, check=False,
        )
        assert dup.returncode != 0
        assert "already used by worktree" in dup.stderr
        # A different branch is still fine in the same store.
        other = _checkout(mock_backend, store, wt2, "feature", "origin/master")
        assert other.returncode == 0

    def test_same_branch_in_another_store_is_allowed(self, fs, mock_backend):
        """The one-checkout-per-branch rule is scoped to one common dir."""
        store1 = self._store(mock_backend)
        store2 = _repo_store(mock_backend, store="/store2/git")
        _add_checkout(mock_backend, store1, Path("/wt/main"))
        r1 = _checkout(mock_backend, store1, Path("/wt/main"), "main")
        assert r1.returncode == 0
        wt2 = Path("/wt2/main")
        _add_checkout(mock_backend, store2, wt2)
        r2 = _checkout(mock_backend, store2, wt2, "main")
        assert r2.returncode == 0

    def test_refs_are_shared_across_worktrees_of_one_common_dir(
        self, fs, mock_backend
    ):
        store = self._store(mock_backend)
        wt_main = Path("/wt/main")
        wt_feature = Path("/wt/feature")
        _add_checkout(mock_backend, store, wt_main)
        _add_checkout(mock_backend, store, wt_feature)
        _checkout(mock_backend, store, wt_main, "main")
        _checkout(mock_backend, store, wt_feature, "feature")
        # Each worktree reports its own branch.
        for wt, branch in ((wt_main, "main"), (wt_feature, "feature")):
            r = mock_backend.git(
                "rev-parse", "--abbrev-ref", "HEAD",
                git_dir=_admin_dir(store, wt), work_tree=wt,
            )
            assert r.stdout.strip() == branch
        # ...but both see the same common-dir refs.
        r = mock_backend.git(
            "show-ref", "--verify", "refs/heads/main",
            git_dir=_admin_dir(store, wt_feature), work_tree=wt_feature,
            check=False,
        )
        assert r.returncode == 0

    def test_detached_checkout_head(self, fs, mock_backend):
        """Tag/commit bindings detach: HEAD resolves to the sha, not a branch."""
        store = self._store(mock_backend)
        wt = Path("/wt/ref-v1")
        _add_checkout(mock_backend, store, wt, ref="origin/master")
        admin = _admin_dir(store, wt)
        r = mock_backend.git(
            "rev-parse", "--abbrev-ref", "HEAD", git_dir=admin, work_tree=wt,
        )
        assert r.stdout.strip() == "HEAD"
        sym = mock_backend.git(
            "symbolic-ref", "HEAD", git_dir=admin, work_tree=wt, check=False,
        )
        assert sym.returncode != 0
        sha = mock_backend.git(
            "rev-parse", "HEAD", git_dir=admin, work_tree=wt,
        )
        assert sha.stdout.strip() == SHA1

    def test_worktree_lock_unlock_and_list(self, fs, mock_backend):
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        _add_checkout(mock_backend, store, wt)
        _checkout(mock_backend, store, wt, "main")

        mock_backend.git(
            "worktree", "lock", "--reason", "gf-managed", str(wt),
            git_dir=store,
        )
        stanzas = _worktree_stanzas(
            mock_backend.git("worktree", "list", "--porcelain", git_dir=store).stdout
        )
        locked = [a for a in stanzas[str(wt)] if a.startswith("locked")]
        assert locked, "locked worktree must be marked in porcelain output"
        assert "gf-managed" in locked[0]

        mock_backend.git("worktree", "unlock", str(wt), git_dir=store)
        stanzas = _worktree_stanzas(
            mock_backend.git("worktree", "list", "--porcelain", git_dir=store).stdout
        )
        assert not any(a.startswith("locked") for a in stanzas[str(wt)])

    def test_worktree_lock_without_reason_marks_locked(self, fs, mock_backend):
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        _add_checkout(mock_backend, store, wt)
        mock_backend.git("worktree", "lock", str(wt), git_dir=store)
        stanzas = _worktree_stanzas(
            mock_backend.git("worktree", "list", "--porcelain", git_dir=store).stdout
        )
        assert "locked" in stanzas[str(wt)]

    def test_worktree_remove_deregisters_the_checkout(self, fs, mock_backend):
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        _add_checkout(mock_backend, store, wt)
        _checkout(mock_backend, store, wt, "main")
        mock_backend.git("worktree", "remove", str(wt), git_dir=store)
        stanzas = _worktree_stanzas(
            mock_backend.git("worktree", "list", "--porcelain", git_dir=store).stdout
        )
        assert str(wt) not in stanzas

    def test_worktree_remove_dirty_needs_force(self, fs, mock_backend):
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        _add_checkout(mock_backend, store, wt)
        _checkout(mock_backend, store, wt, "main")
        (wt / "top.txt").write_text("dirty")
        r = mock_backend.git(
            "worktree", "remove", str(wt), git_dir=store, check=False,
        )
        assert r.returncode != 0
        ok = mock_backend.git(
            "worktree", "remove", "--force", str(wt), git_dir=store,
        )
        assert ok.returncode == 0
        stanzas = _worktree_stanzas(
            mock_backend.git("worktree", "list", "--porcelain", git_dir=store).stdout
        )
        assert str(wt) not in stanzas

    def test_sparse_checkout_cone_limits_written_files(self, fs, mock_backend):
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        _add_checkout(mock_backend, store, wt)
        admin = _admin_dir(store, wt)
        mock_backend.git(
            "sparse-checkout", "set", "--cone", "docs/api",
            git_dir=admin, work_tree=wt,
        )
        _checkout(mock_backend, store, wt, "main")
        # Cone mode always includes root files plus the cone directories.
        assert (wt / "top.txt").read_text() == "root"
        assert (wt / "docs/api/a.txt").read_text() == "api"
        assert not (wt / "docs/other/b.txt").exists()
        assert not (wt / "src/c.txt").exists()
        # Sparse-skipped paths are not reported as missing or modified.
        assert mock_backend.git(
            "status", "--porcelain", git_dir=admin, work_tree=wt,
        ).stdout.strip() == ""

    def test_sparse_checkout_set_widens_the_cone(self, fs, mock_backend):
        """A binding joining a checkout widens the cone to the union of subdirs."""
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        _add_checkout(mock_backend, store, wt)
        admin = _admin_dir(store, wt)
        mock_backend.git(
            "sparse-checkout", "set", "--cone", "docs/api",
            git_dir=admin, work_tree=wt,
        )
        _checkout(mock_backend, store, wt, "main")
        mock_backend.git(
            "sparse-checkout", "set", "--cone", "docs/api", "src",
            git_dir=admin, work_tree=wt,
        )
        _checkout(mock_backend, store, wt, "main")
        assert (wt / "src/c.txt").read_text() == "src"
        assert not (wt / "docs/other/b.txt").exists()

    def test_status_porcelain_scopes_to_the_pathspec(self, fs, mock_backend):
        """`status --porcelain -- <subdir>` reports only changes under the
        repo-relative pathspec."""
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        _add_checkout(mock_backend, store, wt)
        admin = _admin_dir(store, wt)
        mock_backend.git(
            "sparse-checkout", "set", "--cone", "docs/api", "src",
            git_dir=admin, work_tree=wt,
        )
        _checkout(mock_backend, store, wt, "main")
        (wt / "src/c.txt").write_text("dirty")
        # An in-cone pathspec with no changes reports clean.
        clean = mock_backend.git(
            "status", "--porcelain", "--", "docs/api",
            git_dir=admin, work_tree=wt,
        )
        assert clean.stdout.strip() == ""
        # A change outside the pathspec is not reported inside it.
        (wt / "docs/api/a.txt").write_text("dirty")
        r = mock_backend.git(
            "status", "--porcelain", "--", "docs/api",
            git_dir=admin, work_tree=wt,
        )
        assert r.stdout.splitlines() == [" M docs/api/a.txt"]

    def test_stash_push_pop_inside_a_checkout(self, fs, mock_backend):
        store = self._store(mock_backend)
        wt = Path("/wt/main")
        _add_checkout(mock_backend, store, wt)
        admin = _admin_dir(store, wt)
        _checkout(mock_backend, store, wt, "main")
        (wt / "top.txt").write_text("dirty")
        (wt / "new.txt").write_text("local work")
        mock_backend.git(
            "stash", "push", "-u", "-m", "gf autostash",
            git_dir=admin, work_tree=wt,
        )
        assert mock_backend.git(
            "status", "--porcelain", git_dir=admin, work_tree=wt,
        ).stdout.strip() == ""
        mock_backend.git(
            "stash", "pop", git_dir=admin, work_tree=wt,
        )
        assert (wt / "top.txt").read_text() == "dirty"
        assert (wt / "new.txt").read_text() == "local work"

    def test_merge_ff_only_inside_a_checkout_updates_the_shared_branch(
        self, fs, mock_backend
    ):
        store = self._store(mock_backend)
        upstream = mock_backend.repos[_resolve("/upstream")]
        wt = Path("/wt/main")
        _add_checkout(mock_backend, store, wt)
        admin = _admin_dir(store, wt)
        _checkout(mock_backend, store, wt, "main")
        mock_backend.add_commit(
            upstream, SHA2, {**UPSTREAM_FILES, "top.txt": "v2"}, parents=[SHA1],
        )
        upstream.refs["refs/heads/master"] = SHA2
        mock_backend.git("fetch", "--filter=blob:none", "origin", git_dir=store)
        r = mock_backend.git(
            "merge", "--ff-only", "origin/master",
            git_dir=admin, work_tree=wt,
        )
        assert r.returncode == 0
        head = mock_backend.git(
            "rev-parse", "HEAD", git_dir=admin, work_tree=wt,
        )
        assert head.stdout.strip() == SHA2
        # The branch ref moved in the common dir, visible at the store.
        branch = mock_backend.git(
            "rev-parse", "refs/heads/main", git_dir=store,
        )
        assert branch.stdout.strip() == SHA2


class TestExistingWorktreeSeeding:
    """The seeded-worktree surface existing permutation tests rely on."""

    def test_worktree_list_reports_seeded_worktrees(self, fs, mock_backend):
        repo = mock_backend.seed("/repo", head="master")
        repo.worktrees = []
        mock_backend.add_worktree(repo, "/repo", head=SHA1, branch="master")
        mock_backend.add_worktree(repo, "/repo-feat", head=SHA2, branch="feature")
        out = mock_backend.git(
            "worktree", "list", "--porcelain", cwd="/repo",
        ).stdout
        stanzas = _worktree_stanzas(out)
        assert "/repo" in stanzas
        assert "/repo-feat" in stanzas
        assert "branch refs/heads/master" in stanzas["/repo"]
        assert "branch refs/heads/feature" in stanzas["/repo-feat"]
        # The main worktree is listed first.
        assert out.splitlines()[0] == "worktree /repo"

    def test_worktree_remove_drops_a_seeded_worktree(self, fs, mock_backend):
        repo = mock_backend.seed("/repo", head="master")
        repo.worktrees = []
        mock_backend.add_worktree(repo, "/repo", head=SHA1, branch="master")
        mock_backend.add_worktree(repo, "/repo-feat", head=SHA2, branch="feature")
        mock_backend.git("worktree", "remove", "/repo-feat", cwd="/repo")
        stanzas = _worktree_stanzas(
            mock_backend.git("worktree", "list", "--porcelain", cwd="/repo").stdout
        )
        assert "/repo-feat" not in stanzas
        assert "/repo" in stanzas
