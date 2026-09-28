"""Per-project least-privilege policy (Octavryn SI v0.5, D67).

A project can be registered with a policy that narrows what Octavryn will
do for it. The policy lives in Octavryn's own config
(04_Config_Schemas/project_policies.yaml), never inside the project's
repository: an agent working in the repo can edit files there, and a
policy it could edit would let it grant itself permissions.

Fields (all optional; a project without an entry is unrestricted, i.e.
exactly the pre-D67 behaviour):

  allowed_skills          only these skill ids may run; a task must name
                          one explicitly (`--skill`), role defaults are
                          refused. Missing = any skill.
  denied_capabilities     a skill that requires any of these is refused.
  denied_permissions      a skill that requests any of these is refused
                          (even if listed in skill_grants).
  skill_grants            {skill_id: [permission, ...]} granted to
                          non-core skills (skills.effective_permissions).
  forbidden_prompt_patterns
                          case-insensitive regexes; a matching prompt is
                          refused outright (not sent to approval).
  max_autonomy            upper bound for the task autonomy dial.
  allowed_adapters        only these execution intelligences may run.
  execution_profile       per-adapter restrictions applied when the
                          adapter is constructed for this project:
                            claude_code: allowed_tools, disallowed_tools,
                                         permission_mode, execution_backend,
                                         orca
                            codex:       sandbox, execution_backend, orca
                            antigravity: sandbox (bool), mode (plan|accept-edits)

                          execution_backend defaults to direct. The v0.6 Orca
                          pilot permits execution_backend: orca for codex and
                          claude_code only. Provider-specific direct settings
                          cannot be combined with the Orca backend because
                          Octavryn cannot prove Orca enforces them.

Refusals are DENY, not approval: the point of the policy is that these
actions are unavailable for the project, and approving one would need a
policy change made by a human in Octavryn's config.

A policy file that exists but cannot be parsed fails closed: every task
for every project is denied until it is fixed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "04_Config_Schemas" / "project_policies.yaml"
KNOWN_PROFILE_KEYS = {
    "claude_code": {"allowed_tools", "disallowed_tools", "permission_mode", "execution_backend", "orca"},
    "codex": {"sandbox", "execution_backend", "orca"},
    "antigravity": {"sandbox", "mode"},
}
_ORCA_BACKEND_AGENTS = {"codex": "codex", "claude_code": "claude"}
_ORCA_PROFILE_KEYS = {"agent", "worktree"}
_EXECUTION_BACKENDS = {"direct", "orca"}
_CODEX_SANDBOXES = {"read-only", "workspace-write"}
_AGY_MODES = {"plan", "accept-edits"}
_CLAUDE_MODES = {"default", "plan", "acceptEdits"}


class PolicyError(ValueError):
    pass


@dataclass
class ProjectPolicy:
    project_id: str
    allowed_skills: list[str] | None = None
    denied_capabilities: list[str] = field(default_factory=list)
    denied_permissions: list[str] = field(default_factory=list)
    skill_grants: dict[str, list[str]] = field(default_factory=dict)
    forbidden_prompt_patterns: list[str] = field(default_factory=list)
    max_autonomy: int | None = None
    allowed_adapters: list[str] | None = None
    execution_profile: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, project_id: str, raw: dict) -> "ProjectPolicy":
        if not isinstance(raw, dict):
            raise PolicyError(f"policy for '{project_id}' must be a mapping")
        unknown = set(raw) - {f for f in cls.__dataclass_fields__ if f != "project_id"}
        if unknown:
            raise PolicyError(f"policy for '{project_id}' has unknown keys: {sorted(unknown)}")
        pol = cls(
            project_id=project_id,
            allowed_skills=list(raw["allowed_skills"]) if raw.get("allowed_skills") is not None else None,
            denied_capabilities=list(raw.get("denied_capabilities") or []),
            denied_permissions=list(raw.get("denied_permissions") or []),
            skill_grants={k: list(v or []) for k, v in (raw.get("skill_grants") or {}).items()},
            forbidden_prompt_patterns=list(raw.get("forbidden_prompt_patterns") or []),
            max_autonomy=raw.get("max_autonomy"),
            allowed_adapters=list(raw["allowed_adapters"]) if raw.get("allowed_adapters") is not None else None,
            execution_profile=dict(raw.get("execution_profile") or {}),
        )
        pol._validate()
        return pol

    def _validate(self) -> None:
        for pattern in self.forbidden_prompt_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise PolicyError(f"{self.project_id}: bad forbidden_prompt_patterns entry {pattern!r}: {exc}") from exc
        for skill_id, perms in self.skill_grants.items():
            clash = set(perms) & set(self.denied_permissions)
            if clash:
                raise PolicyError(f"{self.project_id}: skill_grants[{skill_id}] grants denied permission(s) {sorted(clash)}")
        for adapter, profile in self.execution_profile.items():
            if adapter not in KNOWN_PROFILE_KEYS:
                raise PolicyError(f"{self.project_id}: execution_profile for unknown adapter '{adapter}'")
            if not isinstance(profile, dict):
                raise PolicyError(f"{self.project_id}: execution_profile.{adapter} must be a mapping")
            extra = set(profile) - KNOWN_PROFILE_KEYS[adapter]
            if extra:
                raise PolicyError(f"{self.project_id}: execution_profile.{adapter} has unknown keys {sorted(extra)}")
            backend = profile.get("execution_backend", "direct")
            if backend not in _EXECUTION_BACKENDS:
                raise PolicyError(
                    f"{self.project_id}: execution_profile.{adapter}.execution_backend "
                    f"must be one of {sorted(_EXECUTION_BACKENDS)}"
                )
            if backend == "orca":
                if adapter not in _ORCA_BACKEND_AGENTS:
                    raise PolicyError(f"{self.project_id}: Orca backend is not supported for {adapter}")
                direct_only = set(profile) - {"execution_backend", "orca"}
                if direct_only:
                    raise PolicyError(
                        f"{self.project_id}: execution_profile.{adapter} cannot combine Orca "
                        f"with direct-adapter settings {sorted(direct_only)}"
                    )
                orca = profile.get("orca") or {}
                if not isinstance(orca, dict):
                    raise PolicyError(
                        f"{self.project_id}: execution_profile.{adapter}.orca must be a mapping"
                    )
                extra_orca = set(orca) - _ORCA_PROFILE_KEYS
                if extra_orca:
                    raise PolicyError(
                        f"{self.project_id}: execution_profile.{adapter}.orca has unknown keys "
                        f"{sorted(extra_orca)}"
                    )
                if orca.get("worktree", "current") != "current":
                    raise PolicyError(
                        f"{self.project_id}: v0.6 Orca pilot supports worktree='current' only"
                    )
                expected_agent = _ORCA_BACKEND_AGENTS[adapter]
                if orca.get("agent", expected_agent) != expected_agent:
                    raise PolicyError(
                        f"{self.project_id}: Orca agent must remain {expected_agent!r} for "
                        f"logical adapter {adapter!r}"
                    )
            elif "orca" in profile:
                raise PolicyError(
                    f"{self.project_id}: execution_profile.{adapter}.orca requires execution_backend='orca'"
                )

        codex = self.execution_profile.get("codex") or {}
        if "sandbox" in codex and codex["sandbox"] not in _CODEX_SANDBOXES:
            raise PolicyError(f"{self.project_id}: codex sandbox must be one of {sorted(_CODEX_SANDBOXES)}")
        agy = self.execution_profile.get("antigravity") or {}
        if "mode" in agy and agy["mode"] not in _AGY_MODES:
            raise PolicyError(f"{self.project_id}: antigravity mode must be one of {sorted(_AGY_MODES)}")
        if agy.get("sandbox") is False:
            raise PolicyError(f"{self.project_id}: a project policy may not disable the antigravity sandbox")
        claude = self.execution_profile.get("claude_code") or {}
        if "permission_mode" in claude and claude["permission_mode"] not in _CLAUDE_MODES:
            raise PolicyError(f"{self.project_id}: claude_code permission_mode must be one of {sorted(_CLAUDE_MODES)}")
        if self.max_autonomy is not None and not (0 <= int(self.max_autonomy) <= 5):
            raise PolicyError(f"{self.project_id}: max_autonomy must be 0-5")

    def forbidden_match(self, prompt: str) -> str | None:
        for pattern in self.forbidden_prompt_patterns:
            if re.search(pattern, prompt, flags=re.IGNORECASE):
                return pattern
        return None

    def adapter_allowed(self, name: str) -> bool:
        return self.allowed_adapters is None or name in self.allowed_adapters


def policy_path() -> Path:
    override = os.environ.get("OCTAVRYN_PROJECT_POLICIES")
    return Path(override) if override else DEFAULT_POLICY_PATH


def load_policies(path: Path | str | None = None) -> dict[str, ProjectPolicy]:
    """{} when the file does not exist. Raises PolicyError when it exists
    but is invalid (callers fail closed)."""
    p = Path(path) if path else policy_path()
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"cannot read project policy file {p}: {exc}") from exc
    projects = raw.get("projects") or {}
    if not isinstance(projects, dict):
        raise PolicyError(f"{p}: `projects` must be a mapping")
    return {pid: ProjectPolicy.from_dict(pid, body) for pid, body in projects.items()}


def policy_for(project_id: str | None, path: Path | str | None = None) -> ProjectPolicy | None:
    """Raises PolicyError on an invalid file (fail closed)."""
    if not project_id:
        return None
    return load_policies(path).get(project_id)
