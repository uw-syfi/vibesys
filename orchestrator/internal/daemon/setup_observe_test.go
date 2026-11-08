package daemon_test

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// These tests hold the daemon's watching of a run in setup to the rules of the design: a read is made
// when something may have changed and not oftener, no read holds the loop, and what a read found is
// what the run's summary says, to every stream however it came to be open. The clock is the rig's and
// the reads are the fake's, so nothing here waits on real time.

func someFacts(heading string) setup.Facts {
	return setup.Facts{
		Task: "t", Version: "v1", Card: core.TaskCard{Task: "t", Heading: heading},
		Decl: setup.Declarations{Objectives: []setup.Objective{{Name: "qps", Unit: "ops"}}, CheckDeclared: true, BenchmarkDeclared: true},
		Base: setup.Base{Commit: strings.Repeat("a", 40), Version: "v1"},
	}
}

// setupRig is a rig with one run in setup whose lead declared a task, and a fake that reads it.
type setupRig struct {
	*daemontest.Rig
	reads *daemontest.FakeSetupReads
	run   core.Run
	lead  core.Agent
	told  int
}

// newSetupRig records a run in setup with the declared task, and starts a daemon that reads it. The
// declare_task command is not decided yet, so the run is recorded as the event it will produce.
func newSetupRig(t *testing.T, task string, facts setup.Facts) *setupRig {
	t.Helper()
	return newSetupRigOn(t, daemontest.NewRig(t), task, facts)
}

// advance moves the clock and lets the daemon do what it then owes.
func (r *setupRig) advance(t *testing.T, d time.Duration) {
	t.Helper()
	r.Clock.Advance(d)
	r.Settle(t)
}

// tell is an event of the run: the operator says something to its lead.
func (r *setupRig) tell(t *testing.T) {
	t.Helper()
	r.told++
	_, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
		Id: fmt.Sprintf("tell-%d", r.told), Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(r.lead.ID), Text: "again"}}}})
	if err != nil {
		t.Fatal(err)
	}
}

func (r *setupRig) calls() int { return len(r.reads.Calls()) }

// untilRead steps the clock a second at a time until a read has started, and says how long that took.
func (r *setupRig) untilRead(t *testing.T, limit time.Duration) (time.Duration, bool) {
	t.Helper()
	before := r.calls()
	for waited := time.Second; waited <= limit; waited += time.Second {
		r.advance(t, time.Second)
		if r.calls() > before {
			return waited, true
		}
	}
	return 0, false
}

// calm lets the events a start of a run leaves behind be read, so that what a test counts afterwards
// is its own.
func (r *setupRig) calm(t *testing.T) {
	t.Helper()
	r.advance(t, setup.MinGap)
	r.advance(t, setup.MinGap)
}

func (r *setupRig) watch(t *testing.T) *daemon.RunsWatch {
	t.Helper()
	w, _, err := r.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(w.Close)
	return w
}

// poll is the setup view of the last summary a poll gave, nil when it gave none.
func (r *setupRig) poll(t *testing.T, w *daemon.RunsWatch) *pb.SetupView {
	t.Helper()
	frames, err := w.Poll()
	if err != nil {
		t.Fatal(err)
	}
	var out *pb.SetupView
	for _, s := range summariesIn(frames) {
		if err := wire.Validate(s); err != nil {
			t.Fatal(err)
		}
		out = s.GetSetup()
	}
	return out
}

// snapshotView is what a stream opened now is first told of the run's setup.
func (r *setupRig) snapshotView(t *testing.T) *pb.SetupView {
	t.Helper()
	w, first, err := r.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	snap := client.WatchRunsSnapshot(first[0])
	if len(snap.GetSummaries()) != 1 {
		t.Fatalf("a snapshot of one run: %v", snap)
	}
	if err := wire.Validate(snap); err != nil {
		t.Fatal(err)
	}
	return snap.GetSummaries()[0].GetSetup()
}

func heading(v *pb.SetupView) string { return v.GetCard().GetHeading() }

