"""LLM serving: the candidate is an OpenAI-compatible server the task reaches over HTTP.

``endpoint()`` is its URL (``VIBESYS_SERVICE_URL``, which vibesys-cli sets).
``agentx.replay`` measures it with the pinned AgentX harness;
``golden.logprobs`` checks its numerics against golden reference outputs.
Build the task image ``FROM vibesys-evaluator-llm-serving``, which installs
the harness this package pins.
"""

from vibesys_evaluator.llm_serving._endpoint import endpoint

__all__ = ["endpoint"]
