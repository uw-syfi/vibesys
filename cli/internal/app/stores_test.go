package app_test

import (
	"math/rand/v2"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// However a measurement was split into top-ups, its samples answer a submission together, and the
// submission lands on the latest of them. One result alone often has too few.
func TestReuseCountsTheSamplesOfEveryTopUp(t *testing.T) {
	const defaultSamples = 5
	reuse := app.Rules(defaultSamples).Reuse
	for seed := range uint64(500) {
		rng := rand.New(rand.NewPCG(seed, 9))
		var saved []core.Result
		var have uint32
		for i := range 1 + rng.IntN(4) {
			samples := make([]float64, 1+rng.IntN(4))
			have += uint32(len(samples))
			saved = append(saved, core.Result{
				Job: core.JobID(rune('a' + i)), Key: "key", RecordedAt: time.Unix(int64(i), 0).UTC(),
				Outcome: core.MeasureOutcome{Samples: samples},
			})
		}
		asked := uint32(rng.IntN(12))
		want := asked
		if want == 0 {
			want = defaultSamples
		}
		job, ok := reuse(saved, core.Submission{Key: "key", Spec: core.JobSpec{Settings: core.MeasureSettings{Samples: asked}}})
		if ok != (have >= want) {
			t.Fatalf("seed %d: %d samples saved, %d wanted: reused = %v", seed, have, want, ok)
		}
		if latest := saved[len(saved)-1].Job; ok && job != latest {
			t.Fatalf("seed %d: landed on %s, want the latest result, %s", seed, job, latest)
		}
	}
}

func TestReuseAnswersACheckAndHonorsNoCache(t *testing.T) {
	saved := []core.Result{{Job: "a", Key: "key", Outcome: core.CheckOutcome{}}}
	reuse := app.Rules(5).Reuse
	if job, ok := reuse(saved, core.Submission{Key: "key", Spec: core.JobSpec{Settings: core.CheckSettings{}}}); !ok || job != "a" {
		t.Errorf("a saved pass: %q, %v; want reused on a", job, ok)
	}
	if _, ok := reuse(saved, core.Submission{Key: "key", NoCache: true, Spec: core.JobSpec{Settings: core.CheckSettings{}}}); ok {
		t.Error("NoCache was answered from the cache")
	}
}
