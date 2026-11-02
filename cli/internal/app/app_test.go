package app_test

import (
	"errors"
	"fmt"
	"math/rand"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

func TestAJobRunsAndSucceeds(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	job := submit(t, a, "print", "hello", ";", "exit", "0")
	final := awaitFinal(t, a, job.ID)
	if final.State != core.JobSucceeded || final.ExitCode == nil || *final.ExitCode != 0 {
		t.Fatalf("job = %+v, want succeeded with exit code 0", final)
	}
	if final.Environment == nil || final.Environment.Kind != core.EnvironmentHost {
		t.Errorf("the job did not pin its environment: %+v", final.Environment)
	}
	if got := logOf(t, a, job.ID); got != "hello\n" {
		t.Errorf("output = %q", got)
	}
	if final.StartedAt == nil || final.FinishedAt == nil || !final.FinishedAt.After(*final.StartedAt) {
		t.Errorf("times = %v .. %v", final.StartedAt, final.FinishedAt)
	}
}

func TestEveryEndOfACommandIsRecordedAsItIs(t *testing.T) {
	for _, tc := range []struct {
		name   string
		script []string
		state  core.JobState
		code   *int
		log    string
	}{
		{"exit 3", []string{"exit", "3"}, core.JobFailed, ptr(3), ""},
		{"time limit", []string{"timeout"}, core.JobFailed, nil, "ran past its time limit"},
		{"service never ready", []string{"service-not-ready"}, core.JobFailed, nil, "service was not ready"},
		{"service exited", []string{"service-exited"}, core.JobFailed, nil, "service exited"},
		{"environment cannot say", []string{"unknown"}, core.JobLost, nil, "could not say how the job ended"},
		{"cannot be attempted", []string{"fail", "no disk"}, core.JobFailed, nil, "no disk"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := newWorld(t)
			a := w.newApp()
			job := submit(t, a, tc.script...)
			final := awaitFinal(t, a, job.ID)
			if final.State != tc.state || !equalCode(final.ExitCode, tc.code) {
				t.Fatalf("job = %s code %v, want %s code %v", final.State, final.ExitCode, tc.state, tc.code)
			}
			if got := logOf(t, a, job.ID); !strings.Contains(got, tc.log) {
				t.Errorf("output %q does not say %q", got, tc.log)
			}
		})
	}
}

func ptr(n int) *int { return &n }

func equalCode(a, b *int) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

func TestJobsRunOneAtATimeInSubmissionOrder(t *testing.T) {
	rng := rand.New(rand.NewSource(3))
	for round := 0; round < 10; round++ {
		w := newWorld(t)
		a := w.newApp()
		n := 3 + rng.Intn(3)
		var jobs []core.Job
		for i := 0; i < n; i++ {
			jobs = append(jobs, submit(t, a, "print", fmt.Sprintf("job-%d", i), ";", "hold", fmt.Sprintf("g%d", i), ";", "exit", "0"))
		}
		for i, job := range jobs {
			awaitState(t, a, job.ID, core.JobRunning)
			// A job is recorded as running just before its command starts, so wait for the line
			// the command prints before it holds: from then on the command is active until its
			// gate opens.
			awaitOutput(t, a, job.ID, fmt.Sprintf("job-%d", i))
			// While job i runs, every other job is queued or already final, and in submission
			// order: the earlier ones are done and the later ones wait.
			all, err := a.Jobs(ctxFor(t), scope, core.JobFilter{})
			if err != nil {
				t.Fatal(err)
			}
			running := 0
			for j, other := range all {
				switch {
				case other.State == core.JobRunning:
					running++
					if j != i {
						t.Errorf("job %d runs while job %d is the oldest unfinished", j, i)
					}
				case j < i && other.State != core.JobSucceeded:
					t.Errorf("job %d is %s although job %d already runs", j, other.State, i)
				case j > i && other.State != core.JobQueued:
					t.Errorf("job %d is %s although job %d has not ended", j, other.State, i)
				}
			}
			if running != 1 || w.env.Active() != 1 {
				t.Fatalf("%d jobs and %d commands run at once, want 1", running, w.env.Active())
			}
			w.gates.Release(fmt.Sprintf("g%d", i))
			awaitState(t, a, job.ID, core.JobSucceeded)
		}
	}
}

