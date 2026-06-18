from __future__ import annotations
import argparse, random, sys, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "reference"))
sys.path.insert(0, str(Path(__file__).parent.parent))
from reference import KVFactory, SCENARIOS

def _load_candidate():
    try:
        from main import VibeServeKV
        return VibeServeKV
    except ImportError as exc:
        raise RuntimeError("Could not import VibeServeKV from main.py") from exc

def _check_point(cls, ops, seed):
    rng = random.Random(seed)
    ref = KVFactory("point")
    cand = cls(scenario="point")
    keys = [f"key{i}" for i in range(max(ops // 10, 20))]
    for i in range(ops):
        op = rng.choice(["put", "get", "delete"])
        key = rng.choice(keys)
        if op == "put":
            value = rng.randbytes(rng.randint(1, 64))
            r_ok = ref.put(key, value)
            c_ok = cand.put(key, value)
            if r_ok != c_ok:
                return False, f"put({key!r}) mismatch at op {i}: ref={r_ok} cand={c_ok}"
        elif op == "get":
            r_val = ref.get(key)
            c_val = cand.get(key)
            if r_val != c_val:
                return False, f"get({key!r}) mismatch at op {i}: ref={r_val!r} cand={c_val!r}"
        else:
            r_ok = ref.delete(key)
            c_ok = cand.delete(key)
            if r_ok != c_ok:
                return False, f"delete({key!r}) mismatch at op {i}: ref={r_ok} cand={c_ok}"
    return True, f"Point OK ({ops} ops)"

def _check_scan(cls, ops, prefix_len, seed):
    rng = random.Random(seed)
    ref = KVFactory("scan")
    cand = cls(scenario="scan")
    prefixes = [chr(ord("a") + i) for i in range(26)]
    keys = [f"{rng.choice(prefixes)}{rng.randint(0, 99)}" for _ in range(ops // 5)]
    for key in keys:
        value = rng.randbytes(8)
        ref.put(key, value)
        cand.put(key, value)
    for prefix in prefixes[:prefix_len]:
        r_scan = ref.scan(prefix)
        try:
            c_scan = cand.scan(prefix)
        except AttributeError:
            return False, "scan() not implemented on candidate"
        if r_scan != c_scan:
            return False, f"scan({prefix!r}) mismatch: ref={len(r_scan)} items cand={len(c_scan)}"
    return True, f"Scan OK ({ops} puts, {prefix_len} prefixes checked)"

def _check_concurrent(cls, scenario, ops, writers, readers, seed):
    cand = cls(scenario=scenario)
    keys = [f"k{i}" for i in range(50)]
    written, lock, errors = {}, threading.Lock(), []
    barrier = threading.Barrier(writers + readers)
    stop = threading.Event()
    def writer(wid):
        local_rng = random.Random(seed + wid)
        barrier.wait()
        for _ in range(ops // writers):
            key = local_rng.choice(keys)
            value = local_rng.randbytes(8)
            if cand.put(key, value):
                with lock: written[key] = value
    def reader(rid):
        local_rng = random.Random(seed + 1000 + rid)
        barrier.wait()
        for _ in range(ops // readers):
            key = local_rng.choice(keys)
            val = cand.get(key)
            if val is not None and not isinstance(val, (bytes, bytearray)):
                with lock: errors.append(f"get({key!r}) wrong type: {type(val)}")
    ts = [threading.Thread(target=writer, args=(i,), daemon=True) for i in range(writers)]
    ts += [threading.Thread(target=reader, args=(i,), daemon=True) for i in range(readers)]
    for t in ts: t.start()
    for t in ts: t.join(timeout=30)
    if errors: return False, f"Concurrent errors: {errors[0]}"
    for key in list(written)[:20]:
        if cand.get(key) is None:
            return False, f"Key {key!r} was put but is now missing (lost write)"
    return True, f"Concurrent {scenario} OK ({ops} ops, {writers}W/{readers}R)"

def _check_delete_semantics(cls, ops, seed):
    rng = random.Random(seed)
    cand = cls(scenario="point")
    for i in range(ops):
        key = f"key{rng.randint(0, 9)}"
        value = rng.randbytes(4)
        cand.put(key, value)
        if cand.get(key) != value:
            return False, f"get after put mismatch at op {i}"
        if not cand.delete(key):
            return False, f"delete returned False for existing key at op {i}"
        if cand.get(key) is not None:
            return False, f"get returned value after delete at op {i}"
        if cand.delete(key):
            return False, f"double delete returned True at op {i}"
    return True, f"Delete semantics OK ({ops} cycles)"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--ops", type=int, default=2000)
    parser.add_argument("--writers", type=int, default=4)
    parser.add_argument("--readers", type=int, default=4)
    parser.add_argument("--prefix-len", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    print("Loading VibeServeKV from main.py ...")
    cls = _load_candidate()
    print("  Loaded.")
    targets = SCENARIOS if args.scenario == "all" else [args.scenario]
    results = {}
    for s in targets:
        print(f"[{s.upper()}] Checking ...")
        try:
            if s == "point":
                ok, msg = _check_point(cls, args.ops, args.seed)
                if ok: ok, msg = _check_delete_semantics(cls, args.ops // 10, args.seed)
            elif s == "scan":      ok, msg = _check_scan(cls, args.ops, args.prefix_len, args.seed)
            elif s == "heavy-write": ok, msg = _check_concurrent(cls, s, args.ops, args.writers, 1, args.seed)
            elif s == "read-heavy":  ok, msg = _check_concurrent(cls, s, args.ops, 1, args.readers, args.seed)
        except Exception as e:
            ok, msg = False, f"Exception: {e}"
        print(f"  PASS - {msg}" if ok else f"  FAIL - {msg}")
        results[s] = ok
    passed = sum(results.values())
    print(f"\nResults: {passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)

if __name__ == "__main__":
    main()
