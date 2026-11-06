package check_test

import (
	"encoding/json"
	"fmt"
	"hash/maphash"
	"iter"
	"math/rand"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check"
)

// A key-value store whose writes and reads may span several keys and are atomic: a small
// generic oracle for the engine's own behaviour (partitions, sampling, budgets, verdicts).
// A call's Info is its kvIn and its one step's body is its kvOut, both JSON.

type kvIn struct {
	Write bool     `json:"write,omitempty"`
	Keys  []string `json:"keys"`
	Vals  []int    `json:"vals,omitempty"` // a write's
}

type kvOut struct {
	Vals []int `json:"vals,omitempty"` // a read's
}

type kvState struct {
	keys []string
	vals []int
}

var kvSeed = maphash.MakeSeed()

// kvSpec checks every call atomically over its keys; perKey projects each call onto its keys
// one by one instead (what a task does when the contract promises per-key views only).
func kvSpec(name string, perKey bool) *check.Spec[kvState, kvIn, kvOut] {
	idx := func(s kvState, k string) int { i, _ := slices.BinarySearch(s.keys, k); return i }
	return &check.Spec[kvState, kvIn, kvOut]{
		Name: name,
		Project: func(phases iter.Seq[[]check.Call], out *check.Projection[kvIn, kvOut]) error {
			for calls := range phases {
				for i := range calls {
					c := &calls[i]
					var in kvIn
					if err := json.Unmarshal(c.Info, &in); err != nil {
						return fmt.Errorf("call %d: info: %w", c.Index, err)
					}
					var res kvOut
					lost := c.Lost()
					if !lost {
						if err := json.Unmarshal(c.Steps[0].Body, &res); err != nil {
							out.Violation(c, "body is not JSON: %q", c.Steps[0].Body)
							continue
						}
						if !in.Write && len(res.Vals) != len(in.Keys) {
							out.Violation(c, "read of %d keys answered %d values", len(in.Keys), len(res.Vals))
							continue
						}
					}
					if !perKey {
						out.Add(check.Piece[kvIn, kvOut]{Keys: in.Keys, In: in, Out: res, Unknown: lost, Write: in.Write, Origin: c})
						continue
					}
					for j, k := range in.Keys {
						one := kvIn{Write: in.Write, Keys: []string{k}}
						var o kvOut
						if in.Write {
							one.Vals = []int{in.Vals[j]}
						} else if !lost {
							o.Vals = []int{res.Vals[j]}
						}
						out.Add(check.Piece[kvIn, kvOut]{Keys: one.Keys, In: one, Out: o, Unknown: lost, Write: in.Write, Origin: c})
					}
				}
			}
			return nil
		},
		Init: func(keys []string) kvState { return kvState{keys: keys, vals: make([]int, len(keys))} },
		Step: func(s kvState, op check.Op[kvIn, kvOut]) ([]kvState, string) {
			if op.In.Write {
				n := kvState{keys: s.keys, vals: slices.Clone(s.vals)}
				for i, k := range op.In.Keys {
					n.vals[idx(s, k)] = op.In.Vals[i]
				}
				if op.Unknown {
					return []kvState{s, n}, ""
				}
				return []kvState{n}, ""
			}
			for i, k := range op.In.Keys {
				if got, want := op.Out.Vals[i], s.vals[idx(s, k)]; got != want {
					return nil, fmt.Sprintf("%s is %d, read %d", k, want, got)
				}
			}
			return []kvState{s}, ""
		},
		Equal: func(a, b kvState) bool { return slices.Equal(a.vals, b.vals) && slices.Equal(a.keys, b.keys) },
		Hash: func(s kvState) uint64 {
			var h maphash.Hash
			h.SetSeed(kvSeed)
			for i, v := range s.vals {
				h.WriteString(s.keys[i])
				maphash.WriteComparable(&h, v)
			}
			return h.Sum64()
		},
		DescribeState: func(s kvState) string { return fmt.Sprint(s.keys, s.vals) },
	}
}

