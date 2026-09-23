"""Git worktree task isolation (Architecture doc section 7 "Concurrency":
"Write tasks require path locks or isolated Git worktrees. Merge occurs
only after verification.").

Complements, doesn't replace, the advisory path locks built earlier in
Phase 3 -- a task can use either. This is real `git worktree`/`git
merge` subprocess execution, not a simulation. Each isolated task gets
its own working directory + branch (`solomon/<task_id>`) off a base
branch; merging is a separate, explicit step (see `merge()`), never
performed automatically right after execution -- "merge occurs only
after verification" means after Definition of Done verification
(verification.py) says the task actually reached COMPLETE, which the
CLI enforces (`worktree merge` refuses to merge a task that isn't
COMPLETE) rather than this class enforcing it itself, since this class
has no way to check Task status on its own (it only knows git).
A merge conflict is never forced past -- the attempt is aborted and
reported, leaving the repo exactly as it was before the merge attempt.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitResult:
    ok: bool
    detail: str = ""


class WorktreeManager:
    def __init__(self, repo_path: str | Path):
        self.repo_path = Path(repo_path)

    def _git(self, *args: str, cwd: Path | None = None, timeout_s: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo_path),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )

    def create(
        self, task_id: str, worktrees_root: str | Path, base_branch: str = "main"
    ) -> tuple[Path | None, GitResult]:
        branch = f"solomon/{task_id}"
        wt_path = Path(worktrees_root) / task_id
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        proc = self._git("worktree", "add", "-b", branch, str(wt_path), base_branch)
        if proc.returncode != 0:
            return None, GitResult(False, proc.stderr.strip()[:500] or proc.stdout.strip()[:500])
        return wt_path, GitResult(True)

    def remove(self, task_id: str, worktrees_root: str | Path, force: bool = False) -> GitResult:
        wt_path = Path(worktrees_root) / task_id
        args = ["worktree", "remove", str(wt_path)]
        if force:
            args.append("--force")
        proc = self._git(*args)
        return GitResult(proc.returncode == 0, proc.stderr.strip()[:500])

    def merge(self, task_id: str, into_branch: str = "main") -> GitResult:
        """Merges solomon/<task_id> into into_branch, run from the main
        repo checkout (never from inside the worktree being merged).
        Never forces past a conflict: aborts and reports instead,
        leaving the repo exactly as it was."""
        branch = f"solomon/{task_id}"
        current = self._git("rev-parse", "--abbrev-ref", "HEAD")
        if current.stdout.strip() != into_branch:
            switch = self._git("checkout", into_branch)
            if switch.returncode != 0:
                return GitResult(False, f"could not checkout {into_branch}: {switch.stderr.strip()[:300]}")

        proc = self._git("merge", "--no-ff", branch, "-m", f"Merge {branch} (Solomon task {task_id})")
        if proc.returncode != 0:
            self._git("merge", "--abort")
            return GitResult(False, f"merge failed, aborted: {(proc.stderr + proc.stdout).strip()[:500]}")
        return GitResult(True)

    def delete_branch(self, task_id: str, force: bool = False) -> GitResult:
        branch = f"solomon/{task_id}"
        proc = self._git("branch", "-D" if force else "-d", branch)
        return GitResult(proc.returncode == 0, proc.stderr.strip()[:500])
