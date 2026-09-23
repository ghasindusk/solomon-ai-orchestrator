"""Minimal CLI entrypoint.

    python -m solomon.cli discover
    python -m solomon.cli run-task --adapter claude_code --prompt "..."
    python -m solomon.cli project-list
    python -m solomon.cli project-detect [PATH]

This is intentionally thin: no router, no context manager, no debate mode.
It proves Task -> Policy -> Adapter -> TaskResult -> State/Log end-to-end.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import uuid

from .dashboard import build_dashboard, render_dashboard
from .debate import run_debate
from .diagnostics import build_diagnostics_report
from .execution import execute_with_fallback
from .eval_suite import run_eval
from .crash_logs import find_crash_reports, parse_crash_report
from .knowledge import build_context_pack, load_global_notes, load_notes_for_project, redact_secrets, search_notes
from .models import Risk, Task, task_status_from_result_status
from .orchestration import build_review_task, needs_review
from .parallel import BatchItem, run_batch
from .worktree import WorktreeManager
from .policy import PolicyEngine
from .registry import ProjectRegistry
from .replay import compare_routers, replay_task
from .risk import classify_risk
from .router import Router
from .state import StateStore
from .telemetry import get_gpu_telemetry
from .token_budget import TokenBudgetManager, hard_stop_approval_reason
from .usage import UsageManager
from .verification import advance_after_result, verify_definition_of_done

PROJECT_ID = "solomon_ai_orchestrator"


def _cli_present(name: str) -> dict:
    path = shutil.which(name)
    return {"name": name, "found": bool(path), "path": path}


def cmd_discover(args: argparse.Namespace) -> int:
    """Phase 0 environment discovery: record what's actually available."""
    findings = {
        "clis": [_cli_present(n) for n in ("claude", "codex", "ollama", "agy", "antigravity-ide")],
    }
    try:
        import urllib.request

        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            findings["ollama_models"] = [m["name"] for m in data.get("models", [])]
    except Exception as exc:  # noqa: BLE001
        findings["ollama_models"] = f"unreachable: {exc}"

    policy = PolicyEngine()
    findings["global_policy_loaded"] = True
    findings["policy_version"] = policy.raw.get("version")

    out_path = Path(__file__).resolve().parents[2] / "08_Discovery" / "phase0_findings.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(findings, ensure_ascii=False, indent=2))
    print(f"\nWritten to {out_path}")
    return 0


def cmd_project_list(args: argparse.Namespace) -> int:
    registry = ProjectRegistry()
    for p in registry.list_projects(include_superseded=args.all):
        print(f"{p.project_id}\t{p.status}\t{p.name}")
        print(f"\trepo_path:      {p.repo_path}")
        print(f"\tknowledge_path: {p.knowledge_path}")
        if p.related_to:
            print(f"\trelated_to:     {p.related_to}")
        if p.see_also:
            print(f"\tsee_also:       {', '.join(p.see_also)}")
    return 0


def cmd_project_detect(args: argparse.Namespace) -> int:
    registry = ProjectRegistry()
    target = args.path or os.getcwd()
    match = registry.detect_from_path(target)
    if match is None:
        print(f"No project matches '{target}'. FR-02: ask the user rather than guessing.")
        return 1
    print(f"{match.project_id}\t{match.name}")
    return 0


def cmd_crash_logs(args: argparse.Namespace) -> int:
    registry = ProjectRegistry()
    project = registry.get(args.project_id)
    if project is None:
        print(f"Unknown project_id: {args.project_id}", file=sys.stderr)
        return 1
    if not project.crash_logs_path:
        print(f"Project '{args.project_id}' has no crash_logs_path registered.")
        return 0

    paths = find_crash_reports(project.crash_logs_path)[: args.limit]
    if not paths:
        print(f"No crash reports found under {project.crash_logs_path}")
        return 0
    for path in paths:
        report = parse_crash_report(path)
        if report is None:
            continue
        print(f"{report.timestamp}\t[{report.flavor}]\t{report.description}")
        print(f"\t{path}")
    return 0


def cmd_context_pack(args: argparse.Namespace) -> int:
    registry = ProjectRegistry()
    project = registry.get(args.project_id)
    if project is None:
        print(f"Unknown project_id: {args.project_id}", file=sys.stderr)
        return 1

    notes = load_notes_for_project(project)
    global_note_count = 0
    if not args.no_global:
        global_notes = load_global_notes()
        global_note_count = len(global_notes)
        notes = notes + global_notes
    results = search_notes(notes, args.query, top_k=args.top_k)
    pack = build_context_pack(results, token_budget=args.budget)

    print(f"Project: {project.name}  ({len(notes)} notes scanned "
          f"[{global_note_count} from global scope], {len(results)} matched)")
    print(f"Context pack: {len(pack.entries)} entries, ~{pack.total_estimated_tokens} tokens "
          f"({pack.token_provenance}), {pack.dropped_count} dropped for budget, "
          f"{pack.redacted_secret_count} secret(s) redacted "
          f"(exact-duplicate notes already excluded by search_notes)")
    for entry in pack.entries:
        print(f"\n--- {entry.title} (score={entry.score:.1f}, ~{entry.estimated_tokens} tok) ---")
        print(entry.path)
        if args.show_snippets:
            print(entry.snippet[:300])
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    store = StateStore()
    manager = UsageManager(store)
    usage = manager.get_project_usage(args.project_id)
    status = manager.check_budget(args.project_id)

    print(f"Project: {args.project_id}")
    print(f"Total cost (all-time): {usage['total_cost_usd']} USD (provenance: {usage['cost_provenance']})")
    print(f"Total tokens (all-time): input={usage['total_input_tokens']} output={usage['total_output_tokens']}")
    print(f"Budget window: {status.window}"
          + (f"  (cost in window: ${status.total_cost_usd})" if status.total_cost_usd is not None else ""))
    print(f"Budget status: {status.level}"
          + (f" ({status.pct_used:.1f}% of ${status.limit_usd})" if status.pct_used is not None else ""))
    if status.actions:
        print(f"Policy actions: {', '.join(status.actions)}")
    print("\nPer-agent:")
    for agent, stats in usage["per_agent"].items():
        print(f"  {agent}: {stats}")
    if usage["local_telemetry"]:
        print("\nLocal (Ollama) telemetry:")
        for entry in usage["local_telemetry"]:
            print(f"  {entry}")
    return 0