// A run in setup is read when it starts, and its summary says what the read found, in the six checks,
// whole, to a stream that opens at any time.
func TestARunInSetupIsReadAndItsSummaryCarriesTheChecks(t *testing.T) {
	r := newSetupRig(t, "t", someFacts("first"))
	if r.calls() == 0 {
		t.Fatal("a run in setup with a declared task was not read")
	}
	v := r.snapshotView(t)
	if v.GetTask() != "t" || heading(v) != "first" || len(v.GetChecks()) != 6 || v.GetReadFailure() != "" {
		t.Fatalf("%v", v)
	}
	for _, c := range v.GetChecks()[:3] {
		if c.GetState() != pb.SetupCheckState_SETUP_CHECK_STATE_PASSED {
			t.Fatalf("the declared checks are passed: %v", v.GetChecks())
		}
	}
	if ref := r.reads.Calls()[0]; ref.Task != "t" || ref.Repository != r.run.Repository {
		t.Fatalf("the read was for %+v", ref)
	}
}

// A run whose lead has declared no task is read for nothing: there is no name to read, and its summary
// says the task is not named.
func TestARunWithNoDeclaredTaskIsNotRead(t *testing.T) {
	r := newSetupRig(t, "", someFacts("first"))
	r.calm(t)
	r.tell(t)
	r.advance(t, 2*setup.IdleMax)
	if r.calls() != 0 {
		t.Fatalf("%d reads for a run with no task", r.calls())
	}
	v := r.snapshotView(t)
	if v.GetTask() != "" || v.GetUnreadable().GetDetail() == "" || len(v.GetChecks()) != 6 {
		t.Fatalf("%v", v)
	}
	for _, c := range v.GetChecks() {
		if c.GetState() != pb.SetupCheckState_SETUP_CHECK_STATE_PENDING {
			t.Fatalf("a task nobody named passes nothing: %v", v.GetChecks())
		}
	}
}

// A person editing a file raises no event. It is seen within Idle of the last read, and a read is never
// started inside MinGap of the one before.
func TestAnEditNoEventAnnouncedIsSeenWithinIdle(t *testing.T) {
	r := newSetupRig(t, "t", someFacts("first"))
	r.calm(t)
	w := r.watch(t)
	r.poll(t, w)
	r.reads.Set(someFacts("second"))
	r.Settle(t)
	if v := r.poll(t, w); v != nil && heading(v) != "first" {
		t.Fatalf("an edit nobody announced was shown at once: %v", v)
	}
	waited, ok := r.untilRead(t, 2*setup.IdleMax)
	if !ok || waited < setup.MinGap {
		t.Fatalf("the next read came after %v, %v", waited, ok)
	}
	// The last read before the edit started at most Idle before, at the cadence's longest wait; the
	// edit is shown by the first read after it.
	if v := r.poll(t, w); heading(v) != "second" {
		t.Fatalf("the read after the edit shows %v", v)
	}
	if waited > setup.IdleMax {
		t.Fatalf("the edit waited %v, more than the longest idle wait", waited)
	}
}

// A job that ends changes the run's job log and nothing in its log of events. The change of the job log
// is a trigger, so the result shows without the idle wait.
func TestAChangeOfTheJobLogIsATrigger(t *testing.T) {
	rig := daemontest.NewRig(t)
	rig.Search = new(search.Store)
	r := newSetupRigOn(t, rig, "t", someFacts("first"))
	r.calm(t)
	// Let the cadence settle into its longest wait, so a read that comes sooner has a trigger.
	for range 8 {
		r.untilRead(t, 2*setup.IdleMax)
	}
	n := r.calls()
	r.reads.Set(someFacts("second"))
	r.advance(t, setup.MinGap)
	if r.calls() != n {
		t.Fatalf("read with no trigger, inside the longest wait")
	}
	rig.Search.Put(r.run.ID, search.View{Reason: core.Connecting})
	r.Settle(t)
	if r.calls() != n+1 {
		t.Fatalf("a change of the job log made %d reads", r.calls()-n)
	}
	if v := r.snapshotView(t); heading(v) != "second" {
		t.Fatalf("%v", v)
	}
}

