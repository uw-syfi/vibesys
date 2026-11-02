// Package jobstest holds the in-memory JobSource and the contract every JobSource passes.
package jobstest

import (
	"cmp"
	"context"
	"fmt"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/jobs"
)

// FakeJobSource is a JobSource over jobs a test sets. As with the real one, a final state does not
// change, and FailNext makes a poll fail without saying anything about any job.
type FakeJobSource struct {
	jobs     map[core.RunID]map[core.JobID]core.JobUpdate
	failNext bool
}

// NewFakeJobSource returns a source with no jobs.
func NewFakeJobSource() *FakeJobSource {
	return &FakeJobSource{jobs: map[core.RunID]map[core.JobID]core.JobUpdate{}}
}

// Set records where a job stands. A job already in a final state keeps it.
func (f *FakeJobSource) Set(update core.JobUpdate) {
	run := f.jobs[update.Run]
	if run == nil {
		run = map[core.JobID]core.JobUpdate{}
		f.jobs[update.Run] = run
	}
	if old, ok := run[update.Job]; ok && old.State.Final() {
		return
	}
	run[update.Job] = update
}

// FailNext makes the next call to Jobs fail.
func (f *FakeJobSource) FailNext() { f.failNext = true }

// Jobs implements jobs.JobSource.
func (f *FakeJobSource) Jobs(ctx context.Context, run core.Run) ([]core.JobUpdate, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if f.failNext {
		f.failNext = false
		return nil, fmt.Errorf("jobs of run %s: source unavailable", run.ID)
	}
	out := make([]core.JobUpdate, 0, len(f.jobs[run.ID]))
	for _, update := range f.jobs[run.ID] {
		out = append(out, update)
	}
	slices.SortFunc(out, func(a, b core.JobUpdate) int { return cmp.Compare(a.Job, b.Job) })
	return out, nil
}

// Harness is what the contract needs of an implementation under test.
type Harness struct {
	Source jobs.JobSource
	// Set makes a job stand at an update, as the running of real jobs would.
	Set func(core.JobUpdate)
}

// Contract is what every JobSource must do. A real source runs it with jobs it really ran.
func Contract(t *testing.T, newHarness func(t *testing.T) Harness) {
	t.Helper()
	run := core.Run{ID: "run"}
	list := func(t *testing.T, h Harness) []core.JobUpdate {
		t.Helper()
		got, err := h.Source.Jobs(context.Background(), run)
		if err != nil {
			t.Fatal(err)
		}
		return got
	}
	t.Run("a run with no jobs lists none", func(t *testing.T) {
		if got := list(t, newHarness(t)); len(got) != 0 {
			t.Fatalf("jobs = %v", got)
		}
	})
	t.Run("each job is listed once in id order", func(t *testing.T) {
		h := newHarness(t)
		h.Set(core.JobUpdate{Job: "b", Run: run.ID, State: core.JobRunning})
		h.Set(core.JobUpdate{Job: "a", Run: run.ID, State: core.JobQueued})
		h.Set(core.JobUpdate{Job: "a", Run: run.ID, State: core.JobRunning})
		got := list(t, h)
		if len(got) != 2 || got[0].Job != "a" || got[1].Job != "b" || got[0].State != core.JobRunning {
			t.Fatalf("jobs = %v", got)
		}
	})
	t.Run("a final state stays", func(t *testing.T) {
		h := newHarness(t)
		h.Set(core.JobUpdate{Job: "a", Run: run.ID, State: core.JobSucceeded})
		h.Set(core.JobUpdate{Job: "a", Run: run.ID, State: core.JobRunning})
		for range 2 {
			if got := list(t, h); len(got) != 1 || got[0].State != core.JobSucceeded {
				t.Fatalf("jobs = %v", got)
			}
		}
	})
	t.Run("jobs of another run are not listed", func(t *testing.T) {
		h := newHarness(t)
		h.Set(core.JobUpdate{Job: "a", Run: "other", State: core.JobRunning})
		if got := list(t, h); len(got) != 0 {
			t.Fatalf("jobs = %v", got)
		}
	})
	t.Run("a cancelled context is an error", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		if _, err := newHarness(t).Source.Jobs(ctx, run); err == nil {
			t.Fatal("no error")
		}
	})
}
