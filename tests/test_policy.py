import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.models import Risk
from solomon.policy import PolicyEngine


def test_critical_always_requires_approval_regardless_of_autonomy():
    policy = PolicyEngine()
    for autonomy in (None, 0, 1, 2, 3, 4, 5):
        assert policy.requires_approval(Risk.CRITICAL, autonomy=autonomy) is True
        assert policy.requires_approval(Risk.VERY_HIGH, autonomy=autonomy) is True


def test_high_requires_approval_per_base_policy_without_autonomy():
    policy = PolicyEngine()
    # GLOBAL_POLICY.yaml has destructive_bulk_change_requires_approval: true
    assert policy.requires_approval(Risk.HIGH) is True


def test_normal_does_not_require_approval_without_autonomy():
    policy = PolicyEngine()
    assert policy.requires_approval(Risk.NORMAL) is False
    assert policy.requires_approval(Risk.NORMAL, autonomy=5) is False
    assert policy.requires_approval(Risk.NORMAL, autonomy=3) is False


def test_low_autonomy_adds_caution_below_base_policy_floor():
    policy = PolicyEngine()
    # autonomy=0: ask even for LOW/NORMAL, which the base policy alone would not.
    assert policy.requires_approval(Risk.LOW, autonomy=0) is True
    assert policy.requires_approval(Risk.NORMAL, autonomy=0) is True

    # autonomy=1: LOW stays unescalated, NORMAL and above require approval.
    assert policy.requires_approval(Risk.LOW, autonomy=1) is False
    assert policy.requires_approval(Risk.NORMAL, autonomy=1) is True

    # autonomy=2: only HIGH and above (matches base policy already, no change).
    assert policy.requires_approval(Risk.NORMAL, autonomy=2) is False
    assert policy.requires_approval(Risk.HIGH, autonomy=2) is True
