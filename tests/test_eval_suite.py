"""Tests for the Eval Suite (Phase 7 4/4, DECISIONS.md D31). Uses small,
throwaway test files under tmp_path -- never re-runs the real suite
recursively, which would be slow and would make this test's own pass/fail
depend on the rest of the codebase's state."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.eval_suite import CATEGORY_FILES, run_eval


def write_fixture(tests_dir: pathlib.Path, name: str, body: str) -> None:
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / name).write_text(body, encoding="utf-8")


def test_category_all_passing(tmp_path):
    tests_dir = tmp_path / "tests"
    write_fixture(tests_dir, "test_ok.py", "def test_a():\n    assert True\ndef test_b():\n    assert True\n")

    report = run_eval(category_files={"routing": ["test_ok.py"]}, tests_dir=tests_dir)
    assert len(report.categories) == 1
    cat = report.categories[0]
    assert cat.category == "routing"
    assert cat.ok is True
    assert cat.passed == 2
    assert cat.failed == 0
    assert report.all_ok is True


def test_category_with_a_failure_is_reported_not_swallowed(tmp_path):
    tests_dir = tmp_path / "tests"
    write_fixture(tests_dir, "test_bad.py", "def test_a():\n    assert True\ndef test_b():\n    assert False\n")

    report = run_eval(category_files={"safety": ["test_bad.py"]}, tests_dir=tests_dir)
    cat = report.categories[0]
    assert cat.ok is False
    assert cat.passed == 1
    assert cat.failed == 1
    assert report.all_ok is False


def test_missing_category_file_is_skipped_not_errored(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    report = run_eval(category_files={"addons": ["test_does_not_exist.py"]}, tests_dir=tests_dir)
    assert report.categories == []


def test_multiple_categories_are_independent(tmp_path):
    tests_dir = tmp_path / "tests"
    write_fixture(tests_dir, "test_good.py", "def test_a():\n    assert True\n")
    write_fixture(tests_dir, "test_broken.py", "def test_a():\n    assert False\n")

    report = run_eval(
        category_files={"routing": ["test_good.py"], "safety": ["test_broken.py"]},
        tests_dir=tests_dir,
    )
    by_category = {c.category: c for c in report.categories}
    assert by_category["routing"].ok is True
    assert by_category["safety"].ok is False
    assert report.all_ok is False


def test_other_files_lists_unmapped_test_files(tmp_path):
    tests_dir = tmp_path / "tests"
    write_fixture(tests_dir, "test_mapped.py", "def test_a():\n    assert True\n")
    write_fixture(tests_dir, "test_unmapped.py", "def test_a():\n    assert True\n")

    report = run_eval(category_files={"routing": ["test_mapped.py"]}, tests_dir=tests_dir)
    assert report.other_files == ["test_unmapped.py"]


def test_default_category_files_only_reference_real_existing_test_files():
    """CATEGORY_FILES is hand-maintained; guard against it silently
    drifting out of sync with tests/ as files get renamed."""
    real_tests_dir = pathlib.Path(__file__).resolve().parent
    for category, filenames in CATEGORY_FILES.items():
        for filename in filenames:
            assert (real_tests_dir / filename).exists(), f"{category}: {filename} does not exist"


def test_running_the_real_default_category_files_all_pass():
    """A real (not fixture-based) smoke run of the actual mapped test
    files, confirming the eval suite's own categorization reports green
    against this repo's current state."""
    report = run_eval()
    assert report.categories, "expected at least one category to have run"
    assert report.all_ok, [c for c in report.categories if not c.ok]