func TestCancelAQueuedJobNeverRunsIt(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	first := submit(t, a, "print", "first", ";", "hold", "g")
	second := submit(t, a, "print", "second")
	awaitState(t, a, first.ID, core.JobRunning)
	got, err := a.Cancel(ctxFor(t), call, second.ID, "not needed")
	if err != nil {
		t.Fatal(err)
	}
	if got.State != core.JobCancelled || got.CancelReason != "not needed" {
		t.Fatalf("cancelled job = %+v", got)
	}
	w.gates.Release("g")
	awaitState(t, a, first.ID, core.JobSucceeded)
	if w.ran("second") != 0 {
		t.Fatal("a cancelled queued job ran")
	}
}

func TestCancelARunningJobEndsItAndWaitsForTheEnd(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	job := submit(t, a, "print", "up", ";", "hold", "g", ";", "print", "never")
	next := submit(t, a, "print", "next")
	awaitOutput(t, a, job.ID, "up")
	got, err := a.Cancel(ctxFor(t), call, job.ID, "enough")
	if err != nil {
		t.Fatal(err)
	}
	// Cancel returns only when the job is final, so by now the command is gone.
	if got.State != core.JobCancelled || got.CancelReason != "enough" {
		t.Fatalf("cancelled job = %+v", got)
	}
	awaitState(t, a, next.ID, core.JobSucceeded)
	if strings.Contains(logOf(t, a, job.ID), "never") {
		t.Error("the command went on after the cancel")
	}
	// Cancelling again, or a final job, returns it as it is.
	again, err := a.Cancel(ctxFor(t), call, job.ID, "twice")
	if err != nil || again.State != core.JobCancelled || again.CancelReason != "enough" {
		t.Fatalf("second cancel = %+v, %v; want the first end", again, err)
	}
}

func TestCancelsFromManyCallersAllSeeTheOneEnd(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	job := submit(t, a, "print", "up", ";", "hold", "g")
	awaitOutput(t, a, job.ID, "up")
	results := make(chan core.Job, 4)
	for i := 0; i < 4; i++ {
		go func() {
			j, err := a.Cancel(ctxFor(t), call, job.ID, fmt.Sprintf("caller %d", i))
			if err != nil {
				t.Error(err)
			}
			results <- j
		}()
	}
	var reason string
	for i := 0; i < 4; i++ {
		j := <-results
		if j.State != core.JobCancelled {
			t.Fatalf("a cancel returned %s", j.State)
		}
		if reason != "" && j.CancelReason != reason {
			t.Errorf("callers saw reasons %q and %q", reason, j.CancelReason)
		}
		reason = j.CancelReason
	}
}

func TestCheckAndMeasureAreRefusedNotQueued(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	for _, settings := range []core.Settings{core.CheckSettings{}, core.MeasureSettings{Samples: 3}} {
		spec := core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Settings: settings}
		_, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec})
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
			t.Errorf("Submit(%s) = %v, want unavailable", settings.Kind(), err)
		}
	}
	jobs, err := a.Jobs(ctxFor(t), scope, core.JobFilter{})
	if err != nil || len(jobs) != 0 {
		t.Fatalf("jobs = %v, %v; a refused submission queues nothing", jobs, err)
	}
}

func TestSubmitValidatesBeforeAnythingChanges(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	bad := []app.SubmitRequest{
		{Context: core.CallContext{Repository: "relative", Task: "t"}, Spec: command("exit", "0")},
		{Context: core.CallContext{Repository: "/r", Task: "../escape"}, Spec: command("exit", "0")},
		{Context: call, Spec: core.JobSpec{Commit: "abc", Environment: core.EnvironmentHost, Settings: core.CommandSettings{Argv: []string{"x"}}}},
		{Context: call, Spec: command("exit", "0"), Note: strings.Repeat("n", app.DefaultMaxNoteBytes+1)},
		{Context: call, Spec: core.JobSpec{Commit: commit}},
	}
	for i, req := range bad {
		var f *core.Failure
		if _, err := a.Submit(ctxFor(t), req); !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
			t.Errorf("request %d: error = %v, want invalid input", i, err)
		}
	}
	if jobs, _ := a.Jobs(ctxFor(t), scope, core.JobFilter{}); len(jobs) != 0 {
		t.Fatalf("%d jobs queued by invalid requests", len(jobs))
	}
}