// kvCall is one recorded call: a write of vals to keys, or a read of keys that saw vals.
// Times are nanoseconds; lost means no response arrived.
func kvCall(index uint64, client int, start, end int64, write bool, keys []string, vals []int, lost bool) check.Call {
	in := kvIn{Write: write, Keys: keys}
	var out kvOut
	if write {
		in.Vals = vals
	} else {
		out.Vals = vals
	}
	info, _ := json.Marshal(in)
	body, _ := json.Marshal(out)
	step := check.Step{Start: start, End: end, Status: 200, Body: body}
	if lost {
		step = check.Step{Start: start, End: end, Error: "timeout"}
	}
	op := "read"
	if write {
		op = "write"
	}
	return check.Call{Index: index, Client: client, Op: op, Info: info, Start: start, End: end, Steps: []check.Step{step}}
}

func w(i uint64, start, end int64, keys string, vals ...int) check.Call {
	return kvCall(i, -1, start, end, true, strings.Split(keys, ","), vals, false)
}

func r(i uint64, start, end int64, keys string, vals ...int) check.Call {
	return kvCall(i, -1, start, end, false, strings.Split(keys, ","), vals, false)
}

func one(calls ...check.Call) *check.History { return check.NewHistory([][]check.Call{calls}, nil) }

func run(h *check.History, o check.Options, specs ...check.AnySpec) *check.Report {
	return check.Check(h, specs, o)
}

// A read torn across an atomic two-key write fails only when the two keys are checked together:
// atomic pieces join their keys into one partition.
func TestAtomicPiecesJoinTheirKeysIntoOnePartition(t *testing.T) {
	h := one(
		w(0, 0, 10, "a,b", 1, 1),
		r(1, 5, 30, "a,b", 1, 0), // concurrent with the write: per key, each value is possible
		w(2, 0, 5, "c", 7),
	)
	atomic := run(h, check.Options{}, kvSpec("atomic", false))
	if atomic.Verdict != check.Fail || len(atomic.Specs[0].Partitions) != 2 {
		t.Fatalf("atomic: %s with %d partitions, want FAIL with {a,b} and {c}\n%s", atomic.Verdict, len(atomic.Specs[0].Partitions), atomic.Text(5))
	}
	perKey := run(h, check.Options{}, kvSpec("per-key", true))
	if perKey.Verdict != check.Pass || len(perKey.Specs[0].Partitions) != 3 {
		t.Fatalf("per key: %s with %d partitions, want PASS with a, b and c\n%s", perKey.Verdict, len(perKey.Specs[0].Partitions), perKey.Text(5))
	}
	if f := atomic.Specs[0].Failures; len(f) != 1 || !strings.Contains(f[0].Explanation, "b is 1, read 0") || f[0].Timeline == nil {
		t.Fatalf("the failure names the step's reason and has a timeline:\n%s", atomic.Text(5))
	}
}

// Several specs over one history must all hold; a spec that cannot be set up makes the verdict
// UNKNOWN, never PASS.
func TestSpecsAreANDed(t *testing.T) {
	h := one(w(0, 0, 10, "a,b", 1, 1), r(1, 5, 30, "a,b", 1, 0))
	if got := run(h, check.Options{}, kvSpec("per-key", true), kvSpec("atomic", false)).Verdict; got != check.Fail {
		t.Fatalf("one failing spec of two: %s, want FAIL", got)
	}
	broken := kvSpec("broken", true)
	broken.Hash = nil
	rep := run(h, check.Options{}, kvSpec("per-key", true), broken)
	if rep.Verdict != check.Unknown || !strings.Contains(rep.Reason, "no Hash") {
		t.Fatalf("a spec without Hash: %s (%s), want UNKNOWN naming it", rep.Verdict, rep.Reason)
	}
	if rep := run(h, check.Options{}); rep.Verdict != check.Unknown {
		t.Fatalf("no spec at all: %s, want UNKNOWN", rep.Verdict)
	}
}

