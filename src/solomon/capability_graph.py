"""Capability Graph (Octavryn SI v0.5 spec 03, roadmap R2).

Normalizes capability names (aliases -> canonical ids from
04_Config_Schemas/capabilities.yaml) and answers "which intelligences
can do X, and how sure are we?" from IntelligenceDescriptors.

Evidence handling:
- an intelligence's evidence for a capability is the strongest evidence
  any source gave it (descriptors.IntelligenceDescriptor.capability_states);
- an `implies` edge yields the implied capability at DECLARED at most. A
  derived capability never inherits stronger evidence than a declaration;
- an unknown capability name is kept as-is (not dropped, not mapped to a
  guess) and reported by unknown_capabilities(), so a typo in a skill
  shows up as "no provider" instead of silently matching something else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .descriptors import (
    EVIDENCE_RANK,
    Availability,
    CapabilityDescriptor,
    EvidenceState,
    IntelligenceDescriptor,
    check_schema_version,
)

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "04_Config_Schemas" / "capabilities.yaml"


@dataclass
class CapabilityMatch:
    intelligence_id: str
    capability: str
    evidence: EvidenceState
    available: bool


class CapabilityGraph:
    def __init__(self, vocabulary: dict[str, CapabilityDescriptor]):
        self.vocabulary = vocabulary
        self._alias: dict[str, str] = {}
        for cap_id, desc in vocabulary.items():
            self._alias[cap_id.lower()] = cap_id
            for alias in desc.aliases:
                self._alias[alias.lower()] = cap_id
        self._intelligences: dict[str, IntelligenceDescriptor] = {}

    @classmethod
    def load(cls, path: Path | str | None = None) -> "CapabilityGraph":
        p = Path(path) if path else _DEFAULT_PATH
        with open(p, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        check_schema_version(str(raw.get("schema_version", "")))
        vocab = {}
        for cap_id, cfg in (raw.get("capabilities") or {}).items():
            cfg = cfg or {}
            vocab[cap_id] = CapabilityDescriptor(
                id=cap_id,
                description=cfg.get("description", ""),
                aliases=list(cfg.get("aliases") or []),
                implies=list(cfg.get("implies") or []),
            )
        return cls(vocab)

    def normalize(self, name: str) -> str:
        return self._alias.get(name.strip().lower(), name.strip())

    def is_known(self, name: str) -> bool:
        return self.normalize(name) in self.vocabulary

    def unknown_capabilities(self, names: list[str]) -> list[str]:
        return [n for n in names if not self.is_known(n)]

    def add_intelligence(self, desc: IntelligenceDescriptor) -> None:
        self._intelligences[desc.id] = desc

    def remove_intelligence(self, intelligence_id: str) -> None:
        self._intelligences.pop(intelligence_id, None)

    def intelligences(self) -> list[IntelligenceDescriptor]:
        return list(self._intelligences.values())

    def evidence_for(self, intelligence_id: str) -> dict[str, EvidenceState]:
        """Normalized capability -> strongest evidence, including implied
        capabilities (DECLARED at most)."""
        desc = self._intelligences.get(intelligence_id)
        if desc is None:
            return {}
        direct: dict[str, EvidenceState] = {}
        for cap, state in desc.capability_states().items():
            norm = self.normalize(cap)
            if norm not in direct or EVIDENCE_RANK[state] > EVIDENCE_RANK[direct[norm]]:
                direct[norm] = state
        result = dict(direct)
        frontier = list(direct)
        visited: set[str] = set()
        while frontier:
            cap = frontier.pop()
            if cap in visited:
                continue
            visited.add(cap)
            for implied in (self.vocabulary.get(cap).implies if cap in self.vocabulary else []):
                implied = self.normalize(implied)
                if implied not in result or EVIDENCE_RANK[result[implied]] < EVIDENCE_RANK[EvidenceState.DECLARED]:
                    result[implied] = EvidenceState.DECLARED
                frontier.append(implied)
        return result

    def providers_for(
        self,
        capability: str,
        min_evidence: EvidenceState = EvidenceState.DECLARED,
        include_unavailable: bool = False,
    ) -> list[CapabilityMatch]:
        """Sorted strongest evidence first, then by id for determinism."""
        cap = self.normalize(capability)
        matches: list[CapabilityMatch] = []
        for desc in self._intelligences.values():
            available = desc.availability == Availability.AVAILABLE
            if not available and not include_unavailable:
                continue
            state = self.evidence_for(desc.id).get(cap)
            if state is None or EVIDENCE_RANK[state] < EVIDENCE_RANK[min_evidence]:
                continue
            matches.append(CapabilityMatch(desc.id, cap, state, available))
        matches.sort(key=lambda m: (-EVIDENCE_RANK[m.evidence], m.intelligence_id))
        return matches
