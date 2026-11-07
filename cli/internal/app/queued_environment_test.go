package app_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A job that a store holds queued without an environment, as an older daemon left it, never runs
// on the host by a guess: a task with a Dockerfile runs in its image, which has the tools a host
// does not, so a guess would fail the job far from its cause. The job is refused naming the cause,
// and nothing ran. A job that names the host runs there, so the host is only ever an explicit
// choice.
func TestAQueuedJobWithoutAnEnvironmentIsRefusedAndOnlyAnExplicitHostRunsOnTheHost(t *testing.T) {
	for _, tc := range []struct {
		name        string
		environment core.EnvironmentKind
		runs        bool
	}{
		{"unspecified", core.EnvironmentAuto, false},
		{"host", core.EnvironmentHost, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := newWorld(t)
			st, err := w.stores.Create(ctxFor(t), scope)
			if err != nil {
				t.Fatal(err)
			}
			spec := command("print", "ran", ";", "exit", "0")
			spec.Environment = tc.environment
			queued, err := st.Submit(ctxFor(t), core.Submission{Context: call, Spec: spec, At: w.clock.Now()})
			if err != nil {
				t.Fatal(err)
			}
			a := w.newApp()
			job := await(t, a, queued.Job.ID, "ended", func(j core.Job) bool { return j.State.Final() })
			if tc.runs {
				if job.State != core.JobSucceeded {
					t.Fatalf("a job that names the host ended %s, want succeeded", job.State)
				}
				return
			}
			if job.State != core.JobFailed {
				t.Fatalf("a job without an environment ended %s, want failed", job.State)
			}
			if started := w.started("ran"); started != 0 {
				t.Fatalf("a job without an environment ran %d times on the host", started)
			}
		})
	}
}
