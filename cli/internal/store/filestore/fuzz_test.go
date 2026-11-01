package filestore_test

import (
	"errors"
	"math"
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/memstore"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// answered is what a call returned, or the kind of failure it ended in, so that two stores can
// be compared by value.
type answered struct {
	Value any
	Kind  core.FailureKind
	Other string
}

func answer(value any, err error) answered {
	var f *core.Failure
	switch {
	case errors.As(err, &f):
		return answered{Kind: f.Kind}
	case err != nil:
		return answered{Other: err.Error()}
	}
	return answered{Value: value}
}

// FuzzValues puts arbitrary text, numbers and instants into every kind of record. The in-memory
// store and the on-disk store must accept and refuse the same values and return the same
// answers, and the on-disk store must show what it showed after it is reopened. The seeds are
// the awkward values: invalid UTF-8, NUL, line separators, signed zero, NaN, the extremes of
// time.
func FuzzValues(f *testing.F) {
	f.Add("plain", 1.5, int64(1_700_000_000), int64(5))
	f.Add("nul\x00 \u2028\u2029 ünï 🧪 \xef\xbb\xbf", math.Copysign(0, -1), int64(-62135596800), int64(0))
	f.Add("bad \xff utf8", 2.5, int64(0), int64(0))
	f.Add("", math.NaN(), int64(253402300799), int64(999999999))
	f.Add("<tag>&\"quoted\" \\ back", math.Inf(1), int64(253402300800), int64(0))
	f.Add("tiny", 5e-324, int64(-62135596801), int64(0))
	f.Add("huge", math.MaxFloat64, int64(math.MaxInt64), int64(math.MinInt64))
	f.Fuzz(func(t *testing.T, text string, x float64, secs, nanos int64) {
		mem := fsys.NewMemFS()
		stores := []store.Store{memstore.New(), openOn(t, mem)}
		instant := time.Unix(secs, nanos)
		run := func(s store.Store) []answered {
			ctx := t.Context()
			var got []answered
			sub, err := s.Submit(ctx, core.Submission{
				Context: core.CallContext{Repository: text, Task: text, Harness: text, Agent: text},
				Spec:    measureSpec(core.Commit(text)), Key: "k1", Note: &core.Note{Text: text, At: instant, Agent: text}, At: instant,
			})
			got = append(got, answer(sub, err))
			plan, err := s.CreatePlan(ctx, core.NewPlan{Title: text, Body: text, At: instant})
			got = append(got, answer(plan, err))
			if err == nil {
				_, err = s.EditPlan(ctx, plan.Number, core.PlanEdit{Comment: &core.PlanComment{Text: text, At: instant, Agent: text}, At: instant})
				got = append(got, answer(nil, err))
			}
			job, err := s.Job(ctx, "j1")
			got = append(got, answer(job, err))
			if err == nil {
				_, err = s.Transition(ctx, core.Transition{Job: "j1", From: core.JobQueued, To: core.JobRunning, At: instant, Environment: pinned()})
				got = append(got, answer(nil, err))
				result := savedResult("j1", "k1", core.Commit(text))
				result.Task, result.Machine.HostID, result.Load = text, text, core.MachineLoad{SampledAt: instant, LoadAverage1m: x}
				result.RecordedAt = instant
				result.Outcome = core.MeasureOutcome{
					Metric: core.Metric{Name: text, Direction: core.Maximize}, Samples: []float64{x, -x},
					Observations: []core.Observation{{Name: text, Values: []float64{x}}}, CheckJob: "j-check",
				}
				done, err := s.Transition(ctx, core.Transition{Job: "j1", From: core.JobRunning, To: core.JobSucceeded, At: instant, Result: result})
				got = append(got, answer(done, err))
			}
			err = s.RecordPromotion(ctx, core.Promotion{
				Commit: core.Commit(text), PreviousHead: core.Commit(text), Verdict: core.Verdict{Kind: core.Better, CandidateMean: x, BaseMean: -x, Ratio: x, Noise: -x},
				Environment: core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, ImageDigest: text, Facts: map[string]string{text: text}},
				ResultJob:   "j1", At: instant,
			})
			got = append(got, answer(nil, err))
			event, err := s.AppendEvent(ctx, core.EventDraft{
				SchemaVersion: 1, Run: "run-a", HostID: text, Task: text, Commit: core.Commit(text), Harness: text, Agent: text, At: instant,
				Command: core.CommandWait, Outcome: core.OutcomeFailed, Job: "j1",
				Detail: core.PromoteRefusal{Reasons: []core.PromoteRefusalReason{{Kind: core.RefusalWorse, Message: text, Verdict: &core.Verdict{Noise: x}}}},
			})
			got = append(got, answer(event, err))
			got = append(got, answer(nil, s.AppendLog(ctx, "j1", []byte(text))))
			return got
		}
		answers := [2][]answered{run(stores[0]), run(stores[1])}
		if !reflect.DeepEqual(answers[0], answers[1]) {
			t.Fatalf("the stores answered differently:\nmemory %+v\ndisk   %+v", answers[0], answers[1])
		}
		want := storetest.Observe(t, stores[0], "run-a")
		if got := storetest.Observe(t, stores[1], "run-a"); !reflect.DeepEqual(got, want) {
			t.Fatalf("the stores show different state:\nmemory %+v\ndisk   %+v", want, got)
		}
		if got := storetest.Observe(t, openOn(t, mem.CrashImage(true)), "run-a"); !reflect.DeepEqual(got, want) {
			t.Fatalf("after a restart the store shows\n%+v\nwant\n%+v", got, want)
		}
	})
}
