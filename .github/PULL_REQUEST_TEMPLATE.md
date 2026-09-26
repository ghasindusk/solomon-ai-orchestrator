## What does this change?

## Why?

## Safety/policy impact
Does this touch approval gates, the autonomy dial, Token Budget
enforcement, project isolation, or diagnostics redaction? If so, explain
how the existing safety floor is preserved (see CONTRIBUTING.md's
contribution boundary).

## Testing
- [ ] `python -m pytest -q` passes locally
- [ ] Added/updated tests for the new behavior
- [ ] If this touches routing, ran `solomon route`/`solomon replay`
      against real data to sanity-check the change

## Checklist
- [ ] No personal paths, secrets, or private project data introduced
      (see the `.example.yaml` pattern for anything config-shaped)
- [ ] `DECISIONS.md` updated if this is a non-obvious design choice
