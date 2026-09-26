"""Typed trusted parameters for the first Phase 1B verifier classes.

These are inert configurations. Lookup and policy must precede invocation;
intake cannot supply commands, paths or schema URLs through these references.
"""
import re
from dataclasses import dataclass

from .contracts import ContractError


def _reference(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", value):
        raise ContractError("trusted registry reference required")


@dataclass(frozen=True)
class ArtifactParameters:
    artifact_rule_id: str
    max_bytes: int = 1048576

    def __post_init__(self):
        _reference(self.artifact_rule_id)
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 104857600:
            raise ContractError("invalid artifact size limit")


@dataclass(frozen=True)
class StructuredDataParameters:
    schema_registry_id: str

    def __post_init__(self):
        _reference(self.schema_registry_id)


@dataclass(frozen=True)
class CodeTestParameters:
    test_profile_id: str
    minimum_tests: int = 1

    def __post_init__(self):
        _reference(self.test_profile_id)
        if type(self.minimum_tests) is not int or not 1 <= self.minimum_tests <= 1000000:
            raise ContractError("nonempty bounded test run required")


PARAMETER_TYPES = {"artifact": ArtifactParameters, "structured_data": StructuredDataParameters,
                   "code_test": CodeTestParameters}


def parameters_for(verifier_class: str, values: dict):
    if verifier_class not in PARAMETER_TYPES or not isinstance(values, dict):
        raise ContractError("verifier class is not enabled for typed configuration")
    try:
        return PARAMETER_TYPES[verifier_class](**values)
    except TypeError as exc:
        raise ContractError("invalid verifier parameters") from exc
