import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.addon_manager import (
    AddonManifest,
    AddonState,
    discover_addons,
    validate_manifest,
    version_satisfies,
)


def make_manifest(**overrides) -> AddonManifest:
    base = dict(
        id="community.example-addon", name="Example", version="0.1.0",
        addon_api="1", solomon_compatibility=">=0.4,<0.6", entrypoint="src/main.py",
    )
    base.update(overrides)
    return AddonManifest.from_dict(base)


def test_version_satisfies_range():
    assert version_satisfies("0.4.0-dev", ">=0.4,<0.5") is True
    assert version_satisfies("0.4.0-dev", ">=0.5") is False
    assert version_satisfies("1.2.3", "==1.2.3") is True
    assert version_satisfies("1.2.3", "!=1.2.3") is False


def test_valid_manifest_with_no_permissions_is_validated():
    manifest = make_manifest()
    record = validate_manifest(manifest)
    assert record.state == AddonState.VALIDATED
    assert record.errors == []


def test_sensitive_permission_requires_review():
    manifest = make_manifest(permissions=["shell.execute", "project.read"])
    record = validate_manifest(manifest)
    assert record.state == AddonState.PERMISSION_REVIEW
    assert record.sensitive_permissions == ["shell.execute"]


def test_missing_required_field_quarantines():
    manifest = make_manifest(id="")
    record = validate_manifest(manifest)
    assert record.state == AddonState.QUARANTINED
    assert any("id" in e for e in record.errors)


def test_unknown_permission_quarantines():
    manifest = make_manifest(permissions=["definitely.not.a.real.permission"])
    record = validate_manifest(manifest)
    assert record.state == AddonState.QUARANTINED


def test_unknown_extension_point_quarantines():
    manifest = make_manifest(provides={"not_a_real_extension_point": ["x"]})
    record = validate_manifest(manifest)
    assert record.state == AddonState.QUARANTINED


def test_incompatible_solomon_version_fails_closed():
    manifest = make_manifest(solomon_compatibility=">=99.0")
    record = validate_manifest(manifest)
    assert record.state == AddonState.QUARANTINED
    assert any("fail closed" in e for e in record.errors)


def test_missing_solomon_compatibility_quarantines():
    manifest = make_manifest(solomon_compatibility="")
    record = validate_manifest(manifest)
    assert record.state == AddonState.QUARANTINED


def test_discover_addons_no_root_returns_empty(tmp_path):
    assert discover_addons(tmp_path / "does-not-exist") == []


def test_discover_addons_finds_valid_manifest(tmp_path):
    addon_dir = tmp_path / "my-addon"
    addon_dir.mkdir()
    (addon_dir / "solomon-addon.yaml").write_text(
        "id: community.my-addon\n"
        "name: My Addon\n"
        "version: 0.1.0\n"
        "addon_api: \"1\"\n"
        "solomon_compatibility: \">=0.4,<0.6\"\n"
        "entrypoint: src/main.py\n"
        "permissions:\n  - project.read\n",
        encoding="utf-8",
    )
    records = discover_addons(tmp_path)
    assert len(records) == 1
    assert records[0].state == AddonState.VALIDATED
    assert records[0].manifest.id == "community.my-addon"


def test_discover_addons_quarantines_malformed_yaml(tmp_path):
    addon_dir = tmp_path / "broken-addon"
    addon_dir.mkdir()
    (addon_dir / "solomon-addon.yaml").write_text("id: [unclosed", encoding="utf-8")
    records = discover_addons(tmp_path)
    assert len(records) == 1
    assert records[0].state == AddonState.QUARANTINED
    assert records[0].manifest is None
