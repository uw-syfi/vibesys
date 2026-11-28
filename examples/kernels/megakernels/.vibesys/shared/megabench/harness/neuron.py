"""Evaluate a submission on AWS Trainium, one rank per logical NeuronCore.

The parent process never touches the device. Per trial it generates the
case's full inputs on the CPU in shared memory, computes the single-device
reference and FP32 oracle on the CPU, and grades the outputs that the ranks
return, reassembled as in ``tasks/sharding.py``. Each rank copies its shard to
its NeuronCore, runs the submission, and later times it and audits launches.

Launch audit: a step must issue exactly one NKI kernel per rank and no other
device work. NKI launches are counted at torch-neuronx's NKI dispatcher, which
both ``nki_op`` and direct ``nki.jit`` calls go through (``NKIHOPCaller``); the torch profiler
records compiled regions. A dispatch mode records ATen and collective ops that touch Neuron
tensors; allocation and view ops issue no device work and are ignored.
"""

from __future__ import annotations

import gc
import os
import queue
import secrets
import socket
import time
import traceback
from dataclasses import replace
from pathlib import Path

from ..cases import Case

STEP_TIMEOUT_S = 3600.0

# Ops that allocate or alias memory without issuing device work.
_NO_DEVICE_WORK = {
    "aten.empty.memory_format", "aten.empty_strided.default", "aten.empty_like.default",
    "aten.view.default", "aten._unsafe_view.default", "aten.as_strided.default",
    "aten.alias.default", "aten.detach.default", "aten.t.default", "aten.transpose.int",
    "aten.permute.default", "aten.slice.Tensor", "aten.select.int", "aten.unsqueeze.default",
    "aten.squeeze.dim", "aten.squeeze.default", "aten.expand.default", "aten.reshape.default",
    "aten.unbind.int", "aten.split.Tensor", "aten.chunk.default", "aten.lift_fresh.default",
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _audit(candidate, inputs: dict, device: str, budget: int) -> dict:
    """Count one step's NKI kernels and any other device work on this rank."""
    import torch
    from torch.profiler import ProfilerActivity, profile
    from torch.utils._python_dispatch import TorchDispatchMode
    from torch.utils._pytree import tree_flatten

    from torch_neuronx.nki_hop import NKIHOPCaller

    # Every eager NKI launch from PyTorch, through nki_op or a direct nki.jit
    # call, goes through NKIHOPCaller; count its calls during one step.
    calls = [0]
    dispatch = NKIHOPCaller.__call__

    def counted(self, *args, **kwargs):
        calls[0] += 1
        return dispatch(self, *args, **kwargs)

    torch.neuron.synchronize()
    NKIHOPCaller.__call__ = counted
    try:
        with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.PrivateUse1]) as prof:
            candidate(inputs)
            torch.neuron.synchronize()
    finally:
        NKIHOPCaller.__call__ = dispatch
    names = [event.name for event in prof.events()]
    nki_kernels = calls[0]
    # Diagnostic only: one NKI call records two profiler events.
    nki_events = names.count("nki_kernel_global")
    compiled = sorted({name for name in names if name.startswith("Torch-Compiled Region")})

    class Recorder(TorchDispatchMode):
        def __init__(self):
            super().__init__()
            self.ops: list[str] = []

        def __torch_dispatch__(self, func, types, args=(), kwargs=None):
            out = func(*args, **(kwargs or {}))
            tensors = [t for t in tree_flatten((args, kwargs, out))[0] if isinstance(t, torch.Tensor)]
            if any(t.device.type == "neuron" for t in tensors):
                self.ops.append(str(func))
            return out

    recorder, dispatch_error = Recorder(), None
    try:
        with recorder:
            candidate(inputs)
        torch.neuron.synchronize()
    except Exception as exc:
        dispatch_error = f"{type(exc).__name__}: {exc}"
    namespaces = ("aten.", "prims.", "_c10d_functional.", "c10d.")
    device_ops = [op for op in recorder.ops if op.startswith(namespaces) and op not in _NO_DEVICE_WORK]
    custom_ops = [op for op in recorder.ops if not op.startswith(namespaces)]
    launches = nki_kernels + len(device_ops) + len(compiled)
    if dispatch_error is not None or compiled:
        status = "launch_budget_failed"
    else:
        status = "within_budget" if nki_kernels >= 1 and launches <= budget else "launch_budget_failed"
    return {"status": status, "gpu_kernel_count": launches, "max_gpu_launches": budget,
            "nki_kernel_calls": nki_kernels, "nki_profiler_events": nki_events,
            "other_device_ops": device_ops[:32],
            "other_device_op_count": len(device_ops), "custom_ops": sorted(set(custom_ops)),
            "compiled_regions": compiled, "dispatch_mode_error": dispatch_error,
            "review_required": True}


