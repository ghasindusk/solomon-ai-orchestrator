"""Publication Sync Workflow tooling (Octavryn SI v0.5 spec 10, roadmap R9).

Covers steps 3, 4 and 8 of the workflow: extract the publishable files
into a separate candidate directory, scan it, and summarize the diff
against the current public checkout. It never runs git in the public
repository, never pushes, and never tags. Steps 5-12 stay with a human,
because publication needs explicit approval.

Fail closed (spec 10 "automation must fail closed on ambiguous/private
content"):
- only allowlisted files are copied (publication.yaml `include`), minus
  `exclude`. A new file is private until someone adds it on purpose;
- any finding makes the candidate not publishable: secrets, absolute
  user paths, the owner's username, email addresses (except GitHub
  noreply), registered private project ids/names, DB/log/backup files,
  and files that cannot be decoded as text and are not a known asset type.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .knowledge import _SECRET_PATTERNS

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = _REPO_ROOT / "publication" / "publication.yaml"

_BINARY_ASSET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp"}
_FORBIDDEN_EXT = {".sqlite", ".sqlite3", ".db", ".log", ".bundle", ".bak", ".docx", ".xlsx", ".pyc", ".jsonl"}
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ALLOWED_EMAIL = re.compile(r"(noreply@(anthropic\.com|github\.com)|@users\.noreply\.github\.com$|@example\.(com|org))")
_WIN_USER_PATH = re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\\\s\"'<>]+", re.IGNORECASE)
_POSIX_USER_PATH = re.compile(r"/(?:c/)?Users/[^/\s\"'<>]+|/home/[^/\s\"'<>]+")


@dataclass
class Finding:
    path: str
    kind: str
    detail: str
    line: int | None = None


@dataclass
class ScanReport:
    files: int = 0
    findings: list[Finding] = field(default_factory=list)
    acknowledged: list[Finding] = field(default_factory=list)

    @property
    def publishable(self) -> bool:
        return not self.findings


def load_manifest(path: Path | str | None = None) -> dict:
    with open(Path(path) if path else DEFAULT_MANIFEST, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel, p) for p in patterns)


def select_files(repo_root: Path, manifest: dict) -> list[str]:
    include = manifest.get("include") or []
    exclude = manifest.get("exclude") or []
    out = []
    for p in sorted(repo_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(repo_root).as_posix()
        if rel.startswith(".git/"):
            continue
        if _matches(rel, include) and not _matches(rel, exclude):
            out.append(rel)
    return out


def build_candidate(repo_root: Path | str, out_dir: Path | str, manifest: dict) -> list[str]:
    repo_root, out_dir = Path(repo_root), Path(out_dir)
    try:
        out_dir.resolve().relative_to(repo_root.resolve())
        raise ValueError("candidate directory must be outside the private repository")
    except ValueError as exc:
        if "must be outside" in str(exc):
            raise
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"{out_dir} is not empty; refusing to mix with other content")
    files = select_files(repo_root, manifest)
    for rel in files:
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo_root / rel, dst)
    # v0.5 (D62): public-only files (pyproject.toml, public CHANGELOG) live in
    # an overlay directory and are copied on top. They are scanned like
    # everything else afterwards.
    overlay = manifest.get("overlay")
    if overlay:
        root = repo_root / overlay
        for p in sorted(root.rglob("*")):
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                dst = out_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
                if rel not in files:
                    files.append(rel)
    return sorted(files)


def scan(candidate_dir: Path | str, private_terms: list[str], username: str | None = None) -> ScanReport:
    root = Path(candidate_dir)
    report = ScanReport()
    terms = sorted({t for t in private_terms if t and len(t) >= 4}, key=len, reverse=True)
    term_res = [(t, re.compile(re.escape(t), re.IGNORECASE)) for t in terms]
    user_re = re.compile(re.escape(username), re.IGNORECASE) if username and len(username) >= 3 else None
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        report.files += 1
        ext = p.suffix.lower()
        if ext in _FORBIDDEN_EXT:
            report.findings.append(Finding(rel, "forbidden_file_type", ext))
            continue
        if ext in _BINARY_ASSET_EXT and ext != ".svg":
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            report.findings.append(Finding(rel, "undecodable", "not UTF-8 text and not a known asset type"))
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat in _SECRET_PATTERNS:
                if pat.search(line):
                    report.findings.append(Finding(rel, "secret", pat.pattern[:40], lineno))
            for m in _EMAIL.finditer(line):
                if not _ALLOWED_EMAIL.search(m.group(0)):
                    report.findings.append(Finding(rel, "email", m.group(0), lineno))
            if _WIN_USER_PATH.search(line) or _POSIX_USER_PATH.search(line):
                report.findings.append(Finding(rel, "user_path", line.strip()[:80], lineno))
            if user_re and user_re.search(line):
                report.findings.append(Finding(rel, "username", username, lineno))
            for term, rx in term_res:
                if rx.search(line):
                    report.findings.append(Finding(rel, "private_term", term, lineno))
                    break
    return report


def apply_acknowledgements(report: ScanReport, manifest: dict) -> ScanReport:
    """Moves findings a human explicitly reviewed (manifest `acknowledged`:
    {path, kind, reason}) out of `findings`. A path+kind pair is required;
    there are no wildcards, so a new finding in the same file is not
    silently covered. private_term and username findings cannot be
    acknowledged: those always block publication. (A user_path may be
    acknowledged, e.g. a regex that describes paths; the separate username
    check still catches a real leaked path.)"""
    acks = {(a.get("path"), a.get("kind")) for a in manifest.get("acknowledged") or []
            if a.get("reason") and a.get("kind") not in ("private_term", "username")}
    keep, acked = [], []
    for f in report.findings:
        (acked if (f.path, f.kind) in acks else keep).append(f)
    report.findings, report.acknowledged = keep, acked
    return report


def private_terms_from_registry(registry) -> list[str]:
    """Registered project ids and display names (the owner's private
    projects). The orchestrator's own id is exempt: it is the product."""
    terms = []
    for entry in registry.list_projects(include_superseded=True):
        if entry.project_id in ("solomon_ai_orchestrator",):
            continue
        terms.append(entry.project_id)
        name = getattr(entry, "name", "") or ""
        for part in re.split(r"\s+[-–—/(（]|[:：]", name):
            part = part.strip()
            if len(part) >= 4:
                terms.append(part)
    return terms


