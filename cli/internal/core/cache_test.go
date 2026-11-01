package core_test

import (
	"fmt"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func plan(t testing.TB, q core.CacheQuery) core.CachePlan {
	t.Helper()
	p, err := core.PlanCache(q)
	if err != nil {
		t.Fatalf("PlanCache(%+v): %v", q, err)
	}
	return p
}

func TestACommandJobNeverReusesAnything(t *testing.T) {
	r := newRand(30)
	for range properties {
		q := core.CacheQuery{
			Key:      "",
			Settings: core.CommandSettings{Argv: []string{"make"}},
			NoCache:  r.IntN(2) == 0,
			Saved:    []core.Result{checkPass("c1", commitN(1), 1), measurement("m1", commitN(1), []float64{1, 2, 3}, 2)},
		}
		if got := plan(t, q); got.Action != core.CacheRun || got.Samples != 0 || got.FreshCheck || len(got.From) != 0 {
			t.Fatalf("%+v: %+v, want a plain run", q, got)
		}
	}
}

func TestACheckReusesPassesOnly(t *testing.T) {
	pass := checkPass("c1", commitN(1), 5, underKey("k"))
	later := checkPass("c2", commitN(1), 9, underKey("k"))
	elsewhere := checkPass("c3", commitN(1), 12, underKey("other-key"))
	measured := measurement("m1", commitN(1), []float64{1, 2, 3}, 13, underKey("k"))
	cases := []struct {
		name  string
		saved []core.Result
		no    bool
		want  core.CachePlan
	}{
		{"nothing saved: a failed check saves nothing, so it reruns", nil, false, core.CachePlan{Action: core.CacheRun}},
		{"a pass under the key", []core.Result{pass}, false, core.CachePlan{Action: core.CacheReuse, From: []core.JobID{"c1"}}},
		{"the most recent pass", []core.Result{later, pass}, false, core.CachePlan{Action: core.CacheReuse, From: []core.JobID{"c2"}}},
		{"a pass under another key does not count", []core.Result{elsewhere}, false, core.CachePlan{Action: core.CacheRun}},
		{"a measurement is not a pass", []core.Result{measured}, false, core.CachePlan{Action: core.CacheRun}},
		{"no cache", []core.Result{pass}, true, core.CachePlan{Action: core.CacheRun}},
	}
	for _, c := range cases {
		got := plan(t, core.CacheQuery{Key: "k", Settings: core.CheckSettings{}, NoCache: c.no, Saved: c.saved})
		if got.Action != c.want.Action || !slices.Equal(got.From, c.want.From) || got.Samples != 0 {
			t.Errorf("%s: %+v, want %+v", c.name, got, c.want)
		}
	}
}

func TestAMeasureTopsUpToTheRequestedCount(t *testing.T) {
	saved := func(counts ...int) []core.Result {
		var out []core.Result
		for i, n := range counts {
			out = append(out, measurement(fmt.Sprintf("m%d", i+1), commitN(1), make([]float64, n), i+1, underKey("k")))
		}
		return out
	}
	cases := []struct {
		name     string
		settings core.MeasureSettings
		def      uint32
		saved    []core.Result
		want     core.CachePlan
	}{
		{"nothing saved", core.MeasureSettings{Samples: 5}, 3, nil, core.CachePlan{Action: core.CacheRun, Samples: 5}},
		{"the task default when none is asked for", core.MeasureSettings{}, 4, nil, core.CachePlan{Action: core.CacheRun, Samples: 4}},
		{"exactly enough", core.MeasureSettings{Samples: 5}, 3, saved(5), core.CachePlan{Action: core.CacheReuse, From: []core.JobID{"m1"}}},
		{"more than enough", core.MeasureSettings{Samples: 3}, 3, saved(5), core.CachePlan{Action: core.CacheReuse, From: []core.JobID{"m1"}}},
		{"only the difference runs", core.MeasureSettings{Samples: 5}, 3, saved(3), core.CachePlan{Action: core.CacheTopUp, Samples: 2, From: []core.JobID{"m1"}}},
		{"earlier top-ups count", core.MeasureSettings{Samples: 8}, 3, saved(3, 2), core.CachePlan{Action: core.CacheTopUp, Samples: 3, From: []core.JobID{"m1", "m2"}}},
		{"top-ups together are enough", core.MeasureSettings{Samples: 5}, 3, saved(3, 2), core.CachePlan{Action: core.CacheReuse, From: []core.JobID{"m1", "m2"}}},
		{"a recheck redoes the check of what runs", core.MeasureSettings{Samples: 5, Recheck: true}, 3, saved(3), core.CachePlan{Action: core.CacheTopUp, Samples: 2, FreshCheck: true, From: []core.JobID{"m1"}}},
		{"a recheck runs nothing when nothing is missing", core.MeasureSettings{Samples: 3, Recheck: true}, 3, saved(3), core.CachePlan{Action: core.CacheReuse, From: []core.JobID{"m1"}}},
		{"another key's samples do not count", core.MeasureSettings{Samples: 3}, 3,
			[]core.Result{measurement("x", commitN(1), []float64{1, 2, 3}, 1, underKey("other"))}, core.CachePlan{Action: core.CacheRun, Samples: 3}},
		{"a check pass is no sample", core.MeasureSettings{Samples: 3}, 3,
			[]core.Result{checkPass("c", commitN(1), 1, underKey("k"))}, core.CachePlan{Action: core.CacheRun, Samples: 3}},
	}
	for _, c := range cases {
		got := plan(t, core.CacheQuery{Key: "k", Settings: c.settings, DefaultSamples: c.def, Saved: c.saved})
		if got.Action != c.want.Action || got.Samples != c.want.Samples || got.FreshCheck != c.want.FreshCheck || !slices.Equal(got.From, c.want.From) {
			t.Errorf("%s: %+v, want %+v", c.name, got, c.want)
		}
	}
}

// Over saved results of any counts: the plan never leaves a count short, never asks for more than
// the difference, and no cache runs the whole job with a fresh check.
func TestMeasurePlansNeverUnderdeliverOrOvershoot(t *testing.T) {
	r := newRand(31)
	for range properties {
		var saved []core.Result
		var have uint32
		for i := range r.IntN(5) {
			n := r.IntN(6)
			have += uint32(n)
			saved = append(saved, measurement(fmt.Sprintf("m%d", i), commitN(1), make([]float64, n), r.IntN(100), underKey("k")))
		}
		r.Shuffle(len(saved), func(i, j int) { saved[i], saved[j] = saved[j], saved[i] })
		settings := core.MeasureSettings{Samples: uint32(r.IntN(8)), Recheck: r.IntN(2) == 0}
		def := uint32(r.IntN(6))
		noCache := r.IntN(4) == 0
		want := settings.Samples
		if want == 0 {
			want = def
		}
		got, err := core.PlanCache(core.CacheQuery{Key: "k", Settings: settings, DefaultSamples: def, NoCache: noCache, Saved: saved})
		if want == 0 {
			if failureKind(err) != core.FailureInvalidInput {
				t.Fatalf("no sample count to ask for: %v, want an invalid_input failure", err)
			}
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		switch got.Action {
		case core.CacheReuse:
			if noCache || have < want || got.Samples != 0 || got.FreshCheck {
				t.Fatalf("reuse with have %d want %d no-cache %v: %+v", have, want, noCache, got)
			}
		case core.CacheTopUp:
			if noCache || have == 0 || have+got.Samples != want {
				t.Fatalf("top-up with have %d want %d no-cache %v: %+v", have, want, noCache, got)
			}
		case core.CacheRun:
			if got.Samples != want || (!noCache && have != 0) {
				t.Fatalf("run with have %d want %d no-cache %v: %+v", have, want, noCache, got)
			}
		default:
			t.Fatalf("action %q", got.Action)
		}
		if got.Action != core.CacheReuse && got.FreshCheck != (noCache || settings.Recheck) {
			t.Fatalf("fresh check %v with no-cache %v recheck %v", got.FreshCheck, noCache, settings.Recheck)
		}
		// The jobs the answer builds on are exactly the saved measurements, oldest first.
		if got.Action != core.CacheRun {
			byTime := slices.Clone(saved)
			slices.SortFunc(byTime, func(a, b core.Result) int {
				if c := a.RecordedAt.Compare(b.RecordedAt); c != 0 {
					return c
				}
				return strings.Compare(string(a.Job), string(b.Job))
			})
			if !slices.Equal(got.From, jobIDs(byTime...)) {
				t.Fatalf("from %v, want %v", got.From, jobIDs(byTime...))
			}
		}
	}
}

func TestPlanCacheRefusesSettingsItCannotRead(t *testing.T) {
	for name, settings := range map[string]core.Settings{
		"none":                nil,
		"a pointer":           &core.MeasureSettings{Samples: 3},
		"a measure with none": core.MeasureSettings{},
	} {
		got, err := core.PlanCache(core.CacheQuery{Key: "k", Settings: settings})
		if failureKind(err) != core.FailureInvalidInput || got.Action != "" {
			t.Errorf("%s: %+v, %v, want an invalid_input failure", name, got, err)
		}
	}
}

func queuedJob(kind core.JobKind, key core.JobKey, settings core.Settings, state core.JobState) core.Job {
	j := newJob(kind, state)
	j.Key, j.Spec.Settings = key, settings
	return j
}

func TestSubmissionsJoinOnlyJobsThatAnswerThem(t *testing.T) {
	check := core.CheckSettings{}
	measure := func(n uint32, recheck bool) core.MeasureSettings {
		return core.MeasureSettings{Samples: n, Recheck: recheck}
	}
	submit := func(key core.JobKey, settings core.Settings, noCache bool) core.Submission {
		return core.Submission{Key: key, Spec: core.JobSpec{Commit: commitN(1), Settings: settings}, NoCache: noCache}
	}
	cases := []struct {
		name     string
		existing core.Job
		sub      core.Submission
		want     bool
	}{
		{"the same check, queued", queuedJob(core.JobCheck, "k", check, core.JobQueued), submit("k", check, false), true},
		{"the same check, running", queuedJob(core.JobCheck, "k", check, core.JobRunning), submit("k", check, false), true},
		{"a finished check is reuse, not a join", queuedJob(core.JobCheck, "k", check, core.JobSucceeded), submit("k", check, false), false},
		{"a failed check", queuedJob(core.JobCheck, "k", check, core.JobFailed), submit("k", check, false), false},
		{"no cache never joins", queuedJob(core.JobCheck, "k", check, core.JobRunning), submit("k", check, true), false},
		{"another key", queuedJob(core.JobCheck, "k", check, core.JobRunning), submit("j", check, false), false},
		{"a check does not join a measure", queuedJob(core.JobMeasure, "k", measure(3, false), core.JobRunning), submit("k", check, false), false},
		{"a command never joins", queuedJob(core.JobCommand, "", core.CommandSettings{Argv: []string{"x"}}, core.JobRunning), submit("", core.CommandSettings{Argv: []string{"x"}}, false), false},
		{"equal samples", queuedJob(core.JobMeasure, "k", measure(5, false), core.JobRunning), submit("k", measure(5, false), false), true},
		{"more samples than asked", queuedJob(core.JobMeasure, "k", measure(8, false), core.JobRunning), submit("k", measure(5, false), false), true},
		{"fewer samples than asked", queuedJob(core.JobMeasure, "k", measure(3, false), core.JobRunning), submit("k", measure(5, false), false), false},
		{"the task default counts as the asked for count", queuedJob(core.JobMeasure, "k", measure(0, false), core.JobRunning), submit("k", measure(3, false), false), true},
		{"a recheck needs a job that rechecks", queuedJob(core.JobMeasure, "k", measure(5, false), core.JobRunning), submit("k", measure(5, true), false), false},
		{"a job that rechecks answers a plain submission", queuedJob(core.JobMeasure, "k", measure(5, true), core.JobRunning), submit("k", measure(5, false), false), true},
		{"no settings", queuedJob(core.JobCheck, "k", check, core.JobRunning), submit("k", nil, false), false},
	}
	for _, c := range cases {
		if got := core.CanJoin(c.existing, c.sub, 3); got != c.want {
			t.Errorf("%s: CanJoin = %v, want %v", c.name, got, c.want)
		}
	}
}

// Joining is never wider than reuse would be: a submission that joins a job is one the saved
// result of that job would also answer.
func TestJoinPropertyAgreesWithTheCache(t *testing.T) {
	r := newRand(32)
	for range properties {
		existing := queuedJob(core.JobMeasure, "k", core.MeasureSettings{Samples: uint32(r.IntN(7)), Recheck: r.IntN(2) == 0}, core.JobRunning)
		sub := core.Submission{
			Key:     "k",
			NoCache: r.IntN(5) == 0,
			Spec:    core.JobSpec{Commit: commitN(1), Settings: core.MeasureSettings{Samples: uint32(r.IntN(7)), Recheck: r.IntN(2) == 0}},
		}
		def := uint32(r.IntN(5))
		if !core.CanJoin(existing, sub, def) {
			continue
		}
		have := existing.Spec.Settings.(core.MeasureSettings)
		saved := []core.Result{measurement("m1", commitN(1), make([]float64, max(have.Samples, def*b2u(have.Samples == 0))), 1, underKey("k"))}
		got, err := core.PlanCache(core.CacheQuery{Key: "k", Settings: sub.Spec.Settings, DefaultSamples: def, Saved: saved})
		if err != nil || got.Action != core.CacheReuse {
			t.Fatalf("%+v joined %+v, but its own result gives %+v (%v)", sub, existing.Spec, got, err)
		}
	}
}

func b2u(b bool) uint32 {
	if b {
		return 1
	}
	return 0
}
