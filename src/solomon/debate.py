"""Bounded Debate Mode -- lean Phase 5 slice (FR-18 / architecture doc
section 9, GLOBAL_POLICY.yaml `communication.debate`).

Disabled by default (spec section 5: DEBATE is exceptional, opt-in).
When run: N adapters each answer independently in round 1; from round 2
on, each adapter sees the previous round's pooled answers (labeled by
adapter, not attributed to "you"/"opponent") and may revise; after the
configured round limit, a judge adapter picks/synthesizes a final
answer from the pooled last-round answers. No unbounded conversational
loop -- rounds and participant count are hard-capped by policy config,
never by the agents themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapters.base import AgentAdapter
from .models import Task
from .policy import PolicyEngine
from .result import TaskResult


@dataclass
class DebateRound:
    round_number: int
    responses: dict[str, TaskResult] = field(default_factory=dict)


@dataclass
class DebateOutcome:
    rounds: list[DebateRound] = field(default_factory=list)
    judge_agent: str | None = None
    final_result: TaskResult | None = None
    aborted_reason: str | None = None


def run_debate(
    task: Task,
    prompt: str,
    participants: dict[str, AgentAdapter],
    judge: str,
    judge_adapter: AgentAdapter,
    policy: PolicyEngine,
    timeout_s: int = 600,
) -> DebateOutcome:
    debate_cfg = (policy.raw.get("communication") or {}).get("debate", {})
    if not debate_cfg.get("enabled", False):
        return DebateOutcome(aborted_reason="debate mode disabled by policy")

    max_agents = debate_cfg.get("max_agents", 3)
    max_rounds = debate_cfg.get("max_rounds", 2)
    if len(participants) > max_agents:
        return DebateOutcome(
            aborted_reason=f"{len(participants)} participants exceeds policy max_agents={max_agents}"
        )
    if len(participants) < 2:
        return DebateOutcome(aborted_reason="debate requires at least 2 participants")

    outcome = DebateOutcome()
    pooled_context = ""
    for round_number in range(1, max_rounds + 1):
        round_prompt = prompt if round_number == 1 else (
            f"{prompt}\n\n--- Other agents' answers so far ---\n{pooled_context}\n"
            "Revise your answer if you now believe it's wrong; otherwise restate it."
        )
        debate_round = DebateRound(round_number=round_number)
        for name, adapter in participants.items():
            result = adapter.execute(task, round_prompt, timeout_s=timeout_s)
            debate_round.responses[name] = result
        outcome.rounds.append(debate_round)
        pooled_context = "\n\n".join(
            f"[{name}]: {r.summary}" for name, r in debate_round.responses.items()
        )

    judge_prompt = (
        f"{prompt}\n\nMultiple agents proposed these answers after debate:\n\n{pooled_context}\n\n"
        "Pick the best answer, or synthesize the strongest correct answer from them. "
        "Respond with only the final answer."
    )
    outcome.judge_agent = judge
    outcome.final_result = judge_adapter.execute(task, judge_prompt, timeout_s=timeout_s)
    return outcome
