from __future__ import annotations

import argparse
import random
import sys
import threading

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "reference"))
from reference import SCENARIOS, QueueFactory


def _load_candidate():
    try:
        from main import VibeServeQueue

        return VibeServeQueue
    except ImportError as exc:
        raise RuntimeError("Could not import VibeServeQueue from main.py") from exc


def _build_log(ops, seed=42):
    rng = random.Random(seed)
    return [("enqueue", i) if rng.random() < 0.6 else ("dequeue", None) for i in range(ops)]


def _replay(queue, log):
    trace = []
    for kind, item in log:
        if kind == "enqueue":
            trace.append(("enqueue", queue.enqueue(item)))
        else:
            trace.append(("dequeue", queue.dequeue()))
    return trace


def _check_spsc(cls, capacity, ops, seed):
    ref = QueueFactory("spsc", capacity)
    cand = cls(scenario="spsc", capacity=capacity)
    log = _build_log(ops, seed)
    for i, (r, c) in enumerate(zip(_replay(ref, log), _replay(cand, log), strict=True)):
        if r != c:
            return False, f"SPSC mismatch at op {i}: ref={r!r} cand={c!r}"
    return True, f"SPSC OK ({ops} ops, capacity={capacity})"


def _check_mpmc(cls, capacity, ops, producers, consumers, seed):
    ipp = ops // producers
    enqueued, dequeued = set(), []
    el, dl = threading.Lock(), threading.Lock()
    cand = cls(scenario="mpmc", capacity=capacity)
    barrier = threading.Barrier(producers + consumers)
    stop = threading.Event()

    def producer(pid):
        barrier.wait()
        for i in range(ipp):
            item = pid * ipp + i
            if cand.enqueue(item):
                with el:
                    enqueued.add(item)

    def consumer(_):
        barrier.wait()
        while not stop.is_set() or cand.size() > 0:
            x = cand.dequeue()
            if x is not None:
                with dl:
                    dequeued.append(x)

    ts = [threading.Thread(target=producer, args=(p,), daemon=True) for p in range(producers)]
    ts += [threading.Thread(target=consumer, args=(c,), daemon=True) for c in range(consumers)]
    for t in ts:
        t.start()
    for t in ts[:producers]:
        t.join()
    stop.set()
    for t in ts[producers:]:
        t.join(timeout=5)
    while True:
        x = cand.dequeue()
        if x is None:
            break
        dequeued.append(x)
    dset = set(dequeued)
    dups = len(dequeued) - len(dset)
    missing = enqueued - dset
    extra = dset - enqueued
    if dups or missing or extra:
        return False, f"MPMC failure: dups={dups}, missing={len(missing)}, extra={len(extra)}"
    return (
        True,
        f"MPMC OK ({producers}P/{consumers}C enqueued={len(enqueued)} dequeued={len(dequeued)})",
    )


def _check_mpsc(cls, capacity, ops, producers, seed):
    return _check_mpmc(cls, capacity, ops, producers, 1, seed)


def _check_lossy(cls, capacity, ops, seed):
    cand = cls(scenario="lossy", capacity=capacity)
    rng = random.Random(seed)
    enqueued, dequeued = set(), []
    for i in range(ops):
        if rng.random() < 0.6:
            ok = cand.enqueue(i)
            if not ok:
                return False, f"Lossy enqueue returned False at op {i}"
            enqueued.add(i)
        else:
            x = cand.dequeue()
            if x is not None:
                dequeued.append(x)
        if cand.size() > capacity:
            return False, f"size {cand.size()} > capacity {capacity}"
    fab = set(dequeued) - enqueued
    if fab:
        return False, f"Lossy returned items never enqueued: {list(fab)[:5]}"
    return True, f"Lossy OK ({ops} ops, capacity={capacity}, dequeued={len(dequeued)})"


def _check_batch(cls, capacity, ops, seed):
    cand = cls(scenario="batch", capacity=capacity)
    rng = random.Random(seed)
    enqueued, dequeued = [], []
    for i in range(ops):
        if rng.random() < 0.6:
            if cand.enqueue(i):
                enqueued.append(i)
        else:
            batch = cand.dequeue()
            if not isinstance(batch, list):
                return False, f"Batch dequeue must return list, got {type(batch).__name__}"
            dequeued.extend(batch)
    fab = set(dequeued) - set(enqueued)
    if fab:
        return False, f"Batch returned items never enqueued: {list(fab)[:5]}"
    dups = len(dequeued) - len(set(dequeued))
    if dups:
        return False, f"Batch returned {dups} duplicates"
    return True, f"Batch OK ({ops} ops, capacity={capacity}, dequeued={len(dequeued)})"


def main():
    parser = argparse.ArgumentParser(
        description="Correctness checker for VibeServe queue scenarios."
    )
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--capacity", type=int, default=64)
    parser.add_argument("--ops", type=int, default=2000)
    parser.add_argument("--producers", type=int, default=4)
    parser.add_argument("--consumers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    print("Loading VibeServeQueue from main.py ...")
    cls = _load_candidate()
    print("  Loaded.")
    targets = SCENARIOS if args.scenario == "all" else [args.scenario]
    results = {}
    for s in targets:
        print(f"[{s.upper()}] Checking ...")
        try:
            if s == "spsc":
                ok, msg = _check_spsc(cls, args.capacity, args.ops, args.seed)
            elif s == "mpmc":
                ok, msg = _check_mpmc(
                    cls, args.capacity, args.ops, args.producers, args.consumers, args.seed
                )
            elif s == "mpsc":
                ok, msg = _check_mpsc(cls, args.capacity, args.ops, args.producers, args.seed)
            elif s == "lossy":
                ok, msg = _check_lossy(cls, args.capacity, args.ops, args.seed)
            elif s == "batch":
                ok, msg = _check_batch(cls, args.capacity, args.ops, args.seed)
        except Exception as e:
            ok, msg = False, f"Exception: {e}"
        print(f"  PASS - {msg}" if ok else f"  FAIL - {msg}")
        results[s] = ok
    passed = sum(results.values())
    print(f"Results: {passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
