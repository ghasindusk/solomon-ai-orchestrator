import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.crash_logs import find_crash_reports, parse_crash_report, crash_reports_as_notes

_SAMPLE_CRASH_TEXT = """---- Minecraft Crash Report ----

// Oops.

Time: 2026-09-03 09:36:46
Description: Initializing game

java.lang.RuntimeException: null
\tat net.minecraftforge.registries.GameData.postRegisterEvents(GameData.java:315)
\tat net.minecraftforge.common.ForgeStatesProvider.lambda$new$4(ForgeStatesProvider.java:25)
\tat net.minecraftforge.fml.ModLoader.handleInlineTransition(ModLoader.java:217)

-- System Details --
Minecraft Version: 1.20.1
"""


def write_crash_report(tmp_path, filename, text=_SAMPLE_CRASH_TEXT) -> pathlib.Path:
    d = tmp_path / "crash-reports"
    d.mkdir(exist_ok=True)
    p = d / filename
    p.write_text(text, encoding="utf-8")
    return d


def test_find_crash_reports_returns_empty_for_missing_dir(tmp_path):
    assert find_crash_reports(tmp_path / "does-not-exist") == []


def test_find_crash_reports_lists_txt_files(tmp_path):
    d = write_crash_report(tmp_path, "crash-2026-09-03_09.36.46-client.txt")
    (d / "not-a-crash.log").write_text("ignore me", encoding="utf-8")
    found = find_crash_reports(d)
    assert len(found) == 1
    assert found[0].name == "crash-2026-09-03_09.36.46-client.txt"


def test_parse_crash_report_extracts_time_description_flavor(tmp_path):
    d = write_crash_report(tmp_path, "crash-2026-09-03_09.36.46-client.txt")
    report = parse_crash_report(d / "crash-2026-09-03_09.36.46-client.txt")
    assert report is not None
    assert report.timestamp == "2026-09-03 09:36:46"
    assert report.flavor == "client"
    assert report.description == "Initializing game"
    assert "java.lang.RuntimeException" in report.summary
    assert "GameData.postRegisterEvents" in report.summary


def test_parse_crash_report_falls_back_to_filename_timestamp_flavor(tmp_path):
    text = "---- Minecraft Crash Report ----\nno structured fields here\n"
    d = write_crash_report(tmp_path, "crash-2026-01-01_00.00.00-fml.txt", text)
    report = parse_crash_report(d / "crash-2026-01-01_00.00.00-fml.txt")
    assert report is not None
    assert report.timestamp == "2026-01-01_00.00.00"
    assert report.flavor == "fml"
    assert report.description == "(no Description line found)"


def test_parse_crash_report_returns_none_for_missing_file(tmp_path):
    assert parse_crash_report(tmp_path / "nope.txt") is None


def test_crash_reports_as_notes_produces_searchable_notes(tmp_path):
    d = write_crash_report(tmp_path, "crash-2026-09-03_09.36.46-client.txt")
    notes = crash_reports_as_notes(d)
    assert len(notes) == 1
    note = notes[0]
    assert "2026-09-03 09:36:46" in note.title
    assert note.frontmatter["source"] == "crash_report"
    assert note.authority == "current"
    assert not note.is_superseded
    assert "RuntimeException" in note.body


def test_crash_reports_as_notes_respects_limit(tmp_path):
    d = tmp_path / "crash-reports"
    d.mkdir()
    for i in range(5):
        (d / f"crash-2026-09-0{i+1}_00.00.00-client.txt").write_text(_SAMPLE_CRASH_TEXT, encoding="utf-8")
    notes = crash_reports_as_notes(d, limit=2)
    assert len(notes) == 2
