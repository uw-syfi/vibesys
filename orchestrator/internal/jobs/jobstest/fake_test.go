package jobstest_test

import (
	"context"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/jobs/jobstest"
)

func TestFakeJobSource(t *testing.T) {
	jobstest.Contract(t, func(*testing.T) jobstest.Harness {
		f := jobstest.NewFakeJobSource()
		return jobstest.Harness{Source: f, Set: f.Set}
	})
}

func TestAFailedPollSaysNothingAboutJobs(t *testing.T) {
	f := jobstest.NewFakeJobSource()
	f.Set(core.JobUpdate{Job: "a", Run: "run", State: core.JobSucceeded})
	f.FailNext()
	if got, err := f.Jobs(context.Background(), core.Run{ID: "run"}); err == nil || got != nil {
		t.Fatalf("jobs = %v, err = %v", got, err)
	}
	if got, err := f.Jobs(context.Background(), core.Run{ID: "run"}); err != nil || len(got) != 1 {
		t.Fatalf("after the failure: jobs = %v, err = %v", got, err)
	}
}
