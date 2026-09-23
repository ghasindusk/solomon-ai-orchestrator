# Examples

- **Config templates**: see `04_Config_Schemas/*.example.yaml` and
  `03_Policies/GLOBAL_POLICY.example.yaml` -- copy each to the
  non-`.example` filename (gitignored) and fill in your own values.
  `projects.registry.example.yaml` shows the three optional project
  fields (`crash_logs_path`/`mods_path`/`app_log_path`) in context.
- **Addon manifest**: `addons/example-addon/solomon-addon.yaml` is a
  real, validated example manifest -- run `python -m solomon.cli
  addons-list` to see it discovered and classified. Addon *execution*
  isn't implemented yet (see CHANGELOG.md's Known limitations); this demonstrates the
  manifest/permission model only.
- **Basic walkthrough**: `basic_walkthrough.md` in this directory.