def _rank_main(rank: int, contract: dict, submission: str, inbox, outbox) -> None:
    """Rank process: init the neuron process group, then serve parent requests."""
    import torch
    import torch.distributed as dist

    from ..tasks.sharding import shard
    from .benchmark import _measure
    from .runner import _load_submission

    case = Case(**contract)
    world = case.gpus
    device = f"neuron:{rank}"
    try:
        dist.init_process_group("neuron")
        torch.neuron.set_device(rank)
        host = dist.new_group(backend="gloo")
        execution = {"rank": rank, "world_size": world, "tp_rank": rank, "ep_rank": 0,
                     "device": device, "backend": "neuron", "tp_group": dist.group.WORLD,
                     "ep_group": None}
    except Exception as exc:
        outbox.put((rank, "error", f"init failed: {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"))
        return
    candidate, inputs, baseline = None, None, None
    while True:
        message = inbox.get()
        kind = message[0]
        if kind == "stop":
            break
        try:
            if kind == "trial":
                _, index, full = message
                local = shard(case, full, rank, world)
                inputs = None
                inputs = {name: value.contiguous().to(device) for name, value in local.items()}
                reply = {}
                if candidate is None:
                    start = time.perf_counter()
                    candidate = _load_submission(Path(submission), case, execution=execution)
                    reply["build_ms"] = (time.perf_counter() - start) * 1000
                torch.neuron.synchronize()
                dist.barrier(group=host)
                start = time.perf_counter()
                with torch.inference_mode():
                    outputs = candidate(inputs)
                torch.neuron.synchronize()
                reply["call_ms_including_jit"] = (time.perf_counter() - start) * 1000
                if not isinstance(outputs, dict):
                    raise AssertionError(f"run must return a dict, got {type(outputs).__name__}")
                reply["outputs"] = {name: (value.detach().to("cpu") if isinstance(value, torch.Tensor) else value)
                                    for name, value in outputs.items()}
                for name, value in inputs.items():
                    if not torch.equal(value.cpu(), local[name]):
                        raise AssertionError(f"candidate mutated input {name}")
                # Release this rank's handles on the shared full inputs (including
                # the request itself) before replying: the parent generates the
                # next trial's inputs only after every rank replies.
                del full, local, outputs, message
                gc.collect()
                outbox.put((rank, "trial", reply))
            elif kind == "baseline":
                _, timing = message
                from ..baselines_neuron import build_tp_baseline
                from ..tasks.sharding import TP_FAMILIES

                if baseline is None and world == 1 and case.family not in TP_FAMILIES:
                    # One unsharded rank: the eager baseline is the reference itself on the core.
                    from ..tasks.workloads import reference

                    single = replace(case, gpus=1, tp=1, backend="cuda")
                    baseline = lambda values: reference(single, values)  # noqa: E731
                elif baseline is None:
                    baseline = build_tp_baseline(case, execution)
                dist.barrier(group=host)
                with torch.inference_mode():
                    if timing is None:
                        outputs = baseline(inputs)
                        torch.neuron.synchronize()
                        reply = {"outputs": {name: value.detach().to("cpu") for name, value in outputs.items()}}
                    else:
                        reply = {"timing": _measure(baseline, inputs, device, *timing)}
                outbox.put((rank, "baseline", reply))
            elif kind == "time":
                _, warmup, reps = message
                dist.barrier(group=host)
                with torch.inference_mode():
                    timing = _measure(candidate, inputs, device, warmup, reps)
                outbox.put((rank, "time", timing))
            elif kind == "audit":
                dist.barrier(group=host)
                with torch.inference_mode():
                    outbox.put((rank, "audit", _audit(candidate, inputs, device, case.max_gpu_launches)))
            else:
                raise ValueError(f"unknown request {kind}")
        except Exception as exc:
            label = "incorrect" if isinstance(exc, AssertionError) else "error"
            outbox.put((rank, label, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"))
    dist.destroy_process_group()


def _diagnostics(expected: dict, actual: dict, exact: dict, case: Case) -> dict:
    """Logit error and greedy-token margins for a failed trial."""
    from .correctness import _band

    out = {}
    try:
        if "logits" in actual and actual["logits"].shape == expected["logits"].shape:
            out["logits"] = _band(expected["logits"], actual["logits"], exact["logits"], case)
        if "next_token" in actual and expected["logits"].ndim == 1:
            want, oracle = exact["logits"].double(), expected["logits"].double()
            noise = (oracle - want).square().mean().sqrt().item()
            token = int(actual["next_token"])
            out["next_token"] = {"chosen": token, "reference": int(expected["next_token"]),
                                 "oracle": int(want.argmax()),
                                 "oracle_logit_gap_to_top": float(want.max() - want[token]),
                                 "band": case.noise_factor * noise}
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


class _Ranks:
    """Rank processes and their request/reply queues."""

    def __init__(self, case: Case, submission: Path):
        import torch.multiprocessing as mp

        context = mp.get_context("spawn")
        self.world = case.gpus
        self.inboxes = [context.Queue() for _ in range(self.world)]
        self.outbox = context.Queue()
        port = str(_free_port())
        saved = {key: os.environ.get(key) for key in
                 ("RANK", "WORLD_SIZE", "LOCAL_RANK", "LOCAL_WORLD_SIZE", "MASTER_ADDR", "MASTER_PORT")}
        self.processes = []
        try:
            for rank in range(self.world):
                os.environ.update(RANK=str(rank), WORLD_SIZE=str(self.world), LOCAL_RANK=str(rank),
                                  LOCAL_WORLD_SIZE=str(self.world), MASTER_ADDR="127.0.0.1", MASTER_PORT=port)
                process = context.Process(target=_rank_main, daemon=True,
                                          args=(rank, case.to_dict(), str(submission), self.inboxes[rank], self.outbox))
                process.start()
                self.processes.append(process)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def send(self, *message) -> None:
        for inbox in self.inboxes:
            inbox.put(message)

    def request(self, *message) -> list:
        self.send(*message)
        return self.collect(message[0])

    def collect(self, request: str) -> list:
        """Each rank's reply to ``request``, in rank order; a rank failure raises."""
        replies: dict[int, object] = {}
        deadline = time.monotonic() + STEP_TIMEOUT_S
        while len(replies) < self.world:
            try:
                rank, kind, payload = self.outbox.get(timeout=5)
            except queue.Empty:
                dead = [rank for rank, process in enumerate(self.processes) if not process.is_alive()]
                if dead:
                    raise RuntimeError(f"rank processes exited: {dead}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{request}: ranks {sorted(set(range(self.world)) - set(replies))} timed out")
                continue
            if kind in ("error", "incorrect"):
                raise (AssertionError if kind == "incorrect" else RuntimeError)(f"rank {rank}: {payload}")
            replies[rank] = payload
        return [replies[rank] for rank in range(self.world)]

    def close(self) -> None:
        for inbox in self.inboxes:
            inbox.put(("stop",))
        for process in self.processes:
            process.join(timeout=60)
            if process.is_alive():
                process.kill()


def evaluate_neuron(case: Case, submission: Path, *, trials: int, warmup: int, reps: int,
                    baseline: bool = True) -> dict:
    """Grade, time and audit a submission; ``baseline`` also times the eager TP baseline."""
    from ..tasks.sharding import assemble, check_divisible
    from ..tasks.workloads import make_inputs, oracle, reference, regrade
    from .correctness import _compare, _teacher_forced
    from .distributed import worst_rank_timing

    single = replace(case, gpus=1, tp=1, backend="cuda")
    ranks = _Ranks(case, submission)
    result: dict = {"topology": {"world_size": case.gpus, "tp": case.tp,
                                 "layout": "megabench/tasks/sharding.py" if case.gpus > 1 else "unsharded",
                                 "device": "one logical NeuronCore per rank"}}
    try:
        correctness = {"status": "pass", "trials": []}
        expected = exact = None
        for index in range(trials):
            seed = secrets.randbits(32)
            values = make_inputs(single, seed, "cpu")
            scenario = "seeded"
            if case.family == "spec_target_step":
                from ..tasks.eagle3 import set_acceptance_scenario

                scenario = ("eagle3_raw", "accept_full", "accept_one")[index % 3]
                if scenario != "eagle3_raw":
                    set_acceptance_scenario(single, values, case.params["draft_depth"]
                                            if scenario == "accept_full" else 1)
            check_divisible(case, values, case.gpus)
            for value in values.values():
                value.share_memory_()
            # The ranks run the candidate while the CPU computes the reference.
            ranks.send("trial", index, values)
            expected, exact = reference(single, values), oracle(single, values)
            replies = ranks.collect("trial")
            if index == 0:
                result["build_ms"] = max(reply.get("build_ms", 0.0) for reply in replies)
                result["first_call_ms_including_jit"] = max(reply["call_ms_including_jit"] for reply in replies)
            actual = assemble([reply["outputs"] for reply in replies])
            # As check_trials: references that follow the candidate's own picks.
            expected, exact = regrade(single, values, expected, exact, actual)
            if case.family == "tts_frame_step":
                forced = _teacher_forced(single, values, expected, exact, actual)
                if forced[0] is not expected:
                    scenario = "teacher_forced"
                expected, exact = forced
            del values
            gc.collect()
            try:
                details = _compare(expected, actual, single, "cpu", exact)
            except AssertionError as exc:
                raise AssertionError(f"trial {index} (seed {seed}): {exc}; "
                                     f"diagnostics {_diagnostics(expected, actual, exact, single)}") from None
            correctness["trials"].append({"seed": seed, "scenario": scenario, "outputs": details})
        result["correctness"] = correctness
        try:
            if not baseline:
                raise RuntimeError("not requested")
            baseline_outputs = assemble([reply["outputs"] for reply in ranks.request("baseline", None)])
            # A near-tie can flip the eager baseline's greedy tokens; that is a
            # diagnostic, not a reason to drop the timing baseline.
            try:
                result["baseline_correctness"] = _compare(expected, baseline_outputs, single, "cpu", exact)
            except AssertionError as exc:
                result["baseline_correctness"] = {"pass": False, "reason": str(exc)[:500]}
            result["reference_timing"] = worst_rank_timing(
                [{"timing": reply["timing"]} for reply in ranks.request("baseline", (warmup, reps))], "timing")
            result["reference_timing"]["baseline"] = (f"eager PyTorch TP{case.gpus} (megabench/baselines_neuron.py)"
                                                     if case.gpus > 1 else "eager PyTorch reference on one NeuronCore")
        except Exception as exc:
            result["baseline_unavailable"] = f"{type(exc).__name__}: {exc}"
        result["candidate_timing"] = worst_rank_timing(
            [{"timing": timing} for timing in ranks.request("time", warmup, reps)], "timing")
        audits = ranks.request("audit")
    except AssertionError as exc:
        return result | {"status": "incorrect", "reason": str(exc)}
    except Exception as exc:
        return result | {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        ranks.close()
    within = all(audit["status"] == "within_budget" for audit in audits)
    result["launch_audit"] = {"status": "within_budget" if within else "launch_budget_failed",
                              "max_gpu_launches_per_rank": case.max_gpu_launches,
                              "per_rank": audits, "review_required": True}
    result["status"] = "ok_provisional" if within else "non_megakernel"
    if "reference_timing" in result:
        for key in ("host", "cuda_event"):
            base, cand = result["reference_timing"][f"{key}_p50_ms"], result["candidate_timing"][f"{key}_p50_ms"]
            if base and cand:
                result[f"speedup_vs_eager_{key}"] = base / cand
        if "speedup_vs_eager_cuda_event" in result:
            result["speedup_vs_best_baseline_cuda_event"] = result["speedup_vs_eager_cuda_event"]
    return result
