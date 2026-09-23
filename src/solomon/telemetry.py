"""Local hardware telemetry -- closes part of the "RAM/VRAM/GPU not
implemented" gap noted in Phase 4/6, scoped to GPU only.

Shells out to `nvidia-smi` (confirmed present on this machine) rather
than adding a new Python dependency (psutil/pynvml) -- consistent with
this codebase's lean-dependency approach throughout. RAM telemetry
stays deferred: it would need either a new dependency or Windows-
specific WMI calls, and unlike GPU load it isn't directly tied to a
specific local-inference call's cost, so the complexity wasn't judged
worth it yet.

This is a LIVE snapshot only (current hardware state), not historical
per-task data -- nvidia-smi has no way to report what GPU load was
during a past, already-finished call, so this cannot be retrofitted
into get_usage_summary's per-task history. The dashboard shows it
alongside its other live checks (adapter health) for that reason,
rather than the Usage Manager's historical aggregation.
"""

from __future__ import annotations

import shutil
import subprocess


def get_gpu_telemetry() -> dict | None:
    """Returns None (never raises) when nvidia-smi is absent or fails --
    this is optional best-effort telemetry, not a hard requirement."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:  # noqa: BLE001 - best-effort telemetry must not raise
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None

    first_line = proc.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in first_line.split(",")]
    if len(parts) != 4:
        return None
    name, gpu_load, mem_used, mem_total = parts
    try:
        return {
            "name": name,
            "gpu_load_percent": float(gpu_load),
            "vram_used_mb": float(mem_used),
            "vram_total_mb": float(mem_total),
        }
    except ValueError:
        return None
