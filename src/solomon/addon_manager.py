"""Addon Manager (v0.4 04_Addon_SDK/Addon_SDK_Specification.md).

Implements only the safe, non-executing slice of Phase 5: manifest
discovery and validation. Deliberately does NOT implement the ENABLED
state (actually loading/running addon code). An addon manifest can
declare shell.execute/filesystem.outside_project/secrets.use
permissions, and this codebase has no real process-isolation mechanism
to safely run untrusted code with those permissions yet -- the same
Windows sandboxing gap already documented for Codex in
08_Discovery/PHASE0_DISCOVERY_REPORT.md (SetNamedSecurityInfoW/ACL
failures on this machine's virtual drives) would apply to any addon
sandbox too. Building a lifecycle that pretends to gate execution
safely, with no real isolation behind it, would be worse than not
building it -- see DECISIONS.md D22.

So this module only takes an addon from DISCOVERED to VALIDATED (or
QUARANTINED on error) and flags PERMISSION_REVIEW when the manifest
requests a sensitive permission. ENABLED/DISABLED are defined in
AddonState for completeness against the spec lifecycle diagram but are
intentionally unreachable from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

_DEFAULT_ADDONS_ROOT = Path(__file__).resolve().parents[2] / "addons"

KNOWN_PERMISSIONS = {
    "project.read", "project.write", "git.read", "git.write",
    "shell.execute", "network.connect", "knowledge.read", "telemetry.emit",
    "filesystem.outside_project", "secrets.use",
}
SENSITIVE_PERMISSIONS = {
    "shell.execute", "network.connect", "filesystem.outside_project",
    "secrets.use", "git.write",
}
KNOWN_EXTENSION_POINTS = {
    "agent_adapters", "tools", "mcp_servers", "a2a_agents", "roles",
    "task_types", "router_strategies", "knowledge_providers",
    "context_processors", "validators", "reviewers", "project_templates",
    "policy_packs", "dashboard_extensions", "event_hooks",
    "telemetry_providers",
}

# v0.5 R7: canonical name OCTAVRYN_VERSION; SOLOMON_VERSION kept as an
# alias for code that imported it. An addon declaring ">=0.4,<0.5" is now
# (correctly) quarantined as incompatible -- fail closed, not a silent load.
OCTAVRYN_VERSION = "0.5.0-dev"
SOLOMON_VERSION = OCTAVRYN_VERSION


class AddonState(str, Enum):
    DISCOVERED = "DISCOVERED"
    VALIDATED = "VALIDATED"
    PERMISSION_REVIEW = "PERMISSION_REVIEW"
    QUARANTINED = "QUARANTINED"
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


def _parse_version(value: str) -> tuple[int, ...]:
    parts = []
    for chunk in value.strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def version_satisfies(version: str, specifier: str) -> bool:
    version_t = _parse_version(version)
    ops = {
        ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
        ">": lambda a, b: a > b, "<": lambda a, b: a < b,
    }
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            continue
        matched = False
        for op in (">=", "<=", "==", "!=", ">", "<"):
            if clause.startswith(op):
                target = _parse_version(clause[len(op):])
                if not ops[op](version_t, target):
                    return False
                matched = True
                break
        if not matched:
            return False
    return True


@dataclass
class AddonManifest:
    id: str
    name: str
    version: str
    addon_api: str
    solomon_compatibility: str
    entrypoint: str
    permissions: list[str] = field(default_factory=list)
    provides: dict = field(default_factory=dict)
    source_path: str = ""

    @classmethod
    def from_dict(cls, data: dict, source_path: str = "") -> "AddonManifest":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            version=str(data.get("version", "")),
            addon_api=str(data.get("addon_api", "")),
            # v0.5: `octavryn_compatibility` is the canonical key; the v0.4
            # key is still read so existing addons keep validating.
            solomon_compatibility=data.get("octavryn_compatibility") or data.get("solomon_compatibility", ""),
            entrypoint=data.get("entrypoint", ""),
            permissions=list(data.get("permissions") or []),
            provides=dict(data.get("provides") or {}),
            source_path=source_path,
        )


@dataclass
class AddonRecord:
    manifest: AddonManifest | None
    state: AddonState
    errors: list[str] = field(default_factory=list)
    sensitive_permissions: list[str] = field(default_factory=list)


def validate_manifest(manifest: AddonManifest) -> AddonRecord:
    errors: list[str] = []

    if not manifest.id:
        errors.append("missing required field: id")
    if not manifest.name:
        errors.append("missing required field: name")
    if not manifest.version:
        errors.append("missing required field: version")
    if not manifest.entrypoint:
        errors.append("missing required field: entrypoint")

    unknown_perms = [p for p in manifest.permissions if p not in KNOWN_PERMISSIONS]
    if unknown_perms:
        errors.append("unknown permission(s): " + ", ".join(unknown_perms))

    unknown_points = [k for k in manifest.provides.keys() if k not in KNOWN_EXTENSION_POINTS]
    if unknown_points:
        errors.append("unknown extension point(s): " + ", ".join(unknown_points))

    if manifest.solomon_compatibility:
        if not version_satisfies(SOLOMON_VERSION, manifest.solomon_compatibility):
            errors.append(
                "solomon_compatibility '" + manifest.solomon_compatibility + "' does not match "
                "running version " + SOLOMON_VERSION + " -- fail closed "
                "(Addon_SDK_Specification.md: Octavryn must fail closed for incompatible addon APIs)"
            )
    else:
        errors.append("missing required field: solomon_compatibility")

    sensitive = [p for p in manifest.permissions if p in SENSITIVE_PERMISSIONS]

    if errors:
        return AddonRecord(manifest=manifest, state=AddonState.QUARANTINED,
                            errors=errors, sensitive_permissions=sensitive)
    if sensitive:
        return AddonRecord(manifest=manifest, state=AddonState.PERMISSION_REVIEW,
                            errors=[], sensitive_permissions=sensitive)
    return AddonRecord(manifest=manifest, state=AddonState.VALIDATED,
                        errors=[], sensitive_permissions=sensitive)


def discover_addons(addons_root: Path | str | None = None) -> list[AddonRecord]:
    root = Path(addons_root) if addons_root else _DEFAULT_ADDONS_ROOT
    records: list[AddonRecord] = []
    if not root.exists():
        return records

    # v0.5: octavryn-addon.yaml is canonical; solomon-addon.yaml still found.
    # If one addon dir has both, only the canonical one is read.
    manifest_paths = sorted(root.glob("*/octavryn-addon.yaml"))
    canonical_dirs = {p.parent for p in manifest_paths}
    manifest_paths += sorted(p for p in root.glob("*/solomon-addon.yaml") if p.parent not in canonical_dirs)
    for manifest_path in manifest_paths:
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("manifest did not parse to a mapping")
        except (yaml.YAMLError, OSError, ValueError) as exc:
            records.append(AddonRecord(
                manifest=None, state=AddonState.QUARANTINED,
                errors=["failed to parse " + str(manifest_path) + ": " + str(exc)],
            ))
            continue
        manifest = AddonManifest.from_dict(raw, source_path=str(manifest_path))
        records.append(validate_manifest(manifest))

    return records
