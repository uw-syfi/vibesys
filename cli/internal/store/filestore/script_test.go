package filestore_test

import (
	"context"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

const stateDir = "state"

// runs are the runs the script appends events to.
var runs = []core.RunID{"run-a", "run-b"}

func at(n int) time.Time { return time.Date(2026, 11, 1, 0, 0, n, 0, time.UTC) }

func openOn(t *testing.T, f fsys.FS, opts ...filestore.Option) *filestore.Store {
	t.Helper()
	s, err := filestore.Open(stateDir, append([]filestore.Option{filestore.WithFS(f)}, opts...)...)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	return s
}

// op is one call to a store, the unit a crash lands in the middle of.
type op struct {
	name string
	run  func(ctx context.Context, s store.Store) error
}

func submit(name string, sub core.Submission) op {
	return op{name, func(ctx context.Context, s store.Store) error { _, err := s.Submit(ctx, sub); return err }}
}

// pinned is the environment the script's jobs run in.
func pinned() *core.Fingerprint {
	return &core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"go": "1.25"}}
}

func move(name string, tr core.Transition) op {
	return op{name, func(ctx context.Context, s store.Store) error { _, err := s.Transition(ctx, tr); return err }}
}

func promotion(name string, c, previous core.Commit, n int) op {
	return op{name, func(ctx context.Context, s store.Store) error {
		return s.RecordPromotion(ctx, core.AssumeApproved(core.Promotion{
			Commit: c, PreviousHead: previous, Verdict: core.Verdict{Kind: core.Better, CandidateSamples: 3, BaseSamples: 3, CandidateMean: 2.5, BaseMean: 2, Ratio: 1.25, Noise: 0.5},
			Environment: core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU}, ResultJob: "j1", At: at(n),
		}))
	}}
}

func bound(name, objective string, value float64, n int) op {
	return op{name, func(ctx context.Context, s store.Store) error {
		_, err := s.DeclareBound(ctx, core.NewBound{
			Origin:    core.RecordOrigin{Run: "run-a", Agent: "lead", At: at(n)},
			Objective: objective, Direction: core.Maximize, Value: value, Unit: "ops/s", Note: "derived from the memory bandwidth",
		})
		return err
	}}
}

func startWorkstream(name, title string, n int) op {
	return op{name, func(ctx context.Context, s store.Store) error {
		_, err := s.StartWorkstream(ctx, core.NewWorkstream{Title: title, Origin: core.RecordOrigin{Run: "run-a", Agent: "lead", At: at(n)}})
		return err
	}}
}

func frontier(name string, from uint64, members ...core.Commit) op {
	return op{name, func(ctx context.Context, s store.Store) error {
		var ms []core.FrontierMember
		for i, c := range members {
			ms = append(ms, core.FrontierMember{Commit: c, AddedAt: at(40 + i)})
		}
		_, err := s.UpdateFrontier(ctx, core.AssumeAdmitted(from, ms))
		return err
	}}
}

func appendEvent(name string, run core.RunID, detail core.EventDetail, n int) op {
	return op{name, func(ctx context.Context, s store.Store) error {
		_, err := s.AppendEvent(ctx, core.EventDraft{
			SchemaVersion: 1, Run: run, HostID: "host-a", Task: "kv-store", Commit: "c1", Harness: "h", Agent: "agent-a", At: at(n),
			Command: core.CommandWait, Outcome: core.OutcomeSucceeded, Job: "j1", Detail: detail,
		})
		return err
	}}
}

func appendLog(name string, job core.JobID, data string) op {
	return op{name, func(ctx context.Context, s store.Store) error { return s.AppendLog(ctx, job, []byte(data)) }}
}

func measureSpec(c core.Commit) core.JobSpec {
	return core.JobSpec{Commit: c, Settings: core.MeasureSettings{Samples: 3}}
}

func savedResult(job core.JobID, key core.JobKey, c core.Commit) *core.Result {
	return &core.Result{
		Job: job, Key: key, Task: "kv-store", TaskVersion: "v1", Commit: c, SchemaVersion: core.SchemaVersion,
		Environment: *pinned(),
		Machine:     core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "amd64", LogicalCPUs: 8, MemoryBytes: 1 << 34},
		Load:        core.MachineLoad{SampledAt: at(20), LoadAverage1m: 0.25},
		RecordedAt:  at(21),
		Outcome: core.MeasureOutcome{
			Metric: core.Metric{Name: "ops", Direction: core.Maximize}, Samples: []float64{1.5, 2.5, 3.5},
			Observations: []core.Observation{{Name: "p99", Values: []float64{0.1}}}, CheckJob: "j-check",
			Objectives: core.Objectives{
				Metrics:       []core.Metric{{Name: "ops", Direction: core.Maximize}, {Name: "p99", Direction: core.Minimize}},
				RelativeNoise: 0.02,
			},
		},
	}
}

