package daemon_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
)

// Completing a setup is decided on a read made at that moment, by the service and not the panel's
// observation: whatever the cache says, the verdict is the world's now. These tests drive a whole daemon
// over fakes, with no real time: the clock is the rig's and the reads are the fake's.

var (
	baseCommit   = strings.Repeat("a", 40)
	mutantCommit = strings.Repeat("b", 40)
)

// passingFacts are the facts of a task whose six checks all hold, read at version v1.
func passingFacts(task string) setup.Facts {
	zero, one := int32(0), int32(1)
	at := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	return setup.Facts{
		Task: task, Version: "v1", Card: core.TaskCard{Task: task, Heading: "working tree"},
		Decl: setup.Declarations{Objectives: []setup.Objective{{Name: "qps", Unit: "ops"}}, CheckDeclared: true, BenchmarkDeclared: true},
		Base: setup.Base{Commit: baseCommit, Version: "v1"},
		Jobs: []setup.Job{
			{ID: "j1", Kind: setup.KindCheck, State: setup.JobSucceeded, Commit: baseCommit, Version: "v1", ExitCode: &zero, Relation: setup.RelationBase, SubmittedAt: at, FinishedAt: at},
			{ID: "j2", Kind: setup.KindCheck, State: setup.JobFailed, Commit: mutantCommit, Version: "v1", ExitCode: &one, FailedStage: setup.StageCheck,
				Relation: setup.RelationDescendant, CodeChanged: true, SubmittedAt: at, FinishedAt: at},
		},
		Measurements: []setup.Measurement{{
			Commit: baseCommit, Version: "v1", Metric: "qps", Unit: "ops", Value: 100, Samples: []float64{100, 100.5, 99.5, 100.2, 99.8},
		}},
	}
}

// completionRig is a rig with a run in setup whose lead has declared task "kv", read through a fake
// that starts out as the given facts.
type completionRig struct {
	*daemontest.Rig
	reads *daemontest.FakeSetupReads
	run   core.Run
	lead  core.Agent
	seen  int
}

func newCompletionRig(t *testing.T, facts setup.Facts) *completionRig {
	t.Helper()
	rig := daemontest.NewRig(t)
	rig.JobTool = core.JobToolEndpoint{Executable: "/cli/1", RuntimeDirectory: "/run/1"}
	rig.Starts.Link("/link", "/repo")
	rig.Tasks.Set("kv", core.TaskCard{Task: "kv", Heading: "committed"})
	reads := daemontest.NewFakeSetupReads(facts)
	rig.Setup, rig.SetupReads = setup.NewStore(), reads
	rig.Restart(t)
	r := &completionRig{Rig: rig, reads: reads}
	receipt := dispatch(t, rig, &pb.Command{Id: "begin", Body: &pb.Command_BeginSetup{BeginSetup: &pb.BeginSetup{
		Repository: "/link", Brief: "make a task", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}})
	if receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("begin refused: %v", receipt)
	}
	rig.Settle(t)
	r.run, _ = rig.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	r.lead, _ = rig.Daemon.State().Agent(r.run.Lead)
	r.declare(t, "kv")
	return r
}

// declare is the lead naming its task.
func (r *completionRig) declare(t *testing.T, task string) {
	t.Helper()
	r.seen++
	answer, err := r.Daemon.Engine.Command(core.Command{
		ID: core.CommandID("declare-" + task + "-" + string(rune('a'+r.seen))), Digest: core.Digest(strings.Repeat("a", 64)), Caller: core.AgentCaller{Agent: r.lead.ID}, Body: core.DeclareTask{Run: r.run.ID, Task: task},
	})
	if err != nil || answer.Receipt.Refusal != "" {
		t.Fatalf("declare %s: %v %+v", task, err, answer.Receipt)
	}
	r.Settle(t)
}

