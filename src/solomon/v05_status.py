"""v0.5 status sections for the dashboard and `octavryn remote status`.

Read-only and cheap. Nothing here spawns an adapter CLI:
- intelligences come from the persisted registry (refreshed only by
  `octavryn intelligences --refresh`);
- surfaces are a filesystem check;
- the remote queue and migration state are SQLite/marker reads.
Every section degrades to an explicit "unknown"/"not initialized" line
instead of failing the whole dashboard.
"""

from __future__ import annotations

from .state import StateStore


def remote_queue_summary(store: StateStore) -> dict:
    from .remote import worker as wk

    wk.ensure_schema(store)
    with store._lock:
        rows = store.conn.execute("SELECT status, COUNT(*) FROM remote_requests GROUP BY status").fetchall()
        workers = store.conn.execute("SELECT worker_id, state, last_heartbeat FROM remote_workers").fetchall()
    return {
        "requests_by_status": {s: n for s, n in rows},
        "workers": [{"worker_id": w[0], "state": w[1], "last_heartbeat": w[2]} for w in workers],
    }


def render_v05_sections(store: StateStore) -> str:
    lines = ["", "-- Octavryn v0.5 --"]
    try:
        from .intelligence_registry import IntelligenceRegistry

        descs = IntelligenceRegistry(state=store).load_persisted()
        if descs:
            lines.append("Intelligences (persisted registry): " + ", ".join(
                f"{d.id}={d.availability.value}" for d in descs))
        else:
            lines.append("Intelligences: registry empty (run `octavryn intelligences --refresh`)")
    except Exception as exc:  # noqa: BLE001 - dashboard must still render
        lines.append(f"Intelligences: unknown ({type(exc).__name__})")
    try:
        from .surfaces import detect_surfaces

        lines.append("Surfaces: " + ", ".join(f"{s.id}={s.availability.value}" for s in detect_surfaces()))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Surfaces: unknown ({type(exc).__name__})")
    try:
        q = remote_queue_summary(store)
        by = q["requests_by_status"]
        lines.append("Remote queue: " + (", ".join(f"{k}={v}" for k, v in sorted(by.items())) or "empty")
                     + f"; workers={len(q['workers'])}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Remote queue: unknown ({type(exc).__name__})")
    try:
        from .migration import status as migration_status

        m = migration_status()
        lines.append(f"State layout: {'migrated (octavryn.sqlite3)' if m['migrated'] else 'legacy (solomon.sqlite3)'}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"State layout: unknown ({type(exc).__name__})")
    return "\n".join(lines)
