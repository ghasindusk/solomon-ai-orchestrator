import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from solomon.registry import ProjectRegistry


def write_registry(tmp_path, projects_yaml: str) -> pathlib.Path:
    p = tmp_path / "projects.registry.yaml"
    p.write_text(projects_yaml, encoding="utf-8")
    return p


def test_see_also_defaults_to_empty_list(tmp_path):
    registry_path = write_registry(
        tmp_path,
        f"""
version: 0.3
projects:
  no_peers:
    name: no peers
    repo_path: "{(tmp_path / 'no_peers').as_posix()}"
    status: active
""",
    )
    registry = ProjectRegistry(registry_path)
    assert registry.get("no_peers").see_also == []


def test_get_unknown_returns_none():
    registry = ProjectRegistry()
    assert registry.get("does_not_exist") is None


def test_detect_from_path_matches_deepest_project(tmp_path):
    inner = tmp_path / "outer" / "inner"
    inner.mkdir(parents=True)
    registry_path = write_registry(
        tmp_path,
        f"""
version: 0.3
projects:
  outer_proj:
    name: outer
    repo_path: "{(tmp_path / 'outer').as_posix()}"
    status: active
  inner_proj:
    name: inner
    repo_path: "{inner.as_posix()}"
    status: active
""",
    )
    registry = ProjectRegistry(registry_path)
    match = registry.detect_from_path(inner / "some" / "file.py")
    assert match is not None
    assert match.project_id == "inner_proj"


def test_detect_from_path_no_match_returns_none(tmp_path):
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    registry_path = write_registry(
        tmp_path,
        f"""
version: 0.3
projects:
  some_proj:
    name: some
    repo_path: "{(tmp_path / 'elsewhere').as_posix()}"
    status: active
""",
    )
    registry = ProjectRegistry(registry_path)
    assert registry.detect_from_path(unrelated) is None
