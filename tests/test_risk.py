import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.models import Risk
from solomon.risk import classify_risk


def test_critical_keywords():
    assert classify_risk("adhoc", "please force push to main") == Risk.CRITICAL
    assert classify_risk("adhoc", "run rm -rf on the build dir") == Risk.CRITICAL


def test_high_keywords():
    assert classify_risk("adhoc", "delete the old config files") == Risk.HIGH
    assert classify_risk("adhoc", "run the database migration") == Risk.HIGH


def test_low_keywords():
    assert classify_risk("adhoc", "summarize this document") == Risk.LOW
    assert classify_risk("adhoc", "review the pull request") == Risk.LOW


def test_default_normal():
    assert classify_risk("adhoc", "add a new button to the settings page") == Risk.NORMAL


def test_critical_takes_priority_over_high_and_low():
    assert classify_risk("adhoc", "read the docs then force push") == Risk.CRITICAL