// newSetupRigOn is newSetupRig over a rig the test has prepared before the run starts.
func newSetupRigOn(t *testing.T, rig *daemontest.Rig, task string, facts setup.Facts, prepare ...func(*daemontest.FakeSetupReads)) *setupRig {
	t.Helper()
	run, lead := beginRun(t, rig)
	run.Phase, run.SetupTask = core.PhaseSetup, task
	if _, err := rig.Store.Commit(store.Result{Events: []core.Event{{Run: run.ID, At: rig.Clock.Now(), Body: run}}}); err != nil {
		t.Fatal(err)
	}
	reads := daemontest.NewFakeSetupReads(facts)
	for _, p := range prepare {
		p(reads)
	}
	rig.Setup, rig.SetupReads = setup.NewStore(), reads
	rig.Restart(t)
	rig.Settle(t)
	return &setupRig{Rig: rig, reads: reads, run: run, lead: lead}
}

// However many events arrive while a read is out, exactly one more read follows it, and none is started
// while it is out.
func TestTriggersDuringAReadMakeExactlyOneMoreRead(t *testing.T) {
	r := newSetupRig(t, "t", someFacts("first"))
	r.calm(t)
	r.reads.Block()
	waited, ok := r.untilRead(t, 2*setup.IdleMax)
	if !ok || r.reads.Held() != 1 {
		t.Fatalf("a read was not started and held: %v %d", ok, r.reads.Held())
	}
	_ = waited
	n := r.calls()
	for range 50 {
		r.tell(t)
		r.Settle(t)
	}
	r.advance(t, 3*setup.IdleMax)
	if r.calls() != n || r.reads.Held() != 1 {
		t.Fatalf("reads started while one was out: %d, held %d", r.calls()-n, r.reads.Held())
	}
	r.reads.Release()
	r.Settle(t)
	if r.calls() != n+1 {
		t.Fatalf("50 triggers made %d more reads, want 1", r.calls()-n)
	}
	r.Settle(t)
	if r.calls() != n+1 {
		t.Fatal("a read was started with nothing new to ask")
	}
}

// With nothing happening, a run is read less and less often, to a read every IdleMax, and anything that
// may have changed the facts puts it back to the short wait.
func TestIdleBackoffReachesItsLimitAndATriggerResetsIt(t *testing.T) {
	r := newSetupRig(t, "t", someFacts("first"))
	r.calm(t)
	var gaps []time.Duration
	for range 8 {
		waited, ok := r.untilRead(t, 2*setup.IdleMax)
		if !ok {
			t.Fatalf("no read within %v", 2*setup.IdleMax)
		}
		gaps = append(gaps, waited)
	}
	last := gaps[len(gaps)-1]
	if last != setup.IdleMax || gaps[len(gaps)-2] != setup.IdleMax {
		t.Fatalf("the waits between reads of an idle run, %v, do not reach IdleMax", gaps)
	}
	for i := 1; i < len(gaps); i++ {
		if gaps[i] < gaps[i-1] {
			t.Fatalf("an idle run was read sooner than before: %v", gaps)
		}
	}
	r.tell(t)
	r.Settle(t)
	if waited, ok := r.untilRead(t, 2*setup.IdleMax); !ok || waited > setup.MinGap {
		t.Fatalf("an event was read after %v, %v", waited, ok)
	}
	r.advance(t, setup.MinGap) // the events of the turn it started
	r.calm(t)
	if waited, ok := r.untilRead(t, 2*setup.IdleMax); !ok || waited > setup.Idle {
		t.Fatalf("after a trigger the wait is %v, want at most Idle", waited)
	}
}