// hardHistory is n fully concurrent writes of one key, then a read of a value none wrote: to
// reject it the search must try every subset of the writes (about n*2^n states).
func hardHistory(n int, key string, base uint64) []check.Call {
	var calls []check.Call
	for i := range n {
		calls = append(calls, w(base+uint64(i), 0, 100, key, i+1))
	}
	return append(calls, r(base+uint64(n), 0, 100, key, 999))
}

// A search the step budget cuts short leaves its partition undecided: the verdict is UNKNOWN with
// what was and was not covered, the others are still decided, and the result does not depend on
// the machine's speed.
func TestAnUndecidedPartitionIsUnknownWithCoverage(t *testing.T) {
	calls := hardHistory(16, "hot", 0)
	calls = append(calls, w(100, 200, 210, "cold", 1), r(101, 220, 230, "cold", 1))
	o := check.Options{MaxSearchSteps: 5000, Workers: 1}
	rep := run(one(calls...), o, kvSpec("kv", true))
	s := rep.Specs[0]
	if rep.Verdict != check.Unknown || s.Coverage.ByStatus[check.StatusTimeout] != 1 || s.Coverage.ByStatus[check.StatusOK] != 1 {
		t.Fatalf("got %s %v, want UNKNOWN with hot undecided and cold ok\n%s", rep.Verdict, s.Coverage.ByStatus, rep.Text(5))
	}
	if s.Coverage.OpsUnchecked != 17 || s.Coverage.OpsChecked != 2 || !strings.Contains(rep.Reason, `"hot"`) || !strings.Contains(rep.Reason, "MaxSearchSteps") {
		t.Fatalf("coverage and reason: %+v\n%s", s.Coverage, rep.Reason)
	}
	again := run(one(calls...), o, kvSpec("kv", true))
	if again.Specs[0].Partitions[0].Steps != s.Partitions[0].Steps {
		t.Fatalf("the step budget is not deterministic: %d then %d steps", s.Partitions[0].Steps, again.Specs[0].Partitions[0].Steps)
	}
	if full := run(one(hardHistory(10, "hot", 0)...), check.Options{}, kvSpec("kv", true)); full.Verdict != check.Fail {
		t.Fatalf("without a budget a smaller hard history fails: %s", full.Verdict)
	}
}

// Over the memory budget the busiest search is aborted; once the others are done it is retried
// alone, and if it is still over, its partition is undecided. A fake heap that is always over the
// budget makes this deterministic: searches of fewer steps than one budget look still finish.
func TestTheMemoryBudgetAbortsTheBusiestSearchAndRetriesItAlone(t *testing.T) {
	calls := hardHistory(12, "hot", 0)
	calls = append(calls, w(100, 200, 210, "cold", 1), r(101, 220, 230, "cold", 1))
	o := check.Options{Workers: 4, MemoryBytes: 1 << 20, HeapBytes: func() int64 { return 1 << 30 }}
	rep := run(one(calls...), o, kvSpec("kv", true))
	s := rep.Specs[0]
	if rep.Verdict != check.Unknown || s.Coverage.ByStatus[check.StatusMemory] != 1 || s.Coverage.ByStatus[check.StatusOK] != 1 {
		t.Fatalf("got %s %v, want UNKNOWN with hot aborted and cold ok\n%s", rep.Verdict, s.Coverage.ByStatus, rep.Text(5))
	}
	for _, p := range s.Partitions {
		if p.Status == check.StatusMemory && !strings.Contains(p.Reason, "retried alone") {
			t.Fatalf("the aborted partition was not retried alone: %s", p.Reason)
		}
	}
}

