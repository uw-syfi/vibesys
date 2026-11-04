package core_test

import (
	"math/rand/v2"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A multi-objective task like the serving benchmark it was built for: output throughput, scored
// and maximized, and two latencies, minimized. The floor makes a verdict on constant samples.
var serving = core.Objectives{
	Metrics: []core.Metric{
		{Name: "output_tok_s", Direction: core.Maximize},
		{Name: "ttft_p50", Direction: core.Minimize},
		{Name: "tpot_p50", Direction: core.Minimize},
	},
	RelativeNoise: 0.01,
}

// paretoMeasurement is a measurement of the serving task whose objectives hold the values given,
// in the order of serving.Metrics, five samples of each.
func paretoMeasurement(job string, commit core.Commit, values []float64, minute int, options ...func(*core.Result)) core.Result {
	five := func(v float64) []float64 { return []float64{v, v, v, v, v} }
	r := measurement(job, commit, five(values[0]), minute, func(r *core.Result) {
		o := r.Outcome.(core.MeasureOutcome)
		o.Metric = serving.Metrics[0]
		o.Objectives = serving
		for i, m := range serving.Metrics[1:] {
			o.Observations = append(o.Observations, core.Observation{Name: m.Name, Values: five(values[i+1])})
		}
		r.Outcome = o
	})
	for _, option := range options {
		option(&r)
	}
	return r
}

// gridPoint is a vector of objective values on a grid whose steps are far apart against the noise
// floor, so the verdict on each objective is the order of the two values.
func gridPoint(r *rand.Rand) []float64 {
	return []float64{float64(100 * (1 + r.IntN(3))), float64(100 * (1 + r.IntN(3))), float64(100 * (1 + r.IntN(3)))}
}

// orientedOrder is how a value of one objective compares with another: positive when it is better.
func orientedOrder(d core.Direction, a, b float64) int {
	switch {
	case a == b:
		return 0
	case (a > b) == (d == core.Maximize):
		return 1
	}
	return -1
}

// dominance is the exact Pareto relation of two grid points: better when a is better on some
// objective and worse on none, and so on.
func dominance(a, b []float64) core.VerdictKind {
	var better, worse bool
	for i, m := range serving.Metrics {
		switch orientedOrder(m.Direction, a[i], b[i]) {
		case 1:
			better = true
		case -1:
			worse = true
		}
	}
	switch {
	case better && worse:
		return core.Tradeoff
	case better:
		return core.Better
	case worse:
		return core.Worse
	}
	return core.WithinNoise
}

// The overall verdict is a function of the set of objective verdicts: inconclusive when any is,
// with the first one's reason, and otherwise the Pareto relation of the kinds.
func TestCombineObjectivesIsTheParetoRelationOfTheKinds(t *testing.T) {
	kinds := []core.VerdictKind{core.Better, core.Worse, core.WithinNoise, core.Inconclusive}
	reasons := []core.InconclusiveReason{core.ReasonTooFewSamples, core.ReasonConstantSamples}
	r := newRand(91)
	for range properties {
		verdicts := make([]core.ObjectiveVerdict, 1+r.IntN(5))
		count := map[core.VerdictKind]int{}
		firstReason := core.ReasonNone
		for i := range verdicts {
			v := core.Verdict{Kind: kinds[r.IntN(len(kinds))]}
			if v.Kind == core.Inconclusive {
				v.Reason = reasons[r.IntN(len(reasons))]
				if firstReason == core.ReasonNone {
					firstReason = v.Reason
				}
			}
			count[v.Kind]++
			verdicts[i] = core.ObjectiveVerdict{Verdict: v}
		}
		want, wantReason := core.WithinNoise, core.ReasonNone
		switch {
		case count[core.Inconclusive] > 0:
			want, wantReason = core.Inconclusive, firstReason
		case count[core.Better] > 0 && count[core.Worse] > 0:
			want = core.Tradeoff
		case count[core.Better] > 0:
			want = core.Better
		case count[core.Worse] > 0:
			want = core.Worse
		}
		kind, reason := core.CombineObjectives(verdicts)
		if kind != want || reason != wantReason {
			t.Fatalf("%+v: %s %q, want %s %q", verdicts, kind, reason, want, wantReason)
		}
		shuffled := slices.Clone(verdicts)
		r.Shuffle(len(shuffled), func(i, j int) { shuffled[i], shuffled[j] = shuffled[j], shuffled[i] })
		if again, _ := core.CombineObjectives(shuffled); again != kind {
			t.Fatalf("the kind depends on the order of the objectives: %s and %s", kind, again)
		}
	}
}

// A multi-objective comparison gives a verdict on every objective, in the task's order, each the
// verdict Compare gives on that objective's samples, and an overall verdict that is the Pareto
// relation of the two commits.
func TestAParetoComparisonJudgesEveryObjective(t *testing.T) {
	r := newRand(92)
	for range properties {
		c, b := gridPoint(r), gridPoint(r)
		got := compareCommits(t, []core.Result{
			paretoMeasurement("base", baseCommit, b, 1),
			paretoMeasurement("cand", candidateCommit, c, 2),
		}, 0)
		if got.Verdict.Kind != dominance(c, b) {
			t.Fatalf("%v against %v: %s, want %s", c, b, got.Verdict.Kind, dominance(c, b))
		}
		if len(got.Objectives) != len(serving.Metrics) {
			t.Fatalf("%d objective verdicts, want %d", len(got.Objectives), len(serving.Metrics))
		}
		for i, ov := range got.Objectives {
			m := serving.Metrics[i]
			five := func(v float64) []float64 { return []float64{v, v, v, v, v} }
			want := mustCompare(t, core.CompareInput{Direction: m.Direction, Candidate: five(c[i]), Base: five(b[i]), RelativeNoise: serving.RelativeNoise})
			if ov.Metric != m || ov.Verdict != want {
				t.Fatalf("objective %d: %+v, want %+v of %+v", i, ov, want, m)
			}
		}
		v := got.Verdict
		if v.CandidateSamples != 5 || v.BaseSamples != 5 || v.Ratio != 0 || v.Noise != 0 || v.CandidateMean != 0 {
			t.Fatalf("the overall verdict carries numbers no one objective has: %+v", v)
		}
	}
}

// Regression for single-metric tasks: a measurement that holds no objectives is compared on its one
// metric exactly as before, whatever observations it keeps beside it, and gives no objective
// verdicts.
func TestASingleMetricComparisonIgnoresObservations(t *testing.T) {
	r := newRand(93)
	for range properties {
		cs, bs := anySamples(r), anySamples(r)
		plain := sides(cs, bs)
		observed := sides(cs, bs)
		for i := range observed {
			o := observed[i].Outcome.(core.MeasureOutcome)
			o.Observations = []core.Observation{{Name: "latency", Values: anySamples(r)}}
			observed[i].Outcome = o
		}
		want, wantErr := core.CompareCommits(core.CompareRequest{Commit: candidateCommit, Against: baseCommit, Results: plain})
		got, err := core.CompareCommits(core.CompareRequest{Commit: candidateCommit, Against: baseCommit, Results: observed})
		if failureKind(err) != failureKind(wantErr) || got.Verdict != want.Verdict || len(got.Objectives) != 0 {
			t.Fatalf("%+v, %v; want %+v, %v", got, err, want, wantErr)
		}
		if got.Verdict.Kind == core.Tradeoff {
			t.Fatal("a single metric is never a tradeoff")
		}
	}
}

func TestAParetoComparisonNeedsEveryObjectiveAndTheSameObjectives(t *testing.T) {
	cand := paretoMeasurement("cand", candidateCommit, []float64{200, 100, 100}, 2)
	o := cand.Outcome.(core.MeasureOutcome)
	o.Observations = o.Observations[:1]
	cand.Outcome = o
	_, err := core.CompareCommits(core.CompareRequest{Commit: candidateCommit, Against: baseCommit, Results: []core.Result{
		paretoMeasurement("base", baseCommit, []float64{100, 100, 100}, 1), cand,
	}})
	if failureKind(err) != core.FailureNotMeasured {
		t.Fatalf("a measurement without an objective's samples: %v, want not measured", err)
	}

	fewer := paretoMeasurement("cand", candidateCommit, []float64{200, 100, 100}, 2, func(r *core.Result) {
		o := r.Outcome.(core.MeasureOutcome)
		o.Objectives = core.Objectives{Metrics: serving.Metrics[:2], RelativeNoise: serving.RelativeNoise}
		r.Outcome = o
	})
	for _, other := range []core.Result{fewer, measurement("cand", candidateCommit, clearWin, 2)} {
		got := compareCommits(t, []core.Result{paretoMeasurement("base", baseCommit, []float64{100, 100, 100}, 1), other}, 0)
		if got.Verdict.Kind != core.Inconclusive || got.Verdict.Reason != core.ReasonMetricMismatch {
			t.Fatalf("measurements judged on other objectives: %+v, want inconclusive with a metric mismatch", got.Verdict)
		}
	}
}

// A multi-objective candidate is promoted only when it is better overall; a tradeoff is refused as
// such, with the verdict on every objective.
func TestParetoPromotionNeedsAnOverallWin(t *testing.T) {
	r := newRand(94)
	for range properties {
		c, b := gridPoint(r), gridPoint(r)
		req := ready()
		req.Results = []core.Result{
			paretoMeasurement("base", baseCommit, b, 1),
			paretoMeasurement("cand", candidateCommit, c, 2),
		}
		got := decide(t, req)
		want := map[core.VerdictKind]core.PromoteRefusalKind{
			core.Worse: core.RefusalWorse, core.WithinNoise: core.RefusalWithinNoise, core.Tradeoff: core.RefusalTradeoff,
		}[dominance(c, b)]
		switch d := got.(type) {
		case core.Approved:
			if want != "" {
				t.Fatalf("%v against %v promoted, want %s", c, b, want)
			}
			if p := d.Promotion(); len(p.Objectives) != len(serving.Metrics) || p.Verdict.Kind != core.Better {
				t.Fatalf("the promotion does not carry the objectives it won on: %+v", p)
			}
		case core.PromoteRefusal:
			if kinds := kindsOf(t, d); !slices.Equal(kinds, []core.PromoteRefusalKind{want}) {
				t.Fatalf("%v against %v: %v, want %s", c, b, kinds, want)
			}
			if reason := d.Reasons[0]; len(reason.Objectives) != len(serving.Metrics) || reason.Verdict == nil {
				t.Fatalf("the refusal does not carry its evidence: %+v", reason)
			}
		}
	}
}
