"""Context Firewall cross-project leakage tests (v0.4 Phase 7 Reliability
& Evaluation, DECISIONS.md D31). Formal Spec v0.4 section 7 lists
"project-boundary violations" among what the Context Firewall must filter.
In this codebase that boundary is architectural, not an internal runtime
check inside search_notes/build_context_pack (neither takes a project_id
at all) -- the only production caller (cli.py cmd_context_pack) resolves
exactly one ProjectEntry per invocation and never concatenates two
projects' note lists. These tests lock in that guarantee at the module
level, so a future refactor that accidentally shares or merges note
sources across projects would fail loudly here instead of silently
leaking one project's knowledge into another's context."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.registry import ProjectEntry
from solomon.knowledge import load_notes_for_project, load_global_notes, search_notes


def make_project(project_id: str, knowledge_dir: pathlib.Path) -> ProjectEntry:
    return ProjectEntry(project_id=project_id, name=project_id, knowledge_path=str(knowledge_dir), status="active")


def test_project_notes_never_include_another_projects_files(tmp_path):
    proj_a_dir = tmp_path / "proj_a"
    proj_b_dir = tmp_path / "proj_b"
    proj_a_dir.mkdir()
    proj_b_dir.mkdir()
    (proj_a_dir / "secret_a.md").write_text("---\ntitle: Project A secret\n---\nAlpha-only content, token ABC123.", encoding="utf-8")
    (proj_b_dir / "secret_b.md").write_text("---\ntitle: Project B secret\n---\nBeta-only content, token XYZ789.", encoding="utf-8")

    project_a = make_project("proj-a", proj_a_dir)
    notes_a = load_notes_for_project(project_a)

    assert len(notes_a) == 1
    assert notes_a[0].title == "Project A secret"
    assert "XYZ789" not in notes_a[0].body
    assert all("proj_b" not in str(n.path) for n in notes_a)


def test_search_results_for_project_a_never_surface_project_b_notes(tmp_path):
    proj_a_dir = tmp_path / "proj_a"
    proj_b_dir = tmp_path / "proj_b"
    proj_a_dir.mkdir()
    proj_b_dir.mkdir()
    (proj_a_dir / "a.md").write_text("---\ntitle: Widget A\n---\nwidget widget widget", encoding="utf-8")
    (proj_b_dir / "b.md").write_text("---\ntitle: Widget B\n---\nwidget widget widget", encoding="utf-8")

    notes_a = load_notes_for_project(make_project("proj-a", proj_a_dir))
    results = search_notes(notes_a, "widget")

    assert len(results) == 1
    assert results[0].note.title == "Widget A"


def test_load_notes_for_project_never_auto_includes_global_scope(tmp_path, monkeypatch):
    """Project notes and global-scope notes (docs/ai_rules/ equivalent,
    D24) are two independently-loaded sources; load_notes_for_project must
    never silently pull in global-scope content -- merging is an explicit,
    opt-out (--no-global) step the CALLER performs (cli.py
    cmd_context_pack), never something the loader does on its own."""
    global_dir = tmp_path / "global_rules"
    global_dir.mkdir()
    (global_dir / "rule.md").write_text("---\ntitle: Global Rule\n---\nApplies to every project.", encoding="utf-8")

    project_dir = tmp_path / "proj_a"
    project_dir.mkdir()
    (project_dir / "a.md").write_text("---\ntitle: Project A note\n---\nProject-specific content.", encoding="utf-8")

    project = make_project("proj-a", project_dir)
    notes = load_notes_for_project(project)

    assert len(notes) == 1
    assert notes[0].title == "Project A note"
    assert all(n.title != "Global Rule" for n in notes)


def test_global_notes_come_only_from_configured_global_paths(tmp_path):
    class _FakePolicy:
        def global_knowledge_paths(self):
            return [str(tmp_path / "global_only")]

    global_dir = tmp_path / "global_only"
    global_dir.mkdir()
    (global_dir / "rule.md").write_text("---\ntitle: Global Rule\n---\nShared rule.", encoding="utf-8")

    other_dir = tmp_path / "not_configured"
    other_dir.mkdir()
    (other_dir / "leak.md").write_text("---\ntitle: Should Not Appear\n---\nUnrelated content.", encoding="utf-8")

    notes = load_global_notes(policy=_FakePolicy())
    assert len(notes) == 1
    assert notes[0].title == "Global Rule"
