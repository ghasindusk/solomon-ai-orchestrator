# Addons (v0.4 Phase 5, safe subset only)

`octavryn addons-list` scans `addons/*/octavryn-addon.yaml` (and the legacy
`solomon-addon.yaml`) and reports each
manifest's state: DISCOVERED -> VALIDATED / PERMISSION_REVIEW / QUARANTINED.

`example-addon/` is a copy of the v0.4 spec's own
`04_Addon_SDK/solomon-addon.example.yaml`, kept here as a live example and
regression fixture -- it requests no sensitive permissions, so it lands in
VALIDATED.

**No addon here is ever executed.** `src/solomon/addon_manager.py` does not
implement the ENABLED lifecycle state -- there is no process-isolation
mechanism in this codebase yet that could safely run an addon requesting
`shell.execute`/`filesystem.outside_project`/`secrets.use`. See
`DECISIONS.md` D22.
