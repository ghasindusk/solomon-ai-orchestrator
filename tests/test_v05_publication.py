"""Octavryn SI v0.5 R9: Publication Sync tooling fails closed."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from solomon.publication import apply_acknowledgements, build_candidate, diff_summary, scan, select_files

MANIFEST = {"include": ["src/**", "README.md"], "exclude": ["**/__pycache__/**"]}


def make_repo(tmp_path, files):
    root = tmp_path / "private"
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return root


def test_only_allowlisted_files_are_copied(tmp_path):
    repo = make_repo(tmp_path, {"src/a.py": "x", "README.md": "r", "DECISIONS.md": "private",
                                "state/solomon.sqlite3": b"db", "src/__pycache__/a.pyc": b"c"})
    assert select_files(repo, MANIFEST) == ["README.md", "src/a.py"]
    files = build_candidate(repo, tmp_path / "cand", MANIFEST)
    assert files == ["README.md", "src/a.py"]
    assert not (tmp_path / "cand" / "DECISIONS.md").exists()


def test_candidate_must_be_outside_repo_and_empty(tmp_path):
    repo = make_repo(tmp_path, {"src/a.py": "x"})
    with pytest.raises(ValueError):
        build_candidate(repo, repo / "out", MANIFEST)
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "f").write_text("x")
    with pytest.raises(ValueError):
        build_candidate(repo, tmp_path / "busy", MANIFEST)


@pytest.mark.parametrize("content,kind", [
    ("key = 'sk-ant-api03-abcdefghijklmnopqrstuvwxyz'", "secret"),
    (r"see C:\Users\alice\notes", "user_path"),
    ("path /home/alice/x", "user_path"),
    ("mail me at alice@gmail.com", "email"),
    ("project my_private_game is great", "private_term"),
    ("hello alice", "username"),
])
def test_scan_detects(tmp_path, content, kind):
    d = tmp_path / "c"
    d.mkdir()
    (d / "f.md").write_text(content, encoding="utf-8")
    rep = scan(d, ["my_private_game"], username="alice")
    assert kind in {f.kind for f in rep.findings}
    assert not rep.publishable


def test_scan_allows_noreply_and_clean_text(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "f.md").write_text("Co-Authored-By: x <noreply@anthropic.com>\nplain text", encoding="utf-8")
    (d / "logo.png").write_bytes(b"\x89PNG\r\n")
    assert scan(d, [], username="alice").publishable


def test_forbidden_and_undecodable_files_block(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "state.sqlite3").write_bytes(b"x")
    (d / "blob.bin").write_bytes(b"\xff\xfe\x00\x81")
    kinds = {f.kind for f in scan(d, []).findings}
    assert kinds == {"forbidden_file_type", "undecodable"}


def test_acknowledgement_is_exact_and_cannot_cover_private_terms(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "t.py").write_text("x = 'sk-abcdefghijklmnopqrstuvwxyz'\nmy_private_game", encoding="utf-8")
    rep = scan(d, ["my_private_game"])
    manifest = {"acknowledged": [
        {"path": "t.py", "kind": "secret", "reason": "synthetic"},
        {"path": "t.py", "kind": "private_term", "reason": "please"},
        {"path": "other.py", "kind": "secret", "reason": "unrelated"},
    ]}
    apply_acknowledgements(rep, manifest)
    assert [f.kind for f in rep.findings] == ["private_term"]
    assert [f.kind for f in rep.acknowledged] == ["secret"]
    assert not rep.publishable


def test_ack_without_reason_is_ignored(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "t.py").write_text("x = 'sk-abcdefghijklmnopqrstuvwxyz'", encoding="utf-8")
    rep = apply_acknowledgements(scan(d, []), {"acknowledged": [{"path": "t.py", "kind": "secret"}]})
    assert not rep.publishable


def test_diff_summary(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d, files in ((a, {"x": "1", "y": "2"}), (b, {"x": "1", "y": "3", "z": "4"})):
        d.mkdir()
        for k, v in files.items():
            (d / k).write_text(v)
    assert diff_summary(a, b) == {"added": [], "removed_from_public": ["z"], "changed": ["y"]}


def test_overlay_files_are_copied_on_top_and_scanned(tmp_path):
    repo = make_repo(tmp_path, {"src/a.py": "x", "README.md": "private readme",
                                "pub/overlay/README.md": "public readme", "pub/overlay/pyproject.toml": "[project]"})
    files = build_candidate(repo, tmp_path / "cand", dict(MANIFEST, overlay="pub/overlay"))
    assert "pyproject.toml" in files
    assert (tmp_path / "cand" / "README.md").read_text() == "public readme"
    assert not (tmp_path / "cand" / "pub").exists()


def test_real_overlay_declares_octavryn_and_legacy_entry_points():
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[1]
    # dev repo: publication/public_overlay/; published candidate: repo root
    path = root / "publication" / "public_overlay" / "pyproject.toml"
    if not path.exists():
        path = root / "pyproject.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["octavryn"] == "octavryn.cli:main"
    assert scripts["solomon"] == "solomon.cli:legacy_main"  # deprecated alias keeps its notice
    assert data["project"]["version"] == "0.6.0a1"
