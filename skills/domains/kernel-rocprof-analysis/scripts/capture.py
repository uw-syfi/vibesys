#!/usr/bin/env python3
"""Run a command under an AMD profiler, after checking that the profiler can trace it.

Copy this one file into the candidate's repository and use it as the argv prefix
of a profiling job, so it runs inside the candidate's own container:

    python3 tools/capture.py <recipe> [options] -- <command...>

It needs only Python 3.8+ and its standard library. In order, it:

1. Reads the versions that decide whether a capture can work: the profiler's
   ROCm release (``rocprofv3 --version``), the HIP runtime the command will load
   (``hipconfig --version`` and ``torch.version.hip``), and the node's amdgpu
   driver (``/sys/module/amdgpu/version``, else ``amd-smi version``).
2. Refuses a known-incompatible combination with a message naming the versions,
   unless ``--allow-version-mismatch`` is given (the override is recorded).
3. Writes ``capture.json`` (schema, recipe, options, versions, findings, the
   wrapped command, the exact profiler argv, the start time) into
   ``<output-dir>/<name>/``; the output directory defaults to
   ``$VIBESYS_ARTIFACTS_DIR``, the directory a job copies back.
4. Replaces itself with the profiler (``exec``), so the profiler keeps this
   process's PID and every signal sent to the job (SIGTERM to flush and stop)
   reaches the profiler directly, with no wrapper in between.

Recipes:
    timeline      rocprofv3 kernel + memory-copy trace and stats (CSV);
                  --hip-api adds the HIP runtime trace, --markers the roctx ranges
    counters      rocprofv3 --pmc with the counters you name (one pass)
    thread-trace  rocprofv3 --att on the kernels matching --kernel-include (ROCm 7.1+)
    kernel-deep   rocprof-compute profile for the kernels matching --kernel
    check         only the version preflight: prints the report, exits 1 on a refusal

Exit status: the profiler's own once it runs; 1 when the preflight refuses or an
input is wrong (the message says which); 2 for a usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

SCHEMA_VERSION = 1
RECORD_NAME = "capture.json"
ARTIFACTS_ENV = "VIBESYS_ARTIFACTS_DIR"
ATT_LIBRARY_ENV = "ROCPROF_ATT_LIBRARY_PATH"
ATT_DECODER_LIB = "librocprof-trace-decoder.so"
ATT_MIN_ROCM = (7, 1)
AMDGPU_VERSION_FILE = "/sys/module/amdgpu/version"
PROBE_TIMEOUT_S = 120.0
EXIT_REFUSED = 1
EXIT_USAGE = 2

ROCPROFV3_RECIPES = ("timeline", "counters", "thread-trace")
RECIPES = (*ROCPROFV3_RECIPES, "kernel-deep", "check")

_TRIPLE_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_ROCM_FIELD_RE = re.compile(r"rocm_version:\s*(\d+\.\d+(?:\.\d+)?)")
_TOOL_FIELD_RE = re.compile(r"^\s*version:\s*(\S+)", re.MULTILINE)
_AMDSMI_DRIVER_RE = re.compile(r"amdgpu(?: driver)? version:\s*([0-9][\w.\-]*)", re.IGNORECASE)
_PERIOD_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$")


class CaptureInputError(ValueError):
    """An option or environment value this script cannot act on; the message names it."""


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def parse_release(text: str) -> tuple[int, int, int] | None:
    """The first ``X.Y[.Z]`` in *text* as a triple, or ``None``."""
    match = _TRIPLE_RE.search(text or "")
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def parse_rocprofv3_version(text: str) -> dict[str, str | None]:
    """``rocprofv3 --version`` output as its tool version and the ROCm release it ships with.

    ROCm 7 prints ``version: 1.3.2`` (the rocprofiler-sdk tool) and, on its own line,
    ``rocm_version: 7.2.3``. Older builds print one number, taken as both.
    """
    rocm = _ROCM_FIELD_RE.search(text or "")
    tool = _TOOL_FIELD_RE.search(text or "")
    first = _TRIPLE_RE.search(text or "")
    return {
        "tool": tool.group(1) if tool else (first.group(0) if first else None),
        "rocm": rocm.group(1) if rocm else (first.group(0) if first else None),
    }


def _major_minor(release: str | None) -> tuple[int, int] | None:
    parsed = parse_release(release or "")
    return (parsed[0], parsed[1]) if parsed else None


def _fmt(pair: tuple[int, int]) -> str:
    return f"{pair[0]}.{pair[1]}"


def missing_tools(recipe: str, versions: dict[str, dict[str, str | None]]) -> list[str]:
    """The profilers *recipe* runs that this container does not have (no override applies)."""
    missing: list[str] = []
    if recipe in ROCPROFV3_RECIPES and versions.get("rocprofv3", {}).get("path") is None:
        missing.append("rocprofv3 was not found on PATH in this container")
    if recipe == "kernel-deep" and versions.get("rocprof-compute", {}).get("path") is None:
        missing.append("rocprof-compute was not found on PATH in this container")
    return missing


def check_versions(recipe: str, versions: dict[str, dict[str, str | None]]) -> list[str]:
    """Every reason the recorded *versions* cannot give a trustworthy capture (empty when none).

    Pure: reads only *versions*, the shape ``collect_versions`` returns. The rules are
    the combinations known to fail, each message names the versions involved:

    - the profiler's ROCm major release differs from a HIP runtime's major release,
      or the profiler is older than the HIP runtime it traces: its tracing tables do
      not know the runtime's newer ones, which loses or crashes the trace;
    - thread trace needs rocprofv3 from ROCm 7.1 or newer.

    The amdgpu driver is recorded, never judged here: whether a runtime runs on a
    driver is the job's own concern, and a capture that runs at all ran on it.
    """
    findings: list[str] = []
    profiler = versions.get("rocprofv3", {})
    profiler_rocm = _major_minor(profiler.get("rocm"))
    if recipe != "kernel-deep" and profiler_rocm is not None:
        for source, runtime in sorted(versions.get("hip_runtime", {}).items()):
            runtime_rocm = _major_minor(runtime)
            if runtime_rocm is None:
                continue
            if runtime_rocm[0] != profiler_rocm[0] or runtime_rocm > profiler_rocm:
                findings.append(
                    f"rocprofv3 is from ROCm {_fmt(profiler_rocm)} but the HIP runtime "
                    f"({source}) is {runtime}: the profiler must come from the same ROCm "
                    f"major release and be at least as new as the runtime it traces; install "
                    f"a matching rocprofv3 in the candidate image"
                )
    if recipe == "thread-trace" and profiler_rocm is not None and profiler_rocm < ATT_MIN_ROCM:
        findings.append(
            f"thread trace needs rocprofv3 from ROCm {_fmt(ATT_MIN_ROCM)} or newer; this "
            f"one is from ROCm {_fmt(profiler_rocm)}"
        )
    return findings


@dataclass(frozen=True)
class Host:
    """The I/O the preflight performs, injectable so tests can describe a container."""

    run: Callable[[Sequence[str]], tuple[int | None, str]]
    read_text: Callable[[str], str | None]
    is_file: Callable[[str], bool]
    which: Callable[[str], str | None]


def _run_probe(argv: Sequence[str]) -> tuple[int | None, str]:
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _which(name: str) -> str | None:
    import shutil  # only the real host needs it

    return shutil.which(name)


def _is_file(path: str) -> bool:
    return Path(path).is_file()


REAL_HOST = Host(run=_run_probe, read_text=_read_text, is_file=_is_file, which=_which)


def collect_versions(host: Host, *, python: str) -> dict[str, dict[str, str | None]]:
    """Read every version the preflight judges and ``capture.json`` records."""
    versions: dict[str, dict[str, str | None]] = {}

    rocprofv3 = host.which("rocprofv3")
    profiler: dict[str, str | None] = {"path": rocprofv3, "tool": None, "rocm": None}
    if rocprofv3 is not None:
        rc, out = host.run([rocprofv3, "--version"])
        if rc == 0:
            profiler.update(parse_rocprofv3_version(out))
    versions["rocprofv3"] = profiler

    compute = host.which("rocprof-compute")
    compute_entry: dict[str, str | None] = {"path": compute, "version": None}
    if compute is not None:
        rc, out = host.run([compute, "--version"])
        release = parse_release(out) if rc == 0 else None
        compute_entry["version"] = ".".join(map(str, release)) if release else None
    versions["rocprof-compute"] = compute_entry

    runtime: dict[str, str | None] = {}
    hipconfig = host.which("hipconfig")
    if hipconfig is not None:
        rc, out = host.run([hipconfig, "--version"])
        release = parse_release(out) if rc == 0 else None
        if release:
            runtime["hipconfig"] = ".".join(map(str, release))
    rc, out = host.run([python, "-c", "import torch; print(torch.version.hip or '')"])
    release = parse_release(out.strip().splitlines()[-1]) if rc == 0 and out.strip() else None
    if release:
        runtime["torch"] = ".".join(map(str, release))
    versions["hip_runtime"] = runtime

    driver: dict[str, str | None] = {"version": None, "source": None}
    text = host.read_text(AMDGPU_VERSION_FILE)
    if text and text.strip():
        driver = {"version": text.strip(), "source": AMDGPU_VERSION_FILE}
    else:
        amd_smi = host.which("amd-smi")
        if amd_smi is not None:
            rc, out = host.run([amd_smi, "version"])
            match = _AMDSMI_DRIVER_RE.search(out) if rc == 0 else None
            if match:
                driver = {"version": match.group(1), "source": "amd-smi version"}
    versions["amdgpu_driver"] = driver
    return versions


# ---------------------------------------------------------------------------
# Profiler argv
# ---------------------------------------------------------------------------


def _collection_period(raw: str | None) -> list[str]:
    if raw is None:
        return []
    match = _PERIOD_RE.match(raw)
    if match is None:
        message = f"--collection-period {raw!r}: expected DELAY_S:DURATION_S, e.g. 120:30"
        raise CaptureInputError(message)
    delay, duration = float(match.group(1)), float(match.group(2))
    if duration <= 0:
        message = f"--collection-period {raw!r}: the duration must be positive"
        raise CaptureInputError(message)
    return ["--collection-period", f"{delay:g}:{duration:g}:1"]


def att_library_dir(environ: dict[str, str], host: Host) -> str | None:
    """The directory holding the thread-trace decoder library, or ``None``."""
    candidates = [environ.get(ATT_LIBRARY_ENV, "").strip()]
    candidates += [f"{root}/lib" for root in (environ.get("ROCM_PATH", "").strip(), "/opt/rocm")]
    for directory in candidates:
        if directory and host.is_file(f"{directory}/{ATT_DECODER_LIB}"):
            return directory
    return None


def profiler_argv(ns: argparse.Namespace, out_dir: Path, *, att_dir: str | None) -> list[str]:
    """The profiler invocation for *ns*'s recipe, ending with ``--`` before the command."""
    if ns.recipe == "kernel-deep":
        argv = ["rocprof-compute", "profile", "-n", ns.name, "-p", str(out_dir / "workload")]
        argv += ["-k", ns.kernel]
        if ns.dispatch is not None:
            argv += ["--dispatch", str(ns.dispatch)]
        return [*argv, *ns.profiler_arg, "--"]
    argv = ["rocprofv3"]
    if ns.recipe == "timeline":
        argv += ["--kernel-trace", "--memory-copy-trace", "--stats"]
        if ns.hip_api:
            argv.append("--hip-runtime-trace")
        if ns.markers:
            argv.append("--marker-trace")
        argv += ["--output-format", "csv"]
    elif ns.recipe == "counters":
        argv += ["--pmc", *ns.pmc, "--output-format", "csv"]
    else:  # thread-trace
        if att_dir is None:
            message = (
                f"thread-trace needs {ATT_DECODER_LIB}: set ${ATT_LIBRARY_ENV} to its directory "
                f"or install it under $ROCM_PATH/lib in the candidate image"
            )
            raise CaptureInputError(message)
        argv += ["--att", "--att-target-cu", str(ns.target_cu)]
        argv += ["--att-buffer-size", str(ns.buffer_bytes), "--att-library-path", att_dir]
        argv += ["--output-format", "csv", "json"]
    if ns.kernel_include:
        argv += ["--kernel-include-regex", ns.kernel_include]
    argv += _collection_period(ns.collection_period)
    if ns.selected_regions:
        argv.append("--selected-regions")
    return [*argv, *ns.profiler_arg, "-d", str(out_dir), "--"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The command line: a recipe, its options, then ``--`` and the command to profile."""
    parser = argparse.ArgumentParser(
        prog="capture.py",
        description="Run a command under an AMD profiler after a version preflight.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("recipe", choices=RECIPES)
    parser.add_argument("--name", help="capture directory under the output dir (default: recipe)")
    parser.add_argument("--output-dir", help=f"default: ${ARTIFACTS_ENV}")
    parser.add_argument(
        "--python", default=sys.executable, help="interpreter whose torch.version.hip to read"
    )
    parser.add_argument("--allow-version-mismatch", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing, run nothing")
    parser.add_argument(
        "--profiler-arg", action="append", default=[], help="extra profiler argument (repeatable)"
    )
    rocprof = parser.add_argument_group("rocprofv3 recipes")
    rocprof.add_argument("--kernel-include", help="--kernel-include-regex value")
    rocprof.add_argument("--collection-period", help="DELAY_S:DURATION_S, e.g. 120:30")
    rocprof.add_argument(
        "--selected-regions", action="store_true", help="collect only between roctx resume/pause"
    )
    rocprof.add_argument("--hip-api", action="store_true", help="timeline: HIP runtime trace")
    rocprof.add_argument("--markers", action="store_true", help="timeline: roctx marker trace")
    rocprof.add_argument("--pmc", nargs="+", default=[], help="counters: counter names")
    rocprof.add_argument("--target-cu", type=int, default=1, help="thread-trace: compute unit")
    rocprof.add_argument(
        "--buffer-bytes", type=int, default=67_108_864, help="thread-trace: plain byte count"
    )
    compute = parser.add_argument_group("kernel-deep")
    compute.add_argument("--kernel", help="literal substring of the kernel name")
    compute.add_argument("--dispatch", type=int, help="one dispatch index")
    return parser


def _split_command(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    args = list(argv)
    if "--" not in args:
        return args, []
    cut = args.index("--")
    return args[:cut], args[cut + 1 :]


def _validate(ns: argparse.Namespace, command: list[str]) -> None:
    if ns.recipe != "check" and not command:
        raise CaptureInputError(f"{ns.recipe}: give the command to profile after --")
    if ns.recipe == "counters" and not ns.pmc:
        raise CaptureInputError("counters: name the counters with --pmc")
    if ns.recipe == "thread-trace" and not ns.kernel_include:
        raise CaptureInputError("thread-trace: name the kernels with --kernel-include")
    if ns.recipe == "kernel-deep" and not ns.kernel:
        raise CaptureInputError("kernel-deep: name the kernel with --kernel")
    if ns.name is not None and (not ns.name or "/" in ns.name or ns.name in (".", "..")):
        raise CaptureInputError(f"--name {ns.name!r}: use one plain directory name")


def _output_root(ns: argparse.Namespace, environ: dict[str, str]) -> Path:
    raw = ns.output_dir or environ.get(ARTIFACTS_ENV, "")
    if not raw:
        message = f"no output directory: ${ARTIFACTS_ENV} is not set; pass --output-dir"
        raise CaptureInputError(message)
    return Path(raw)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    host: Host = REAL_HOST,
    execvp: Callable[[str, list[str]], object] = os.execvp,
    now: Callable[[], datetime] = _utc_now,
    stdout: object = None,
) -> int:
    """Preflight, record, then ``exec`` the profiler; return an exit status if it does not exec.

    *environ*, *host*, *execvp* and *now* are the script's effects, passed in so a test
    can run it without a GPU, a clock or a process.
    """
    out = stdout if stdout is not None else sys.stdout
    env = dict(os.environ) if environ is None else environ
    raw = list(sys.argv[1:] if argv is None else argv)
    options, command = _split_command(raw)
    parser = build_parser()
    try:
        ns = parser.parse_args(options)
    except SystemExit as exc:
        return int(exc.code or 0) and EXIT_USAGE
    ns.name = ns.name if ns.name is not None else ns.recipe
    try:
        _validate(ns, command)
        versions = collect_versions(host, python=ns.python)
        findings = check_versions(ns.recipe, versions)
        refused = bool(findings) and not ns.allow_version_mismatch
        if ns.recipe == "check":
            report = {"versions": versions, "findings": findings}
            out.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
            return EXIT_REFUSED if findings else 0
        missing = missing_tools(ns.recipe, versions)
        if missing:
            message = "; ".join(missing) + "; install it in the candidate image"
            raise CaptureInputError(message)
        if refused:
            message = "refusing to capture: " + "; ".join(findings)
            message += " (pass --allow-version-mismatch to capture anyway)"
            raise CaptureInputError(message)
        capture_dir = _output_root(ns, env) / ns.name
        record_path = capture_dir / RECORD_NAME
        if record_path.exists():
            message = f"{capture_dir} already holds a capture; pass a different --name"
            raise CaptureInputError(message)
        att_dir = att_library_dir(env, host) if ns.recipe == "thread-trace" else None
        exec_argv = [*profiler_argv(ns, capture_dir, att_dir=att_dir), *command]
    except CaptureInputError as exc:
        sys.stderr.write(f"capture.py: {exc}\n")
        return EXIT_REFUSED

    record = {
        "schema": SCHEMA_VERSION,
        "recipe": ns.recipe,
        "name": ns.name,
        "options": {
            key: value
            for key, value in sorted(vars(ns).items())
            if key not in ("recipe", "name", "output_dir", "dry_run")
        },
        "argv": raw,
        "command": command,
        "profiler_argv": exec_argv,
        "output_dir": str(capture_dir),
        "versions": versions,
        "preflight": {
            "findings": findings,
            "overridden": bool(findings) and ns.allow_version_mismatch,
        },
        "started_at": now().isoformat(),
    }
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if ns.dry_run:
        out.write(text)
        out.write(shlex.join(exec_argv) + "\n")
        return 0
    capture_dir.mkdir(parents=True, exist_ok=True)
    record_path.write_text(text, encoding="utf-8")
    for warning in findings:
        sys.stderr.write(f"capture.py: warning (overridden): {warning}\n")
    if hasattr(out, "flush"):
        out.flush()
    sys.stderr.flush()
    execvp(exec_argv[0], exec_argv)
    # A real execvp never returns; a substitute that does means the profiler never ran.
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
