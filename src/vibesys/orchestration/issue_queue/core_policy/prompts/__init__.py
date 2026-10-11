"""Prompt templates of the issue queue on the core path, one per `PromptTemplate`.

Each template reads only the fields of its typed `PromptContext` plus the run-level variables
the plan supplies (`facts`, `agent_evaluation`). Replies are typed, so no template mentions the
issue board's own tools: an agent files an issue by naming it in its reply.
"""
