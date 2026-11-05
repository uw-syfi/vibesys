"""The sample lifecycle every domain shares: seed, artifacts, records, errors, signals.

A sample runs in one of three modes (``docs/spec.md``, "The sample"):

- ``benchmark``: ``hello`` first, then exactly one outcome on ``--vs-output``:
  the result the task reports, or an error record naming what failed.
- ``check``: no records; the exit status is the verdict (0 passes).
- ``profile``: the standard load for a profiler; never scored, so nothing may
  be reported; its files go to the artifacts directory, which vibesys-cli
  copies back.

A domain supplies the body (deploy the system under test if it owns one, run
the task's function on a ``Run``); this module owns everything around it.
The first SIGTERM, SIGINT or SIGHUP ends the sample as an error and later ones
are ignored, so whatever the body opened is closed on the way out.
"""

from __future__ import annotations

import json
import re
import secrets
import signal
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys_evaluator._private.records import RecordError, RecordStream

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import FrameType

    from vibesys_evaluator._private.records import Partial
    from vibesys_evaluator._private.results import Metric

#: The sample seed vibesys may pass; drawn fresh (``secrets``) when unset.
SAMPLE_SEED_ENV = "VIBESYS_SAMPLE_SEED"
#: The directory vibesys keeps with the job.
ARTIFACTS_ENV = "VIBESYS_ARTIFACTS_DIR"
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_MAX_SEED = (1 << 64) - 1
_PHASE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class Mode(StrEnum):
    """What one invocation of the task script does."""

    CHECK = "check"
    BENCHMARK = "benchmark"
    PROFILE = "profile"


def log(message: str) -> None:
    """One progress line on standard error."""
    print(f"vibesys-evaluator: {message}", file=sys.stderr, flush=True)


class Stopped(BaseException):
    """A signal asked the process to stop."""

    def __init__(self, signum: int) -> None:
        """Record which signal."""
        super().__init__(signum)
        self.signum = signum


class SampleError(Exception):
    """The sample is not a measurement; the message is what the error record says.

    Domains raise subclasses for the failures they expect (a deploy step, a
    phase), so the record names what failed without a traceback.
    """

    def __init__(self, message: str, *, partial: Partial | None = None) -> None:
        """Carry the message and what was measured before the failure, if anything."""
        super().__init__(message)
        self.partial = partial


def _stop(signum: int, _frame: FrameType | None) -> None:
    # The first signal stops the work; later ones are ignored so that cleanup completes.
    for sig in SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    raise Stopped(signum)


def with_signals(command: Callable[[], int]) -> int:
    """Run ``command`` with the stop signals turned into ``Stopped``; 128 + signal if one came."""
    for sig in SIGNALS:
        signal.signal(sig, _stop)
    try:
        return command()
    except Stopped as stop:
        return 128 + stop.signum


@dataclass(kw_only=True)
class Sample:
    """One sample as a domain's body sees it.

    Attributes:
        mode: What this invocation does.
        seed: The sample seed (``seeds.json`` in the artifacts records it).
        artifacts: An existing directory kept with the job when vibesys gives one.
        records: The record stream (benchmark mode only).
        partial: What was last measured, for an error record; the domain updates it.
    """

    mode: Mode
    seed: int
    artifacts: Path
    records: RecordStream | None = None
    partial: Partial | None = None
    _phases: set[str] = field(default_factory=set)

    def phase_dir(self, name: str) -> Path:
        """A new, empty directory for one phase's files: ``<artifacts>/<name>``.

        Raises:
            ValueError: ``name`` is not a lowercase file name or was used before.
        """
        if not isinstance(name, str) or not _PHASE_NAME.match(name):
            msg = (
                f"phase name {name!r} must be 1 to 63 of a-z, 0-9, '.', '_' and '-', "
                "starting with a letter or digit"
            )
            raise ValueError(msg)
        if name in self._phases:
            msg = f"phase name {name!r} is used twice in this sample; give each phase its own"
            raise ValueError(msg)
        self._phases.add(name)
        path = self.artifacts / name
        path.mkdir(parents=True, exist_ok=False)
        return path

    def report(self, values: Mapping[str, float]) -> None:
        """Write the result record (benchmark mode, once).

        Raises:
            ValueError: Not the benchmark mode, or a record the protocol refuses.
        """
        if self.records is None:
            msg = f"run.report: the {self.mode} mode is never scored; report only in benchmark"
            raise ValueError(msg)
        try:
            self.records.result(values)
        except RecordError as error:
            msg = f"run.report: {error}"
            raise ValueError(msg) from error


