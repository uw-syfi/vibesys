// Package storetest is the one contract suite every store.Store passes. It covers each sentence
// of the Store documentation: joining and reuse, notes and plan links, conflicts, a result saved
// with its state, numbering, gapless event positions, log offsets, the failure kinds and a
// cancelled context.
//
// An implementation runs it from a test with Run. The suite never skips a case for an
// implementation, so a weaker store fails it instead of passing it quietly. It reaches a store
// only through store.Store; what an implementation adds (reopening, faults) is tested beside it.
package storetest

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// New builds an empty Store for one test, opened with the rules. A test that needs the store to
// outlive it ends it in t.Cleanup.
type New func(t *testing.T, rules store.Rules) store.Store

// Run runs the contract against stores New builds.
func Run(t *testing.T, newStore New) {
	t.Helper()
	groups := []struct {
		name string
		run  func(*testing.T, New)
	}{
		{"submit", testSubmit},
		{"transition", testTransition},
		{"notes", testNotes},
		{"jobs", testJobs},
		{"results", testResults},
		{"plans", testPlans},
		{"events", testEvents},
		{"logs", testLogs},
		{"values", testValues},
		{"failures", testFailures},
		{"cancelled context", testCancelled},
		{"concurrency", testConcurrency},
	}
	for _, g := range groups {
		t.Run(g.name, func(t *testing.T) { g.run(t, newStore) })
	}
}

// env is one store under test with a clock that moves only when the test asks for a time.
type env struct {
	t     *testing.T
	s     store.Store
	ctx   context.Context
	ticks int
}

// newEnv opens a store with the rules, the zero Rules when none are given.
func newEnv(t *testing.T, newStore New, rules ...store.Rules) *env {
	t.Helper()
	var r store.Rules
	if len(rules) > 0 {
		r = rules[0]
	}
	return &env{t: t, s: newStore(t, r), ctx: context.Background()}
}

var epoch = time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)

// now is a new instant, a second after the last.
func (e *env) now() time.Time {
	e.ticks++
	return epoch.Add(time.Duration(e.ticks) * time.Second)
}

func commit(n int) core.Commit { return core.Commit(fmt.Sprintf("%040x", n)) }

func (e *env) check(c int, key string) core.Submission {
	return core.Submission{Spec: core.JobSpec{Commit: commit(c), Settings: core.CheckSettings{}}, Key: core.JobKey(key), At: e.now()}
}

func (e *env) measure(c int, key string, samples uint32) core.Submission {
	return core.Submission{
		Spec: core.JobSpec{Commit: commit(c), Settings: core.MeasureSettings{Samples: samples}},
		Key:  core.JobKey(key), At: e.now(),
	}
}

func (e *env) command(c int, argv ...string) core.Submission {
	return core.Submission{Spec: core.JobSpec{Commit: commit(c), Settings: core.CommandSettings{Argv: argv}}, At: e.now()}
}

func (e *env) note(text string) *core.Note {
	return &core.Note{Text: text, At: e.now(), Agent: "agent"}
}

func (e *env) submit(sub core.Submission) core.SubmitOutcome {
	e.t.Helper()
	out, err := e.s.Submit(e.ctx, sub)
	if err != nil {
		e.t.Fatalf("Submit(%+v): %v", sub.Spec, err)
	}
	return out
}

func (e *env) job(id core.JobID) core.Job {
	e.t.Helper()
	job, err := e.s.Job(e.ctx, id)
	if err != nil {
		e.t.Fatalf("Job(%s): %v", id, err)
	}
	return job
}

func (e *env) jobs(filter core.JobFilter) []core.Job {
	e.t.Helper()
	jobs, err := e.s.Jobs(e.ctx, filter)
	if err != nil {
		e.t.Fatalf("Jobs(%+v): %v", filter, err)
	}
	return jobs
}

func (e *env) results(filter core.ResultFilter) []core.Result {
	e.t.Helper()
	results, err := e.s.Results(e.ctx, filter)
	if err != nil {
		e.t.Fatalf("Results(%+v): %v", filter, err)
	}
	return results
}

// pinned is the environment the suite's jobs run in and its results are taken in.
func pinned() core.Fingerprint {
	return core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"go": "1.25"}}
}

// move applies the transition between two states that core allows, with what the move needs: the
// environment a job runs in. A move to succeeded needs a result and goes through finish.
func (e *env) move(id core.JobID, from, to core.JobState) core.Job {
	e.t.Helper()
	tr := core.Transition{Job: id, From: from, To: to, At: e.now()}
	if to == core.JobRunning {
		fp := pinned()
		tr.Environment = &fp
	}
	job, err := e.s.Transition(e.ctx, tr)
	if err != nil {
		e.t.Fatalf("Transition(%s %s->%s): %v", id, from, to, err)
	}
	return job
}

// resultAt is a result core accepts for the job once it has run in the pinned environment, taken
// at the instant, with the outcome its kind records: a measurement of the samples, naming a
// check pass.
func resultAt(job core.Job, at time.Time, samples ...float64) core.Result {
	r := core.Result{
		Job: job.ID, Key: job.Key, Task: "kv-store", TaskVersion: "v1", Commit: job.Spec.Commit, SchemaVersion: core.SchemaVersion,
		Environment: pinned(),
		Machine:     core.MachineFacts{HostID: "host", OS: "linux", Arch: "amd64", LogicalCPUs: 8, MemoryBytes: 1 << 34},
		Load:        core.MachineLoad{SampledAt: at, LoadAverage1m: 0.25},
		RecordedAt:  at,
		Outcome:     core.CheckOutcome{Duration: time.Second},
	}
	if job.Spec.Settings.Kind() == core.JobMeasure {
		r.Outcome = core.MeasureOutcome{Metric: core.Metric{Name: "ops", Direction: core.Maximize}, Samples: samples, CheckJob: "j-check"}
	}
	return r
}

