"""Prompt templates for a dynamic run on the core path, one per `PromptTemplate`.

Each template reads only the fields of its typed `PromptContext` plus the run-level
variables the plan supplies (`objective`). Replies are typed, so no template mentions
agent tools.
"""