def cmd_approvals_list(args: argparse.Namespace) -> int:
    store = StateStore()
    requests = store.list_approval_requests(project_id=args.project_id, status=args.status)
    for req in requests:
        print(f"{req['request_id']}\t{req['status']}\t{req['risk']}\t{req['project_id']}")
        print(f"\treason: {req['reason']}")
    if not requests:
        print("No matching approval requests.")
    return 0


def cmd_approvals_decide(args: argparse.Namespace) -> int:
    store = StateStore()
    ok = store.decide_approval_request(args.request_id, approved=args.approve, note=args.note or "")
    if not ok:
        print(f"Request '{args.request_id}' not found or already decided.", file=sys.stderr)
        return 1
    print(f"Request '{args.request_id}' marked {'approved' if args.approve else 'denied'}.")
    if args.approve:
        print(f"Run: python -m solomon.cli execute-approved {args.request_id}")
    return 0


def cmd_execute_approved(args: argparse.Namespace) -> int:
    store = StateStore()
    req = store.get_approval_request(args.request_id)
    if req is None:
        print(f"No such approval request: {args.request_id}", file=sys.stderr)
        return 1
    if req["status"] != "approved":
        print(f"Request '{args.request_id}' is '{req['status']}', not approved; refusing to execute.", file=sys.stderr)
        return 1

    params = req["task_params"]
    task_kwargs = dict(
        goal_id=params["goal_id"],
        project_id=params["project_id"],
        type=params["type"],
        role=params["role"],
        definition_of_done=params["definition_of_done"],
        risk=Risk(params["risk"]),
    )
    if params.get("task_id"):
        # Preserve the original task_id across the approval boundary so a
        # `review_of:<task_id>` DoD criterion (added at request time by
        # cmd_run_task) still resolves correctly once this executes.
        # Older stored requests (pre this fix) won't have it -- falls
        # back to a fresh id, same as before.
        task_kwargs["task_id"] = params["task_id"]
    task = Task(**task_kwargs)
    store.save_task(task)
    adapter = _load_adapter(params["adapter"])
    health = adapter.health()
    if not health.available:
        print(f"Adapter '{params['adapter']}' unavailable: {health.detail}", file=sys.stderr)
        return 1
    result = adapter.execute(task, params["prompt"], timeout_s=params.get("timeout", 600))
    store.save_result(result)
    task.status = task_status_from_result_status(result.status)
    task.status = advance_after_result(task, store, PolicyEngine())
    store.save_task(task)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status != "FAILED" else 1


def cmd_verify_task(args: argparse.Namespace) -> int:
    """Re-check an already-recorded Task's Definition of Done (FR-09).

    Needed because DoD verification only runs automatically right after
    a Task's own execution; a task blocked on e.g. `review_of:<id>`
    stays at RESULT_RECEIVED until something re-checks it once that
    review actually completes -- this is that re-check, callable on
    demand instead of guessing when to auto-poll.
    """
    store = StateStore()
    data = store.get_task(args.task_id)
    if data is None:
        print(f"No such task: {args.task_id}", file=sys.stderr)
        return 1
    task = Task.from_schema_dict(data)

    verification = verify_definition_of_done(task, store)
    print(f"satisfied: {verification.satisfied}")
    print(f"unsatisfied: {verification.unsatisfied}")
    print(f"unverifiable: {verification.unverifiable}")

    before = task.status
    task.status = advance_after_result(task, store, PolicyEngine())
    store.save_task(task)
    print(f"status: {before.value} -> {task.status.value}")
    return 0


def cmd_review_task(args: argparse.Namespace) -> int:
    store = StateStore()
    router = Router(state=store, usage_manager=UsageManager(store), token_budget_manager=TokenBudgetManager())

    original_data = store.get_task(args.original_task_id)
    if original_data is None:
        print(f"No such task: {args.original_task_id}", file=sys.stderr)
        return 1
    original = Task.from_schema_dict(original_data)

    candidates = router.candidates_for_role("reviewer")
    health_checks = {name: _load_adapter(name).health() for name in candidates}
    review_task, score = build_review_task(
        original, router, implementer_agent=args.implementer_agent, health_checks=health_checks
    )
    if review_task is None:
        print("No reviewer candidate other than the implementer is available; "
              "per policy, do not silently reuse the same adapter.")
        return 1
    store.save_task(review_task)
    print(f"Review task {review_task.task_id} assigned to {review_task.assigned_agent} "
          f"(score={score.total:.3f}), depends on {original.task_id}")

    if not args.execute:
        print("(dry-run only; pass --execute to actually run the review)")
        return 0

    results = store.get_task_results(original.task_id)
    if not results:
        print("Original task has no recorded result to review yet.", file=sys.stderr)
        return 1
    original_summary = results[-1].get("summary", "")
    review_prompt = (
        "You are reviewing another AI agent's output for correctness.\n\n"
        f"Task type: {original.type} (role={original.role})\n"
        f"Output to review:\n{original_summary}\n\n"
        "Reply with OK if this looks correct, or NEEDS_FIX plus a one-line reason if not."
    )
    adapter = _load_adapter(review_task.assigned_agent)
    result = adapter.execute(review_task, review_prompt, timeout_s=args.timeout)
    store.save_result(result)
    review_task.status = task_status_from_result_status(result.status)
    review_task.status = advance_after_result(review_task, store, PolicyEngine())
    store.save_task(review_task)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status != "FAILED" else 1


