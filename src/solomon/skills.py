"""Skill Registry and Skill Packs (Octavryn SI v0.5 spec 04, roadmap R3).

A Skill describes reusable work independently of any AI: what
capabilities and tools it needs, its risk, the permissions it asks for,
and how its result is verified. Routing resolves the skill first and the
provider second (spec 01 "Capability-first routing").

Scopes and precedence (descriptors.SCOPE_PRECEDENCE): project > user >
addon > mcp > provider > core. Conflict rules are explicit and recorded
in `conflicts`:
- same id in *different* scopes: the higher scope wins (`overridden`);
- same id twice in the *same* scope: ambiguous, so neither is loaded
  (`ambiguous`). Fail closed instead of picking one at random.

Least privilege (spec 04 "Skill installation MUST NOT elevate
permissions"):
- an override's risk is never lower than the skill it overrides, so a
  project skill cannot turn a HIGH core skill into a LOW one to skip
  approval;
- requested_permissions grant nothing. effective_permissions() returns
  requested ∩ granted, where the grants come from policy (only core
  skills get their declared permissions implicitly, because they ship
  with Core). Anything requested but not granted is reported so
  governance can route it to Human Approval.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .capability_graph import CapabilityGraph
from .descriptors import (
    SCOPE_PRECEDENCE,
    SchemaVersionError,
    SkillDescriptor,
    SkillScope,
    check_schema_version,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_PACK_DIR = _REPO_ROOT / "skill_packs" / "core"
_RISK_ORDER = ["LOW", "NORMAL", "HIGH", "VERY_HIGH", "CRITICAL"]

# Verification names a skill may declare -> Definition-of-Done criteria that
# verification.py can actually check. Anything else becomes an
# unverifiable criterion, which keeps the task out of COMPLETE (honest).
VERIFICATION_TO_DOD = {
    "result_recorded": "result_recorded",
    "build": "build_passes",
    "build_passes": "build_passes",
}


def user_skills_dir() -> Path:
    base = os.environ.get("OCTAVRYN_USER_HOME") or os.path.join(os.path.expanduser("~"), ".octavryn")
    return Path(base) / "skills"


def project_skills_dir(repo_path: str | Path) -> Path:
    return Path(repo_path) / ".octavryn" / "skills"


@dataclass
class SkillConflict:
    skill_id: str
    kind: str  # overridden | ambiguous | invalid
    detail: str


@dataclass
class SkillPack:
    id: str
    version: int
    name: str
    skills: list[SkillDescriptor]
    source: str


@dataclass
class ToolCheck:
    missing: list[str] = field(default_factory=list)


def load_pack_file(path: Path, scope: SkillScope) -> SkillPack:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    check_schema_version(str(raw.get("schema_version", "")))
    skills_raw = raw.get("skills")
    if skills_raw is None and "id" in raw and "requires" in raw:
        skills_raw = [raw]  # a single-skill file
    skills = [
        SkillDescriptor.from_dict(s, scope=scope, provenance=f"{scope.value}:{path}")
        for s in (skills_raw or [])
    ]
    return SkillPack(
        id=raw.get("id", path.parent.name),
        version=int(raw.get("version", 1)),
        name=raw.get("name", raw.get("id", path.parent.name)),
        skills=skills,
        source=str(path),
    )


class SkillRegistry:
    def __init__(self, graph: CapabilityGraph | None = None):
        self.graph = graph
        self._by_scope: dict[SkillScope, dict[str, list[SkillDescriptor]]] = {s: {} for s in SkillScope}
        self.packs: list[SkillPack] = []
        self.conflicts: list[SkillConflict] = []
        self._resolved: dict[str, SkillDescriptor] | None = None

    # -- loading ---------------------------------------------------------

    def add_skill(self, skill: SkillDescriptor) -> None:
        self._by_scope[skill.scope].setdefault(skill.id, []).append(skill)
        self._resolved = None

    def add_pack(self, pack: SkillPack) -> None:
        self.packs.append(pack)
        for skill in pack.skills:
            self.add_skill(skill)

    def load_dir(self, directory: Path | str, scope: SkillScope) -> None:
        """Loads every *.yaml (a pack or a single skill) under `directory`.
        A missing directory is fine (the scope is just empty). An invalid
        file is recorded as a conflict of kind `invalid`; it does not abort
        loading the others."""
        d = Path(directory)
        if not d.is_dir():
            return
        for path in sorted(d.rglob("*.yaml")):
            if ".removed" in path.relative_to(d).parts:
                continue  # uninstalled packs (kept for rollback, never loaded)
            try:
                self.add_pack(load_pack_file(path, scope))
            except (SchemaVersionError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
                self.conflicts.append(SkillConflict(path.name, "invalid", f"{path}: {exc}"))

    @classmethod
    def default(
        cls,
        graph: CapabilityGraph | None = None,
        project_repo_path: str | Path | None = None,
        include_user: bool = True,
    ) -> "SkillRegistry":
        reg = cls(graph=graph)
        reg.load_dir(CORE_PACK_DIR, SkillScope.CORE)
        if include_user:
            reg.load_dir(user_skills_dir(), SkillScope.USER)
        if project_repo_path:
            reg.load_dir(project_skills_dir(project_repo_path), SkillScope.PROJECT)
        return reg

    # -- resolution ------------------------------------------------------

    def _resolve_all(self) -> dict[str, SkillDescriptor]:
        if self._resolved is not None:
            return self._resolved
        conflicts = [c for c in self.conflicts if c.kind == "invalid"]
        winners: dict[str, SkillDescriptor] = {}
        for scope in sorted(SkillScope, key=lambda s: SCOPE_PRECEDENCE[s]):
            for skill_id, entries in self._by_scope[scope].items():
                if len(entries) > 1:
                    conflicts.append(
                        SkillConflict(
                            skill_id, "ambiguous",
                            f"{len(entries)} definitions in scope '{scope.value}': "
                            + ", ".join(e.provenance for e in entries),
                        )
                    )
                    # Fail closed, and hide any lower-scope definition as
                    # well: the higher scope clearly meant to override it,
                    # we just can't tell with which definition.
                    winners.pop(skill_id, None)
                    winners[skill_id] = None  # type: ignore[assignment]
                    continue
                new = entries[0]
                old = winners.get(skill_id)
                if old is not None:
                    new = self._no_escalation(old, new)
                    conflicts.append(
                        SkillConflict(
                            skill_id, "overridden",
                            f"{old.scope.value} definition overridden by {new.scope.value} ({new.provenance})",
                        )
                    )
                winners[skill_id] = new
        self.conflicts = conflicts
        self._resolved = {k: v for k, v in winners.items() if v is not None}
        return self._resolved

    @staticmethod
    def _no_escalation(old: SkillDescriptor, new: SkillDescriptor) -> SkillDescriptor:
        if _RISK_ORDER.index(new.risk) < _RISK_ORDER.index(old.risk):
            new.risk = old.risk
        return new

    def get(self, skill_id: str) -> SkillDescriptor | None:
        skill = self._resolve_all().get(skill_id)
        if skill is None or not skill.enabled:
            return None
        return skill

    def all(self) -> list[SkillDescriptor]:
        return sorted(self._resolve_all().values(), key=lambda s: s.id)

    def for_role(self, role: str) -> SkillDescriptor | None:
        """The default skill for a v0.4 role: `role.<role>` if present,
        else the first enabled skill that lists the role."""
        skill = self.get(f"role.{role}")
        if skill is not None:
            return skill
        for s in self.all():
            if s.enabled and role in s.roles:
                return s
        return None

    # -- checks used by routing/governance --------------------------------

    def required_capabilities(self, skill: SkillDescriptor) -> list[str]:
        if self.graph is None:
            return list(skill.required_capabilities)
        return [self.graph.normalize(c) for c in skill.required_capabilities]

    def unknown_capabilities(self, skill: SkillDescriptor) -> list[str]:
        if self.graph is None:
            return []
        return self.graph.unknown_capabilities(skill.required_capabilities)

    @staticmethod
    def check_tools(skill: SkillDescriptor, which=shutil.which) -> ToolCheck:
        return ToolCheck(missing=[t for t in skill.tools if which(t) is None])

    @staticmethod
    def effective_permissions(skill: SkillDescriptor, grants: dict[str, list[str]] | None = None) -> tuple[list[str], list[str]]:
        """(granted, denied). Core skills are granted what they declare;
        every other scope gets only what `grants[skill.id]` lists."""
        requested = list(skill.requested_permissions)
        if skill.scope == SkillScope.CORE:
            return requested, []
        allowed = set((grants or {}).get(skill.id, []))
        granted = [p for p in requested if p in allowed]
        denied = [p for p in requested if p not in allowed]
        return granted, denied

    @staticmethod
    def definition_of_done(skill: SkillDescriptor, prompt: str | None = None,
                           artifact_pattern: str | None = None) -> list[str]:
        """Maps skill.verification to DoD criteria. Unmapped names are kept
        verbatim, so verification.py reports them as unverifiable (never
        silently treated as passed). Always includes result_recorded.

        D78: `artifacts_in_prompt` expands to one `artifact:<path>` per
        match of the project's artifact_pattern in the prompt. No pattern
        or no match keeps `skill_verification:artifacts_in_prompt`, which
        is unverifiable, so the task can never be COMPLETE on its word
        alone."""
        import re

        dod = ["result_recorded"]
        for name in skill.verification:
            if name == "artifacts_in_prompt":
                paths = sorted(set(re.findall(artifact_pattern, prompt or ""))) if artifact_pattern else []
                crits = [f"artifact:{p}" for p in paths if isinstance(p, str)] or [f"skill_verification:{name}"]
            else:
                crits = [VERIFICATION_TO_DOD.get(name, f"skill_verification:{name}")]
            for crit in crits:
                if crit not in dod:
                    dod.append(crit)
        return dod


# -- pack install / uninstall (v0.5 R3, D55) ------------------------------------

class PackInstallError(ValueError):
    pass


def install_pack(source: Path | str, target_dir: Path | str, scope: SkillScope, replace: bool = False) -> dict:
    """Validates a pack file and copies it to a scope directory as
    <pack_id>.yaml. Refuses: an invalid pack; a pack whose skill ids clash
    with *another* pack already in that scope (that would make them
    ambiguous); and re-installing the same pack id unless replace=True and
    the new version is higher. Grants no permission: the result lists what
    the skills request, and governance still decides."""
    source, target_dir = Path(source), Path(target_dir)
    try:
        pack = load_pack_file(source, scope)
    except (SchemaVersionError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise PackInstallError(f"invalid pack {source}: {exc}") from exc
    if not pack.skills:
        raise PackInstallError("pack contains no skills")
    ids = [s.id for s in pack.skills]
    if len(ids) != len(set(ids)):
        raise PackInstallError("pack defines the same skill id twice")
    dest = target_dir / f"{pack.id}.yaml"
    existing = SkillRegistry()
    existing.load_dir(target_dir, scope)
    for other in existing.packs:
        if Path(other.source).resolve() == dest.resolve():
            if not replace:
                raise PackInstallError(f"pack '{pack.id}' already installed (use replace)")
            if pack.version <= other.version:
                raise PackInstallError(f"installed version {other.version} >= new version {pack.version}")
            continue
        clash = set(ids) & {s.id for s in other.skills}
        if clash:
            raise PackInstallError(f"skill id(s) {sorted(clash)} already provided by pack '{other.id}'")
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    return {
        "installed": pack.id, "version": pack.version, "scope": scope.value, "path": str(dest),
        "skills": ids,
        "requested_permissions": sorted({p for s in pack.skills for p in s.requested_permissions}),
        "note": "No permission was granted. Ungranted permissions route tasks to Human Approval.",
    }


def uninstall_pack(pack_id: str, target_dir: Path | str) -> dict:
    """Moves <pack_id>.yaml to .removed/ in the same directory (reversible,
    nothing is deleted)."""
    target_dir = Path(target_dir)
    src = target_dir / f"{pack_id}.yaml"
    if not src.is_file():
        raise PackInstallError(f"pack '{pack_id}' is not installed in {target_dir}")
    from datetime import datetime, timezone

    removed = target_dir / ".removed"
    removed.mkdir(exist_ok=True)
    dest = removed / f"{pack_id}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.yaml"
    shutil.move(str(src), str(dest))
    return {"uninstalled": pack_id, "moved_to": str(dest)}
