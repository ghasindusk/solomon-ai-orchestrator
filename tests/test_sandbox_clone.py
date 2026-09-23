import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.sandbox_clone import is_incompatible_path, local_clone_path, sync_after, sync_before


def _run(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", check=True
    )


def make_repo(tmp_path, name="origin") -> pathlib.Path:
    repo = tmp_path / name
    repo.mkdir()
    _run("init", "-b", "main", cwd=repo)
    _run("config", "user.email", "test@example.com", cwd=repo)
    _run("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _run("add", "README.md", cwd=repo)
    _run("commit", "-m", "initial commit", cwd=repo)
    return repo


# --- config-driven path matching ---


def test_is_incompatible_path_matches_configured_root(tmp_path):
    nested = tmp_path / "sub" / "project"
    nested.mkdir(parents=True)
    config = {"incompatible_roots": [str(tmp_path)]}
    assert is_incompatible_path(nested, config=config)


def test_is_incompatible_path_no_match(tmp_path):
    config = {"incompatible_roots": ["Z:\\nowhere"]}
    assert not is_incompatible_path(tmp_path, config=config)


def test_is_incompatible_path_empty_config():
    assert not is_incompatible_path("C:\\anything", config={})


def test_local_clone_path_prefers_explicit_mapping():
    config = {
        "local_clone_paths": {"proj_a": "C:\\explicit\\path"},
        "local_clone_root": "C:\\fallback",
    }
    assert local_clone_path("proj_a", config=config) == pathlib.Path("C:\\explicit\\path")
    assert local_clone_path("proj_b", config=config) == pathlib.Path("C:\\fallback") / "proj_b"


def test_local_clone_path_none_when_unconfigured():
    assert local_clone_path("proj_a", config={}) is None


# --- sync_before ---


def test_sync_before_clones_when_clone_missing(tmp_path):
    origin = make_repo(tmp_path)
    clone_path = tmp_path / "clone"

    result = sync_before(origin, clone_path)

    assert result.ok
    assert (clone_path / "README.md").exists()


def test_sync_before_fast_forwards_existing_clone(tmp_path):
    origin = make_repo(tmp_path)
    clone_path = tmp_path / "clone"
    sync_before(origin, clone_path)

    (origin / "new.txt").write_text("from origin\n", encoding="utf-8")
    _run("add", "new.txt", cwd=origin)
    _run("commit", "-m", "add new.txt", cwd=origin)

    result = sync_before(origin, clone_path)

    assert result.ok
    assert (clone_path / "new.txt").exists()


def test_sync_before_refuses_when_clone_has_uncommitted_changes(tmp_path):
    origin = make_repo(tmp_path)
    clone_path = tmp_path / "clone"
    sync_before(origin, clone_path)
    (clone_path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    result = sync_before(origin, clone_path)

    assert not result.ok
    assert "uncommitted" in result.detail


# --- sync_after ---


def test_sync_after_commits_and_pulls_back_into_origin(tmp_path):
    origin = make_repo(tmp_path)
    clone_path = tmp_path / "clone"
    sync_before(origin, clone_path)
    (clone_path / "generated.txt").write_text("from codex\n", encoding="utf-8")

    result = sync_after(origin, clone_path, task_id="task-1")

    assert result.ok
    assert (origin / "generated.txt").exists()


def test_sync_after_is_a_noop_when_clone_has_no_changes(tmp_path):
    origin = make_repo(tmp_path)
    clone_path = tmp_path / "clone"
    sync_before(origin, clone_path)

    result = sync_after(origin, clone_path, task_id="task-1")

    assert result.ok


def test_sync_after_reports_failure_when_origin_has_diverged(tmp_path):
    origin = make_repo(tmp_path)
    clone_path = tmp_path / "clone"
    sync_before(origin, clone_path)
    (clone_path / "generated.txt").write_text("from codex\n", encoding="utf-8")

    # Origin gets an unrelated commit after the clone was synced, so the
    # clone's eventual pull-back is no longer a fast-forward.
    (origin / "origin_only.txt").write_text("from origin\n", encoding="utf-8")
    _run("add", "origin_only.txt", cwd=origin)
    _run("commit", "-m", "diverging commit", cwd=origin)

    result = sync_after(origin, clone_path, task_id="task-1")

    assert not result.ok
    # origin is left exactly as it was -- pull --ff-only never partially applies
    assert not (origin / "generated.txt").exists()
