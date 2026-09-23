import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.app_logs import ingest_app_log


def test_returns_none_for_missing_file(tmp_path):
    assert ingest_app_log(tmp_path / "nope.log") is None


def test_ingests_short_log_fully(tmp_path):
    p = tmp_path / "run.log"
    p.write_text("line1\nline2\nline3\n", encoding="utf-8")
    note = ingest_app_log(p)
    assert note is not None
    assert "line1" in note.body
    assert "line3" in note.body
    assert note.frontmatter["source"] == "app_log"
    assert note.authority == "current"
    assert "run.log" in note.title


def test_truncates_to_tail_for_long_log(tmp_path):
    p = tmp_path / "run.log"
    p.write_text("\n".join(f"line{i}" for i in range(1000)), encoding="utf-8")
    note = ingest_app_log(p, max_lines=50)
    assert "line999" in note.body
    assert "line0\n" not in note.body
    assert "showing last 50" in note.body