// Writes are never sampled; each key keeps at most MaxReadsPerKey reads, the same ones for the
// same seed.
func TestReadSamplingKeepsEveryWriteAndIsSeeded(t *testing.T) {
	var calls []check.Call
	for i := range 50 {
		t0 := int64(i * 100)
		calls = append(calls, w(uint64(3*i), t0, t0+10, "a", i), r(uint64(3*i+1), t0+20, t0+30, "a", i), r(uint64(3*i+2), t0+40, t0+50, "b", 0))
	}
	o := check.Options{MaxReadsPerKey: 7, Seed: 3}
	rep := run(one(calls...), o, kvSpec("kv", true))
	s := rep.Specs[0]
	if rep.Verdict != check.Pass || s.Coverage.ReadsSampledOut != 2*(50-7) || s.Coverage.OpsChecked != 50+2*7 {
		t.Fatalf("got %s, %d sampled out, %d checked; want PASS, 86, 64\n%s", rep.Verdict, s.Coverage.ReadsSampledOut, s.Coverage.OpsChecked, rep.Text(5))
	}
	if all := run(one(calls...), check.Options{MaxReadsPerKey: -1}, kvSpec("kv", true)); all.Specs[0].Coverage.ReadsSampledOut != 0 {
		t.Fatalf("MaxReadsPerKey -1 keeps every read")
	}
}

// Sampling can hide a violation but never invent one: on random histories of a correct store,
// every sample passes; on histories with one wrong read, a sample fails exactly when it keeps
// that read (here: a lone wrong read, so it is the only way to fail).
func TestSamplingOnlyHidesViolations(t *testing.T) {
	for seed := range int64(20) {
		rng := rand.New(rand.NewSource(seed))
		var calls []check.Call
		val, t0 := 0, int64(0)
		for i := range 60 {
			if rng.Intn(3) == 0 {
				val = rng.Intn(100)
				calls = append(calls, w(uint64(i), t0, t0+5, "k", val))
			} else {
				calls = append(calls, r(uint64(i), t0, t0+5, "k", val))
			}
			t0 += 10
		}
		wrong := len(calls) / 2
		for calls[wrong].Op != "read" {
			wrong++
		}
		bad := slices.Clone(calls)
		bad[wrong] = r(bad[wrong].Index, bad[wrong].Start, bad[wrong].End, "k", -1)
		for _, max := range []int{-1, 1, 5, 20} {
			o := check.Options{MaxReadsPerKey: max, Seed: seed + 1}
			if got := run(one(calls...), o, kvSpec("kv", true)).Verdict; got != check.Pass {
				t.Fatalf("seed %d, max %d: a correct history got %s", seed, max, got)
			}
			got := run(one(bad...), o, kvSpec("kv", true))
			if got.Verdict == check.Unknown || (max < 0 && got.Verdict != check.Fail) {
				t.Fatalf("seed %d, max %d: one wrong read got %s", seed, max, got.Verdict)
			}
		}
	}
}

// More unknown outcomes than MaxLostShare allows fail the check (at the check's fixed load,
// losing responses is the candidate's fault); below it they are unknown pieces.
func TestTooManyLostResponsesFail(t *testing.T) {
	var calls []check.Call
	for i := range 100 {
		t0 := int64(i * 100)
		calls = append(calls, kvCall(uint64(i), 0, t0, t0+10, true, []string{"k"}, []int{i}, i%20 == 0))
	}
	rep := run(one(calls...), check.Options{}, kvSpec("kv", true))
	if rep.Verdict != check.Fail || !rep.Specs[0].LostShareExceeded || !strings.Contains(rep.Reason, "MaxLostShare") {
		t.Fatalf("5%% lost with the default 1%% limit: %s (%s), want FAIL naming MaxLostShare", rep.Verdict, rep.Reason)
	}
	if rep := run(one(calls...), check.Options{MaxLostShare: 0.1}, kvSpec("kv", true)); rep.Verdict != check.Pass || rep.Specs[0].Coverage.UnknownPieces != 5 {
		t.Fatalf("5%% lost with a 10%% limit: %s, %d unknown pieces, want PASS and 5", rep.Verdict, rep.Specs[0].Coverage.UnknownPieces)
	}
}