def diff_summary(candidate_dir: Path | str, public_dir: Path | str) -> dict:
    """Public side = tracked files only (read-only `git ls-files`), so the
    public checkout's local caches and gitignored configs are not reported
    as differences."""
    import subprocess  # nosec B404

    cand, pub = Path(candidate_dir), Path(public_dir)

    def files(root: Path) -> set[str]:
        return {p.relative_to(root).as_posix() for p in root.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(root).parts}

    a = files(cand)
    if (pub / ".git").exists():
        # Fixed argv, no shell, read-only command; `git` resolved from PATH.
        proc = subprocess.run(["git", "-C", str(pub), "ls-files"], capture_output=True,  # nosec B603 B607
                              encoding="utf-8", errors="replace", timeout=60)
        b = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    else:
        b = files(pub)
    changed = sorted(r for r in a & b if (cand / r).read_bytes() != (pub / r).read_bytes())
    return {"added": sorted(a - b), "removed_from_public": sorted(b - a), "changed": changed}


# -- CLI --------------------------------------------------------------------

def _cmd_publication_check(args) -> int:
    import getpass
    import json

    from .registry import ProjectRegistry

    manifest = load_manifest(args.manifest)
    files = build_candidate(_REPO_ROOT, args.out, manifest)
    report = scan(args.out, private_terms_from_registry(ProjectRegistry()), username=getpass.getuser())
    apply_acknowledgements(report, manifest)
    out = {
        "candidate": str(args.out),
        "files": len(files),
        "publishable": report.publishable,
        "findings": [f.__dict__ for f in report.findings],
        "acknowledged": [f.__dict__ for f in report.acknowledged],
        "note": "No git operation, push, tag or release was performed. Publication needs explicit human approval.",
    }
    if args.public_dir:
        out["diff_vs_public"] = diff_summary(args.out, args.public_dir)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if report.publishable else 2


def add_publication_parsers(sub) -> None:
    p = sub.add_parser("publication-check",
                       help="v0.5: build a sanitized public candidate from the allowlist and scan it (never pushes)")
    p.add_argument("--out", required=True, help="Empty directory OUTSIDE this repository for the candidate")
    p.add_argument("--manifest", default=None)
    p.add_argument("--public-dir", dest="public_dir", default=None, help="Public checkout to diff against (read-only)")
    p.set_defaults(func=_cmd_publication_check)