// A read that fails keeps the facts the last good one found, says so beside them, backs off, and the
// next good read takes the failure away.
func TestAFailedReadKeepsTheFactsAndSaysSo(t *testing.T) {
	r := newSetupRig(t, "t", someFacts("first"))
	r.calm(t)
	r.reads.Fail(errors.New("the job tool is away"))
	if _, ok := r.untilRead(t, 2*setup.IdleMax); !ok {
		t.Fatal("no read")
	}
	v := r.snapshotView(t)
	if heading(v) != "first" || !strings.Contains(v.GetReadFailure(), "away") {
		t.Fatalf("a failed read must keep the card and name itself: %v", v)
	}
	for _, c := range v.GetChecks()[:3] {
		if c.GetState() != pb.SetupCheckState_SETUP_CHECK_STATE_PASSED {
			t.Fatalf("a failed read changed the checks: %v", v.GetChecks())
		}
	}
	first, _ := r.untilRead(t, 2*setup.FailureMax)
	second, _ := r.untilRead(t, 2*setup.FailureMax)
	if first < setup.FailureMin || second <= first {
		t.Fatalf("failures were retried after %v and %v", first, second)
	}
	r.reads.Fail(nil)
	r.reads.Set(someFacts("healed"))
	if _, ok := r.untilRead(t, 2*setup.FailureMax); !ok {
		t.Fatal("no read after the failure")
	}
	if v := r.snapshotView(t); heading(v) != "healed" || v.GetReadFailure() != "" {
		t.Fatalf("a good read must clear the failure: %v", v)
	}
}

// A read that fails before any has succeeded is not a pass: the checks stay pending and the failure is
// shown.
func TestAFailureBeforeAnyReadPassesNothing(t *testing.T) {
	rig := daemontest.NewRig(t)
	run, _ := beginRun(t, rig)
	run.Phase, run.SetupTask = core.PhaseSetup, "t"
	if _, err := rig.Store.Commit(store.Result{Events: []core.Event{{Run: run.ID, At: rig.Clock.Now(), Body: run}}}); err != nil {
		t.Fatal(err)
	}
	reads := daemontest.NewFakeSetupReads(someFacts("first"))
	reads.Fail(errors.New("not installed"))
	rig.Setup, rig.SetupReads = setup.NewStore(), reads
	rig.Restart(t)
	rig.Settle(t)
	r := &setupRig{Rig: rig, reads: reads, run: run}
	v := r.snapshotView(t)
	if !strings.Contains(v.GetReadFailure(), "not installed") || v.GetCard() != nil {
		t.Fatalf("%v", v)
	}
	for _, c := range v.GetChecks() {
		if c.GetState() != pb.SetupCheckState_SETUP_CHECK_STATE_PENDING {
			t.Fatalf("an unreadable task passes nothing: %v", v.GetChecks())
		}
	}
}

// A task that cannot be read, such as a half-written manifest, is shown as that, with its reader's
// words, and not as a failed check.
func TestAnUnreadableDraftIsShownInTheReadersWords(t *testing.T) {
	r := newSetupRig(t, "t", someFacts("first"))
	r.reads.Unreadable("objective.toml line 3: unknown key")
	if _, ok := r.untilRead(t, 2*setup.IdleMax); !ok {
		t.Fatal("no read")
	}
	v := r.snapshotView(t)
	if v.GetUnreadable().GetDetail() != "objective.toml line 3: unknown key" || v.GetReadFailure() != "" {
		t.Fatalf("%v", v)
	}
	for _, c := range v.GetChecks() {
		if c.GetState() != pb.SetupCheckState_SETUP_CHECK_STATE_PENDING {
			t.Fatalf("%v", v.GetChecks())
		}
	}
}

