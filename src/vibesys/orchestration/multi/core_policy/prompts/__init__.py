"""Prompt templates for a multi run on the core path, one per `PromptTemplate`.

Each template reads only the fields of its typed `PromptContext` plus the run-level variables
the plan supplies (`objective`, `environment_notes`, `validation_recipe_schema`). The planning
template is shared with `single` (`vibesys.serial_search`), the protocol templates (reply
correction, resume, evaluation tool, environment notes) come from `vibesys.prompts`, and the
profiler, modality, interface and notice texts come from the shared prompt tree.
"""
