import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.state import StateStore


def make_store(tmp_path) -> StateStore:
    return StateStore(db_path=tmp_path / "state.sqlite3")


def test_create_and_get_approval_request(tmp_path):
    store = make_store(tmp_path)
    store.create_approval_request("appr-1", "proj", "HIGH", "test reason", {"foo": "bar"})
    req = store.get_approval_request("appr-1")
    assert req["status"] == "pending"
    assert req["risk"] == "HIGH"
    assert req["task_params"] == {"foo": "bar"}


def test_get_unknown_returns_none(tmp_path):
    store = make_store(tmp_path)
    assert store.get_approval_request("does-not-exist") is None


def test_decide_approve(tmp_path):
    store = make_store(tmp_path)
    store.create_approval_request("appr-1", "proj", "HIGH", "reason", {})
    ok = store.decide_approval_request("appr-1", approved=True, note="looks fine")
    assert ok is True
    req = store.get_approval_request("appr-1")
    assert req["status"] == "approved"
    assert req["decision_note"] == "looks fine"


def test_decide_deny(tmp_path):
    store = make_store(tmp_path)
    store.create_approval_request("appr-1", "proj", "HIGH", "reason", {})
    store.decide_approval_request("appr-1", approved=False)
    assert store.get_approval_request("appr-1")["status"] == "denied"


def test_cannot_redecide_already_decided(tmp_path):
    store = make_store(tmp_path)
    store.create_approval_request("appr-1", "proj", "HIGH", "reason", {})
    store.decide_approval_request("appr-1", approved=True)
    ok = store.decide_approval_request("appr-1", approved=False)
    assert ok is False
    assert store.get_approval_request("appr-1")["status"] == "approved"


def test_list_filters_by_project_and_status(tmp_path):
    store = make_store(tmp_path)
    store.create_approval_request("appr-1", "proj_a", "HIGH", "r1", {})
    store.create_approval_request("appr-2", "proj_b", "HIGH", "r2", {})
    store.decide_approval_request("appr-2", approved=True)

    assert len(store.list_approval_requests(project_id="proj_a")) == 1
    assert len(store.list_approval_requests(status="pending")) == 1
    assert len(store.list_approval_requests(status="approved")) == 1
    assert len(store.list_approval_requests()) == 2