func (r *completionRig) complete(t *testing.T, id string, task string) (*pb.Receipt, error) {
	t.Helper()
	resp, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: id, Body: &pb.Command_CompleteSetup{CompleteSetup: &pb.CompleteSetup{
		RunId: string(r.run.ID), Task: task, FanoutLimit: 2, Brief: "go fast",
		Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}}})
	if err != nil {
		return nil, err
	}
	r.Settle(t)
	return resp.GetReceipt(), nil
}

func (r *completionRig) state() core.Run {
	run, _ := r.Daemon.State().Run(r.run.ID)
	return run
}

func TestBeginningASetupRecordsTheCanonicalRepositoryAndTheJobTool(t *testing.T) {
	r := newCompletionRig(t, passingFacts("kv"))
	if r.run.Repository != "/repo" || r.run.JobTool != r.JobTool || r.run.Phase != core.PhaseSetup {
		t.Fatalf("the run: %+v", r.run)
	}
}

func TestCompletionIsDecidedOnAFreshReadWhateverThePanelShows(t *testing.T) {
	t.Run("the panel says not ready and the world is", func(t *testing.T) {
		stale := passingFacts("kv")
		stale.Jobs = nil
		r := newCompletionRig(t, stale)
		r.advanceThroughReads(t)
		if v := r.snapshotChecks(t); v.unmet == 0 {
			t.Fatalf("the cache should say not ready: %+v", v)
		}
		r.reads.Set(passingFacts("kv"))
		receipt, err := r.complete(t, "c1", "kv")
		if err != nil || receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
			t.Fatalf("receipt %v, err %v", receipt, err)
		}
		run := r.state()
		if run.Phase != core.PhaseOptimize || run.Task != "kv" || run.TaskCard == nil || run.TaskCard.Heading != "committed" {
			t.Fatalf("after completion: %+v", run)
		}
		var committed bool
		for _, ref := range r.Tasks.Calls() {
			committed = committed || ref.Source == daemon.TaskCommitted && ref.Revision == baseCommit && ref.Task == "kv"
		}
		if !committed {
			t.Fatalf("the card was not read at the base commit: %+v", r.Tasks.Calls())
		}
	})
	t.Run("the panel says ready and the world is not", func(t *testing.T) {
		r := newCompletionRig(t, passingFacts("kv"))
		r.advanceThroughReads(t)
		if v := r.snapshotChecks(t); v.unmet != 0 {
			t.Fatalf("the cache should say ready: %+v", v)
		}
		broken := passingFacts("kv")
		broken.Version = "v2" // the task was edited since: every fact is stale
		r.reads.Set(broken)
		receipt, err := r.complete(t, "c1", "kv")
		if err != nil || receipt.GetRefusal() != pb.Refusal_REFUSAL_SETUP_INCOMPLETE || len(receipt.GetUnmetChecks()) == 0 {
			t.Fatalf("receipt %v, err %v", receipt, err)
		}
		for _, c := range receipt.GetUnmetChecks() {
			if c == pb.SetupCheckId_SETUP_CHECK_ID_OBJECTIVE || c == pb.SetupCheckId_SETUP_CHECK_ID_CORRECTNESS_CHECK {
				t.Fatalf("a declared check is not unmet: %v", receipt.GetUnmetChecks())
			}
		}
		if run := r.state(); run.Phase != core.PhaseSetup || run.PendingLead != nil {
			t.Fatalf("a refused completion changed the run: %+v", run)
		}
	})
}

type checksView struct{ unmet int }

func (r *completionRig) snapshotChecks(t *testing.T) checksView {
	t.Helper()
	obs, ok := r.Setup.Get(r.run.ID)
	if !ok {
		t.Fatal("nothing observed")
	}
	return checksView{unmet: len(obs.Derived(setup.Standard).Unmet())}
}

// advanceThroughReads lets the daemon read the run once.
func (r *completionRig) advanceThroughReads(t *testing.T) {
	t.Helper()
	for range 10 {
		r.Clock.Advance(time.Second)
		r.Settle(t)
		if _, ok := r.Setup.Get(r.run.ID); ok {
			return
		}
	}
	t.Fatal("the run was never read")
}

