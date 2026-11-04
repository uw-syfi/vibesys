"""Contract every ``ValidationPasses`` implementation must pass.

A pass store answers one trust question: did the runtime itself observe this exact recipe
pass over exactly these inputs? So it must return a recorded pass only for the same recipe
and digest, never for a near miss, and must refuse to record anything that is not a pass
the runtime ran. A durable store must still answer after it is reopened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime._local_validation import FrameworkValidationResult, ValidationRecipe

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_runtime._validation_passes import ValidationPasses


def _recipe(name: str = "unit", command: str = "pytest -q") -> ValidationRecipe:
    return ValidationRecipe(
        name=name,
        command=command,
        input_paths=("src",),
        timeout_seconds=30,
        purpose="exercise the contract",
    )


def _result(
    recipe: ValidationRecipe, digest: str, *, passed: bool = True, reused: bool = False
) -> FrameworkValidationResult:
    return FrameworkValidationResult(
        recipe=recipe, input_digest=digest, passed=passed, reused=reused, exit_code=0
    )


def _refuses(store: ValidationPasses, result: FrameworkValidationResult) -> bool:
    try:
        store.record(result)
    except ValueError:
        return True
    return False


def check_validation_passes(open_store: Callable[[], ValidationPasses], *, durable: bool) -> None:
    """Exercise one implementation; ``open_store`` reopens the same underlying store."""
    store = open_store()
    recipe = _recipe()
    assert store.find(recipe, "a" * 64) is None

    recorded = _result(recipe, "a" * 64)
    store.record(recorded)
    assert store.find(recipe, "a" * 64) == recorded
    assert store.find(recipe, "b" * 64) is None
    assert store.find(_recipe(command="pytest -q -x"), "a" * 64) is None
    assert store.find(_recipe(name="other"), "a" * 64) is None

    for refused in (
        _result(recipe, "c" * 64, passed=False),
        _result(recipe, "c" * 64, reused=True),
        _result(recipe, ""),
    ):
        assert _refuses(store, refused), (
            f"recorded a result that is not an observed pass: {refused}"
        )
        assert store.find(refused.recipe, refused.input_digest) is None

    if durable:
        assert open_store().find(recipe, "a" * 64) == recorded


__all__ = ["check_validation_passes"]
