"""Prompt templates for an evolve run on the core path, one per `PromptTemplate`.

Each template reads only the fields of its typed `PromptContext` plus the run-level variables
the plan supplies (`objective`, `environment_notes`, `agent_evaluation`). The protocol templates
(reply correction, resume, evaluation tool, environment notes) come from `vibesys.prompts`, and
the profiler, modality and interface texts come from the shared prompt tree.
"""