// What a read found for another name than the run's declared one is ignored: after the lead renames
// its task, a read of the old name that finishes late shows nothing of the new one.
func TestAReadOfAnotherNameIsIgnored(t *testing.T) {
	// The first read of each daemon is the first it numbers, so that the order the two end in is the
	// store's to settle and not an accident of their numbers.
	block := func(f *daemontest.FakeSetupReads) { f.Block() }
	r := newSetupRigOn(t, daemontest.NewRig(t), "old", someFacts("of the old name"), block)
	if r.reads.Held() != 1 {
		t.Fatalf("%d reads held", r.reads.Held())
	}
	run := r.run
	run.Phase, run.SetupTask = core.PhaseSetup, "new"
	if _, err := r.Store.Commit(store.Result{Events: []core.Event{{Run: run.ID, At: r.Clock.Now(), Body: run}}}); err != nil {
		t.Fatal(err)
	}
	r.Restart(t)
	r.reads.Set(someFacts("of the new name"))
	r.Settle(t)
	if r.reads.Held() != 2 {
		t.Fatalf("the new daemon read %d times", r.reads.Held()-1)
	}
	r.reads.ReleaseAt(0) // the read of the old name, ended late
	r.Settle(t)
	if v := r.snapshotView(t); v.GetTask() != "new" || v.GetCard() != nil {
		t.Fatalf("a read of the old name showed in the new one: %v", v)
	}
	r.reads.Release()
	r.Settle(t)
	if v := r.snapshotView(t); heading(v) != "of the new name" {
		t.Fatalf("%v", v)
	}
	if refs := r.reads.Calls(); refs[len(refs)-1].Task != "new" {
		t.Fatalf("the read was for %+v", refs[len(refs)-1])
	}
}

// A read that never ends does not stall the loop: steps go on, the run's other work is done and its
// commands are answered, and the run's summary says the task is not read yet.
func TestAReadThatNeverEndsDoesNotStallTheLoop(t *testing.T) {
	r := newSetupRig(t, "t", someFacts("first"))
	r.reads.Block()
	r.calm(t)
	for range 10 {
		r.tell(t)
		r.advance(t, setup.IdleMax)
	}
	if last := r.Daemon.Last(); last == 0 {
		t.Fatal("nothing committed")
	}
	if r.reads.Held() != 1 {
		t.Fatalf("%d reads held, want the one", r.reads.Held())
	}
}

// Reading a summary makes no call: only a step does, and only when one is due.
func TestSummariesAreWorkedOutFromMemory(t *testing.T) {
	r := newSetupRig(t, "t", someFacts("first"))
	r.calm(t)
	n := r.calls()
	for range 20 {
		r.snapshotView(t)
	}
	if r.calls() != n {
		t.Fatalf("working out summaries made %d reads", r.calls()-n)
	}
}

