"""Knowledge & Context Manager -- lean Phase 2 slice (Architecture doc
section 2 "Knowledge & Context Manager" / FR-03 / FR-14).

Deliberately does NOT read the Obsidian vault's existing `.smart-env`
(Smart Connections plugin) embedding index: that format is internal,
undocumented and plugin-version-dependent, so parsing it directly would be
fragile. Reusing it properly (or querying it live) is a documented
follow-up in 08_Discovery/PHASE0_DISCOVERY_REPORT.md, not done here.

Instead this is a plain, dependency-free retrieval layer over a project's
Markdown notes:
  - parse YAML frontmatter, honoring an `sao_authority: current|superseded`
    convention (FR-03: distinguish authoritative/current from historical)
  - score notes against a query via simple term-frequency matching
    (title weighted higher than body) -- no embedding model required
  - pack top matches into a token-budgeted "context pack" (FR-14: send
    only relevant, compressed, current context to expensive agents),
    using a char/4 ESTIMATED token count since no tokenizer is wired up
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .registry import ProjectEntry
from .usage_record import ContextSavings

_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_SKIP_DIR_NAMES = {".obsidian", ".smart-env", ".trash", ".git", ".claude", "old"}
_TOKEN_RE = re.compile(r"[\w一-龠ぁ-んァ-ヶー]+", re.UNICODE)

# v0.4 Architecture "Context Firewall": filters secrets before context
# reaches an execution agent. Deliberately conservative regexes (a few
# well-known key formats + a generic "key/token/secret/password = value"
# catch-all) -- a lean first pass, not a claimed-complete secret scanner.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\"']?[A-Za-z0-9\-_.]{12,}"),
]


@dataclass
class Note:
    path: Path
    title: str
    frontmatter: dict
    body: str

    @property
    def authority(self) -> str:
        return str(self.frontmatter.get("sao_authority", "current")).lower()

    @property
    def is_superseded(self) -> bool:
        return self.authority == "superseded"


@dataclass
class ScoredNote:
    note: Note
    score: float


@dataclass
class ContextPackEntry:
    path: str
    title: str
    snippet: str
    score: float
    estimated_tokens: int


@dataclass
class ContextPack:
    entries: list[ContextPackEntry] = field(default_factory=list)
    total_estimated_tokens: int = 0
    token_provenance: str = "ESTIMATED"
    dropped_count: int = 0  # dropped for token budget
    redacted_secret_count: int = 0  # v0.4 Context Firewall: secret-looking substrings redacted


def redact_secrets(text: str) -> tuple[str, int]:
    """v0.4 Architecture 'Context Firewall': strip anything that looks
    like a credential before it can reach an execution agent's context.
    Returns (redacted_text, count) -- the count feeds ContextPack's
    redacted_secret_count so this is auditable, not silent."""
    count = 0

    def _sub(match: re.Match) -> str:
        nonlocal count
        count += 1
        return "[REDACTED]"

    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_sub, text)
    return text, count


def _parse_note(path: Path) -> Note | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return None

    match = _FRONTMATTER_RE.match(raw)
    frontmatter: dict = {}
    body = raw
    if match:
        body = raw[match.end() :]
        try:
            parsed = yaml.safe_load(match.group(1))
            if isinstance(parsed, dict):
                frontmatter = parsed
        except yaml.YAMLError:
            frontmatter = {}

    title = str(frontmatter.get("title") or path.stem)
    return Note(path=path, title=title, frontmatter=frontmatter, body=body)


def load_notes_for_project(project: ProjectEntry) -> list[Note]:
    notes: list[Note] = []

    if project.knowledge_path:
        root = Path(project.knowledge_path)
        if root.exists():
            for md_path in root.rglob("*.md"):
                if any(part in _SKIP_DIR_NAMES for part in md_path.parts):
                    continue
                note = _parse_note(md_path)
                if note is not None:
                    notes.append(note)

    if project.crash_logs_path:
        # Lazy import: crash_logs.py imports Note from here, so importing
        # it at module load time would be circular.
        from .crash_logs import crash_reports_as_notes

        notes.extend(crash_reports_as_notes(project.crash_logs_path))

    if project.mods_path:
        from .mod_inventory import mod_inventory_as_note

        inventory_note = mod_inventory_as_note(project.mods_path)
        if inventory_note is not None:
            notes.append(inventory_note)

    if project.app_log_path:
        from .app_logs import ingest_app_log

        log_note = ingest_app_log(project.app_log_path)
        if log_note is not None:
            notes.append(log_note)

    return notes


def load_global_notes(policy=None) -> list[Note]:
    """v0.4 Phase 3 'Project/global knowledge scopes': loads shared,
    cross-project rules content configured via GLOBAL_POLICY.yaml's
    knowledge.global_knowledge_paths (e.g. docs/ai_rules/). Kept
    separate from load_notes_for_project rather than folded in
    automatically, so callers explicitly opt into merging the two --
    isolate_projects stays true for actual project data."""
    from .policy import PolicyEngine

    policy = policy or PolicyEngine()
    notes: list[Note] = []
    for raw_path in policy.global_knowledge_paths():
        root = Path(raw_path)
        if not root.exists():
            continue
        for md_path in root.rglob("*.md"):
            if any(part in _SKIP_DIR_NAMES for part in md_path.parts):
                continue
            note = _parse_note(md_path)
            if note is not None:
                notes.append(note)
    return notes


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def search_notes(
    notes: list[Note], query: str, top_k: int = 5, include_superseded: bool = False
) -> list[ScoredNote]:
    query_terms = _tokenize(query)
    if not query_terms:
        return []

    scored: list[ScoredNote] = []
    for note in notes:
        if note.is_superseded and not include_superseded:
            continue
        title_tokens = _tokenize(note.title)
        body_tokens = _tokenize(note.body)
        score = 0.0
        for term in query_terms:
            score += 3.0 * title_tokens.count(term)
            score += 1.0 * body_tokens.count(term)
        if score > 0:
            scored.append(ScoredNote(note=note, score=score))

    scored.sort(key=lambda s: s.score, reverse=True)

    # v0.4 Context Firewall: drop exact-duplicate bodies, keeping the
    # highest-scored copy (already sorted above). Exact-hash only -- a
    # lean pass, not fuzzy near-duplicate detection.
    seen_bodies: set[str] = set()
    deduped: list[ScoredNote] = []
    for item in scored:
        body_key = item.note.body.strip()
        if body_key in seen_bodies:
            continue
        seen_bodies.add(body_key)
        deduped.append(item)

    return deduped[:top_k]


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def estimate_notes_tokens(notes: list[Note]) -> int:
    """Raw ESTIMATED token size of every note's full body, unfiltered --
    the Phase 6 dashboard's "context savings" panel compares a real
    context-pack's total_estimated_tokens against this to show how much
    a query-scoped pack actually saves vs. sending everything."""
    return sum(_estimate_tokens(note.body) for note in notes)


def context_savings(pack: ContextPack, raw_context_tokens: int) -> ContextSavings:
    """v0.4 Token & Compute Intelligence Context Savings (Phase 6 reopen
    step 5, DECISIONS.md D27): how much the Context Firewall kept out of
    an execution agent's context. `raw_context_tokens` is
    estimate_notes_tokens() over the full unfiltered note set that was
    available to search_notes(); `pack` is what build_context_pack()
    actually assembled (post relevance-ranking, de-dup, secret redaction
    and token-budget truncation) for this task.

    Both sides share the same ESTIMATED provenance (see
    estimate_notes_tokens' docstring) -- this is a size estimate
    (len(text)//4), not a real tokenizer count, so reduction_ratio is
    directionally meaningful, not an exact figure.
    """
    sent = pack.total_estimated_tokens
    saved = max(0, raw_context_tokens - sent)
    reduction_ratio = (saved / raw_context_tokens) if raw_context_tokens > 0 else None
    return ContextSavings(
        raw_context_tokens=raw_context_tokens,
        sent_context_tokens=sent,
        context_saved_tokens=saved,
        reduction_ratio=reduction_ratio,
    )


def build_context_pack(
    scored_notes: list[ScoredNote], token_budget: int = 4000, snippet_chars: int = 800
) -> ContextPack:
    pack = ContextPack()
    for scored in scored_notes:
        raw_snippet = scored.note.body.strip()[:snippet_chars]
        snippet, redacted = redact_secrets(raw_snippet)
        pack.redacted_secret_count += redacted
        entry = ContextPackEntry(
            path=str(scored.note.path),
            title=scored.note.title,
            snippet=snippet,
            score=scored.score,
            estimated_tokens=_estimate_tokens(snippet),
        )
        if pack.total_estimated_tokens + entry.estimated_tokens > token_budget:
            pack.dropped_count += 1
            continue
        pack.entries.append(entry)
        pack.total_estimated_tokens += entry.estimated_tokens
    return pack
