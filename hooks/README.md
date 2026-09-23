# Solomon v0.4 Gateway hooks (Phase 2)

Registering this hook is opt-in, not automatic on install, because
Claude Code hooks fire on every Claude Code session on the machine,
not just Solomon-related work, so wiring one in is a deliberate choice
the user makes explicitly per machine (see "To activate" below) -- it is
not bundled with writing the code.

## `solomon_user_prompt_submit.py`

Implements the "advisory-only" delegation mode identified during Phase 0
environment discovery: a
`UserPromptSubmit` hook cannot block or reroute a prompt, only add
`additionalContext`. This script:

1. Reads the hook's JSON stdin (`user_prompt`, `cwd`, `session_id`).
2. Shells out to `solomon gateway-evaluate` (8s timeout, well under the
   hook's 30s budget) to get a delegation decision from
   `src/solomon/gateway.py`'s keyword heuristic.
3. If it would delegate, prints `hookSpecificOutput.additionalContext`
   suggesting the `solomon route-and-run` command CLAUDE.md's own routing
   rule already asks for manually -- this just automates noticing it.
4. On any error, unmatched project, or non-delegate decision: silent
   no-op, exit 0. Never blocks, never raises to the caller.

**Known limitation of this heuristic** (see `gateway.py`'s module
comment): keyword matching on the prompt text, not a real classifier. It
will both under- and over-trigger; treat it as a first pass, not a
finished router.

### To activate

Add to `~/.claude/settings.json` (machine-wide, every project) or a
project's own `.claude/settings.json` (scoped to that project only):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"<path-to-your-clone>\\hooks\\solomon_user_prompt_submit.py\""
          }
        ]
      }
    ]
  }
}
```

Project-scoped is the lower-blast-radius choice if you want to try it
before going machine-wide -- put it in that project's own
`.claude/settings.json` instead of `~/.claude/settings.json`.

### Testing it yourself before activating

Piping Japanese JSON through `echo`/`printf` in Git Bash on this machine
was unreliable during development (stdin encoding got mangled somewhere
in the Git Bash pipe, not in the script itself -- confirmed by feeding
the exact same bytes to the script's `main()` directly in Python, which
worked correctly both times). If you want to smoke-test this script
yourself, prefer a real file redirect over a shell pipe:

```
python -c "import json; open('input.json','wb').write(json.dumps({'user_prompt': '実装してからレビューもお願いします', 'cwd': r'<path-to-your-clone>', 'session_id': 'test'}, ensure_ascii=False).encode('utf-8'))"
python hooks\solomon_user_prompt_submit.py < input.json
```
