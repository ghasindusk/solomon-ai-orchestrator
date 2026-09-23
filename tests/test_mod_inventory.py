import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.mod_inventory import list_installed_mods, mod_inventory_as_note


def test_returns_empty_for_missing_dir(tmp_path):
    assert list_installed_mods(tmp_path / "no-mods") == []


def test_lists_jars_with_best_effort_name_version_guess(tmp_path):
    d = tmp_path / "mods"
    d.mkdir()
    (d / "Apotheosis-1.20.1-7.4.8.jar").write_bytes(b"")
    (d / "notes.txt").write_text("ignore me", encoding="utf-8")
    mods = list_installed_mods(d)
    assert len(mods) == 1
    assert mods[0].filename == "Apotheosis-1.20.1-7.4.8.jar"
    assert mods[0].guessed_version == "1.20.1-7.4.8"


def test_unparseable_filename_keeps_raw_name_without_guess(tmp_path):
    d = tmp_path / "mods"
    d.mkdir()
    (d / "weirdname.jar").write_bytes(b"")
    mods = list_installed_mods(d)
    assert len(mods) == 1
    assert mods[0].filename == "weirdname.jar"
    assert mods[0].guessed_name is None
    assert mods[0].guessed_version is None


def test_mod_inventory_as_note_returns_none_when_empty(tmp_path):
    assert mod_inventory_as_note(tmp_path / "no-mods") is None


def test_mod_inventory_as_note_shape(tmp_path):
    d = tmp_path / "mods"
    d.mkdir()
    (d / "Apotheosis-1.20.1-7.4.8.jar").write_bytes(b"")
    (d / "BCLib-20.0.13.jar").write_bytes(b"")
    note = mod_inventory_as_note(d)
    assert note is not None
    assert "2 mods installed" in note.body
    assert "Apotheosis-1.20.1-7.4.8.jar" in note.body
    assert note.frontmatter["source"] == "mod_inventory"
    assert "Installed Mods (2)" in note.title
