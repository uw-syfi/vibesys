"""Prompt-template owner for the zero-agent input-check orchestration.

The strategy never renders an agent prompt, so this package intentionally owns
no templates. Its path still declares the strategy's package-local ownership at
the same public interface as role-bearing orchestrations.
"""
