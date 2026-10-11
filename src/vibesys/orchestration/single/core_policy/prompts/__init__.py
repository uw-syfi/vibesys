"""Prompt templates for a single run on the core path, one per `PromptTemplate`.

Each template reads only the fields of its typed `PromptContext` plus the run-level variables
the plan supplies (`objective`, `environment_notes`). The shared protocol templates (reply
correction, resume, evaluation tool, environment notes) come from `vibesys.prompts`.
"""
