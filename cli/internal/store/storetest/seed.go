package storetest

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// HostEnvironment is a pinned host environment, the one seeded results are taken in unless a test
// says otherwise.
func HostEnvironment() core.Fingerprint { return pinned() }

// DockerEnvironment is a pinned docker environment, for a test of what must not mix with the
// host's.
func DockerEnvironment() core.Fingerprint {
	return core.Fingerprint{
		Kind: core.EnvironmentDocker, Backend: core.BackendCPU,
		ImageDigest: "sha256:" + strings.Repeat("ab", 32),
	}
}

// Measurement is a saved measurement a test puts into a store directly, as if a measure job had
// run to the end. Zero fields take the defaults noted.
type Measurement struct {
	Commit  core.Commit
	Samples []float64
	// At is when the measurement is recorded. It must not be zero.
	At time.Time
	// Environment defaults to HostEnvironment.
	Environment core.Fingerprint
	// TaskVersion defaults to "v1".
	TaskVersion string
	// Metric defaults to "ops", maximized.
	Metric core.Metric
	// CheckJob is the check pass the measurement rests on; the empty default is the measurement's
	// own job, as when the check ran inside it.
	CheckJob core.JobID
}

// SeedMeasurement saves a measurement of a commit through the store's own interface, by running a
// measure job through its states, and returns the result as the store holds it. A job is created
// for every call, with a key of its own, so seeding never joins or reuses anything.
func SeedMeasurement(t testing.TB, s store.Store, m Measurement) core.Result {
	t.Helper()
	if m.Environment.Kind == "" {
		m.Environment = HostEnvironment()
	}
	if m.TaskVersion == "" {
		m.TaskVersion = "v1"
	}
	if m.Metric.Name == "" {
		m.Metric = core.Metric{Name: "ops", Direction: core.Maximize}
	}
	return seed(t, s, m.Commit, core.MeasureSettings{Samples: uint32(len(m.Samples))}, m.At, m.Environment, m.TaskVersion,
		func(job core.Job) core.Outcome {
			check := m.CheckJob
			if check == "" {
				check = job.ID
			}
			return core.MeasureOutcome{Metric: m.Metric, Samples: m.Samples, CheckJob: check}
		})
}

// SeedCheck saves a passed check of a commit in the host environment under task version "v1",
// recorded at the instant, and returns the result as the store holds it.
func SeedCheck(t testing.TB, s store.Store, commit core.Commit, at time.Time) core.Result {
	t.Helper()
	return SeedCheckIn(t, s, commit, at, HostEnvironment(), "v1")
}

// SeedCheckIn is SeedCheck in an environment and under a task version.
func SeedCheckIn(t testing.TB, s store.Store, commit core.Commit, at time.Time, environment core.Fingerprint, taskVersion string) core.Result {
	t.Helper()
	return seed(t, s, commit, core.CheckSettings{}, at, environment, taskVersion,
		func(core.Job) core.Outcome { return core.CheckOutcome{Duration: time.Second} })
}

func seed(t testing.TB, s store.Store, commit core.Commit, settings core.Settings, at time.Time, environment core.Fingerprint, taskVersion string, outcome func(core.Job) core.Outcome) core.Result {
	t.Helper()
	ctx := context.Background()
	jobs, err := s.Jobs(ctx, core.JobFilter{})
	if err != nil {
		t.Fatalf("seeding: Jobs: %v", err)
	}
	out, err := s.Submit(ctx, core.Submission{
		Spec: core.JobSpec{Commit: commit, Environment: environment.Kind, Settings: settings},
		Key:  core.JobKey(fmt.Sprintf("seed-%d", len(jobs)+1)),
		At:   at,
	})
	if err != nil {
		t.Fatalf("seeding: Submit: %v", err)
	}
	job := out.Job
	if _, err := s.Transition(ctx, core.Transition{Job: job.ID, From: core.JobQueued, To: core.JobRunning, At: at, Environment: &environment}); err != nil {
		t.Fatalf("seeding: running: %v", err)
	}
	result := core.Result{
		Job: job.ID, Key: job.Key, Task: "kv-store", TaskVersion: taskVersion, Commit: commit,
		SchemaVersion: core.SchemaVersion, Environment: environment,
		Machine:    core.MachineFacts{HostID: "host", OS: "linux", Arch: "amd64", LogicalCPUs: 8, MemoryBytes: 1 << 34},
		Load:       core.MachineLoad{SampledAt: at, LoadAverage1m: 0.25},
		RecordedAt: at, Outcome: outcome(job),
	}
	tr := core.Transition{Job: job.ID, From: core.JobRunning, To: core.JobSucceeded, At: at, Result: &result}
	if measured, ok := result.Outcome.(core.MeasureOutcome); ok {
		tr.CheckJob = measured.CheckJob
	}
	if _, err := s.Transition(ctx, tr); err != nil {
		t.Fatalf("seeding: succeeded: %v", err)
	}
	return result
}
