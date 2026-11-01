package wire_test

import (
	"fmt"
	"math"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

func commitN(n int) core.Commit { return core.Commit(fmt.Sprintf("%040x", n)) }

func anyVerdict(r interface {
	Float64() float64
	IntN(int) int
}) core.Verdict {
	kinds := []core.VerdictKind{core.Better, core.Worse, core.WithinNoise, core.Inconclusive}
	v := core.Verdict{
		Kind:             kinds[r.IntN(len(kinds))],
		CandidateSamples: uint32(r.IntN(20)), BaseSamples: uint32(r.IntN(20)),
		CandidateMean: r.Float64() * 1e6, BaseMean: r.Float64() * 1e6, Noise: r.Float64(),
	}
	v.Ratio = v.CandidateMean / v.BaseMean
	if v.Kind == core.Inconclusive {
		reasons := []core.InconclusiveReason{core.ReasonTooFewSamples, core.ReasonConstantSamples, core.ReasonMetricMismatch, core.ReasonTaskVersionMismatch}
		v.Reason = reasons[r.IntN(len(reasons))]
	}
	return v
}

// A verdict goes over the wire as it is: the same bits in every number, never rounded, and a
// kind and a reason the contract names.
func TestVerdictCrossesTheWireUnrounded(t *testing.T) {
	for seed := uint64(0); seed < 300; seed++ {
		v := anyVerdict(newRand(seed))
		got := wire.Verdict(v)
		same := func(a, b float64) bool { return math.Float64bits(a) == math.Float64bits(b) }
		if !same(got.GetRatio(), v.Ratio) || !same(got.GetNoise(), v.Noise) || !same(got.GetCandidateMean(), v.CandidateMean) ||
			!same(got.GetBaseMean(), v.BaseMean) || got.GetCandidateSamples() != v.CandidateSamples || got.GetBaseSamples() != v.BaseSamples {
			t.Fatalf("seed %d: %+v became %+v", seed, v, got)
		}
		if got.GetKind() == cliv1.VerdictKind_VERDICT_KIND_UNSPECIFIED {
			t.Fatalf("seed %d: kind %q has no wire value", seed, v.Kind)
		}
		if (v.Kind == core.Inconclusive) != (got.GetReason() != cliv1.InconclusiveReason_INCONCLUSIVE_REASON_UNSPECIFIED) {
			t.Fatalf("seed %d: kind %q with reason %q became reason %s", seed, v.Kind, v.Reason, got.GetReason())
		}
	}
}

// Every refusal kind and every reason `result` can give has a wire value of its own, and a
// refusal keeps each reason in order with the evidence it carries.
func TestRefusalsCarryTheirReasonsAndEvidence(t *testing.T) {
	kinds := []core.PromoteRefusalKind{
		core.RefusalUnmeasured, core.RefusalCheckMissing, core.RefusalWorse, core.RefusalWithinNoise,
		core.RefusalInconclusive, core.RefusalStale, core.RefusalEnvironmentMismatch,
	}
	seen := map[cliv1.PromoteRefusalKind]bool{}
	var refusal core.PromoteRefusal
	for i, k := range kinds {
		reason := core.PromoteRefusalReason{Kind: k, Message: "m" + string(k)}
		switch k {
		case core.RefusalWorse, core.RefusalWithinNoise, core.RefusalInconclusive:
			v := anyVerdict(newRand(uint64(i)))
			reason.Verdict = &v
		case core.RefusalStale:
			reason.MeasuredAgainst, reason.CurrentHead = commitN(1), commitN(2)
		}
		refusal.Reasons = append(refusal.Reasons, reason)
	}
	got := wire.PromoteResponse(refusal).GetRefused()
	if len(got.GetReasons()) != len(kinds) {
		t.Fatalf("%d reasons became %d", len(kinds), len(got.GetReasons()))
	}
	for i, r := range got.GetReasons() {
		want := refusal.Reasons[i]
		if r.GetKind() == cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_UNSPECIFIED || seen[r.GetKind()] {
			t.Errorf("kind %q became %s, which is unnamed or shared", want.Kind, r.GetKind())
		}
		seen[r.GetKind()] = true
		if r.GetMessage() != want.Message || r.GetMeasuredAgainst() != string(want.MeasuredAgainst) || r.GetCurrentHead() != string(want.CurrentHead) {
			t.Errorf("reason %d = %+v, want %+v", i, r, want)
		}
		if (want.Verdict != nil) != (r.GetVerdict() != nil) || (want.Verdict != nil && r.GetVerdict().GetRatio() != want.Verdict.Ratio) {
			t.Errorf("reason %d verdict = %+v, want %+v", i, r.GetVerdict(), want.Verdict)
		}
	}

	for _, reason := range []core.NoCurrentResultReason{core.NoHead, core.NotMeasuredSincePromotion} {
		got := wire.CurrentResultResponse(core.NoCurrentResult{Reason: reason, Head: commitN(3)}).GetNone()
		if got.GetReason() == cliv1.NoCurrentResultReason_NO_CURRENT_RESULT_REASON_UNSPECIFIED || got.GetHead() != string(commitN(3)) {
			t.Errorf("reason %q became %+v", reason, got)
		}
	}
}

func TestResultsCrossTheWireWithEverythingTheyRecorded(t *testing.T) {
	for seed := uint64(0); seed < 100; seed++ {
		r := newRand(seed)
		fp := core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux"}}
		res := core.Result{
			Job: "j3", Key: "k", Task: "kv", TaskVersion: "v2", Commit: commitN(int(seed)), SchemaVersion: core.SchemaVersion,
			Environment: fp, Machine: core.MachineFacts{HostID: "h", OS: "linux", Arch: "amd64", CPUModel: "m", LogicalCPUs: 8, MemoryBytes: 1 << 33},
			Load:       core.MachineLoad{SampledAt: epoch, LoadAverage1m: r.Float64(), CPUBusyFraction: r.Float64(), MemoryAvailableBytes: 7},
			RecordedAt: epoch.Add(5),
		}
		samples := []float64{r.Float64() * 1e9, r.Float64(), math.SmallestNonzeroFloat64}
		if seed%2 == 0 {
			res.Outcome = core.MeasureOutcome{
				Metric: core.Metric{Name: "ops", Direction: core.Minimize}, Samples: samples, CheckJob: "j2",
				Observations: []core.Observation{{Name: "latency", Values: []float64{1.5, 2.5}}},
			}
		} else {
			res.Outcome = core.CheckOutcome{Duration: 1500 * 1e6}
		}
		got := wire.Result(res)
		if got.GetJobId() != "j3" || got.GetKey() != "k" || got.GetTask() != "kv" || got.GetTaskVersion() != "v2" ||
			got.GetCommit() != string(res.Commit) || got.GetSchemaVersion() != core.SchemaVersion ||
			got.GetEnvironment().GetFacts()["os"] != "linux" || got.GetMachine().GetCpuModel() != "m" || got.GetMachine().GetMemoryBytes() != 1<<33 ||
			got.GetLoad().GetMemoryAvailableBytes() != 7 || got.GetLoad().GetLoadAverage_1M() != res.Load.LoadAverage1m ||
			!got.GetRecordedAt().AsTime().Equal(res.RecordedAt) {
			t.Fatalf("seed %d: %+v became %+v", seed, res, got)
		}
		if m, ok := res.Outcome.(core.MeasureOutcome); ok {
			g := got.GetMeasure()
			if g == nil || g.GetCheckJobId() != "j2" || g.GetMetric().GetDirection() != cliv1.Direction_DIRECTION_MINIMIZE || len(g.GetSamples()) != len(m.Samples) ||
				len(g.GetObservations()) != 1 || g.GetObservations()[0].GetValues()[1] != 2.5 {
				t.Fatalf("seed %d: measurement became %+v", seed, g)
			}
			for i := range samples {
				if math.Float64bits(g.GetSamples()[i]) != math.Float64bits(samples[i]) {
					t.Fatalf("seed %d: sample %d changed", seed, i)
				}
			}
		} else if got.GetCheck() == nil || got.GetCheck().GetDuration().AsDuration() != res.Outcome.(core.CheckOutcome).Duration {
			t.Fatalf("seed %d: check became %+v", seed, got.GetCheck())
		}
	}
}
