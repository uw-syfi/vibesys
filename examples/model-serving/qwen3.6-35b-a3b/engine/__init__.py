"""Baseline OpenAI-compatible server for Qwen3.6-35B-A3B.

Simple and correct, not fast: Transformers runs the BF16 checkpoint sharded over
every visible GPU, one request at a time, with a small snapshot prefix cache.
Launch it from the project root with ``python -m engine.server``.
"""