def cmd_route_and_run(args: argparse.Namespace) -> int:
    store = StateStore()
    router = Router(state=store, usage_manager=UsageManager(store), token_budget_manager=TokenBudgetManager())
    task = Task(
        goal_id=args.goal_id or "adhoc-goal",
        project_id=args.project_id,
        type="adhoc",
        role=args.role,
        definition_of_done=["result_recorded"],
    )
    store.save_task(task)
    outcome = execute_with_fallback(
        task, args.prompt, router, _load_adapter, state=store,
        max_attempts=args.max_attempts, timeout_s=args.timeout,
        gpu_telemetry=get_gpu_telemetry(),
    )
    for attempt in outcome.attempts:
        if attempt.skipped:
            print(f"[skip] {attempt.adapter_name}: unavailable ({attempt.health.detail})")
        else:
            print(f"[try]  {attempt.adapter_name}: {attempt.result.status}")
    if outcome.final_result is None:
        print("No adapter produced a result.", file=sys.stderr)
        return 1
    print(json.dumps(outcome.final_result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if outcome.final_result.status != "FAILED" else 1


_DASHBOARD_ADAPTERS = ["claude_code", "codex", "localai_ollama", "antigravity"]


def cmd_dashboard(args: argparse.Namespace) -> int:
    import time

    store = StateStore()
    registry = ProjectRegistry()

    def snapshot() -> str:
        health_checks = {name: _load_adapter(name).health() for name in _DASHBOARD_ADAPTERS}
        gpu = get_gpu_telemetry()
        data = build_dashboard(store, registry, health_checks, project_id=args.project_id, gpu_telemetry=gpu)
        return render_dashboard(data)

    if args.watch:
        try:
            while True:
                print(snapshot())
                print(f"\n(refreshing every {args.watch}s, Ctrl+C to stop)")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
    else:
        print(snapshot())
    return 0


def cmd_run_batch(args: argparse.Namespace) -> int:
    """Phase 3 parallel-safe execution: run several tasks concurrently.

    Input JSON is a list of {prompt, role, project_id, touches?, timeout?,
    use_worktree?, base_branch?} objects. Items sharing a `touches` path
    are serialized via the advisory lock (one runs, the other is
    reported deferred, never both run concurrently). Items with
    `use_worktree: true` instead get a real isolated git worktree each
    -- all such items in one batch must share the same project, which
    must have a git repo_path registered.
    """
    with open(args.batch_file, "r", encoding="utf-8") as f:
        raw_items = json.load(f)

    store = StateStore()
    router = Router(state=store, usage_manager=UsageManager(store), token_budget_manager=TokenBudgetManager())
    items = [
        BatchItem(
            task=Task(
                goal_id=entry.get("goal_id", "batch-goal"),
                project_id=entry["project_id"],
                type="batch",
                role=entry["role"],
                definition_of_done=["result_recorded"],
            ),
            prompt=entry["prompt"],
            touches=entry.get("touches", []),
            timeout_s=entry.get("timeout", 600),
            use_worktree=entry.get("use_worktree", False),
            base_branch=entry.get("base_branch", "main"),
        )
        for entry in raw_items
    ]

    worktree_manager = None
    worktrees_root = None
    worktree_project_ids = {item.task.project_id for item in items if item.use_worktree}
    if worktree_project_ids:
        if len(worktree_project_ids) > 1:
            print(f"Cannot mix use_worktree items across multiple projects in one batch: "
                  f"{sorted(worktree_project_ids)}", file=sys.stderr)
            return 1
        project = ProjectRegistry().get(next(iter(worktree_project_ids)))
        if project is None or not project.repo_path or project.vcs != "git":
            print(f"Project '{next(iter(worktree_project_ids))}' has no registered git repo_path; "
                  "cannot use worktree isolation.", file=sys.stderr)
            return 1
        worktree_manager = WorktreeManager(project.repo_path)
        worktrees_root = Path(project.repo_path) / ".solomon_worktrees"

    outcomes = run_batch(
        items, router, _load_adapter, store, max_workers=args.max_workers,
        worktree_manager=worktree_manager, worktrees_root=worktrees_root,
        gpu_telemetry=get_gpu_telemetry(),
    )
    exit_code = 0
    for outcome in outcomes:
        if outcome.status == "completed":
            wt_note = f"  (worktree: {outcome.worktree_path})" if outcome.worktree_path else ""
            print(f"[done]     {outcome.task_id}: {outcome.result.agent} -> {outcome.result.status}{wt_note}")
        else:
            print(f"[{outcome.status}] {outcome.task_id}: {outcome.detail}")
            exit_code = 1
    return exit_code


def cmd_worktree_list(args: argparse.Namespace) -> int:
    store = StateStore()
    worktrees = store.list_worktrees(project_id=args.project_id, status=args.status)
    if not worktrees:
        print("No worktrees.")
        return 0
    for wt in worktrees:
        print(f"{wt['task_id']}\t[{wt['status']}]\t{wt['branch']} -> {wt['base_branch']}")
        print(f"\t{wt['path']}")
    return 0


def cmd_worktree_merge(args: argparse.Namespace) -> int:
    """"Merge occurs only after verification" (architecture doc section 7)
    -- refuses unless the Task's own status is COMPLETE (run `verify-task`
    first if it's stuck at REMEDIATION_REQUIRED or RESULT_RECEIVED)."""
    store = StateStore()
    wt = store.get_worktree(args.task_id)
    if wt is None:
        print(f"No worktree record for task {args.task_id}", file=sys.stderr)
        return 1
    if wt["status"] != "active":
        print(f"Worktree for {args.task_id} is '{wt['status']}', not active; refusing.", file=sys.stderr)
        return 1

    task_data = store.get_task(args.task_id)
    task_status = task_data["status"] if task_data else "unknown"
    if task_status != "COMPLETE":
        print(f"Task {args.task_id} is '{task_status}', not COMPLETE; refusing to merge "
              f"(merge only happens after verification -- try `verify-task {args.task_id}` first).",
              file=sys.stderr)
        return 1

    project = ProjectRegistry().get(wt["project_id"])
    if project is None or not project.repo_path:
        print(f"Project '{wt['project_id']}' has no registered repo_path.", file=sys.stderr)
        return 1

    manager = WorktreeManager(project.repo_path)
    result = manager.merge(args.task_id, into_branch=wt["base_branch"])
    if not result.ok:
        print(f"Merge failed: {result.detail}", file=sys.stderr)
        return 1

    store.update_worktree_status(args.task_id, "merged")
    worktrees_root = Path(project.repo_path) / ".solomon_worktrees"
    remove_result = manager.remove(args.task_id, worktrees_root, force=True)
    manager.delete_branch(args.task_id, force=True)
    if not remove_result.ok:
        print(f"Merged, but failed to clean up the worktree directory: {remove_result.detail}")
    print(f"Merged {wt['branch']} into {wt['base_branch']} and cleaned up.")
    return 0


def cmd_worktree_remove(args: argparse.Namespace) -> int:
    store = StateStore()
    wt = store.get_worktree(args.task_id)
    if wt is None:
        print(f"No worktree record for task {args.task_id}", file=sys.stderr)
        return 1
    project = ProjectRegistry().get(wt["project_id"])
    if project is None or not project.repo_path:
        print(f"Project '{wt['project_id']}' has no registered repo_path.", file=sys.stderr)
        return 1

    manager = WorktreeManager(project.repo_path)
    worktrees_root = Path(project.repo_path) / ".solomon_worktrees"
    result = manager.remove(args.task_id, worktrees_root, force=args.force)
    if not result.ok:
        print(f"Failed to remove worktree: {result.detail}", file=sys.stderr)
        return 1
    store.update_worktree_status(args.task_id, "discarded")
    if args.force:
        manager.delete_branch(args.task_id, force=True)
    print(f"Removed worktree for {args.task_id}.")
    return 0


def cmd_debate(args: argparse.Namespace) -> int:
    policy = PolicyEngine()
    participants = {name: _load_adapter(name) for name in args.agents.split(",")}
    judge_adapter = _load_adapter(args.judge)
    task = Task(
        goal_id=args.goal_id or "adhoc-goal",
        project_id=args.project_id,
        type="debate",
        role=args.role,
        definition_of_done=["debate_result"],
    )
    outcome = run_debate(
        task, args.prompt, participants, args.judge, judge_adapter, policy, timeout_s=args.timeout
    )
    if outcome.aborted_reason:
        print(f"Debate aborted: {outcome.aborted_reason}", file=sys.stderr)
        return 1
    for debate_round in outcome.rounds:
        print(f"-- Round {debate_round.round_number} --")
        for name, result in debate_round.responses.items():
            print(f"  {name}: {result.summary[:200]}")
    print(f"\nJudge ({outcome.judge_agent}) final answer:\n{outcome.final_result.summary}")
    return 0


def _render_execution_log_markdown(scope, tasks, events, approvals, task_results_by_id, generated_at):
    """Builds the FR-10/FR-17 execution-log Markdown body. Pure/testable
    (no StateStore access) so redaction behavior can be verified without
    a real database -- see test_cli_export_log.py. task_results_by_id
    maps task_id -> that task's list of result dicts (fetched by the caller,
    since a raw task_id list alone can't answer "what results did this
    task have").

    v0.4 Phase 9 follow-up (D36): task summaries, approval reasons and
    event detail are free text derived from prompts/agent output, so
    each passes through knowledge.redact_secrets() before being written
    -- the same credential-pattern redaction already used for Context
    Firewall snippets (build_context_pack) and Diagnostics Export,
    reused here rather than a third implementation. This is secret
    redaction only, not the omit-by-default posture of
    `diagnostics-export`: export-log's whole purpose is a readable local
    project record, so content stays in place, just with embedded
    credential-shaped substrings stripped.
    """
    lines = [
        f"# Execution Log -- {scope}",
        "",
        f"Exported: {generated_at}",
        f"Tasks: {len(tasks)}  Events: {len(events)}  Approval requests: {len(approvals)}",
        "",
        "## Tasks",
    ]
    for t in tasks:
        results = task_results_by_id.get(t["task_id"], [])
        lines.append(f"\n### {t['task_id']}  [{t['status']}]")
        lines.append(f"role={t['role']}  project={t['project_id']}  risk={t['risk']}")
        for r in results:
            cost = r.get("usage", {}).get("cost_usd")
            cost_str = f"${cost:.4f}" if cost is not None else "n/a"
            summary, _ = redact_secrets(r["summary"][:150])
            lines.append(f"- {r['agent']}: {r['status']}  cost={cost_str}  summary: {summary}")

    lines.append("\n## Approval Requests")
    for a in approvals:
        reason, _ = redact_secrets(a["reason"][:150])
        lines.append(f"- {a['request_id']} [{a['status']}] risk={a['risk']}: {reason}")

    lines.append("\n## Event Log")
    for e in events:
        detail, _ = redact_secrets(e["detail"] or "")
        lines.append(f"- {e['ts']}  {e['event']}  task={e['task_id']}  agent={e['agent']}  detail={detail}")

    return "\n".join(lines)


def cmd_export_log(args: argparse.Namespace) -> int:
    """FR-10/FR-17: write the recorded task/result/event history as a real
    Markdown file into the vault. The SQLite state file is gitignored
    runtime state, not durable project documentation -- this closes that
    gap on demand rather than leaving history trapped in a local DB."""
    from datetime import datetime, timezone

    store = StateStore()
    scope = args.project_id or "all_projects"
    tasks = store.list_tasks(project_id=args.project_id)
    events = store.list_events(project_id=args.project_id)
    approvals = store.list_approval_requests(project_id=args.project_id)
    task_results_by_id = {t["task_id"]: store.get_task_results(t["task_id"]) for t in tasks}

    out_path = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[2] / "09_Logs" / f"{datetime.now().strftime('%Y-%m-%d')}_{scope}_log.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    content = _render_execution_log_markdown(
        scope, tasks, events, approvals, task_results_by_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    out_path.write_text(content, encoding="utf-8")
    print(f"Exported {len(tasks)} tasks, {len(events)} events, {len(approvals)} approvals to {out_path}")
    return 0


def cmd_addons_list(args: argparse.Namespace) -> int:
    """v0.4 migration Phase 5 (safe subset): discover+validate addon
    manifests under --addons-root (default: <repo>/addons/*/
    solomon-addon.yaml). Reports DISCOVERED->VALIDATED/PERMISSION_REVIEW/
    QUARANTINED -- never ENABLED, since there is no addon execution/
    isolation mechanism yet (see DECISIONS.md D22)."""
    from .addon_manager import discover_addons

    records = discover_addons(args.addons_root)
    if not records:
        print("No addon manifests found.")
        return 0
    for record in records:
        name = record.manifest.id if record.manifest else "(unparseable manifest)"
        print(f"{name}  [{record.state.value}]")
        if record.sensitive_permissions:
            print(f"  sensitive permissions requiring consent: {', '.join(record.sensitive_permissions)}")
        for error in record.errors:
            print(f"  error: {error}")
    return 0


def cmd_gateway_evaluate(args: argparse.Namespace) -> int:
    """v0.4 migration Phase 2: pure delegation-decision entry point for an
    external caller (a hook script, eventually) to shell out to. Prints a
    single JSON object on stdout and returns 0 always -- callers (a
    UserPromptSubmit hook has a 30s budget, D17) should fail open, not
    treat a non-zero exit or malformed JSON as anything but "stay local"."""
    from .gateway import GatewayMode, InvocationEnvelope, should_delegate
    from .registry import ProjectRegistry

    envelope = InvocationEnvelope(
        request=args.request,
        caller_type=args.caller_type,
        project_hint=args.project_hint,
        working_directory=args.working_directory,
    )
    registry = ProjectRegistry()
    project_id = envelope.resolve_project_id(registry)
    decision = should_delegate(envelope, GatewayMode(args.mode), project_id=project_id)
    print(json.dumps({
        "invocation_id": envelope.invocation_id,
        "project_id": project_id,
        "delegate": decision.delegate,
        "reason": decision.reason,
        "matched_signals": decision.matched_signals,
    }, ensure_ascii=False))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """v0.4 migration Phase 1: mark in-flight tasks (QUEUED/ASSIGNED/
    RUNNING) as UNKNOWN once they've had no event-log activity for
    --stale-after-minutes, so a crashed process doesn't leave a task
    silently stuck forever. Does not guess COMPLETE/FAILED -- a human
    reviews UNKNOWN tasks and decides whether to retry."""
    store = StateStore()
    reconciled = store.reconcile_stale_tasks(stale_after_minutes=args.stale_after_minutes)
    if not reconciled:
        print("No stale in-flight tasks found.")
        return 0
    print(f"Reconciled {len(reconciled)} stale task(s) to UNKNOWN:")
    for task in reconciled:
        print(f"  {task['task_id']}  project={task['project_id']}  role={task['role']}")
    return 0


def cmd_export_events_json(args: argparse.Namespace) -> int:
    """v0.4 migration Phase 1: JSONL export matching
    07_Schemas/event.schema.json field-for-field (event_id, timestamp,
    event_type, agent_id, payload as an object), alongside (not
    replacing) the existing Markdown export-log path."""
    from datetime import datetime

    store = StateStore()
    scope = args.project_id or "all_projects"
    events = store.list_events_v04(project_id=args.project_id)

    out_path = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[2] / "09_Logs" / f"{datetime.now().strftime('%Y-%m-%d')}_{scope}_events.jsonl"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + chr(10))
    print(f"Exported {len(events)} events (v0.4 schema) to {out_path}")
    return 0


def cmd_diagnostics_export(args: argparse.Namespace) -> int:
    """v0.4 Phase 8 UX (D33): export a redacted-by-default diagnostics
    bundle (Formal Spec v0.4 section 21). --include-sensitive opts into
    prompts/approval reasons/project file paths, still with embedded
    secrets redacted -- never the default."""
    from datetime import datetime

    store = StateStore()
    registry = ProjectRegistry()
    health_checks = {name: _load_adapter(name).health() for name in _DASHBOARD_ADAPTERS}
    report = build_diagnostics_report(
        store, registry, health_checks,
        project_id=args.project_id, gpu_telemetry=get_gpu_telemetry(),
        include_sensitive=args.include_sensitive, event_limit=args.event_limit,
    )

    scope = args.project_id or "all_projects"
    suffix = "-sensitive" if args.include_sensitive else ""
    out_path = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[2] / "09_Logs" /
        f"{datetime.now().strftime('%Y-%m-%d')}_{scope}_diagnostics{suffix}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    mode = "including sensitive data" if args.include_sensitive else "redacted"
    print(f"Diagnostics exported ({mode}) to {out_path}")
    if args.include_sensitive:
        print("WARNING: this export includes prompts, approval reasons, and project file paths. Review before sharing.")
    return 0


def cmd_learning_report(args: argparse.Namespace) -> int:
    """Phase 7: the raw per-role/per-agent performance table driving the
    Router's role_fitness component -- kept inspectable rather than a
    hidden black box."""
    store = StateStore()
    table = store.get_role_performance(project_id=args.project_id)
    if not table:
        print("No recorded task history yet.")
        return 0
    for role in sorted(table):
        print(f"role: {role}")
        for agent, stats in sorted(table[role].items(), key=lambda kv: kv[1]["success_rate"] or 0, reverse=True):
            sr = f"{stats['success_rate']:.0%}" if stats["success_rate"] is not None else "n/a"
            flag = "" if stats["count"] >= 3 else "  (< 3 samples, not yet trusted by the router)"
            print(f"  {agent:16s} count={stats['count']:3d}  success_rate={sr}{flag}")
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    store = StateStore()
    router = Router(state=store, usage_manager=UsageManager(store), token_budget_manager=TokenBudgetManager())

    task = Task(
        goal_id=args.goal_id or "adhoc-goal",
        project_id=args.project_id,
        type="route-only",
        role=args.role,
        definition_of_done=["route_decision"],
        context_budget_tokens=args.context_budget,
    )

    candidates = router.candidates_for_role(args.role)
    if not candidates:
        print(f"No adapters map to role '{args.role}' (see ROLE_CAPABILITY_MAP / agents.example.yaml).")
        return 1

    health_checks = {}
    for name in candidates:
        adapter = _load_adapter(name)
        health_checks[name] = adapter.health()

    scores = router.route(task, health_checks=health_checks, gpu_telemetry=get_gpu_telemetry())
    for score in scores:
        print(f"{score.adapter_name}\ttotal={score.total:.3f}")
        for comp, val in score.components.items():
            print(f"\t{comp}: {val:.2f}")
        for note in score.notes:
            print(f"\tnote: {note}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """v0.4 Phase 7 Reliability & Evaluation (D31): re-score a previously
    stored task right now, without mutating anything, and show whether
    the top choice would differ from what actually happened."""
    store = StateStore()
    router = Router(state=store, usage_manager=UsageManager(store), token_budget_manager=TokenBudgetManager())

    role = _task_role_for_replay(store, args.task_id)
    health_checks = {}
    if role is not None:
        for name in router.candidates_for_role(role):
            health_checks[name] = _load_adapter(name).health()

    try:
        result = replay_task(args.task_id, store, router, health_checks=health_checks, gpu_telemetry=get_gpu_telemetry())
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(f"task_id: {result.task_id}")
    print(f"original agent: {result.original_agent!r}")
    print(f"replayed top choice: {result.replayed_top_choice!r}")
    print(f"decision changed: {result.decision_changed}")
    for score in result.scores:
        print(f"  {score.adapter_name}  total={score.total:.3f}")
    return 0


def _task_role_for_replay(store: StateStore, task_id: str) -> str | None:
    data = store.get_task(task_id)
    return data.get("role") if data else None


def cmd_router_compare(args: argparse.Namespace) -> int:
    """v0.4 Phase 7 (D31): replay every task in a project under the
    current router config and an alternate weights file, reporting where
    their top choices diverge. Read-only -- never mutates state."""
    store = StateStore()
    router_a = Router(state=store, usage_manager=UsageManager(store), token_budget_manager=TokenBudgetManager())
    router_b = Router(
        state=store, usage_manager=UsageManager(store), token_budget_manager=TokenBudgetManager(),
        weights_path=args.weights_path,
    )
    task_ids = [t["task_id"] for t in store.list_tasks(project_id=args.project_id)]
    report = compare_routers(task_ids, store, router_a, router_b)
    print(f"tasks compared: {report.task_count}")
    print(f"changed: {report.changed_count} ({(report.change_rate or 0.0):.0%})")
    for change in report.changes:
        print(f"  {change['task_id']}: original={change['original']!r} a={change['router_a']!r} b={change['router_b']!r}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """v0.4 Phase 7 Reliability & Evaluation (D31): run the existing
    pytest suite grouped into the spec's named categories (routing,
    context_firewall, safety, recovery, addons, completion) and report
    pass/fail per category, rather than one undifferentiated pytest run."""
    report = run_eval()
    for cat in report.categories:
        status = "OK" if cat.ok else "FAIL"
        print(f"[{status}] {cat.category}: {cat.passed} passed, {cat.failed} failed, {cat.errors} errors")
    if report.other_files:
        print(f"(uncategorized: {', '.join(report.other_files)})")
    return 0 if report.all_ok else 1


def cmd_run_task(args: argparse.Namespace) -> int:
    store = StateStore()
    policy = PolicyEngine()

    project_id = args.project_id or PROJECT_ID
    risk = classify_risk("adhoc", args.prompt)
    task = Task(
        goal_id=args.goal_id or "adhoc-goal",
        project_id=project_id,
        type="adhoc",
        role="coder",
        definition_of_done=["result_recorded"],
        risk=risk,
        autonomy=args.autonomy,
    )
    if needs_review(task):
        task.definition_of_done.append(f"review_of:{task.task_id}")
    store.save_task(task)

    approval_reason = None
    if policy.requires_approval(task.risk, autonomy=task.autonomy):
        approval_reason = f"classify_risk() flagged this prompt as {task.risk.value}: {args.prompt[:200]}"
    else:
        # v0.4 Token & Compute Intelligence (Formal Spec v0.4 section 17.8 /
        # DECISIONS.md D27, D30): budget exhaustion must never be used to
        # skip Safety/Verification -- route to Human Approval instead of
        # silently letting the task through or silently blocking it.
        budget_status = TokenBudgetManager().check(
            store.get_usage_records(project_id=project_id), scope="project", scope_id=project_id
        )
        approval_reason = hard_stop_approval_reason(budget_status)

    if approval_reason is not None:
        request_id = f"appr-{uuid.uuid4().hex[:12]}"
        store.create_approval_request(
            request_id=request_id,
            project_id=project_id,
            risk=task.risk.value,
            reason=approval_reason,
            task_params={
                "task_id": task.task_id,
                "goal_id": task.goal_id,
                "project_id": project_id,
                "type": task.type,
                "role": task.role,
                "definition_of_done": task.definition_of_done,
                "risk": task.risk.value,
                "adapter": args.adapter,
                "prompt": args.prompt,
                "timeout": args.timeout,
            },
        )
        print(f"Task requires human approval (risk={task.risk.value}). Request created: {request_id}")
        print(f"Reason:  {approval_reason}")
        print(f"Decide:  python -m solomon.cli approvals decide {request_id} --approve|--deny")
        print(f"Then:    python -m solomon.cli execute-approved {request_id}")
        return 2

    adapter = _load_adapter(args.adapter)
    health = adapter.health()
    if not health.available:
        print(f"Adapter '{args.adapter}' unavailable: {health.detail}", file=sys.stderr)
        return 1

    result = adapter.execute(task, args.prompt, timeout_s=args.timeout)
    store.save_result(result)
    task.status = task_status_from_result_status(result.status)
    task.status = advance_after_result(task, store, policy)
    store.save_task(task)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status != "FAILED" else 1


def _load_adapter(name: str, cwd: str | None = None):
    """cwd is honored by the three CLI-based adapters (used for worktree
    isolation, parallel.py); localai_ollama is HTTP-based and has no
    filesystem cwd concept, so it's silently ignored there."""
    if name == "claude_code":
        from .adapters.claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter(cwd=cwd)
    if name == "codex":
        from .adapters.codex_adapter import CodexAdapter

        return CodexAdapter(cwd=cwd)
    if name == "localai_ollama":
        from .adapters.ollama_adapter import OllamaAdapter

        return OllamaAdapter()
    if name == "antigravity":
        from .adapters.antigravity_adapter import AntigravityAdapter

        return AntigravityAdapter(cwd=cwd)
    raise ValueError(f"Unknown adapter: {name}")


def main(argv: list[str] | None = None) -> int:
    # Windows consoles often default to a legacy codepage (e.g. cp932) that
    # can't encode arbitrary Unicode (Japanese vault content, em dashes,
    # etc.); force UTF-8 on stdout/stderr rather than crashing on print().
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="solomon")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Phase 0 environment discovery")
    p_discover.set_defaults(func=cmd_discover)

    p_run = sub.add_parser("run-task", help="Run a single ad-hoc task through an adapter")
    p_run.add_argument(
        "--adapter",
        required=True,
        choices=["claude_code", "codex", "localai_ollama", "antigravity"],
    )
    p_run.add_argument("--prompt", required=True)
    p_run.add_argument("--goal-id", dest="goal_id", default=None)
    p_run.add_argument("--project-id", dest="project_id", default=None)
    p_run.add_argument("--timeout", type=int, default=600)
    p_run.add_argument(
        "--autonomy", type=int, default=None, choices=range(0, 6), metavar="0-5",
        help="v0.4 autonomy dial: lower values add caution below the base policy floor (never below it)",
    )
    p_run.set_defaults(func=cmd_run_task)

    p_proj_list = sub.add_parser("project-list", help="List registered projects")
    p_proj_list.add_argument("--all", action="store_true", help="Include superseded projects")
    p_proj_list.set_defaults(func=cmd_project_list)

    p_proj_detect = sub.add_parser("project-detect", help="Detect project from a filesystem path")
    p_proj_detect.add_argument("path", nargs="?", default=None, help="Defaults to cwd")
    p_proj_detect.set_defaults(func=cmd_project_detect)

    p_ctx = sub.add_parser("context-pack", help="Build a token-budgeted context pack from a project's notes")
    p_ctx.add_argument("--project-id", dest="project_id", required=True)
    p_ctx.add_argument("--query", required=True)
    p_ctx.add_argument("--top-k", dest="top_k", type=int, default=5)
    p_ctx.add_argument("--budget", type=int, default=4000)
    p_ctx.add_argument("--show-snippets", dest="show_snippets", action="store_true")
    p_ctx.add_argument("--no-global", dest="no_global", action="store_true",
                        help="Exclude the global knowledge scope (GLOBAL_POLICY.yaml knowledge.global_knowledge_paths)")
    p_ctx.set_defaults(func=cmd_context_pack)

    p_crash = sub.add_parser("crash-logs", help="List a project's registered crash reports (e.g. a Minecraft instance)")
    p_crash.add_argument("--project-id", dest="project_id", required=True)
    p_crash.add_argument("--limit", type=int, default=20)
    p_crash.set_defaults(func=cmd_crash_logs)

    p_route = sub.add_parser("route", help="Score candidate adapters for a role (dry-run, no execution)")
    p_route.add_argument("--role", required=True)
    p_route.add_argument("--project-id", dest="project_id", required=True)
    p_route.add_argument("--goal-id", dest="goal_id", default=None)
    p_route.add_argument("--context-budget", dest="context_budget", type=int, default=None)
    p_route.set_defaults(func=cmd_route)

    p_replay = sub.add_parser("replay", help="Re-score a stored task now (no mutation); compare to the original agent")
    p_replay.add_argument("--task-id", dest="task_id", required=True)
    p_replay.set_defaults(func=cmd_replay)

    p_rcompare = sub.add_parser("router-compare", help="Replay every task in a project under an alternate router weights file")
    p_rcompare.add_argument("--project-id", dest="project_id", required=True)
    p_rcompare.add_argument("--weights-path", dest="weights_path", required=True)
    p_rcompare.set_defaults(func=cmd_router_compare)

    p_eval = sub.add_parser("eval", help="Run the test suite grouped by category (routing/context_firewall/safety/recovery/addons/completion)")
    p_eval.set_defaults(func=cmd_eval)

    p_usage = sub.add_parser("usage", help="Show recorded usage/cost and budget status for a project")
    p_usage.add_argument("--project-id", dest="project_id", required=True)
    p_usage.set_defaults(func=cmd_usage)

    p_appr = sub.add_parser("approvals", help="Manage pending approval requests (FR-16)")
    appr_sub = p_appr.add_subparsers(dest="approvals_command", required=True)

    p_appr_list = appr_sub.add_parser("list", help="List approval requests")
    p_appr_list.add_argument("--project-id", dest="project_id", default=None)
    p_appr_list.add_argument("--status", default=None, choices=["pending", "approved", "denied"])
    p_appr_list.set_defaults(func=cmd_approvals_list)

    p_appr_decide = appr_sub.add_parser("decide", help="Approve or deny a pending request")
    p_appr_decide.add_argument("request_id")
    group = p_appr_decide.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", dest="approve", action="store_true")
    group.add_argument("--deny", dest="approve", action="store_false")
    p_appr_decide.add_argument("--note", default=None)
    p_appr_decide.set_defaults(func=cmd_approvals_decide)

    p_exec = sub.add_parser("execute-approved", help="Execute a Task whose approval request was approved")
    p_exec.add_argument("request_id")
    p_exec.set_defaults(func=cmd_execute_approved)

    p_review = sub.add_parser("review-task", help="Create (and optionally run) a review Task for an implementer Task (FR-08)")
    p_review.add_argument("--original-task-id", dest="original_task_id", required=True)
    p_review.add_argument("--implementer-agent", dest="implementer_agent", required=True)
    p_review.add_argument("--execute", action="store_true", help="Actually run the review, not just create/route it")
    p_review.add_argument("--timeout", type=int, default=600)
    p_review.set_defaults(func=cmd_review_task)

    p_rar = sub.add_parser("route-and-run", help="Route a task to the best adapter and execute, with retry/fallback")
    p_rar.add_argument("--role", required=True)
    p_rar.add_argument("--project-id", dest="project_id", required=True)
    p_rar.add_argument("--prompt", required=True)
    p_rar.add_argument("--goal-id", dest="goal_id", default=None)
    p_rar.add_argument("--max-attempts", dest="max_attempts", type=int, default=2)
    p_rar.add_argument("--timeout", type=int, default=600)
    p_rar.set_defaults(func=cmd_route_and_run)

    p_debate = sub.add_parser("debate", help="Bounded multi-agent debate (FR-18, opt-in only)")
    p_debate.add_argument("--role", required=True)
    p_debate.add_argument("--project-id", dest="project_id", required=True)
    p_debate.add_argument("--prompt", required=True)
    p_debate.add_argument("--agents", required=True, help="Comma-separated adapter names")
    p_debate.add_argument("--judge", required=True)
    p_debate.add_argument("--goal-id", dest="goal_id", default=None)
    p_debate.add_argument("--timeout", type=int, default=600)
    p_debate.set_defaults(func=cmd_debate)

    p_batch = sub.add_parser("run-batch", help="Execute several tasks concurrently (Phase 3 parallel-safe execution)")
    p_batch.add_argument("batch_file", help="JSON file: list of {prompt, role, project_id, touches?, timeout?, use_worktree?, base_branch?}")
    p_batch.add_argument("--max-workers", dest="max_workers", type=int, default=4)
    p_batch.set_defaults(func=cmd_run_batch)

    p_wt = sub.add_parser("worktree", help="Manage git worktree task isolation (Phase 3)")
    wt_sub = p_wt.add_subparsers(dest="worktree_command", required=True)

    p_wt_list = wt_sub.add_parser("list", help="List worktree records")
    p_wt_list.add_argument("--project-id", dest="project_id", default=None)
    p_wt_list.add_argument("--status", default=None, choices=["active", "merged", "discarded"])
    p_wt_list.set_defaults(func=cmd_worktree_list)

    p_wt_merge = wt_sub.add_parser("merge", help="Merge a COMPLETE task's worktree branch back and clean up")
    p_wt_merge.add_argument("task_id")
    p_wt_merge.set_defaults(func=cmd_worktree_merge)

    p_wt_remove = wt_sub.add_parser("remove", help="Discard a worktree without merging")
    p_wt_remove.add_argument("task_id")
    p_wt_remove.add_argument("--force", action="store_true")
    p_wt_remove.set_defaults(func=cmd_worktree_remove)

    p_dash = sub.add_parser("dashboard", help="Project progress, agent status, task queue, usage, approvals (Phase 6)")
    p_dash.add_argument("--project-id", dest="project_id", default=None, help="Defaults to all registered projects")
    p_dash.add_argument("--watch", type=int, default=None, metavar="SECONDS", help="Refresh on a loop")
    p_dash.set_defaults(func=cmd_dashboard)

    p_learn = sub.add_parser("learning-report", help="Per-role/per-agent performance table (Phase 7 Learning Router)")
    p_learn.add_argument("--project-id", dest="project_id", default=None, help="Defaults to all projects")
    p_learn.set_defaults(func=cmd_learning_report)

    p_diag = sub.add_parser("diagnostics-export", help="Export a redacted-by-default diagnostics bundle (Formal Spec v0.4 section 21)")
    p_diag.add_argument("--project-id", dest="project_id", default=None)
    p_diag.add_argument("--include-sensitive", dest="include_sensitive", action="store_true",
                         help="Include prompts/approval reasons/project file paths (still with embedded secrets redacted). Off by default.")
    p_diag.add_argument("--event-limit", dest="event_limit", type=int, default=50)
    p_diag.add_argument("--out", default=None)
    p_diag.set_defaults(func=cmd_diagnostics_export)

    p_export = sub.add_parser("export-log", help="Export recorded task/event history as Markdown into the vault (FR-10/FR-17)")
    p_export.add_argument("--project-id", dest="project_id", default=None, help="Defaults to all projects")
    p_export.add_argument("--out", default=None, help="Output path (defaults to 09_Logs/<date>_<scope>_log.md)")
    p_export.set_defaults(func=cmd_export_log)

    p_export_events = sub.add_parser(
        "export-events-json",
        help="Export the event log as JSONL matching v0.4's event.schema.json (07_Schemas)",
    )
    p_export_events.add_argument("--project-id", dest="project_id", default=None, help="Defaults to all projects")
    p_export_events.add_argument("--out", default=None, help="Output path (defaults to 09_Logs/<date>_<scope>_events.jsonl)")
    p_export_events.set_defaults(func=cmd_export_events_json)

    p_reconcile = sub.add_parser(
        "reconcile",
        help="Mark stale in-flight tasks (no activity for N minutes) as UNKNOWN (v0.4 Recovery)",
    )
    p_reconcile.add_argument(
        "--stale-after-minutes", dest="stale_after_minutes", type=int, default=30,
        help="Age threshold in minutes since last event-log activity (default: 30)",
    )
    p_reconcile.set_defaults(func=cmd_reconcile)

    p_gateway_eval = sub.add_parser(
        "gateway-evaluate",
        help="v0.4 Phase 2: evaluate whether a request should delegate to Solomon (advisory only, not yet wired into any hook)",
    )
    p_gateway_eval.add_argument("--request", required=True, help="The user's raw request text")
    p_gateway_eval.add_argument("--caller-type", dest="caller_type", default="claude_code")
    p_gateway_eval.add_argument("--project-hint", dest="project_hint", default=None)
    p_gateway_eval.add_argument("--working-directory", dest="working_directory", default=None)
    p_gateway_eval.add_argument(
        "--mode", default="AUTO", choices=["AUTO", "FORCE_LOCAL", "FORCE_SOLOMON", "SHADOW"],
    )
    p_gateway_eval.set_defaults(func=cmd_gateway_evaluate)

    p_addons_list = sub.add_parser(
        "addons-list",
        help="v0.4 Phase 5: discover and validate addon manifests (DISCOVERED/VALIDATED/PERMISSION_REVIEW/QUARANTINED only, no execution)",
    )
    p_addons_list.add_argument("--addons-root", dest="addons_root", default=None)
    p_addons_list.set_defaults(func=cmd_addons_list)

    p_verify = sub.add_parser("verify-task", help="Re-check a Task's Definition of Done and advance its status (FR-09)")
    p_verify.add_argument("task_id")
    p_verify.set_defaults(func=cmd_verify_task)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
