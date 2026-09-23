import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.registry import ProjectEntry
from solomon.knowledge import (
    load_notes_for_project,
    load_global_notes,
    search_notes,
    build_context_pack,
    redact_secrets,
    context_savings,
    ContextPack,
)


def make_project(tmp_path) -> ProjectEntry:
    return ProjectEntry(
        project_id="p1",
        name="Test Project",
        knowledge_path=str(tmp_path),
        status="active",
    )


def test_load_notes_parses_frontmatter_and_skips_system_dirs(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\ntitle: Alpha Spec\nsao_authority: current\n---\nAlpha content about widgets.",
        encoding="utf-8",
    )
    skip_dir = tmp_path / ".obsidian"
    skip_dir.mkdir()
    (skip_dir / "workspace.md").write_text("should be skipped", encoding="utf-8")

    notes = load_notes_for_project(make_project(tmp_path))
    assert len(notes) == 1
    assert notes[0].title == "Alpha Spec"
    assert notes[0].authority == "current"
    assert not notes[0].is_superseded


def test_load_notes_no_knowledge_path_returns_empty():
    project = ProjectEntry(project_id="p1", name="No knowledge", knowledge_path=None)
    assert load_notes_for_project(project) == []


def test_search_excludes_superseded_by_default(tmp_path):
    (tmp_path / "current.md").write_text(
        "---\ntitle: Widget Guide\n---\nHow to build a widget.", encoding="utf-8"
    )
    (tmp_path / "old.md").write_text(
        "---\ntitle: Widget Guide Old\nsao_authority: superseded\n---\nOld widget instructions.",
        encoding="utf-8",
    )
    notes = load_notes_for_project(make_project(tmp_path))
    results = search_notes(notes, "widget")
    titles = [r.note.title for r in results]
    assert "Widget Guide" in titles
    assert "Widget Guide Old" not in titles


def test_search_can_include_superseded(tmp_path):
    (tmp_path / "old.md").write_text(
        "---\ntitle: Old Doc\nsao_authority: superseded\n---\nwidget widget widget",
        encoding="utf-8",
    )
    notes = load_notes_for_project(make_project(tmp_path))
    results = search_notes(notes, "widget", include_superseded=True)
    assert len(results) == 1


def test_search_ranks_title_match_higher(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\ntitle: Widget\n---\nunrelated content", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ntitle: Unrelated\n---\nwidget mentioned once", encoding="utf-8"
    )
    notes = load_notes_for_project(make_project(tmp_path))
    results = search_notes(notes, "widget")
    assert results[0].note.title == "Widget"


def test_context_pack_respects_token_budget(tmp_path):
    for i in range(5):
        (tmp_path / f"n{i}.md").write_text(
            f"---\ntitle: Doc {i}\n---\n" + ("widget " * 500), encoding="utf-8"
        )
    notes = load_notes_for_project(make_project(tmp_path))
    results = search_notes(notes, "widget", top_k=5)
    pack = build_context_pack(results, token_budget=100, snippet_chars=800)
    assert pack.total_estimated_tokens <= 100
    assert pack.dropped_count >= 1
    assert pack.token_provenance == "ESTIMATED"


def test_search_notes_drops_exact_duplicate_bodies(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\ntitle: Widget A\n---\nwidget widget widget identical body", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ntitle: Widget B\n---\nwidget widget widget identical body", encoding="utf-8"
    )
    notes = load_notes_for_project(make_project(tmp_path))
    results = search_notes(notes, "widget", top_k=5)
    assert len(results) == 1


def test_redact_secrets_masks_known_key_formats():
    text = "here is a key: sk-abcdefghijklmnopqrstuvwx and an aws one AKIAABCDEFGHIJKLMNOP"
    redacted, count = redact_secrets(text)
    assert count == 2
    assert "sk-abcdefghijklmnopqrstuvwx" not in redacted
    assert "AKIAABCDEFGHIJKLMNOP" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_secrets_leaves_normal_text_untouched():
    text = "just a normal sentence about widgets and gadgets"
    redacted, count = redact_secrets(text)
    assert redacted == text
    assert count == 0


def test_build_context_pack_redacts_secrets_in_snippets(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\ntitle: Widget\n---\nwidget notes, api_key: abcdef0123456789secretvalue",
        encoding="utf-8",
    )
    notes = load_notes_for_project(make_project(tmp_path))
    results = search_notes(notes, "widget")
    pack = build_context_pack(results, token_budget=4000, snippet_chars=800)
    assert pack.redacted_secret_count >= 1
    assert "abcdef0123456789secretvalue" not in pack.entries[0].snippet


class _FakePolicy:
    def __init__(self, paths):
        self._paths = paths

    def global_knowledge_paths(self):
        return self._paths


def test_load_global_notes_reads_configured_paths(tmp_path):
    (tmp_path / "rule.md").write_text(
        "---\ntitle: Shared Rule\n---\neveryone follows this", encoding="utf-8"
    )
    notes = load_global_notes(policy=_FakePolicy([str(tmp_path)]))
    assert len(notes) == 1
    assert notes[0].title == "Shared Rule"


def test_load_global_notes_skips_old_dir(tmp_path):
    (tmp_path / "current.md").write_text(
        "---\ntitle: Current\n---\ncurrent content", encoding="utf-8"
    )
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    (old_dir / "stale.md").write_text("---\ntitle: Stale\n---\nold", encoding="utf-8")

    notes = load_global_notes(policy=_FakePolicy([str(tmp_path)]))
    titles = [n.title for n in notes]
    assert "Current" in titles
    assert "Stale" not in titles


def test_load_global_notes_missing_path_returns_empty(tmp_path):
    notes = load_global_notes(policy=_FakePolicy([str(tmp_path / "does-not-exist")]))
    assert notes == []


def test_context_savings_computes_saved_and_ratio():
    pack = ContextPack(total_estimated_tokens=200)
    savings = context_savings(pack, raw_context_tokens=1000)
    assert savings.raw_context_tokens == 1000
    assert savings.sent_context_tokens == 200
    assert savings.context_saved_tokens == 800
    assert savings.reduction_ratio == 0.8


def test_context_savings_handles_zero_raw_tokens_without_dividing_by_zero():
    pack = ContextPack(total_estimated_tokens=0)
    savings = context_savings(pack, raw_context_tokens=0)
    assert savings.context_saved_tokens == 0
    assert savings.reduction_ratio is None


def test_context_savings_never_goes_negative_when_pack_exceeds_raw_estimate():
    # Defensive: the two token counts come from independent estimators
    # (estimate_notes_tokens vs per-entry _estimate_tokens on redacted
    # snippets), so they could in principle disagree slightly.
    pack = ContextPack(total_estimated_tokens=150)
    savings = context_savings(pack, raw_context_tokens=100)
    assert savings.context_saved_tokens == 0