func TestNotesAreKeptAndNeverChangeTheJob(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: command("hold", "g"), Note: "first"})
	if err != nil {
		t.Fatal(err)
	}
	awaitState(t, a, out.Job.ID, core.JobRunning)
	noted, err := a.AddNote(ctxFor(t), call, out.Job.ID, "second")
	if err != nil {
		t.Fatal(err)
	}
	if len(noted.Notes) != 2 || noted.Notes[0].Text != "first" || noted.Notes[1].Text != "second" || noted.Notes[1].Agent != "agent-1" {
		t.Fatalf("notes = %+v", noted.Notes)
	}
	if noted.Key != out.Job.Key || noted.State != core.JobRunning {
		t.Fatalf("a note changed the job: %+v", noted)
	}
	w.gates.Release("g")
	end := awaitFinal(t, a, out.Job.ID)
	if len(end.Notes) != 2 {
		t.Fatalf("the notes were lost at the end: %+v", end.Notes)
	}
	if _, err := a.AddNote(ctxFor(t), call, out.Job.ID, ""); err == nil {
		t.Error("an empty note was accepted")
	}
}

func TestWatchSendsCurrentStatesThenChangesAndEndsWhenAllAreFinal(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	one := submit(t, a, "hold", "g1")
	two := submit(t, a, "hold", "g2")
	awaitState(t, a, one.ID, core.JobRunning)

	var seen []string
	done := make(chan error, 1)
	first := make(chan struct{}, 8)
	go func() {
		done <- a.Watch(ctxFor(t), scope, []core.JobID{one.ID, two.ID}, func(j core.Job) error {
			seen = append(seen, describe(j))
			first <- struct{}{}
			return nil
		})
	}()
	<-first
	<-first
	w.gates.Release("g1")
	w.gates.Release("g2")
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if len(seen) < 4 || seen[0] != describe(core.Job{ID: one.ID, State: core.JobRunning}) || seen[1] != describe(core.Job{ID: two.ID, State: core.JobQueued}) {
		t.Fatalf("a watch starts with the current states, got %v", seen)
	}
	last := seen[len(seen)-2:]
	if !strings.Contains(strings.Join(last, " "), "succeeded") {
		t.Fatalf("a watch ends on the final states, got %v", seen)
	}
}

func TestLogsFollowTheOutputUntilTheJobEnds(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	job := submit(t, a, "print", "one", ";", "hold", "g", ";", "print", "two")
	awaitOutput(t, a, job.ID, "one")
	var got strings.Builder
	done := make(chan error, 1)
	go func() {
		done <- a.ReadLogs(ctxFor(t), scope, job.ID, 0, true, func(c core.LogChunk) error {
			got.WriteString(string(c.Data))
			return nil
		})
	}()
	w.gates.Release("g")
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if got.String() != "one\ntwo\n" {
		t.Fatalf("followed output = %q", got.String())
	}
	// From an offset, a reader gets the rest and no byte twice.
	var rest strings.Builder
	if err := a.ReadLogs(ctxFor(t), scope, job.ID, 4, false, func(c core.LogChunk) error { rest.WriteString(string(c.Data)); return nil }); err != nil {
		t.Fatal(err)
	}
	if rest.String() != "two\n" {
		t.Fatalf("output from offset 4 = %q", rest.String())
	}
}

func TestAnUnavailableEnvironmentFailsTheJobAndRunsNothingElsewhere(t *testing.T) {
	w := newWorld(t)
	a := newAppWith(w, fakeenv.Environments{}) // no environment of any kind
	job := submit(t, a, "print", "x")
	final := awaitFinal(t, a, job.ID)
	if final.State != core.JobFailed {
		t.Fatalf("job = %s, want failed", final.State)
	}
	if got := logOf(t, a, job.ID); !strings.Contains(got, "could not be started") || !strings.Contains(got, "environment") {
		t.Errorf("output %q does not say why", got)
	}
	if len(w.env.Runs()) != 0 {
		t.Fatal("the job ran in an environment it did not choose")
	}
}

func newAppWith(w *world, envs environment.Environments) *app.App {
	a := app.New(instant(app.Config{Clock: w.clock, Stores: w.stores, Environments: envs, Supervisors: w.supervisors()}))
	w.apps = append(w.apps, a)
	if err := a.Start(ctxFor(w.t)); err != nil {
		w.t.Fatal(err)
	}
	return a
}

func TestStatusCountsTheWork(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	one := submit(t, a, "hold", "g")
	submit(t, a, "exit", "0")
	awaitState(t, a, one.ID, core.JobRunning)
	if st := a.Status(); st.Running != 1 || st.Queued != 1 {
		t.Fatalf("status = %+v, want 1 running, 1 queued", st)
	}
}

var _ = lifecycle.Ref{}