// A stream opened in the middle of setup, one that resumes after a restart of the host, and one that
// resumes from a sequence it already holds are each told the whole of the setup.
func TestEveryWayOfOpeningAStreamGivesTheWholeSetup(t *testing.T) {
	r := newSetupRig(t, "t", someFacts("first"))
	r.calm(t)
	if v := r.snapshotView(t); heading(v) != "first" || len(v.GetChecks()) != 6 {
		t.Fatalf("mid-setup: %v", v)
	}
	// Resuming from the sequence the client holds: a stream with nothing held gets the summary.
	w, first, err := r.Service.WatchRunsDetailFrom(core.Operator{}, r.Daemon.Last(), pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	sums := summariesIn(first)
	if len(sums) != 1 || heading(sums[0].GetSetup()) != "first" {
		t.Fatalf("a resumed stream was owed the summary: %v", first)
	}
	// After a restart the host has read nothing, and says so until it has.
	r.Restart(t)
	r.reads.Block()
	r.Settle(t)
	if v := r.snapshotView(t); len(v.GetChecks()) != 6 || v.GetTask() != "t" {
		t.Fatalf("after a restart: %v", v)
	}
	r.reads.Release()
	r.Settle(t)
	if v := r.snapshotView(t); heading(v) != "first" {
		t.Fatalf("after a restart and a read: %v", v)
	}
}

// A read that finds what the last one found sends nothing.
func TestAnUnchangedReadSendsNothing(t *testing.T) {
	r := newSetupRig(t, "t", someFacts("first"))
	r.calm(t)
	w := r.watch(t)
	r.poll(t, w)
	for range 3 {
		before := r.calls()
		if _, ok := r.untilRead(t, 2*setup.IdleMax); !ok || r.calls() == before {
			t.Fatal("no read")
		}
		frames, err := w.Poll()
		if err != nil {
			t.Fatal(err)
		}
		if len(summariesIn(frames)) != 0 {
			t.Fatalf("an unchanged read sent %v", frames)
		}
	}
}

// No more than MaxSetupReads reads are out at once, whatever the number of runs in setup, and a run
// held back by it is read when one ends.
func TestTheHostHoldsAtMostTwoReadsOut(t *testing.T) {
	rig := daemontest.NewRig(t)
	var runs []core.Run
	for i := range 4 {
		resp, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: fmt.Sprint("start-", i), Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
			Repository: fmt.Sprint("/repo-", i), Task: "task", FanoutLimit: 1, Brief: "go", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
		}}}})
		if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
			t.Fatalf("%v %v", resp, err)
		}
		rig.Settle(t)
		run, _ := rig.Daemon.State().Run(core.RunID(resp.GetReceipt().GetRunId()))
		run.Phase, run.SetupTask = core.PhaseSetup, "t"
		if _, err := rig.Store.Commit(store.Result{Events: []core.Event{{Run: run.ID, At: rig.Clock.Now(), Body: run}}}); err != nil {
			t.Fatal(err)
		}
		runs = append(runs, run)
	}
	reads := daemontest.NewFakeSetupReads(someFacts("first"))
	reads.Block()
	rig.Setup, rig.SetupReads = setup.NewStore(), reads
	rig.Restart(t)
	rig.Settle(t)
	if reads.Held() != daemon.MaxSetupReads {
		t.Fatalf("%d reads out, want %d", reads.Held(), daemon.MaxSetupReads)
	}
	reads.Release()
	rig.Settle(t)
	served := map[string]bool{}
	for range 6 {
		rig.Clock.Advance(setup.MinGap)
		rig.Settle(t)
	}
	for _, ref := range reads.Calls() {
		served[ref.Repository] = true
	}
	if len(served) != len(runs) {
		t.Fatalf("only %v were read", served)
	}
}

// A run that has left setup is not read, and its summary holds the finished setup: every check passed,
// the card it holds and what setup cost.
func TestARunThatLeftSetupIsNotReadAndShowsItsFinishedSetup(t *testing.T) {
	rig := daemontest.NewRig(t)
	run, _ := beginRun(t, rig)
	spent := 1.5
	run.Phase, run.SetupTask = core.PhaseOptimize, "t"
	run.OptimizeStartedAt = rig.Clock.Now().Add(90 * time.Second)
	run.SetupUsage = &core.Usage{CostUSD: &spent}
	run.TaskCard = &core.TaskCard{Task: "t", Heading: "kept"}
	if _, err := rig.Store.Commit(store.Result{Events: []core.Event{{Run: run.ID, At: rig.Clock.Now(), Body: run}}}); err != nil {
		t.Fatal(err)
	}
	reads := daemontest.NewFakeSetupReads(someFacts("first"))
	rig.Setup, rig.SetupReads = setup.NewStore(), reads
	rig.Restart(t)
	rig.Settle(t)
	rig.Clock.Advance(time.Minute)
	rig.Settle(t)
	r := &setupRig{Rig: rig, reads: reads, run: run}
	if r.calls() != 0 {
		t.Fatalf("a run that left setup was read %d times", r.calls())
	}
	v := r.snapshotView(t)
	if heading(v) != "kept" || v.GetSpentUsd() != spent || v.GetElapsed().AsDuration() != 90*time.Second || len(v.GetChecks()) != 6 {
		t.Fatalf("%v", v)
	}
	for _, c := range v.GetChecks() {
		if c.GetState() != pb.SetupCheckState_SETUP_CHECK_STATE_PASSED {
			t.Fatalf("%v", v.GetChecks())
		}
	}
}

// A run that never had a setup has no setup in its summary.
func TestARunWithNoSetupHasNoSetupView(t *testing.T) {
	rig := daemontest.NewRig(t)
	beginRun(t, rig)
	r := &setupRig{Rig: rig}
	if v := r.snapshotView(t); v != nil {
		t.Fatalf("%v", v)
	}
}
