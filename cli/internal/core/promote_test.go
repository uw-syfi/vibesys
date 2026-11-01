package core_test

import (
	"math"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var (
	clearWin  = []float64{120, 121, 119, 122, 120}
	baseline  = []float64{100, 101, 99, 100, 102}
	promoteAt = at(100)
)

// ready is a request that is promoted: a clear win, measured after nothing has moved, resting on
// a check that ran in its own job.
func ready() core.PromotionRequest {
	return core.PromotionRequest{
		Candidate: candidateCommit,
		Head:      baseCommit,
		Results:   sides(clearWin, baseline),
		At:        promoteAt,
	}
}

func decide(t testing.TB, req core.PromotionRequest) core.PromotionDecision {
	t.Helper()
	got, err := core.DecidePromotion(req)
	if err != nil {
		t.Fatalf("DecidePromotion: %v", err)
	}
	return got
}

func kindsOf(t testing.TB, decision core.PromotionDecision) []core.PromoteRefusalKind {
	t.Helper()
	refusal, ok := decision.(core.PromoteRefusal)
	if !ok {
		t.Fatalf("%+v, want a refusal", decision)
	}
	var kinds []core.PromoteRefusalKind
	for _, reason := range refusal.Reasons {
		kinds = append(kinds, reason.Kind)
	}
	return kinds
}

func TestAClearWinIsPromoted(t *testing.T) {
	req := ready()
	got, ok := decide(t, req).(core.Promotion)
	if !ok {
		t.Fatalf("%+v, want a promotion", got)
	}
	if got.Commit != candidateCommit || got.PreviousHead != "" || got.ResultJob != "cand" || !got.At.Equal(promoteAt) || !got.Environment.Equal(hostEnv) {
		t.Fatalf("%+v", got)
	}
	if got.Verdict.Kind != core.Better || got.Verdict.CandidateSamples != 5 || got.Verdict.BaseSamples != 5 {
		t.Fatalf("verdict %+v", got.Verdict)
	}
	comparison := compareCommits(t, req.Results, 0)
	if got.Verdict != comparison.Verdict {
		t.Fatalf("the promotion rests on %+v, CompareCommits says %+v", got.Verdict, comparison.Verdict)
	}
}

func TestAPromotionAfterAnotherNamesTheHeadItReplaces(t *testing.T) {
	req := ready()
	req.Promotions = []core.Promotion{{Commit: baseCommit, At: at(0)}}
	got, ok := decide(t, req).(core.Promotion)
	if !ok || got.PreviousHead != baseCommit {
		t.Fatalf("%+v, want a promotion that replaces %s", got, baseCommit)
	}
}

func TestThePromotionRestsOnTheLatestMeasurement(t *testing.T) {
	req := ready()
	req.Results = append(req.Results, measurement("later", candidateCommit, []float64{121, 120}, 7), measurement("earlier", candidateCommit, []float64{121, 120}, 0))
	got, ok := decide(t, req).(core.Promotion)
	if !ok || got.ResultJob != "later" {
		t.Fatalf("%+v, want a promotion resting on the latest measurement", got)
	}
}

func TestRefusalsNameEveryReasonInOrder(t *testing.T) {
	stale := []core.Promotion{{Commit: baseCommit, PreviousHead: commitN(7), At: at(2)}}
	cases := []struct {
		name   string
		change func(*core.PromotionRequest)
		want   []core.PromoteRefusalKind
	}{
		{"the candidate was never measured", func(r *core.PromotionRequest) { r.Results = r.Results[:1] }, []core.PromoteRefusalKind{core.RefusalUnmeasured}},
		{"the head was never measured", func(r *core.PromotionRequest) { r.Results = r.Results[1:] }, []core.PromoteRefusalKind{core.RefusalUnmeasured}},
		{"neither was", func(r *core.PromotionRequest) { r.Results = nil }, []core.PromoteRefusalKind{core.RefusalUnmeasured, core.RefusalUnmeasured}},
		{"worse", func(r *core.PromotionRequest) { r.Results = sides(baseline, clearWin) }, []core.PromoteRefusalKind{core.RefusalWorse}},
		{"within noise", func(r *core.PromotionRequest) { r.Results = sides(baseline, []float64{100, 101, 100, 99, 102}) }, []core.PromoteRefusalKind{core.RefusalWithinNoise}},
		{"too few samples to say", func(r *core.PromotionRequest) { r.Results = sides([]float64{120}, baseline) }, []core.PromoteRefusalKind{core.RefusalInconclusive}},
		{"a declared floor settles one sample", func(r *core.PromotionRequest) { r.Results = sides([]float64{120}, baseline); r.RelativeNoise = 0.01 }, nil},
		{"constant samples", func(r *core.PromotionRequest) { r.Results = sides([]float64{5, 5, 5}, []float64{4, 4, 4}) }, []core.PromoteRefusalKind{core.RefusalInconclusive}},
		{"a changed task", func(r *core.PromotionRequest) { underTask("t2")(&r.Results[1]) }, []core.PromoteRefusalKind{core.RefusalInconclusive}},
		{"a changed metric", func(r *core.PromotionRequest) {
			scoring(core.Metric{Name: "p99", Direction: core.Minimize})(&r.Results[1])
		}, []core.PromoteRefusalKind{core.RefusalInconclusive}},
		{"the candidate names no check", func(r *core.PromotionRequest) { checkedBy("")(&r.Results[1]) }, []core.PromoteRefusalKind{core.RefusalCheckMissing}},
		{"the check it names is not saved", func(r *core.PromotionRequest) { checkedBy("gone")(&r.Results[1]) }, []core.PromoteRefusalKind{core.RefusalCheckMissing}},
		{"the check passed another commit", func(r *core.PromotionRequest) {
			checkedBy("ck")(&r.Results[1])
			r.Results = append(r.Results, checkPass("ck", commitN(9), 1))
		}, []core.PromoteRefusalKind{core.RefusalCheckMissing}},
		{"the check ran under another task version", func(r *core.PromotionRequest) {
			checkedBy("ck")(&r.Results[1])
			r.Results = append(r.Results, checkPass("ck", candidateCommit, 1, underTask("t0")))
		}, []core.PromoteRefusalKind{core.RefusalCheckMissing}},
		{"the check ran in another environment", func(r *core.PromotionRequest) {
			checkedBy("ck")(&r.Results[1])
			r.Results = append(r.Results, checkPass("ck", candidateCommit, 1, inEnvironment(dockerEnv)))
		}, []core.PromoteRefusalKind{core.RefusalCheckMissing}},
		{"a measurement is not a check", func(r *core.PromotionRequest) {
			checkedBy("m2")(&r.Results[1])
			r.Results = append(r.Results, measurement("m2", candidateCommit, clearWin, 1))
		}, []core.PromoteRefusalKind{core.RefusalCheckMissing}},
		{"the branch moved after the measurement", func(r *core.PromotionRequest) { r.Promotions = []core.Promotion{{Commit: baseCommit, At: at(3)}} }, []core.PromoteRefusalKind{core.RefusalStale}},
		{"the branch moved at the instant of the measurement", func(r *core.PromotionRequest) { r.Promotions = []core.Promotion{{Commit: baseCommit, At: at(2)}} }, []core.PromoteRefusalKind{core.RefusalStale}},
		{"the branch moved before the measurement", func(r *core.PromotionRequest) { r.Promotions = []core.Promotion{{Commit: baseCommit, At: at(1)}} }, nil},
		{"a top-up after the move does not freshen the samples from before it", func(r *core.PromotionRequest) {
			r.Results = append(r.Results, measurement("topup", candidateCommit, clearWin, 9))
			r.Promotions = []core.Promotion{{Commit: baseCommit, At: at(5)}}
		}, []core.PromoteRefusalKind{core.RefusalStale}},
		{"another environment", func(r *core.PromotionRequest) { inEnvironment(dockerEnv)(&r.Results[1]) }, []core.PromoteRefusalKind{core.RefusalEnvironmentMismatch}},
		{"two environments on the candidate side", func(r *core.PromotionRequest) {
			r.Results = append(r.Results, measurement("c2", candidateCommit, clearWin, 2, inEnvironment(dockerEnv)))
		}, []core.PromoteRefusalKind{core.RefusalEnvironmentMismatch}},
		{"worse, stale and unchecked together", func(r *core.PromotionRequest) {
			r.Results = sides(baseline, clearWin)
			checkedBy("")(&r.Results[1])
			r.Promotions = stale
		}, []core.PromoteRefusalKind{core.RefusalCheckMissing, core.RefusalWorse, core.RefusalStale}},
		{"stale in another environment: no verdict on a mix", func(r *core.PromotionRequest) {
			r.Results = sides(baseline, clearWin)
			inEnvironment(dockerEnv)(&r.Results[1])
			r.Promotions = stale
		}, []core.PromoteRefusalKind{core.RefusalStale, core.RefusalEnvironmentMismatch}},
	}
	for _, c := range cases {
		req := ready()
		c.change(&req)
		got := decide(t, req)
		if c.want == nil {
			if _, ok := got.(core.Promotion); !ok {
				t.Errorf("%s: %+v, want a promotion", c.name, got)
			}
			continue
		}
		if kinds := kindsOf(t, got); !slices.Equal(kinds, c.want) {
			t.Errorf("%s: %v, want %v", c.name, kinds, c.want)
		}
	}
}

func TestRefusalsCarryTheirEvidence(t *testing.T) {
	// A verdict refusal carries the verdict.
	req := ready()
	req.Results = sides(baseline, clearWin)
	refusal := decide(t, req).(core.PromoteRefusal)
	if v := refusal.Reasons[0].Verdict; v == nil || v.Kind != core.Worse || v.Ratio >= 1 {
		t.Errorf("worse: %+v, want the verdict that says so", refusal.Reasons[0])
	}
	// A stale refusal names the head it was measured against and the head now.
	req = ready()
	req.Promotions = []core.Promotion{
		{Commit: commitN(5), PreviousHead: commitN(4), At: at(0)},
		{Commit: baseCommit, PreviousHead: commitN(5), At: at(3)},
		{Commit: commitN(8), PreviousHead: baseCommit, At: at(4)},
	}
	req.Head = commitN(8)
	req.Results = append(req.Results, measurement("h", commitN(8), baseline, 1))
	reason := decide(t, req).(core.PromoteRefusal).Reasons[0]
	if reason.Kind != core.RefusalStale || reason.MeasuredAgainst != commitN(5) || reason.CurrentHead != commitN(8) {
		t.Errorf("stale: %+v, want it measured against the head the first later move replaced", reason)
	}
	// A reason that is not about a verdict carries none, and every reason carries a message.
	if reason.Verdict != nil || reason.Message == "" {
		t.Errorf("stale: %+v, want a message and no verdict", reason)
	}
}

func TestPromotionNeedsATimeAndComparableSamples(t *testing.T) {
	req := ready()
	req.At = time.Time{}
	if _, err := core.DecidePromotion(req); failureKind(err) != core.FailureInvalidInput {
		t.Errorf("no time: %v, want an invalid_input failure", err)
	}
	for name, samples := range map[string][]float64{
		"a missing number":  {120, math.NaN(), 119},
		"an infinite value": {120, math.Inf(1), 119},
	} {
		req := ready()
		req.Results = sides(samples, baseline)
		if got, err := core.DecidePromotion(req); failureKind(err) != core.FailureInvalidInput || got != nil {
			t.Errorf("%s: %+v, %v, want an invalid_input failure and no decision", name, got, err)
		}
	}
	req = ready()
	req.Results = sides(clearWin, []float64{0, 0, 0})
	if _, err := core.DecidePromotion(req); failureKind(err) != core.FailureInvalidInput {
		t.Errorf("a head with mean zero: %v, want an invalid_input failure", err)
	}
}

// The decision is the sum of its rules. A model that states each rule separately, with Compare as
// the oracle for the verdict, must give the same reasons for any mix of the facts.
func TestDecisionMatchesTheRulesOverRandomScenarios(t *testing.T) {
	r := newRand(43)
	for range properties {
		cand, base := anySamples(r), anySamples(r)
		floor := 0.0
		if r.IntN(3) == 0 {
			floor = r.Float64() * 0.1
		}
		measuredAt := 20 + r.IntN(20)
		req := core.PromotionRequest{
			Candidate: candidateCommit, Head: baseCommit, At: promoteAt, RelativeNoise: floor,
			Results: []core.Result{
				measurement("base", baseCommit, base, 1),
				measurement("cand", candidateCommit, cand, measuredAt),
			},
		}
		uncheckedCandidate, mixed := r.IntN(4) == 0, r.IntN(4) == 0
		if uncheckedCandidate {
			checkedBy("")(&req.Results[1])
		}
		if mixed {
			inEnvironment(dockerEnv)(&req.Results[1])
		}
		var movedTimes []int
		for range r.IntN(3) {
			movedTimes = append(movedTimes, r.IntN(60))
		}
		slices.Sort(movedTimes)
		for i, m := range movedTimes {
			req.Promotions = append(req.Promotions, core.Promotion{Commit: commitN(50 + i), PreviousHead: commitN(40 + i), At: at(m)})
		}

		var want []core.PromoteRefusalKind
		var wantErr error
		if uncheckedCandidate {
			want = append(want, core.RefusalCheckMissing)
		}
		if !mixed {
			verdict, err := core.Compare(core.CompareInput{Direction: core.Maximize, Candidate: cand, Base: base, RelativeNoise: floor})
			wantErr = err
			if kind, refused := map[core.VerdictKind]core.PromoteRefusalKind{
				core.Worse: core.RefusalWorse, core.WithinNoise: core.RefusalWithinNoise, core.Inconclusive: core.RefusalInconclusive,
			}[verdict.Kind]; err == nil && refused {
				want = append(want, kind)
			}
		}
		if len(movedTimes) > 0 && movedTimes[len(movedTimes)-1] >= measuredAt {
			want = append(want, core.RefusalStale)
		}
		if mixed {
			want = append(want, core.RefusalEnvironmentMismatch)
		}

		got, err := core.DecidePromotion(req)
		if failureKind(err) != failureKind(wantErr) {
			t.Fatalf("error %v, want %v (%+v)", err, wantErr, req)
		}
		if err != nil {
			continue
		}
		if len(want) == 0 {
			p, ok := got.(core.Promotion)
			if !ok {
				t.Fatalf("%+v, want a promotion", got)
			}
			wantPrevious := core.Commit("")
			if len(req.Promotions) > 0 {
				wantPrevious = baseCommit
			}
			if p.Commit != candidateCommit || p.PreviousHead != wantPrevious || p.ResultJob != "cand" {
				t.Fatalf("%+v", p)
			}
			continue
		}
		if kinds := kindsOf(t, got); !slices.Equal(kinds, want) {
			t.Fatalf("%v, want %v for %+v", kinds, want, req)
		}
	}
}

// Whatever the facts, refusals keep the order of the kinds and never repeat a kind except
// unmeasured, which names the candidate and the head separately.
func TestRefusalOrderFollowsTheKinds(t *testing.T) {
	order := []core.PromoteRefusalKind{
		core.RefusalUnmeasured, core.RefusalCheckMissing, core.RefusalWorse, core.RefusalWithinNoise,
		core.RefusalInconclusive, core.RefusalStale, core.RefusalEnvironmentMismatch,
	}
	r := newRand(44)
	for range properties {
		req := ready()
		req.Results = sides(anySamples(r), anySamples(r))
		if r.IntN(2) == 0 {
			checkedBy("")(&req.Results[1])
		}
		if r.IntN(3) == 0 {
			inEnvironment(dockerEnv)(&req.Results[1])
		}
		if r.IntN(3) == 0 {
			req.Promotions = []core.Promotion{{Commit: baseCommit, At: at(r.IntN(5))}}
		}
		switch r.IntN(6) { // sometimes a side was never measured
		case 0:
			req.Results = req.Results[1:]
		case 1:
			req.Results = req.Results[:1]
		}
		got, err := core.DecidePromotion(req)
		if err != nil {
			continue
		}
		refusal, ok := got.(core.PromoteRefusal)
		if !ok {
			continue
		}
		last := -1
		for _, reason := range refusal.Reasons {
			i := slices.Index(order, reason.Kind)
			if i < last || (i == last && reason.Kind != core.RefusalUnmeasured) {
				t.Fatalf("reasons out of order or repeated: %+v", refusal.Reasons)
			}
			last = i
		}
	}
}
