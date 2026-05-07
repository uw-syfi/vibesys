"""Autonomous orchestrator-driven inference-server build loop.

The :func:`run_agent_loop` entry point replaces the step-by-step
curriculum loop: an *orchestrator* agent decides per-round what task the
implementer should tackle, judged by pass criteria the orchestrator writes
and optionally informed by a profiling pass it requests.
"""
