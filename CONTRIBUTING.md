# Contributing to Solomon AI Orchestrator

Thanks for your interest. This is an early-alpha, personal/research
project -- expect rapid change and a fairly deliberate, cautious review
process around anything that touches safety or policy behavior.

## Before you start

- For small fixes (typos, docs, a clear bug with a minimal repro), open
  a PR directly.
- For anything larger (a new adapter, a new routing signal, a schema
  change, an addon capability), please open a Discussion or Issue first
  to agree on the approach before investing in an implementation --
  see "Contribution boundary" below for why.

## Development setup

```bash
pip install -r requirements.txt
python -m pytest -q
```

The suite is the source of truth for expected behavior; a change that
regresses it will not be merged without a clear reason recorded for the
change (see `DECISIONS.md` for the format this project uses).

## Contribution boundary

- **Core safety/policy changes require stricter review.** Anything that
  touches `policy.py`'s approval gate, the autonomy dial's safety floor,
  or Token Budget's hard-stop behavior needs an explicit rationale and
  test coverage for both the "gate fires" and "gate does not silently
  weaken" cases.
- **Prefer adapters/addons for new provider integrations** over adding
  provider-specific logic to Core. `src/solomon/adapters/` is the
  extension point for new AI CLIs/APIs.
- **New community integrations should include tests and compatibility
  metadata** (an addon manifest's `solomon_compatibility` version range,
  or an adapter's own test coverage).

## Reporting bugs / requesting features

Use GitHub Issues for bugs and concrete, actionable feature requests.
Use GitHub Discussions for open-ended ideas, proposed adapters/addons,
workflow questions, or routing-quality feedback that doesn't yet have a
specific fix in mind. Issue templates are provided for bug reports,
feature requests, adapter/addon requests, and routing-quality reports.

## Security issues

Do not open a public issue for a security-relevant report -- see
`SECURITY.md`.

## Code of Conduct

By participating, you agree to abide by `CODE_OF_CONDUCT.md`.