// script is a workload that makes every kind of change a store makes: a job created, joined,
// moved, finished with a result and reused; plans created and edited; promotions; two revisions of
// the frontier; events of two
// runs; a log appended in pieces; and a workstream started, joined by a plan and edited. Each operation is one call to the store, so a crash during
// it leaves the state before the call or the state after it.
func script() []op {
	note := func(text string, n int) *core.Note { return &core.Note{Text: text, At: at(n), Agent: "agent-a"} }
	return []op{
		submit("create a measure job", core.Submission{Spec: measureSpec("c1"), Key: "k1", Note: note("first", 1), At: at(1)}),
		submit("join it with a note, from a second run", core.Submission{Context: core.CallContext{Run: "run-b", Agent: "lead"}, Spec: measureSpec("c1"), Key: "k1", Note: note("second", 2), At: at(2)}),
		{"create a plan", func(ctx context.Context, s store.Store) error {
			_, err := s.CreatePlan(ctx, core.NewPlan{Title: "tile the loop", Body: "try 8x8", Origin: core.RecordOrigin{Run: "run-a", Agent: "lead", At: at(3)}})
			return err
		}},
		submit("link the plan", core.Submission{Spec: measureSpec("c1"), Key: "k1", Plan: 1, At: at(4)}),
		move("start the job", core.Transition{Job: "j1", From: core.JobQueued, To: core.JobRunning, At: at(5), Environment: pinned()}),
		move("finish it with a result", core.Transition{
			Job: "j1", From: core.JobRunning, To: core.JobSucceeded, At: at(6), Result: savedResult("j1", "k1", "c1"),
		}),
		submit("reuse the result with a note, from a third run", core.Submission{Context: core.CallContext{Run: "run-c", Agent: "lead"}, Spec: measureSpec("c1"), Key: "k1", Note: note("third", 7), At: at(7)}),
		submit("create a check job", core.Submission{Spec: core.JobSpec{Commit: "c2", Settings: core.CheckSettings{}}, Key: "k2", At: at(8)}),
		move("start the check", core.Transition{Job: "j2", From: core.JobQueued, To: core.JobRunning, At: at(9), Environment: pinned()}),
		move("fail the check", core.Transition{Job: "j2", From: core.JobRunning, To: core.JobFailed, At: at(10)}),
		{"note the check", func(ctx context.Context, s store.Store) error {
			_, err := s.AddNote(ctx, "j2", core.Note{Text: "flaky", At: at(11), Agent: "agent-b"})
			return err
		}},
		{"edit the plan", func(ctx context.Context, s store.Store) error {
			status := core.PlanChosen
			_, err := s.EditPlan(ctx, 1, core.PlanEdit{Status: &status, Comment: &core.PlanComment{Text: "go", At: at(12), Agent: "agent-a"}, Origin: core.RecordOrigin{Run: "run-b", Agent: "lead", At: at(12)}})
			return err
		}},
		bound("declare a bound", "ops", 15000, 12),
		bound("revise it", "ops", 12000, 13),
		promotion("record a promotion", "c1", "c0", 13),
		promotion("record a second promotion", "c4", "c1", 14),
		frontier("admit a commit to the frontier", 0, "c1"),
		frontier("admit another beside it", 1, "c1", "c4"),
		appendEvent("append the first event of run-a", "run-a", nil, 14),
		appendEvent("append a second event with a detail", "run-a", core.Verdict{Kind: core.WithinNoise}, 15),
		appendEvent("append the first event of run-b", "run-b", nil, 16),
		appendLog("append to the log of j1", "j1", "hello "),
		appendLog("append again", "j1", "world"),
		appendLog("append to the log of j2", "j2", "boom"),
		submit("create a command job", core.Submission{Spec: core.JobSpec{Commit: "c3", Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"make", "test"}}}, At: at(30)}),
		move("cancel it", core.Transition{Job: "j3", From: core.JobQueued, To: core.JobCancelled, At: at(31), CancelReason: "not needed"}),
		startWorkstream("start a workstream", "KV cache layout", 32),
		{"move the plan into it", func(ctx context.Context, s store.Store) error {
			workstream := core.WorkstreamNumber(1)
			_, err := s.EditPlan(ctx, 1, core.PlanEdit{Workstream: &workstream, Origin: core.RecordOrigin{Run: "run-b", Agent: "planner", At: at(33)}})
			return err
		}},
		{"pause the workstream", func(ctx context.Context, s store.Store) error {
			status, note := core.WorkstreamPaused, "paused: accuracy gate"
			_, err := s.EditWorkstream(ctx, 1, core.WorkstreamEdit{Status: &status, Note: &note, At: at(34)})
			return err
		}},
	}
}