func TestAReadThatFailsOrFindsTheTaskUnreadableCompletesNothing(t *testing.T) {
	r := newCompletionRig(t, passingFacts("kv"))
	r.reads.Fail(errors.New("the job tool is away"))
	_, err := r.complete(t, "c1", "kv")
	if status.Code(err) != codes.Unavailable || !strings.Contains(err.Error(), "away") {
		t.Fatalf("a failed read: %v", err)
	}
	r.reads.Fail(nil)
	r.reads.Unreadable("manifest half written")
	_, err = r.complete(t, "c2", "kv")
	if status.Code(err) != codes.Unavailable || !strings.Contains(err.Error(), "half written") {
		t.Fatalf("an unreadable task: %v", err)
	}
	if run := r.state(); run.Phase != core.PhaseSetup {
		t.Fatalf("a read that could not be made completed the setup: %+v", run)
	}
	// The failures left no receipt, so the same ids may be tried again when the world is back.
	r.reads.Unreadable("")
	receipt, err := r.complete(t, "c1", "kv")
	if err != nil || receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("after the read came back: %v %v", receipt, err)
	}
}

// A setup whose checks all hold still completes nothing for a task the job tool cannot run at the commit
// the run would start from: the card read is a start's, and refuses it with the job tool's reason.
func TestACompletionIsRefusedForATaskTheJobToolCannotRun(t *testing.T) {
	r := newCompletionRig(t, passingFacts("kv"))
	why := "vibesys.input.toml: [environment.modal] cannot be honored on this machine"
	r.Tasks.Set("kv", core.TaskCard{Task: "kv", Heading: "committed", Unsupported: why})
	_, err := r.complete(t, "c1", "kv")
	if status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), why) {
		t.Fatalf("got %v, want InvalidArgument with the job tool's reason", err)
	}
	if run := r.state(); run.Phase != core.PhaseSetup {
		t.Fatalf("a task the job tool cannot run completed the setup: %+v", run)
	}
}

func TestAHostThatCannotReadSetupFactsCompletesNothing(t *testing.T) {
	rig := daemontest.NewRig(t)
	rig.Setup = setup.NewStore()
	rig.Restart(t)
	r := &completionRig{Rig: rig}
	receipt := dispatch(t, rig, &pb.Command{Id: "begin", Body: &pb.Command_BeginSetup{BeginSetup: &pb.BeginSetup{
		Repository: "/repo", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}})
	r.run, _ = rig.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	if _, err := r.complete(t, "c1", "kv"); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("a host with no reads: %v", err)
	}
}

func TestARetriedCompletionGetsTheFirstReceiptWhateverTheWorldSaysNow(t *testing.T) {
	r := newCompletionRig(t, passingFacts("kv"))
	first, err := r.complete(t, "c1", "kv")
	if err != nil || first.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("%v %v", first, err)
	}
	calls := len(r.reads.Calls())
	r.reads.Set(setup.Facts{})
	again, err := r.complete(t, "c1", "kv")
	if err != nil || again.GetRunId() != first.GetRunId() || again.GetMessageId() != first.GetMessageId() || again.GetRefusal() != first.GetRefusal() {
		t.Fatalf("a retry got %v (%v), first was %v", again, err, first)
	}
	if len(r.reads.Calls()) != calls {
		t.Fatal("a retry read the world again")
	}
}

// A completion of a run that is not in setup is refused as such with a receipt, and reads nothing.
func TestCompletionOfARunNotInSetupReadsNothing(t *testing.T) {
	r := newCompletionRig(t, passingFacts("kv"))
	if receipt, err := r.complete(t, "c1", "kv"); err != nil || receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("%v %v", receipt, err)
	}
	calls := len(r.reads.Calls())
	receipt, err := r.complete(t, "c2", "kv")
	if err != nil || receipt.GetRefusal() != pb.Refusal_REFUSAL_NOT_IN_SETUP {
		t.Fatalf("a second completion: %v %v", receipt, err)
	}
	if len(r.reads.Calls()) != calls {
		t.Fatal("a refusal for the phase read the task")
	}
}

