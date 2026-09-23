#!/usr/bin/env python3
"""Solomon v0.4 Gateway -- UserPromptSubmit hook.

See hooks/README.md.
This is Phase 2's advisory-only gateway hook: it can only ADD context to
what Claude sees, it can never block a prompt (Claude Code hooks have no
genuine pre-decision interception point -- D17). It fails open
unconditionally: any error, bad input, missing project, or timeout
results in a silent no-op (exit 0, no stdout), so a bug here cannot break
normal Claude Code usage in any project, including unrelated ones.

To activate (or on a fresh clone): add an entry under
hooks.UserPromptSubmit in ~/.claude/settings.json (machine-wide) or a
project's .claude/settings.json (scoped). See hooks/README.md for the
exact snippet -- registration itself lives in your own local
settings.json, not in this repo, and is a deliberate per-machine choice
(D19), not something this migration/install does automatically.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# v0.4 Phase 9 (D34): derived from this file's own location rather than
# hardcoded, so this script works on any machine/clone -- it previously
# hardcoded one specific developer's path, which was both a portability
# bug (broke on any other machine) and a personal-path leak (v0.4
# GitHub Publication Plan: no personal paths in the published repo).
_SOLOMON_HOME = str(Path(__file__).resolve().parent.parent)
_TIMEOUT_SECONDS = 8  # well under UserPromptSubmit's 30s hook budget (D17)


def _evaluate(prompt: str, cwd: str) -> dict | None:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SOLOMON_HOME + r"\src"
    result = subprocess.run(
        [sys.executable, "-m", "solomon.cli", "gateway-evaluate",
         "--request", prompt, "--working-directory", cwd],
        cwd=_SOLOMON_HOME, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    try:
        # Read raw bytes and decode as UTF-8 explicitly rather than
        # json.load(sys.stdin): Python's default stdin text-mode decodes
        # using the OS locale codepage (cp932 on this machine), which
        # mangles Japanese prompts -- same root cause already documented
        # for subprocess.run(text=True) in 08_Discovery/
        # PHASE0_DISCOVERY_REPORT.md ("Windows subprocess text-mode
        # encoding"). Caught by the fail-open except below either way,
        # but this avoids silently no-op'ing on every Japanese prompt.
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        prompt = payload.get("user_prompt") or ""
        cwd = payload.get("cwd") or ""
        if not prompt:
            return 0

        decision = _evaluate(prompt, cwd)
        if not decision or not decision.get("delegate"):
            return 0

        project_id = decision["project_id"]
        signals = ", ".join(decision.get("matched_signals") or [])
        context_lines = [
            "[Solomon v0.4 gateway -- advisory only, not binding]",
            "This request looks like it matches registered project '" + project_id + "'",
            "(delegation signal(s): " + signals + ").",
            "Per this repo's CLAUDE.md routing rule, consider running it via",
            "solomon route-and-run --role coder --project-id " + project_id + " --prompt \"<task>\"",
            "instead of editing directly, unless the user asked to bypass Solomon.",
        ]
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": " ".join(context_lines),
            }
        }
        sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode("utf-8"))
        sys.stdout.buffer.write(chr(10).encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        # Fail open, unconditionally -- see module docstring.
        return 0


if __name__ == "__main__":
    sys.exit(main())
