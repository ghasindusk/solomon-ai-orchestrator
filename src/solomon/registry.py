"""Project Registry (Architecture doc section 2 "Project Manager" / FR-02).

Loads 04_Config_Schemas/projects.registry.yaml and provides project lookup
and working-directory detection. Detection returns the project whose
repo_path or knowledge_path is the longest matching ancestor of the given
path. No match (or a genuinely ambiguous one) returns None -- per FR-02
("Ambiguous destructive work requires user confirmation"), callers must
treat None as "ask the user", not as license to guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "04_Config_Schemas" / "projects.registry.yaml"
)


@dataclass
class ProjectEntry:
    project_id: str
    name: str
    repo_path: str | None = None
    knowledge_path: str | None = None
    vcs: str = "none"
    status: str = "active"
    tags: list[str] = field(default_factory=list)
    related_to: str | None = None  # single hierarchical parent, if any
    see_also: list[str] = field(default_factory=list)  # peer relationships (non-hierarchical)
    crash_logs_path: str | None = None  # e.g. a Minecraft instance's crash-reports/ dir
    mods_path: str | None = None  # e.g. a Minecraft instance's mods/ dir
    app_log_path: str | None = None  # e.g. a Flutter .flutter_run.log file
    build_command: str | None = None  # shell command, run in repo_path, for the "build_passes" DoD criterion
    # D78: skills declaring the `artifacts_in_prompt` verification get one
    # `artifact:<path>` DoD criterion per repo-relative path in the prompt that
    # matches artifact_pattern; artifact_verify_command ({path} placeholder,
    # same D56 trust boundary as build_command) must then succeed on it.
    artifact_pattern: str | None = None
    artifact_verify_command: str | None = None


class ProjectRegistry:
    def __init__(self, registry_path: Path | str | None = None):
        self.path = Path(registry_path) if registry_path else _DEFAULT_REGISTRY_PATH
        if not self.path.exists():
            raise FileNotFoundError(f"Project registry not found: {self.path}")
        with open(self.path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self._projects: dict[str, ProjectEntry] = {}
        for project_id, data in (raw.get("projects") or {}).items():
            data = data or {}
            self._projects[project_id] = ProjectEntry(
                project_id=project_id,
                name=data.get("name", project_id),
                repo_path=data.get("repo_path"),
                knowledge_path=data.get("knowledge_path"),
                vcs=data.get("vcs", "none"),
                status=data.get("status", "active"),
                tags=list(data.get("tags") or []),
                related_to=data.get("related_to"),
                see_also=list(data.get("see_also") or []),
                crash_logs_path=data.get("crash_logs_path"),
                mods_path=data.get("mods_path"),
                app_log_path=data.get("app_log_path"),
                build_command=data.get("build_command"),
                artifact_pattern=data.get("artifact_pattern"),
                artifact_verify_command=data.get("artifact_verify_command"),
            )

    def list_projects(self, include_superseded: bool = False) -> list[ProjectEntry]:
        return [
            p for p in self._projects.values() if include_superseded or p.status != "superseded"
        ]

    def get(self, project_id: str) -> ProjectEntry | None:
        return self._projects.get(project_id)

    def detect_from_path(self, path: str | Path) -> ProjectEntry | None:
        target = Path(path).resolve()
        best: ProjectEntry | None = None
        best_depth = -1
        for project in self._projects.values():
            for candidate in (project.repo_path, project.knowledge_path):
                if not candidate:
                    continue
                candidate_path = Path(candidate).resolve()
                try:
                    target.relative_to(candidate_path)
                except ValueError:
                    continue
                depth = len(candidate_path.parts)
                if depth > best_depth:
                    best_depth = depth
                    best = project
        return best