func (e *env) resultFor(job core.Job, samples ...float64) core.Result {
	return resultAt(job, e.now(), samples...)
}

// finish runs a job to success, saving res with it. Only a command job succeeds with no result.
func (e *env) finish(id core.JobID, res *core.Result) core.Job {
	e.t.Helper()
	e.move(id, core.JobQueued, core.JobRunning)
	tr := core.Transition{Job: id, From: core.JobRunning, To: core.JobSucceeded, At: e.now(), Result: res}
	if res != nil {
		if measured, ok := res.Outcome.(core.MeasureOutcome); ok {
			tr.CheckJob = measured.CheckJob
		}
	}
	job, err := e.s.Transition(e.ctx, tr)
	if err != nil {
		e.t.Fatalf("finishing %s: %v", id, err)
	}
	return job
}

// reach takes a queued job to a state, by moves core allows. A job that succeeds saves a result,
// unless it is a command job.
func (e *env) reach(job core.Job, state core.JobState) {
	e.t.Helper()
	switch state {
	case core.JobQueued:
	case core.JobRunning, core.JobFailed, core.JobLost:
		e.move(job.ID, core.JobQueued, core.JobRunning)
		if state != core.JobRunning {
			e.move(job.ID, core.JobRunning, state)
		}
	case core.JobSucceeded:
		var res *core.Result
		if job.Spec.Settings.Kind() != core.JobCommand {
			r := e.resultFor(job, 1, 2, 3)
			res = &r
		}
		e.finish(job.ID, res)
	default:
		e.move(job.ID, core.JobQueued, state)
	}
}

// savedJob submits a job with a key and runs it to success with a result.
func (e *env) savedJob(sub core.Submission, samples ...float64) core.Job {
	e.t.Helper()
	out := e.submit(sub)
	res := e.resultFor(out.Job, samples...)
	return e.finish(out.Job.ID, &res)
}

func wantKind(t *testing.T, what string, err error, kind core.FailureKind) {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) {
		t.Errorf("%s: error = %v, want a *core.Failure of kind %s", what, err, kind)
		return
	}
	if f.Kind != kind {
		t.Errorf("%s: failure kind = %s (%s), want %s", what, f.Kind, f.Message, kind)
	}
	if strings.TrimSpace(f.Message) == "" {
		t.Errorf("%s: the failure has no message", what)
	}
}

// state is everything observable through the interface.
type state struct {
	Jobs       []core.Job
	Results    []core.Result
	Promotions []core.Promotion
	Plans      []core.Plan
	Events     map[core.RunID][]core.Event
	Logs       map[core.JobID][]byte
}

// snapshot reads the whole store, including the events of the runs and the logs of the jobs it
// finds, and the events of the runs listed.
func snapshot(t *testing.T, s store.Store, runs ...core.RunID) state {
	t.Helper()
	ctx := context.Background()
	var st state
	var err error
	if st.Jobs, err = s.Jobs(ctx, core.JobFilter{}); err != nil {
		t.Fatalf("Jobs: %v", err)
	}
	if st.Results, err = s.Results(ctx, core.ResultFilter{}); err != nil {
		t.Fatalf("Results: %v", err)
	}
	if st.Promotions, err = s.Promotions(ctx); err != nil {
		t.Fatalf("Promotions: %v", err)
	}
	if st.Plans, err = s.Plans(ctx, core.PlanFilter{}); err != nil {
		t.Fatalf("Plans: %v", err)
	}
	st.Events = map[core.RunID][]core.Event{}
	for _, run := range runs {
		if st.Events[run], err = s.Events(ctx, run, 0, 0); err != nil {
			t.Fatalf("Events(%s): %v", run, err)
		}
	}
	st.Logs = map[core.JobID][]byte{}
	for _, job := range st.Jobs {
		chunk, err := s.ReadLog(ctx, job.ID, 0, 0)
		if err != nil {
			t.Fatalf("ReadLog(%s): %v", job.ID, err)
		}
		st.Logs[job.ID] = chunk.Data
	}
	return st
}

// unchanged fails when the store is not as it was in before.
func unchanged(t *testing.T, what string, s store.Store, before state, runs ...core.RunID) {
	t.Helper()
	if after := snapshot(t, s, runs...); !reflect.DeepEqual(before, after) {
		t.Errorf("%s changed the store:\nbefore %+v\nafter  %+v", what, before, after)
	}
}

// Observe reads everything a Store shows: its jobs, results, promotions and plans, the logs of its
// jobs, and the events of the runs listed. Two Observations are equal exactly when a caller
// could not tell the stores apart, which is how a test compares a store with itself after a
// restart or a crash. Compare them with reflect.DeepEqual.
func Observe(t *testing.T, s store.Store, runs ...core.RunID) any {
	t.Helper()
	return snapshot(t, s, runs...)
}
