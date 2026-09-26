"""Solomon -> Octavryn SI data migration (v0.5 spec 08, roadmap R7).

Only one piece of user data changes location: the SQLite state store,
`state/solomon.sqlite3` -> `state/octavryn.sqlite3`. Config files keep
their names (nothing Solomon-specific in them), and `.solomon_worktrees/`
is kept because stored worktree rows hold absolute paths into it (see
11_v0.5_Octavryn/R0_NAMING_INVENTORY.md).

Guarantees (spec 08 "Never silently lose user data"):
- copy, never move: the legacy file is left in place during apply;
- backup first: a consistent SQLite online-backup copy of the legacy DB
  goes to state/backups/ before anything else happens;
- verify: integrity_check on the new DB plus per-table row counts must
  equal the source's, otherwise the new file is removed again (it is our
  own partial output) and the command fails;
- idempotent: re-running apply against an unchanged source is a no-op;
- rollback: the (possibly newer) octavryn DB is copied back over the
  legacy path after backing the legacy file up, and the octavryn DB is
  moved aside, not deleted. Data written after migration survives a
  rollback.

Which file StateStore opens by default is decided by
`resolve_default_db_path()`: OCTAVRYN_DB env var, else the migrated DB when
the marker file exists, else the legacy DB. So nothing changes for a user
who has not migrated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parents[2] / "state"
LEGACY_DB_NAME = "solomon.sqlite3"
NEW_DB_NAME = "octavryn.sqlite3"
MARKER_NAME = "MIGRATION_v0.5.json"


def resolve_default_db_path(state_dir: Path | None = None) -> Path:
    env = os.environ.get("OCTAVRYN_DB")
    if env:
        return Path(env)
    d = state_dir or STATE_DIR
    if (d / MARKER_NAME).exists() and (d / NEW_DB_NAME).exists():
        return d / NEW_DB_NAME
    return d / LEGACY_DB_NAME


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _online_copy(src: Path, dst: Path) -> None:
    """sqlite3 backup API: a consistent snapshot even if another process
    has the source open (unlike a raw file copy mid-write)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def table_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        # Table names come from the DB file itself; quote them as SQL
        # identifiers (double any embedded quote) instead of trusting them.
        return {t: conn.execute('SELECT COUNT(*) FROM "' + t.replace('"', '""') + '"').fetchone()[0]  # nosec B608
                for t in tables}
    finally:
        conn.close()


def _integrity_ok(path: Path) -> bool:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def status(state_dir: Path | None = None) -> dict:
    d = state_dir or STATE_DIR
    legacy, new, marker = d / LEGACY_DB_NAME, d / NEW_DB_NAME, d / MARKER_NAME
    info = {
        "state_dir": str(d),
        "legacy_db_exists": legacy.exists(),
        "new_db_exists": new.exists(),
        "migrated": marker.exists(),
        "active_db": str(resolve_default_db_path(d)),
        "legacy_env_vars": sorted(k for k in os.environ if k.startswith("SOLOMON_")),
    }
    if marker.exists():
        m = json.loads(marker.read_text(encoding="utf-8"))
        info["marker"] = m
        if legacy.exists():
            info["legacy_changed_since_migration"] = _sha256(legacy) != m.get("source_sha256")
    return info


class MigrationError(RuntimeError):
    pass