func TestCompletionRefusesAnEvalPlaceThatDoesNotFit(t *testing.T) {
	r := newCompletionRig(t, passingFacts("kv"))
	r.Tasks.Set("kv", core.TaskCard{Task: "kv", Hardware: &core.AcceleratorNeed{Count: 1, Vendor: core.VendorNvidia}})
	receipt, err := r.complete(t, "c1", "kv")
	if err != nil || receipt.GetRefusal() != pb.Refusal_REFUSAL_EVAL_PLACE_MISFIT {
		t.Fatalf("%v %v", receipt, err)
	}
	if run := r.state(); run.Phase != core.PhaseSetup {
		t.Fatalf("the run left setup for a place that does not fit: %+v", run)
	}
}

// The whole of it with no agent: begin, the lead names its task, the facts come in and the six checks pass
// as the daemon watches, the operator completes, and one fresh lead opens to read the brief.
func TestAWholeSetupWithNoAgent(t *testing.T) {
	r := newCompletionRig(t, passingFacts("kv"))
	r.advanceThroughReads(t)
	if r.snapshotChecks(t).unmet != 0 {
		t.Fatal("the six checks should pass")
	}
	receipt, err := r.complete(t, "c1", "kv")
	if err != nil || receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("%v %v", receipt, err)
	}
	r.Settle(t)
	lead, _ := r.Daemon.State().Agent(r.run.Lead)
	run := r.state()
	if run.PendingLead != nil || lead.Session != 2 || lead.Status.Over() {
		t.Fatalf("lead %+v, run %+v", lead, run)
	}
	fresh := r.Sidecars.Session(core.Session{Agent: run.Lead, N: 2})
	if fresh == nil {
		t.Fatal("no fresh session was launched")
	}
	turns := 0
	for _, c := range fresh.Commands() {
		if c.GetStartTurn() != nil {
			turns++
		}
	}
	if turns != 1 {
		t.Fatalf("the fresh session was given %d turns, want its brief once", turns)
	}
}

// The task is renamed with DeclareTask inside one live daemon, and a read of the old name that finishes
// afterwards is ignored: the panel shows the new name, not facts of the old.
func TestARenamedTaskIgnoresAnObservationOfTheOldName(t *testing.T) {
	facts := passingFacts("old")
	facts.Card.Heading = "the old task"
	r := newCompletionRig(t, facts)
	r.declare(t, "old")
	r.reads.Block()
	waited := false
	for range 40 {
		r.Clock.Advance(time.Second)
		r.Settle(t)
		if r.reads.Held() > 0 {
			waited = true
			break
		}
	}
	if !waited {
		t.Fatal("no read of the old name was started")
	}
	// The rename, while that read is still out; then the old read lands, as a slow one does.
	renamed := passingFacts("new")
	renamed.Card.Heading = "the new task"
	r.reads.Set(renamed)
	r.declare(t, "new")
	r.reads.ReleaseAt(0)
	r.Settle(t)
	if obs, _ := r.Setup.Get(r.run.ID); obs.Task != "old" {
		t.Fatalf("the old read should be what the store holds: %+v", obs)
	}
	view := r.summaryOf(t)
	if view.GetTask() != "new" || view.GetCard().GetHeading() != "" || view.GetUnreadable() == nil {
		t.Fatalf("the panel shows the old name's facts: %v", view)
	}
	r.reads.Release()
	for range 40 {
		r.Clock.Advance(time.Second)
		r.Settle(t)
		if v := r.summaryOf(t); v.GetTask() == "new" && v.GetCard().GetHeading() == "the new task" {
			return
		}
	}
	t.Fatalf("the new name was never read: %v", r.summaryOf(t))
}

func (r *completionRig) summaryOf(t *testing.T) *pb.SetupView {
	t.Helper()
	w, first, err := r.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	return client.WatchRunsSnapshot(first[0]).GetSummaries()[0].GetSetup()
}
