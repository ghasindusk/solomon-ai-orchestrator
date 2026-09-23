import subprocess
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.worktree import WorktreeManager


def _run(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", check=True
    )


def make_repo(tmp_path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("init", "-b", "main", cwd=repo)
    _run("config", "user.email", "test@example.com", cwd=repo)
    _run("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _run("add", "README.md", cwd=repo)
    _run("commit", "-m", "initial commit", cwd=repo)
    return repo


def test_create_worktree_makes_branch_and_directory(tmp_path):
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo)
    wt_root = tmp_path / "worktrees"

    wt_path, result = manager.create("task-1", wt_root, base_branch="main")
    assert result.ok
    assert wt_path is not None
    assert wt_path.exists()
    assert (wt_path / "README.md").exists()

    branches = subprocess.run(
        ["git", "branch"], cwd=repo, capture_output=True, encoding="utf-8"
    ).stdout
    assert "solomon/task-1" in branches


def test_worktree_is_isolated_from_main_checkout(tmp_path):
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo)
    wt_root = tmp_path / "worktrees"
    wt_path, _ = manager.create("task-1", wt_root)

    # Edit + commit a file inside the worktree only.
    (wt_path / "new_file.txt").write_text("from the worktree\n", encoding="utf-8")
    _run("add", "new_file.txt", cwd=wt_path)
    _run("commit", "-m", "add new_file from worktree", cwd=wt_path)

    # The main repo checkout must be unaffected.
    assert not (repo / "new_file.txt").exists()


def test_merge_brings_worktree_changes_into_base_branch(tmp_path):
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo)
    wt_root = tmp_path / "worktrees"
    wt_path, _ = manager.create("task-1", wt_root)

    (wt_path / "new_file.txt").write_text("from the worktree\n", encoding="utf-8")
    _run("add", "new_file.txt", cwd=wt_path)
    _run("commit", "-m", "add new_file from worktree", cwd=wt_path)

    result = manager.merge("task-1", into_branch="main")
    assert result.ok
    assert (repo / "new_file.txt").exists()
    assert (repo / "new_file.txt").read_text(encoding="utf-8") == "from the worktree\n"


def test_merge_conflict_is_aborted_not_forced(tmp_path):
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo)
    wt_root = tmp_path / "worktrees"
    wt_path, _ = manager.create("task-1", wt_root)

    # Conflicting edits to the same file/line on both sides.
    (wt_path / "README.md").write_text("worktree version\n", encoding="utf-8")
    _run("add", "README.md", cwd=wt_path)
    _run("commit", "-m", "worktree edit", cwd=wt_path)

    (repo / "README.md").write_text("main version\n", encoding="utf-8")
    _run("add", "README.md", cwd=repo)
    _run("commit", "-m", "main edit", cwd=repo)

    result = manager.merge("task-1", into_branch="main")
    assert not result.ok
    # Repo must be left clean -- no in-progress merge, no conflict markers.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, encoding="utf-8")
    assert status.stdout.strip() == ""
    assert (repo / "README.md").read_text(encoding="utf-8") == "main version\n"


def test_remove_worktree(tmp_path):
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo)
    wt_root = tmp_path / "worktrees"
    wt_path, _ = manager.create("task-1", wt_root)
    assert wt_path.exists()

    result = manager.remove("task-1", wt_root)
    assert result.ok
    assert not wt_path.exists()


def test_create_fails_gracefully_for_nonexistent_base_branch(tmp_path):
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo)
    wt_path, result = manager.create("task-1", tmp_path / "worktrees", base_branch="no-such-branch")
    assert not result.ok
    assert wt_path is None