def apply(state_dir: Path | None = None, dry_run: bool = False) -> dict:
    d = state_dir or STATE_DIR
    legacy, new, marker = d / LEGACY_DB_NAME, d / NEW_DB_NAME, d / MARKER_NAME
    if not legacy.exists():
        if new.exists():
            return {"action": "none", "reason": "no legacy DB; already on octavryn layout"}
        return {"action": "none", "reason": "no legacy DB and no state yet (fresh install)"}
    source_hash = _sha256(legacy)
    if marker.exists() and new.exists():
        m = json.loads(marker.read_text(encoding="utf-8"))
        if m.get("source_sha256") == source_hash:
            return {"action": "none", "reason": "already migrated from this exact source (idempotent)"}
        raise MigrationError(
            "legacy DB changed after a previous migration; refusing to overwrite the migrated DB. "
            "Run `octavryn migrate rollback` first or set OCTAVRYN_DB explicitly."
        )
    if new.exists():
        raise MigrationError(f"{new} exists without a migration marker; refusing to overwrite it")
    source_counts = table_counts(legacy)
    plan = {
        "action": "migrate", "source": str(legacy), "target": str(new),
        "source_sha256": source_hash, "source_counts": source_counts,
    }
    if dry_run:
        plan["dry_run"] = True
        return plan

    backup = d / "backups" / f"{LEGACY_DB_NAME}.{_ts()}.pre-v0.5.bak"
    _online_copy(legacy, backup)
    if table_counts(backup) != source_counts:
        backup.unlink(missing_ok=True)
        raise MigrationError("backup verification failed (row counts differ); nothing changed")

    _online_copy(legacy, new)
    try:
        from .state import StateStore

        StateStore(new).close()  # additive v0.5 schema (new tables/columns)
        new_counts = table_counts(new)
        mismatched = {t: (c, new_counts.get(t)) for t, c in source_counts.items() if new_counts.get(t) != c}
        if mismatched or not _integrity_ok(new):
            raise MigrationError(f"verification failed: {mismatched or 'integrity_check'}")
    except Exception:
        new.unlink(missing_ok=True)
        raise

    record = dict(plan)
    record.update({
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "backup": str(backup),
        "target_counts": table_counts(new),
        "rollback": "octavryn migrate rollback",
    })
    marker.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


def rollback(state_dir: Path | None = None) -> dict:
    d = state_dir or STATE_DIR
    legacy, new, marker = d / LEGACY_DB_NAME, d / NEW_DB_NAME, d / MARKER_NAME
    if not marker.exists():
        return {"action": "none", "reason": "not migrated"}
    ts = _ts()
    result: dict = {"action": "rollback"}
    if legacy.exists():
        legacy_backup = d / "backups" / f"{LEGACY_DB_NAME}.{ts}.pre-rollback.bak"
        _online_copy(legacy, legacy_backup)
        result["legacy_backup"] = str(legacy_backup)
    if new.exists():
        _online_copy(new, legacy)  # keep everything written since migration
        aside = d / "backups" / f"{NEW_DB_NAME}.{ts}.rolled-back"
        aside.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(new), str(aside))
        result["octavryn_db_moved_to"] = str(aside)
    marker_aside = d / "backups" / f"{MARKER_NAME}.{ts}.rolled-back"
    shutil.move(str(marker), str(marker_aside))
    result["marker_moved_to"] = str(marker_aside)
    result["active_db"] = str(resolve_default_db_path(d))
    return result


# -- CLI --------------------------------------------------------------------

def _cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(status(), indent=2, ensure_ascii=False))
    return 0


def _cmd_state(args: argparse.Namespace) -> int:
    try:
        out = apply(dry_run=not args.apply)
    except MigrationError as exc:
        print(f"Migration refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if not args.apply and out.get("action") == "migrate":
        print("(dry run; re-run with --apply to perform it)", file=sys.stderr)
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    print(json.dumps(rollback(), indent=2, ensure_ascii=False))
    return 0


def add_migration_parsers(sub) -> None:
    p = sub.add_parser("migrate", help="v0.5: Solomon -> Octavryn data migration (backup, verify, rollback)")
    msub = p.add_subparsers(dest="migrate_command", required=True)
    ps = msub.add_parser("status", help="Show layout, marker, and legacy env vars")
    ps.set_defaults(func=_cmd_status)
    pst = msub.add_parser("state", help="Migrate the SQLite state store (dry run unless --apply)")
    pst.add_argument("--apply", action="store_true")
    pst.set_defaults(func=_cmd_state)
    pr = msub.add_parser("rollback", help="Return to the legacy layout, keeping post-migration data")
    pr.set_defaults(func=_cmd_rollback)
