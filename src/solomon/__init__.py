"""Solomon AI Orchestrator (SAO) - lean Phase 1 core.

This package intentionally implements only the minimal slice of the v0.3
formal spec needed to prove the orchestration loop end-to-end:
Task model -> Policy gate -> Adapter execution -> Structured Result -> State/Log.

Router scoring, context/knowledge management, debate mode and the
TUI/dashboard are out of scope until their respective roadmap phases.
"""

__version__ = "0.1.0"
