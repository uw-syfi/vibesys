"""Policy for rendering live operator steering into an agent prompt."""


def splice_steering(user_prompt: str, messages: list[str]) -> str:
    """Append queued operator steering to *user_prompt*."""
    if not messages:
        return user_prompt
    block = "\n".join(f"- {message}" for message in messages)
    return (
        f"{user_prompt.rstrip()}\n\n"
        "## Operator steering (live)\n\n"
        "The operator sent the following instruction(s) for this invocation. "
        "Treat them as high-priority guidance for the work you do now:\n\n"
        f"{block}\n"
    )


__all__ = ["splice_steering"]
