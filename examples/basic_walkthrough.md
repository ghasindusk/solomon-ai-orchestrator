# Basic walkthrough

This assumes you've already copied the example configs (see
`../README.md`'s "Getting started" section) and registered at least one
project in `projects.registry.yaml`.

## 1. Confirm your project is registered

```bash
python -m octavryn project-list
```

## 2. See which adapters Octavryn would consider for a role, and why

Dry-run only -- no adapter is actually invoked.

```bash
python -m octavryn route --role coder --project-id your_project
```

Each candidate prints its weighted score components (`skill_match`,
`historical_quality`, `availability`, `usage_efficiency`, ...) and any
notes explaining a neutral/placeholder value (e.g. "no recorded task
history yet"). Nothing here is fabricated -- a component you have no
data for yet shows up as a documented neutral default, not a guess.

## 3. Run a real task end-to-end, with routing + fallback

```bash
python -m octavryn route-and-run --role coder --project-id your_project \
  --prompt "describe the task here"
```

If the prompt classifies as high-risk (see `src/solomon/risk.py`), or
your project's Token Budget has hit its hard-stop threshold, this may
instead print an approval request ID. Decide it explicitly:

```bash
python -m octavryn approvals list
python -m octavryn approvals decide <request-id> --approve
python -m octavryn execute-approved <request-id>
```

## 4. Check on things

```bash
python -m octavryn dashboard --project-id your_project
python -m octavryn usage --project-id your_project
python -m octavryn learning-report
```

## 5. Export a redacted diagnostics bundle (e.g. to attach to a bug report)

```bash
python -m octavryn diagnostics-export --project-id your_project
```

By default this **omits** prompts, approval reasons, project file
paths, and note content -- see `README.md` and `SECURITY.md` before
ever using `--include-sensitive`.