// A response wrong on its own fails the check without a search, and projection stops after the
// first phase that has one.
func TestViolationsFailWithoutASearchAndStopTheProjection(t *testing.T) {
	bad := w(1, 20, 30, "a", 1)
	bad.Steps[0].Body = []byte("<html>")
	h := check.NewHistory([][]check.Call{{w(0, 0, 10, "a", 1), bad}, {r(0, 100, 110, "a", 1)}}, nil)
	rep := run(h, check.Options{}, kvSpec("kv", true))
	s := rep.Specs[0]
	if rep.Verdict != check.Fail || len(s.Violations) != 1 || len(s.Partitions) != 0 || s.Coverage.Calls != 2 {
		t.Fatalf("got %s, %d violations, %d partitions, %d calls read; want FAIL, 1, 0, 2\n%s", rep.Verdict, len(s.Violations), len(s.Partitions), s.Coverage.Calls, rep.Text(5))
	}
	if s.Violations[0].Call.Steps[0].Body != nil {
		t.Fatalf("a violation's call keeps no body")
	}
}

// A fault while an op was in flight is a capture problem: UNKNOWN.
func TestAFaultMustBeQuiescent(t *testing.T) {
	h := check.NewHistory([][]check.Call{{w(0, 0, 10, "a", 1)}, {r(0, 100, 110, "a", 1)}}, []check.Fault{{Kind: "restart", Start: 5, End: 50}})
	spec := kvSpec("kv", true)
	spec.Fault = func(s kvState, _ check.Fault) []kvState { return []kvState{s} }
	if rep := run(h, check.Options{}, spec); rep.Verdict != check.Unknown || !strings.Contains(rep.Reason, "not quiescent") {
		t.Fatalf("got %s (%s), want UNKNOWN naming the fault", rep.Verdict, rep.Reason)
	}
}

// A fault the model says loses state is checked like any op: a read after a restart that lost a
// write passes only if the model says restarts lose it.
func TestFaultsStepTheModel(t *testing.T) {
	h := check.NewHistory([][]check.Call{{w(0, 0, 10, "a", 1)}, {r(0, 100, 110, "a", 0)}}, []check.Fault{{Kind: "restart", Start: 50, End: 60, AfterPhase: 0}})
	durable := kvSpec("durable", true)
	if rep := run(h, check.Options{}, durable); rep.Verdict != check.Fail {
		t.Fatalf("a lost write with no Fault (durable): %s, want FAIL", rep.Verdict)
	}
	volatile := kvSpec("volatile", true)
	volatile.Fault = func(s kvState, _ check.Fault) []kvState {
		return []kvState{{keys: s.keys, vals: make([]int, len(s.vals))}}
	}
	if rep := run(h, check.Options{}, volatile); rep.Verdict != check.Pass {
		t.Fatalf("a lost write when restarts forget: %s, want PASS\n%s", rep.Verdict, rep.Text(5))
	}
}

// Many ops in flight on one key get a warning: a search that must reject such a history is
// exponential in them.
func TestManyOpsInFlightOnOneKeyWarn(t *testing.T) {
	var calls []check.Call
	for i := range 20 {
		calls = append(calls, w(uint64(i), int64(i), 1000, "k", 5))
	}
	rep := run(one(calls...), check.Options{}, kvSpec("kv", true))
	if rep.Verdict != check.Pass || rep.Specs[0].MaxInFlight != 20 || len(rep.Specs[0].Warnings) == 0 {
		t.Fatalf("got %s, max in flight %d, warnings %v", rep.Verdict, rep.Specs[0].MaxInFlight, rep.Specs[0].Warnings)
	}
}

// A partition with more ops than MaxOpsPerPartition is not searched.
func TestTooLargeAPartitionIsNotSearched(t *testing.T) {
	var calls []check.Call
	for i := range 30 {
		calls = append(calls, w(uint64(i), int64(10*i), int64(10*i+5), "k", i))
	}
	rep := run(one(calls...), check.Options{MaxOpsPerPartition: 10}, kvSpec("kv", true))
	if p := rep.Specs[0].Partitions[0]; rep.Verdict != check.Unknown || p.Status != check.StatusTooLarge || p.Steps != 0 {
		t.Fatalf("got %s %s after %d steps, want UNKNOWN too-large with no search", rep.Verdict, p.Status, p.Steps)
	}
}