def sample_seed(flag: int | None, env: Mapping[str, str]) -> tuple[int, str]:
    """The sample seed and where it came from: ``--seed``, the environment, or drawn."""
    if flag is not None:
        if not 0 <= flag <= _MAX_SEED:
            msg = f"--seed must be in [0, 2**64), got {flag}"
            raise ValueError(msg)
        return flag, "--seed"
    value = env.get(SAMPLE_SEED_ENV, "").strip()
    if value:
        try:
            seed = int(value, 0)
        except ValueError:
            seed = -1
        if not 0 <= seed <= _MAX_SEED:
            msg = f"${SAMPLE_SEED_ENV}={value!r} is not an integer in [0, 2**64)"
            raise ValueError(msg)
        return seed, SAMPLE_SEED_ENV
    return secrets.randbits(63), "drawn"


def _artifacts(env: Mapping[str, str], scratch: Path) -> Path:
    value = env.get(ARTIFACTS_ENV, "")
    path = Path(value) if value else scratch / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _new_sample(
    mode: Mode, *, seed_flag: int | None, env: Mapping[str, str], scratch: Path
) -> Sample:
    seed, source = sample_seed(seed_flag, env)
    artifacts = _artifacts(env, scratch)
    (artifacts / "seeds.json").write_text(
        json.dumps({"sample_seed": seed, "source": source}) + "\n"
    )
    log(f"{mode} sample, seed {seed} ({source})")
    return Sample(mode=mode, seed=seed, artifacts=artifacts)


def benchmark_sample(
    *,
    metrics: Mapping[str, Metric],
    output: Path,
    seed_flag: int | None,
    env: Mapping[str, str],
    body: Callable[[Sample], None],
) -> int:
    """Measure one sample; write its records to ``output``. Returns the exit status."""
    records = RecordStream(output)
    sample: Sample | None = None
    try:
        records.hello(metrics)
    except RecordError as error:
        records.error(f"main(metrics=...): {error}")
        records.close()
        return 1
    with tempfile.TemporaryDirectory(prefix="vibesys-evaluator-") as scratch:
        try:
            sample = _new_sample(
                Mode.BENCHMARK, seed_flag=seed_flag, env=env, scratch=Path(scratch)
            )
            sample.records = records
            body(sample)
            if records.outcome is None:
                records.error("benchmark(run) returned without calling run.report(...)")
        except SampleError as error:
            log(f"failed: {error}")
            partial = error.partial or (sample.partial if sample else None)
            records.error(str(error), partial=partial)
        except Stopped as stop:
            log(f"stopped by signal {stop.signum}")
            records.error(
                f"stopped by signal {signal.Signals(stop.signum).name} before the sample finished",
                partial=sample.partial if sample else None,
            )
        except Exception as error:
            traceback.print_exc()
            records.error(
                f"{type(error).__name__}: {error}", partial=sample.partial if sample else None
            )
        finally:
            records.close()
    return 0 if records.outcome == "result" else 1


def unscored_sample(
    mode: Mode,
    *,
    seed_flag: int | None,
    env: Mapping[str, str],
    body: Callable[[Sample], str | None],
) -> int:
    """Run a check or profile sample: exit 0 when ``body`` returns None, else 1 with its reason."""
    with tempfile.TemporaryDirectory(prefix="vibesys-evaluator-") as scratch:
        try:
            sample = _new_sample(mode, seed_flag=seed_flag, env=env, scratch=Path(scratch))
            problem = body(sample)
        except SampleError as error:
            log(f"{mode} failed: {error}")
            return 1
        except Exception as error:
            traceback.print_exc()
            log(f"{mode} failed: {type(error).__name__}: {error}")
            return 1
    if problem is not None:
        log(f"{mode} failed: {problem}")
        return 1
    log(f"{mode}: ok")
    return 0
