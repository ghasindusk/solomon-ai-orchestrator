"""Codex Windows-sandbox workaround for cloud-sync virtual drives.

Codex's sandboxed `exec` fails immediately under Google Drive for Desktop's
virtual/cloud-sync drives: `SetNamedSecurityInfoW` (the Windows ACL API
Codex's sandbox helper uses to grant the restricted sandbox user access to
the working directory) is unsupported there. Confirmed root cause, not
fixable by Defender exclusions or reinstalling Codex -- see
08_Discovery/PHASE0_DISCOVERY_REPORT.md. The same command succeeds
immediately against a real local NTFS volume.

For any codex task whose working directory resolves under one of the
configured `incompatible_roots`, codex_adapter.py uses this module to run
against a local git clone on a real drive instead: fast-forward the clone
from the original repo before the run (`sync_before`), then fast-forward
the original repo from the clone's new commit(s) after (`sync_after`).
Both directions are plain `git pull --ff-only` -- never a forced push or
merge past a conflict, matching the same "never force past a conflict"
principle worktree.py already uses for its own merges.

Config: 04_Config_Schemas/codex_sandbox_workaround.yaml
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "04_Config_Schemas" / "codex_sandbox_workaround.yaml"
)


@dataclass
class GitResult:
    ok: bool
    detail: str = ""


def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def is_incompatible_path(path: str | Path, config: dict | None = None) -> bool:
    """True if `path` sits under one of the configured incompatible_roots."""
    config = load_config() if config is None else config
    roots = config.get("incompatible_roots") or []
    if not roots:
        return False
    resolved = str(Path(path).resolve())
    return any(resolved.startswith(str(Path(root))) for root in roots)


def local_clone_path(project_id: str, config: dict | None = None) -> Path | None:
    """project_id -> local clone directory. Explicit `local_clone_paths`
    entries win; otherwise falls back to `<local_clone_root>/<project_id>`.
    None if neither is configured (caller must treat that as a hard stop,
    not a silent fall-through to the incompatible path)."""
    config = load_config() if config is None else config
    explicit = (config.get("local_clone_paths") or {}).get(project_id)
    if explicit:
        return Path(explicit)
    root = config.get("local_clone_root")
    if not root:
        return None
    return Path(root) / project_id


def _git(args: list[str], cwd: Path | str, timeout_s: int = 120) -> subprocess.CompletedProcess:
    # git.exe is a real PE binary (unlike codex's npm .cmd shim), so no
    # shell=True is needed here -- see codex_adapter.py's _USE_SHELL comment
    # for why that distinction matters on Windows.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
    )


def _current_branch(repo: Path | str) -> str | None:
    proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def sync_before(original_repo: Path, clone_path: Path) -> GitResult:
    """Ensure clone_path exists and is fast-forwarded to original_repo's
    current branch tip. Clones fresh if clone_path doesn't exist yet.
    Refuses (rather than clobbering) if the clone has uncommitted changes
    left over from a previous run, or has diverged from original_repo."""
    try:
        if not (clone_path / ".git").exists():
            clone_path.parent.mkdir(parents=True, exist_ok=True)
            proc = _git(["clone", str(original_repo), str(clone_path)], cwd=clone_path.parent, timeout_s=300)
            if proc.returncode != 0:
                return GitResult(False, f"clone failed: {(proc.stderr or proc.stdout).strip()[:500]}")
            return GitResult(True, "cloned")

        status = _git(["status", "--porcelain"], cwd=clone_path)
        if status.returncode != 0:
            return GitResult(
                False, f"'{clone_path}' exists but isn't a usable git checkout: {status.stderr.strip()[:300]}"
            )
        if status.stdout.strip():
            return GitResult(
                False,
                f"local clone at {clone_path} has uncommitted changes left over from a previous run "
                "-- resolve manually before retrying",
            )

        branch = _current_branch(original_repo) or "main"
        pull = _git(["pull", "--ff-only", str(original_repo), branch], cwd=clone_path)
        if pull.returncode != 0:
            return GitResult(
                False, f"could not fast-forward local clone from {original_repo}: "
                f"{(pull.stderr or pull.stdout).strip()[:500]}"
            )
        return GitResult(True, "synced")
    except subprocess.TimeoutExpired as exc:
        return GitResult(False, f"git sync timed out: {exc}")
    except Exception as exc:  # noqa: BLE001 - caller reports this as a FAILED TaskResult, must not raise
        return GitResult(False, f"git sync failed: {exc}")


def sync_after(original_repo: Path, clone_path: Path, task_id: str) -> GitResult:
    """Commit any changes codex made in clone_path (no-op if clean), then
    fast-forward original_repo from them. Never force-pushed/merged --
    a diverged original_repo is reported as a failure, not overwritten."""
    try:
        status = _git(["status", "--porcelain"], cwd=clone_path)
        if status.returncode != 0:
            return GitResult(False, f"could not read clone status: {status.stderr.strip()[:300]}")
        if status.stdout.strip():
            add = _git(["add", "-A"], cwd=clone_path)
            if add.returncode != 0:
                return GitResult(False, f"git add failed: {add.stderr.strip()[:500]}")
            commit = _git(["commit", "-m", f"solomon: codex task {task_id}"], cwd=clone_path)
            if commit.returncode != 0:
                return GitResult(False, f"git commit failed: {commit.stderr.strip()[:500]}")

        branch = _current_branch(clone_path) or "main"
        pull = _git(["pull", "--ff-only", str(clone_path), branch], cwd=original_repo)
        if pull.returncode != 0:
            return GitResult(
                False,
                f"could not fast-forward {original_repo} from the local clone (not a fast-forward?): "
                f"{(pull.stderr or pull.stdout).strip()[:500]}",
            )
        return GitResult(True, "synced back")
    except subprocess.TimeoutExpired as exc:
        return GitResult(False, f"git sync-back timed out: {exc}")
    except Exception as exc:  # noqa: BLE001
        return GitResult(False, f"git sync-back failed: {exc}")
