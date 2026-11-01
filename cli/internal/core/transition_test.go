package core_test

import (
	"fmt"
	"math"
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var jobKinds = []core.JobKind{core.JobCheck, core.JobMeasure, core.JobCommand}

// newJob builds a job of a kind in a state. It is built the same way every time, so a test can
// build a second copy to compare a job against after something was done to the first.
func newJob(kind core.JobKind, state core.JobState) core.Job {
	j := core.Job{
		ID:          "job-1",
		State:       state,
		Spec:        core.JobSpec{Commit: commitN(1)},
		SubmittedAt: at(0),
	}
	switch kind {
	case core.JobCheck:
		j.Spec.Settings, j.Key = core.CheckSettings{}, "key-check"
	case core.JobMeasure:
		j.Spec.Settings, j.Key = core.MeasureSettings{Samples: 3}, "key-measure"
	case core.JobCommand:
		j.Spec.Settings = core.CommandSettings{Argv: []string{"true"}}
	}
	if state != core.JobQueued {
		pinned := hostEnv
		j.Environment = &pinned
		started := at(1)
		j.StartedAt = &started
	}
	return j
}

// resultFor is the result a well-formed run of the job saves.
func resultFor(j core.Job) *core.Result {
	r := core.Result{
		Job: j.ID, Key: j.Key, Task: "kv", TaskVersion: "t1", Commit: j.Spec.Commit,
		SchemaVersion: core.SchemaVersion, RecordedAt: at(10),
	}
	if j.Environment != nil {
		r.Environment = *j.Environment
	}
	switch j.Spec.Settings.Kind() {
	case core.JobCheck:
		r.Outcome = core.CheckOutcome{}
	case core.JobMeasure:
		r.Outcome = core.MeasureOutcome{
			Metric: core.Metric{Name: "throughput", Direction: core.Maximize}, Samples: []float64{1, 2, 3}, CheckJob: j.ID,
		}
	default:
		return nil
	}
	return &r
}

// moveTo is the well-formed transition of a job to a state: it carries exactly the effects that
// move has.
func moveTo(j core.Job, to core.JobState) core.Transition {
	t := core.Transition{Job: j.ID, From: j.State, To: to, At: at(10)}
	exit := 1
	switch to {
	case core.JobRunning:
		pinned := hostEnv
		t.Environment = &pinned
	case core.JobSucceeded:
		t.Result = resultFor(j)
		exit = 0
		t.ExitCode = &exit
	case core.JobFailed:
		t.ExitCode = &exit
	case core.JobCancelled:
		t.CancelReason = "no longer needed"
	}
	return t
}

func applied(t testing.TB, j core.Job, tr core.Transition) core.Job {
	t.Helper()
	out, err := j.Apply(tr)
	if err != nil {
		t.Fatalf("Apply(%+v): %v", tr, err)
	}
	return out
}

func TestTheJobStateMachineMovesOnlyAlongItsEdges(t *testing.T) {
	legal := map[core.JobState][]core.JobState{
		core.JobQueued:  {core.JobRunning, core.JobCancelled, core.JobFailed},
		core.JobRunning: {core.JobSucceeded, core.JobFailed, core.JobCancelled, core.JobLost},
	}
	states := append(core.JobStates(), "", "paused")
	for _, from := range states {
		for _, to := range states {
			if got, want := from.CanMoveTo(to), slices.Contains(legal[from], to); got != want {
				t.Errorf("%q.CanMoveTo(%q) = %v, want %v", from, to, got, want)
			}
		}
	}
}

func TestAStateIsFinalExactlyWhenNothingLeavesIt(t *testing.T) {
	for _, state := range append(core.JobStates(), "", "paused") {
		leaves := slices.ContainsFunc(core.JobStates(), state.CanMoveTo)
		known := slices.Contains(core.JobStates(), state)
		if got, want := state.Final(), known && !leaves; got != want {
			t.Errorf("%q.Final() = %v, want %v", state, got, want)
		}
	}
}

// Every well-formed transition of every kind of job from every state: it applies exactly when the
// state machine allows it, and it leaves the job it was given as it was.
func TestApplyFollowsTheStateMachine(t *testing.T) {
	for _, kind := range jobKinds {
		for _, from := range core.JobStates() {
			for _, to := range core.JobStates() {
				name := fmt.Sprintf("%s job %s to %s", kind, from, to)
				j, same := newJob(kind, from), newJob(kind, from)
				out, err := j.Apply(moveTo(j, to))
				if !reflect.DeepEqual(j, same) {
					t.Fatalf("%s: Apply changed the job it was given", name)
				}
				if !from.CanMoveTo(to) {
					if failureKind(err) != core.FailureInvalidInput {
						t.Errorf("%s: error %v, want an invalid_input failure", name, err)
					}
					continue
				}
				if err != nil {
					t.Errorf("%s: %v", name, err)
					continue
				}
				checkMoved(t, name, kind, j, out, to)
			}
		}
	}
}

func checkMoved(t *testing.T, name string, kind core.JobKind, before, after core.Job, to core.JobState) {
	t.Helper()
	if after.State != to {
		t.Errorf("%s: state %s", name, after.State)
	}
	if (to == core.JobRunning) != (after.StartedAt != nil && before.StartedAt == nil) {
		t.Errorf("%s: started at %v", name, after.StartedAt)
	}
	if after.StartedAt == nil && before.State == core.JobRunning {
		t.Errorf("%s: a running job lost its start time", name)
	}
	if (after.FinishedAt != nil) != to.Final() {
		t.Errorf("%s: finished at %v", name, after.FinishedAt)
	}
	if to == core.JobRunning && (after.Environment == nil || !after.Environment.Equal(hostEnv)) {
		t.Errorf("%s: environment %v not pinned", name, after.Environment)
	}
	if wantResult := to == core.JobSucceeded && kind != core.JobCommand; (after.ResultJob == after.ID) != wantResult {
		t.Errorf("%s: result job %q", name, after.ResultJob)
	}
	if (after.CancelReason != "") != (to == core.JobCancelled) {
		t.Errorf("%s: cancel reason %q", name, after.CancelReason)
	}
	if (kind == core.JobMeasure && to == core.JobSucceeded) != (after.CheckJob != "") {
		t.Errorf("%s: check job %q", name, after.CheckJob)
	}
}

func TestApplyRefusesAStaleTransitionWithAConflict(t *testing.T) {
	for _, kind := range jobKinds {
		for _, actual := range core.JobStates() {
			for _, claimed := range core.JobStates() {
				if actual == claimed {
					continue
				}
				j := newJob(kind, actual)
				tr := moveTo(newJob(kind, claimed), core.JobCancelled)
				if _, err := j.Apply(tr); failureKind(err) != core.FailureConflict {
					t.Errorf("%s job in %s, transition from %s: %v, want a conflict", kind, actual, claimed, err)
				}
			}
		}
	}
}

func TestApplyRefusesEveryEffectThatDoesNotBelongToTheMove(t *testing.T) {
	// Each effect, how to add it to a transition, and when it belongs to the move.
	effects := []struct {
		name    string
		add     func(t *core.Transition)
		belongs func(kind core.JobKind, to core.JobState) bool
	}{
		{"environment", func(t *core.Transition) { e := hostEnv; t.Environment = &e },
			func(_ core.JobKind, to core.JobState) bool { return to == core.JobRunning }},
		{"result", func(t *core.Transition) { t.Result = resultFor(newJob(core.JobMeasure, core.JobRunning)) },
			func(kind core.JobKind, to core.JobState) bool {
				return to == core.JobSucceeded && kind != core.JobCommand
			}},
		{"cancel reason", func(t *core.Transition) { t.CancelReason = "because" },
			func(_ core.JobKind, to core.JobState) bool { return to == core.JobCancelled }},
		{"exit code", func(t *core.Transition) { code := 3; t.ExitCode = &code },
			func(_ core.JobKind, to core.JobState) bool { return to == core.JobSucceeded || to == core.JobFailed }},
		{"check job", func(t *core.Transition) { t.CheckJob = "check-1" },
			func(kind core.JobKind, _ core.JobState) bool { return kind == core.JobMeasure }},
	}
	for _, kind := range jobKinds {
		for _, from := range []core.JobState{core.JobQueued, core.JobRunning} {
			j := newJob(kind, from)
			for _, to := range core.JobStates() {
				if !from.CanMoveTo(to) {
					continue
				}
				for _, effect := range effects {
					if effect.belongs(kind, to) {
						continue
					}
					tr := moveTo(j, to)
					effect.add(&tr)
					if _, err := j.Apply(tr); failureKind(err) != core.FailureInvalidInput {
						t.Errorf("%s job %s to %s with a stray %s: %v, want an invalid_input failure", kind, from, to, effect.name, err)
					}
				}
			}
		}
	}
}

func TestApplyRefusesAMoveThatLeavesOutWhatItNeeds(t *testing.T) {
	running := newJob(core.JobMeasure, core.JobRunning)
	queued := newJob(core.JobMeasure, core.JobQueued)
	command := newJob(core.JobCommand, core.JobRunning)
	edit := func(j core.Job, to core.JobState, change func(*core.Transition)) struct {
		job core.Job
		tr  core.Transition
	} {
		tr := moveTo(j, to)
		change(&tr)
		return struct {
			job core.Job
			tr  core.Transition
		}{j, tr}
	}
	cases := map[string]struct {
		job core.Job
		tr  core.Transition
	}{
		"running without an environment": edit(queued, core.JobRunning, func(tr *core.Transition) { tr.Environment = nil }),
		"running in an unpinned image": edit(queued, core.JobRunning, func(tr *core.Transition) {
			tr.Environment = &core.Fingerprint{Kind: core.EnvironmentDocker, Backend: core.BackendCPU, ImageDigest: "ubuntu:latest"}
		}),
		"a transition with no time":         edit(queued, core.JobRunning, func(tr *core.Transition) { tr.At = time.Time{} }),
		"a transition for another job":      edit(running, core.JobFailed, func(tr *core.Transition) { tr.Job = "job-2" }),
		"a measure succeeding with nothing": edit(running, core.JobSucceeded, func(tr *core.Transition) { tr.Result = nil }),
		"a command saving a result":         edit(command, core.JobSucceeded, func(tr *core.Transition) { tr.Result = resultFor(running) }),
		"a job with no settings":            edit(running, core.JobFailed, func(*core.Transition) {}),
	}
	noSettings := cases["a job with no settings"]
	noSettings.job.Spec.Settings = nil
	cases["a job with no settings"] = noSettings
	for name, c := range cases {
		if _, err := c.job.Apply(c.tr); failureKind(err) != core.FailureInvalidInput {
			t.Errorf("%s: %v, want an invalid_input failure", name, err)
		}
	}
}

// A result is saved only if it is this job's, of this commit and key, at this schema version, in
// the environment the job pinned, of the job's own kind; and a measurement needs a metric, finite
// samples and a check pass to name. That last rule is the guarantee that a measurement exists only
// for a check that passed.
func TestApplyRefusesAResultThatIsNotTheJobs(t *testing.T) {
	measure := newJob(core.JobMeasure, core.JobRunning)
	check := newJob(core.JobCheck, core.JobRunning)
	measured := func(f func(*core.Result, *core.MeasureOutcome)) core.Transition {
		tr := moveTo(measure, core.JobSucceeded)
		outcome := tr.Result.Outcome.(core.MeasureOutcome)
		f(tr.Result, &outcome)
		tr.Result.Outcome = outcome
		return tr
	}
	other := hostEnv
	other.Facts = map[string]string{"os": "darwin"}
	cases := map[string]struct {
		job core.Job
		tr  core.Transition
	}{
		"another job's result": {measure, measured(func(r *core.Result, _ *core.MeasureOutcome) { r.Job = "job-2" })},
		"another commit":       {measure, measured(func(r *core.Result, _ *core.MeasureOutcome) { r.Commit = commitN(2) })},
		"another key":          {measure, measured(func(r *core.Result, _ *core.MeasureOutcome) { r.Key = "key-other" })},
		"an old schema":        {measure, measured(func(r *core.Result, _ *core.MeasureOutcome) { r.SchemaVersion = core.SchemaVersion + 1 })},
		"another environment":  {measure, measured(func(r *core.Result, _ *core.MeasureOutcome) { r.Environment = other })},
		"no check pass named":  {measure, measured(func(_ *core.Result, o *core.MeasureOutcome) { o.CheckJob = "" })},
		"no samples":           {measure, measured(func(_ *core.Result, o *core.MeasureOutcome) { o.Samples = nil })},
		"a NaN sample":         {measure, measured(func(_ *core.Result, o *core.MeasureOutcome) { o.Samples = []float64{1, math.NaN()} })},
		"an infinite sample":   {measure, measured(func(_ *core.Result, o *core.MeasureOutcome) { o.Samples = []float64{math.Inf(1)} })},
		"no metric name":       {measure, measured(func(_ *core.Result, o *core.MeasureOutcome) { o.Metric.Name = "" })},
		"no metric direction":  {measure, measured(func(_ *core.Result, o *core.MeasureOutcome) { o.Metric.Direction = "" })},
		"a check outcome for a measure": {measure, func() core.Transition {
			tr := moveTo(measure, core.JobSucceeded)
			tr.Result.Outcome = core.CheckOutcome{}
			return tr
		}()},
		"a measure outcome for a check": {check, func() core.Transition {
			tr := moveTo(check, core.JobSucceeded)
			tr.Result.Outcome = resultFor(measure).Outcome
			return tr
		}()},
		"no outcome": {check, func() core.Transition {
			tr := moveTo(check, core.JobSucceeded)
			tr.Result.Outcome = nil
			return tr
		}()},
		"a check job that disagrees": {measure, func() core.Transition {
			tr := moveTo(measure, core.JobSucceeded)
			tr.CheckJob = "check-elsewhere"
			return tr
		}()},
	}
	for name, c := range cases {
		out, err := c.job.Apply(c.tr)
		if failureKind(err) != core.FailureInvalidInput {
			t.Errorf("%s: %v, want an invalid_input failure", name, err)
		}
		if out.State != "" {
			t.Errorf("%s: a refused transition returned a job in state %q", name, out.State)
		}
	}
}

// Random attempts at moving a job: well-formed moves between random states, some spoiled by a
// stray effect or a missing one. A model that knows only the state machine predicts every
// outcome, and the job keeps its invariants throughout.
func TestRandomAttemptsAtMovingAJobKeepItsInvariants(t *testing.T) {
	r := newRand(20)
	for range properties {
		kind := jobKinds[r.IntN(len(jobKinds))]
		j := newJob(kind, core.JobQueued)
		ran := false
		for range 1 + r.IntN(8) {
			to := core.JobStates()[r.IntN(6)]
			tr := moveTo(j, to)
			spoiled := r.IntN(4) == 0
			if spoiled {
				spoil(r, kind, &tr)
			}
			before, copyOf := j, j
			out, err := j.Apply(tr)
			if !reflect.DeepEqual(before, copyOf) {
				t.Fatalf("Apply changed the job it was given")
			}
			legal := j.State.CanMoveTo(to)
			if err != nil {
				if legal && !spoiled {
					t.Fatalf("%s job %s to %s: a well-formed legal move failed: %v", kind, j.State, to, err)
				}
				continue
			}
			if !legal || spoiled {
				t.Fatalf("%s job %s to %s: an illegal or spoiled move (spoiled %v) applied", kind, j.State, to, spoiled)
			}
			ran = ran || to == core.JobRunning
			j = out
			checkInvariants(t, j, ran)
		}
		if j.State.Final() {
			for _, to := range core.JobStates() {
				if _, err := j.Apply(moveTo(j, to)); err == nil {
					t.Fatalf("a %s job moved from final state %s to %s", kind, j.State, to)
				}
			}
		}
	}
}

// spoil changes a well-formed transition of a job of a kind into one that must be refused.
func spoil(r *rand.Rand, kind core.JobKind, tr *core.Transition) {
	var ways []func()
	ways = append(ways, func() { tr.Job += "x" })
	if tr.To == core.JobRunning {
		ways = append(ways, func() { tr.Environment = nil })
	} else {
		ways = append(ways, func() { e := hostEnv; tr.Environment = &e })
	}
	switch {
	case tr.To == core.JobSucceeded && kind != core.JobCommand:
		ways = append(ways, func() { tr.Result = nil })
	default:
		ways = append(ways, func() { tr.Result = resultFor(newJob(core.JobMeasure, core.JobRunning)) })
	}
	if tr.To != core.JobCancelled {
		ways = append(ways, func() { tr.CancelReason = "stray" })
	}
	if tr.To != core.JobSucceeded && tr.To != core.JobFailed {
		ways = append(ways, func() { code := 9; tr.ExitCode = &code })
	}
	ways[r.IntN(len(ways))]()
}

func checkInvariants(t *testing.T, j core.Job, ran bool) {
	t.Helper()
	if (j.StartedAt != nil) != ran {
		t.Fatalf("job %+v: started at %v, but ran is %v", j, j.StartedAt, ran)
	}
	if (j.FinishedAt != nil) != j.State.Final() {
		t.Fatalf("job %+v: finished at %v in state %s", j, j.FinishedAt, j.State)
	}
	if (j.Environment != nil) != ran {
		t.Fatalf("job %+v: environment %v but ran is %v", j, j.Environment, ran)
	}
	if hasResult := j.ResultJob != ""; hasResult && (j.State != core.JobSucceeded || j.Spec.Settings.Kind() == core.JobCommand) {
		t.Fatalf("job %+v: a result in state %s", j, j.State)
	}
	if j.CancelReason != "" && j.State != core.JobCancelled {
		t.Fatalf("job %+v: a cancel reason in state %s", j, j.State)
	}
	if j.State == core.JobSucceeded && j.Spec.Settings.Kind() != core.JobCommand && j.ResultJob != j.ID {
		t.Fatalf("job %+v: succeeded without its result", j)
	}
}
