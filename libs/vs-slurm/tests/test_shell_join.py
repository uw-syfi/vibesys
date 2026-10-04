"""The job script's command lines pass every argv byte through and expand only the port."""

from __future__ import annotations

import shlex
import shutil
import subprocess

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_slurm.api import PORT_PLACEHOLDER, shell_join_with_port

_PORT = "20047"
# Shell-significant text an evaluator command really carries: the trusted
# benchmark wrapper is a `bash -c` script with `$?`, `"$status"`, and quotes.
_SHELL_TEXT = st.sampled_from(['"', "'", "$", "$?", '"$status"', "`", "\\", "!", ";", " ", "\n"])
_PIECE = st.one_of(
    st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00")),
    _SHELL_TEXT,
    st.just(PORT_PLACEHOLDER),
)
_ARGUMENT = st.lists(_PIECE, max_size=6).map("".join)


def _run(line: str) -> tuple[int, str, str]:
    bash = shutil.which("bash")
    assert bash is not None
    # Bytes, not text mode: text mode would turn a generated "\r" into "\n".
    result = subprocess.run(  # noqa: S603  # lint-waiver: LW-951201 [S603]; runs generated text through bash on purpose, since what bash makes of the joined line is the property under test.
        # > Inspecting the joined string instead would re-implement shell quoting
        # > in the test; shell=True would add a second, untested shell layer.
        [bash, "-c", f"PORT={_PORT}; {line}"],
        capture_output=True,
        check=False,
    )
    return (
        result.returncode,
        result.stdout.decode("utf-8", "surrogateescape"),
        result.stderr.decode("utf-8", "replace"),
    )


@settings(max_examples=60, deadline=None)
@given(st.lists(_ARGUMENT, min_size=1, max_size=4))
def test_every_argument_reaches_the_command_with_only_the_port_expanded(
    arguments: list[str],
) -> None:
    status, stdout, stderr = _run("printf '%s\\0' " + shell_join_with_port(arguments))

    assert status == 0, stderr
    assert stdout.split("\0")[:-1] == [
        argument.replace(PORT_PLACEHOLDER, _PORT) for argument in arguments
    ]


@settings(max_examples=20, deadline=None)
@given(status=st.integers(min_value=0, max_value=125))
def test_a_wrapped_script_with_a_port_argument_keeps_its_own_exit_status(status: int) -> None:
    """Regression for r19: a failed trusted benchmark exited 0 on Slurm.

    Its `bash -c` wrapper saves `status=$?` and ends with `(exit "$status")`.
    Because its benchmark arguments carried the port placeholder, the job
    script spliced the whole wrapper into double quotes, so the job's own shell
    expanded `$?` and `$status` before the wrapper ran.
    """
    script = (
        f"sh -c {shlex.quote(f'exit {status}')} --base-url"
        f" http://127.0.0.1:{PORT_PLACEHOLDER}/v1; status=$?;"
        ' printf \'%s\\n\' "$status"; (exit "$status")'
    )

    exit_status, stdout, _ = _run(shell_join_with_port(("bash", "-c", script)))

    assert (exit_status, stdout) == (status, f"{status}\n")
