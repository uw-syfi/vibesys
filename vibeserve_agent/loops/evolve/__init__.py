"""Evolutionary search loop.

An LLM-driven evolutionary search over inference-server implementations,
inspired by OpenEvolve and SkyDiscover. The loop maintains a *population*
of candidate workspaces (each a git commit), repeatedly samples parents
weighted by fitness (the profiled headline metric), and asks an LLM
mutator to produce an offspring conditioned on the parent's code, judge
feedback, and a few "inspiration" peers drawn from the wider population.
"""
