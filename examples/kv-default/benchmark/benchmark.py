from __future__ import annotations
import argparse, json, random, sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "reference"))
sys.path.insert(0, str(Path(__file__).parent.parent))
from reference import KVFactory, SCENARIOS

def _load_candidate():
    try:
        from main import VibeServeKV
        return VibeServeKV
    except ImportError:
        return None

def _run(store, scenario, duration, warmup, writers, readers, key_count, value_bytes):
    keys = [f"key{i}" for i in range(key_count)]
    value = b"x" * value_bytes
    stop = threading.Event()
    lock = threading.Lock()
    counters = {"put": 0, "get": 0, "delete": 0, "hit": 0}
    for k in keys:
        store.put(k, value)
    def writer_fn(wid):
        rng = random.Random(wid)
        local = {"put": 0, "delete": 0}
        while not stop.is_set():
            key = rng.choice(keys)
            if rng.random() < 0.7:
                store.put(key, value); local["put"] += 1
            else:
                store.delete(key); local["delete"] += 1
        with lock: counters["put"] += local["put"]; counters["delete"] += local["delete"]
    def reader_fn(rid):
        rng = random.Random(rid + 1000)
        local = {"get": 0, "hit": 0}
        while not stop.is_set():
            key = rng.choice(keys)
            val = store.get(key)
            local["get"] += 1
            if val is not None: local["hit"] += 1
        with lock: counters["get"] += local["get"]; counters["hit"] += local["hit"]
    n_writers = writers if scenario in ("heavy-write", "point") else 1
    n_readers = readers if scenario in ("read-heavy", "point") else 1
    def make_threads():
        ts = [threading.Thread(target=writer_fn, args=(i,), daemon=True) for i in range(n_writers)]
        ts += [threading.Thread(target=reader_fn, args=(i,), daemon=True) for i in range(n_readers)]
        return ts
    if warmup > 0:
        wts = make_threads()
        for t in wts: t.start()
        time.sleep(warmup)
        stop.set()
        for t in wts: t.join(timeout=5)
        stop.clear(); counters.update({"put": 0, "get": 0, "delete": 0, "hit": 0})
    ts = make_threads()
    for t in ts: t.start()
    t0 = time.perf_counter()
    time.sleep(duration)
    stop.set()
    for t in ts: t.join(timeout=10)
    elapsed = time.perf_counter() - t0
    total = counters["put"] + counters["get"] + counters["delete"]
    hit_rate = counters["hit"] / counters["get"] if counters["get"] > 0 else 0.0
    print(f"Scenario: {scenario.upper()}  Duration: {elapsed:.1f}s  Writers: {n_writers}  Readers: {n_readers}")
    print(f"  put: {counters['put']:,} ({counters['put']/elapsed:,.0f}/s)  get: {counters['get']:,} ({counters['get']/elapsed:,.0f}/s)  delete: {counters['delete']:,}")
    print(f"  hit rate: {hit_rate:.1%}  total: {total:,} ({total/elapsed:,.0f} ops/s)")
    return {"scenario": scenario, "duration": elapsed, "writers": n_writers, "readers": n_readers,
            "put_ops": counters["put"], "get_ops": counters["get"], "delete_ops": counters["delete"],
            "hit_rate": hit_rate, "total_ops_per_sec": total / elapsed}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="point")
    parser.add_argument("--key-count", type=int, default=1000)
    parser.add_argument("--value-bytes", type=int, default=64)
    parser.add_argument("--writers", type=int, default=4)
    parser.add_argument("--readers", type=int, default=4)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--use-reference", action="store_true")
    parser.add_argument("--output-json", type=str, default=None)
    args = parser.parse_args()
    targets = SCENARIOS if args.scenario == "all" else [args.scenario]
    results = []
    for s in targets:
        if args.use_reference:
            store = KVFactory(s)
        else:
            cls = _load_candidate()
            store = cls(scenario=s) if cls else KVFactory(s)
        results.append(_run(store, s, args.duration, args.warmup,
                            args.writers, args.readers, args.key_count, args.value_bytes))
    if args.output_json:
        with open(args.output_json, "w") as f: json.dump(results, f, indent=2)
        print(f"Results written to {args.output_json}")

if __name__ == "__main__":
    main()
