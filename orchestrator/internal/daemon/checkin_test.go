package daemon_test

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/protocol"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

var ann = core.Viewer{ID: "install-ann", Label: "Ann's laptop"}

// viewing is a call as a client of that install makes it.
func viewing(v core.Viewer) context.Context {
	return metadata.NewIncomingContext(context.Background(), metadata.Pairs(protocol.MetaViewerID, string(v.ID), protocol.MetaViewerLabel, v.Label))
}

var presses int

// press presses Catch me up on a run, as a viewer, through the service.
func press(t *testing.T, ctx context.Context, rig *daemontest.Rig, run core.RunID) (*pb.Receipt, error) {
	t.Helper()
	presses++
	resp, err := rig.Service.Dispatch(ctx, &pb.DispatchRequest{Command: &pb.Command{
		Id: fmt.Sprintf("press-%d", presses), Body: &pb.Command_RequestCatchUp{RequestCatchUp: &pb.RequestCatchUp{RunId: string(run)}},
	}})
	return resp.GetReceipt(), err
}

func checkIn(t *testing.T, rig *daemontest.Rig, id string) core.CheckIn {
	t.Helper()
	c, ok := rig.Daemon.State().CheckIn(core.CheckInID(id))
	if !ok {
		t.Fatalf("no check-in %q", id)
	}
	return c
}

func measure(job string, run core.RunID, at time.Time, metric string, value float64) []*cli.TaskFact {
	return []*cli.TaskFact{
		{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: job, Submitters: []*cli.Submitter{{RunId: string(run), At: timestamppb.New(at)}}}}},
		{Fact: &cli.TaskFact_Result{Result: &cli.Result{
			JobId: job, Commit: "c-" + job, RecordedAt: timestamppb.New(at),
			Outcome: &cli.Result_Measure{Measure: &cli.MeasureResult{Metric: &cli.Metric{Name: metric, Direction: cli.Direction_DIRECTION_MAXIMIZE}, Value: value}},
		}}},
	}
}

func plan(n uint32, at time.Time) *cli.TaskFact {
	return &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: n, CreatedAt: timestamppb.New(at), UpdatedAt: timestamppb.New(at)}}}
}

func ready(t *testing.T, c core.CheckIn) core.WindowFacts {
	t.Helper()
	r, ok := c.Outcome.(core.CheckInReady)
	if !ok {
		t.Fatalf("check-in %+v is %T, want ready", c, c.Outcome)
	}
	return r.Facts
}

// A press records a check-in at once, and the host then works its facts out from what it holds: the job
// log of the run's task, the run's turns, agents and reports. The next window starts where this one
// ended and holds only what came after, so what a viewer is told over two presses is what happened
// over the whole run, once.
func TestACatchUpFreezesFactsWorkedOutFromTheHostsRecords(t *testing.T) {
	rig := daemontest.NewRig(t)
	searches := new(search.Store)
	rig.Daemon.Search = searches
	run, lead := beginRun(t, rig)

	rig.Clock.Advance(time.Minute)
	first := rig.Clock.Now()
	rig.Clock.Advance(time.Minute)
	facts := slices.Concat(measure("j1", run.ID, first, "tput", 5), []*cli.TaskFact{plan(1, first)})
	searches.Put(run.ID, search.Fold(facts))

	receipt, err := press(t, viewing(ann), rig, run.ID)
	if err != nil || receipt.GetCheckInId() == "" || receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED || receipt.GetMessageId() == "" {
		t.Fatalf("press: %v, %v", receipt, err)
	}
	pending := checkIn(t, rig, receipt.GetCheckInId())
	if _, ok := pending.Outcome.(core.CheckInPending); !ok || pending.Viewer != ann || !pending.Since.Equal(run.CreatedAt) {
		t.Fatalf("a press leaves %+v", pending)
	}
	rig.Settle(t)
	one := ready(t, checkIn(t, rig, receipt.GetCheckInId()))
	if one.JobsMeasured != 1 || len(one.Objectives) != 1 || one.Objectives[0].End.Job != "j1" || one.Objectives[0].End.Value != 5 || one.Objectives[0].Start != nil ||
		!slices.Equal(one.PlansCreated, []uint32{1}) || one.AgentsStarted != 1 || one.Spend.Turns == 0 {
		t.Fatalf("the first window's facts: %+v", one)
	}

	// More happens, then the viewer looks again.
	rig.Clock.Advance(time.Minute)
	second := rig.Clock.Now()
	rig.Clock.Advance(time.Minute)
	searches.Put(run.ID, search.Fold(slices.Concat(facts, measure("j2", run.ID, second, "tput", 9), []*cli.TaskFact{plan(2, second)})))
	again, err := press(t, viewing(ann), rig, run.ID)
	if err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	next := checkIn(t, rig, again.GetCheckInId())
	two := ready(t, next)
	if !next.Since.Equal(checkIn(t, rig, receipt.GetCheckInId()).At) {
		t.Fatalf("the second window starts at %v, the first ended at %v", next.Since, checkIn(t, rig, receipt.GetCheckInId()).At)
	}
	// The lead's answer to the first press is a turn like any other: it is in this window's spend, as the
	// lead's, and the host adds no kind of agent for it.
	if two.JobsMeasured != 1 || two.Objectives[0].Start.Job != "j1" || two.Objectives[0].End.Job != "j2" ||
		!slices.Equal(two.PlansCreated, []uint32{2}) || two.AgentsStarted != 0 || two.Spend.Turns != 1 ||
		len(two.Spend.Roles) != 1 || two.Spend.Roles[0].Role != core.RoleLead {
		t.Fatalf("the second window's facts: %+v", two)
	}
	// What was frozen stays: more records afterwards change no check-in.
	rig.Clock.Advance(time.Minute)
	searches.Put(run.ID, search.Fold(slices.Concat(facts, measure("j3", run.ID, first, "tput", 99))))
	rig.Settle(t)
	if got := ready(t, checkIn(t, rig, receipt.GetCheckInId())); got.JobsMeasured != one.JobsMeasured || got.Objectives[0].End.Job != "j1" {
		t.Fatalf("a check-in's facts changed after it was frozen: %+v", got)
	}
	// The lead was asked once for each press, and the host never steers it.
	asks := 0
	records, err := rig.Store.Messages(run.ID, 0, 200)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range records {
		if _, ok := r.Message.Body.(core.CatchUpRequest); ok && r.Message.To == lead.ID {
			asks++
		}
	}
	if asks == 0 {
		t.Fatal("the lead was never asked for a report")
	}
}

// A job log that cannot be read fails the check-in rather than saying nothing was measured, and the
// viewer's next press covers the same ground.
func TestACatchUpFailsWhenTheJobLogCannotBeReadAndTheNextOneStartsWhereItWould(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(*daemontest.Rig, core.RunID)
		why   string
	}{
		{"no job log is wired in", func(r *daemontest.Rig, _ core.RunID) { r.Daemon.Search = nil }, "reads no job log"},
		{"the first read is still going", func(r *daemontest.Rig, _ core.RunID) { r.Daemon.Search = new(search.Store) }, "has not been read yet"},
		{"the store is away", func(r *daemontest.Rig, run core.RunID) {
			s := new(search.Store)
			s.Put(run, search.View{Reason: core.StoreAway, Detail: "away"})
			r.Daemon.Search = s
		}, "the task's store is away"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rig := daemontest.NewRig(t)
			run, _ := beginRun(t, rig)
			tc.setup(rig, run.ID)
			rig.Clock.Advance(time.Minute)
			receipt, err := press(t, viewing(ann), rig, run.ID)
			if err != nil {
				t.Fatal(err)
			}
			rig.Settle(t)
			failed, ok := checkIn(t, rig, receipt.GetCheckInId()).Outcome.(core.CheckInFailed)
			if !ok || !strings.Contains(failed.Detail, tc.why) || len(failed.Detail) > 600 {
				t.Fatalf("outcome %+v, want a failure that says %q", checkIn(t, rig, receipt.GetCheckInId()).Outcome, tc.why)
			}
			// The log is readable again: the next press covers the whole run, from where the failure began.
			searches := new(search.Store)
			searches.Put(run.ID, search.Fold(measure("j1", run.ID, run.CreatedAt.Add(time.Second), "tput", 1)))
			rig.Daemon.Search = searches
			rig.Clock.Advance(time.Minute)
			retry, err := press(t, viewing(ann), rig, run.ID)
			if err != nil {
				t.Fatal(err)
			}
			rig.Settle(t)
			c := checkIn(t, rig, retry.GetCheckInId())
			if !c.Since.Equal(run.CreatedAt) || ready(t, c).JobsMeasured != 1 {
				t.Fatalf("the retry covers %v onward: %+v", c.Since, c.Outcome)
			}
		})
	}
}

// A window with more in it than a check-in can keep fails and says so, and is not cut short.
func TestACatchUpFailsRatherThanKeepLessThanHappened(t *testing.T) {
	rig := daemontest.NewRig(t)
	searches := new(search.Store)
	rig.Daemon.Search = searches
	run, _ := beginRun(t, rig)
	var facts []*cli.TaskFact
	for n := range uint32(1200) {
		facts = append(facts, plan(n+1, run.CreatedAt.Add(time.Second)))
	}
	searches.Put(run.ID, search.Fold(facts))
	rig.Clock.Advance(time.Minute)
	receipt, err := press(t, viewing(ann), rig, run.ID)
	if err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	failed, ok := checkIn(t, rig, receipt.GetCheckInId()).Outcome.(core.CheckInFailed)
	if !ok || !strings.Contains(failed.Detail, "more than a check-in can keep") {
		t.Fatalf("%+v", checkIn(t, rig, receipt.GetCheckInId()).Outcome)
	}
}

// A press needs a viewer, and a client that sends none, or half of one, is told which field is missing:
// in the error's details as well as its words, before the command is decided, so the command id is
// not used up.
func TestACatchUpWithoutAViewerIsRefusedNamingTheField(t *testing.T) {
	rig := daemontest.NewRig(t)
	run, _ := beginRun(t, rig)
	for _, tc := range []struct {
		name  string
		ctx   context.Context
		field string
	}{
		{"no metadata", context.Background(), protocol.MetaViewerID},
		{"an id and no label", metadata.NewIncomingContext(context.Background(), metadata.Pairs(protocol.MetaViewerID, "install")), protocol.MetaViewerLabel},
		{"a label and no id", metadata.NewIncomingContext(context.Background(), metadata.Pairs(protocol.MetaViewerLabel, "laptop")), protocol.MetaViewerID},
		{"an id too long", metadata.NewIncomingContext(context.Background(), metadata.Pairs(protocol.MetaViewerID, strings.Repeat("x", 129), protocol.MetaViewerLabel, "l")), protocol.MetaViewerID},
		{"a label that is not text", metadata.NewIncomingContext(context.Background(), metadata.Pairs(protocol.MetaViewerID, "i", protocol.MetaViewerLabel, "\xff\xfe")), protocol.MetaViewerLabel},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := press(t, tc.ctx, rig, run.ID)
			st, ok := status.FromError(err)
			if !ok || st.Code() != codes.InvalidArgument || !strings.Contains(st.Message(), tc.field) {
				t.Fatalf("got %v, want InvalidArgument naming %s", err, tc.field)
			}
			var named string
			for _, d := range st.Details() {
				if bad, ok := d.(*errdetails.BadRequest); ok {
					named = bad.GetFieldViolations()[0].GetField()
				}
			}
			if named != tc.field {
				t.Fatalf("the details name %q, want %q", named, tc.field)
			}
		})
	}
	if len(rig.Daemon.State().CheckIns(run.ID)) != 0 {
		t.Fatal("a refused press made a check-in")
	}
}

// Everything but a catch-up goes through for a call with no viewer: a viewer is a label and the
// operator is the operator.
func TestOnlyACatchUpNeedsAViewer(t *testing.T) {
	rig := daemontest.NewRig(t)
	run, lead := beginRun(t, rig)
	resp, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
		Id: "tell", Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead.ID), Text: "hi"}},
	}})
	if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("%v %v", resp, err)
	}
	// An agent's socket takes the viewer for nothing: an agent cannot press it.
	if _, err := press(t, viewing(ann), rig, run.ID); err != nil {
		t.Fatal(err)
	}
}

// A watcher is given the check-ins in its snapshot with their frozen facts, and each change to one as
// it comes, in the order of the log; a watcher that joins later sees the same.
func TestWatchersAreServedCheckIns(t *testing.T) {
	rig := daemontest.NewRig(t)
	searches := new(search.Store)
	rig.Daemon.Search = searches
	run, _ := beginRun(t, rig)
	searches.Put(run.ID, search.Fold(measure("j1", run.ID, run.CreatedAt.Add(time.Second), "tput", 3)))
	w, first, err := rig.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	if snap := client.WatchRunsSnapshot(first[0]); len(snap.GetCheckIns()) != 0 {
		t.Fatalf("check-ins before any press: %v", snap.GetCheckIns())
	}

	rig.Clock.Advance(time.Minute)
	receipt, err := press(t, viewing(ann), rig, run.ID)
	if err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	var changes []*pb.CheckIn
	msgs, err := w.Poll()
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range msgs {
		if _, change, _ := client.DecodeWatchRuns(m); change.GetCheckIn() != nil {
			c := change.GetCheckIn()
			changes = append(changes, c)
			if err := wire.Validate(c); err != nil {
				t.Fatalf("a served check-in breaks the contract: %v\n%v", err, c)
			}
		}
	}
	if len(changes) < 2 || changes[0].GetState() != pb.CheckInState_CHECK_IN_STATE_PENDING ||
		changes[len(changes)-1].GetState() != pb.CheckInState_CHECK_IN_STATE_READY || changes[0].GetId() != receipt.GetCheckInId() {
		t.Fatalf("changes %v: want the check-in pending and then ready", changes)
	}

	// A watcher that joins now is given the whole check-in, facts included.
	late, frames, err := rig.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer late.Close()
	served := client.WatchRunsSnapshot(frames[0]).GetCheckIns()
	if len(served) != 1 || served[0].GetId() != receipt.GetCheckInId() || served[0].GetFacts().GetJobsMeasured() != 1 || served[0].GetViewer().GetLabel() != ann.Label {
		t.Fatalf("snapshot check-ins %v", served)
	}
	if err := wire.Validate(client.WatchRunsSnapshot(frames[0])); err != nil {
		t.Fatal(err)
	}
}

// A snapshot holds a viewer's newest check-ins of a run and no more, and says it left some out.
func TestASnapshotHoldsTheNewestCheckInsOfAViewer(t *testing.T) {
	rig := daemontest.NewRig(t)
	rig.Daemon.Search = new(search.Store)
	run, _ := beginRun(t, rig)
	var made []string
	for range 70 {
		rig.Clock.Advance(time.Second)
		receipt, err := press(t, viewing(ann), rig, run.ID)
		if err != nil {
			t.Fatal(err)
		}
		made = append(made, receipt.GetCheckInId())
		rig.Settle(t)
	}
	_, frames, err := rig.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	snap := client.WatchRunsSnapshot(frames[0])
	var ids []string
	for _, c := range snap.GetCheckIns() {
		ids = append(ids, c.GetId())
	}
	if !slices.Equal(ids, made[len(made)-daemonMaxSnapshotCheckIns:]) || !snap.GetTruncated() {
		t.Fatalf("a snapshot of %d check-ins holds %d, truncated %t", len(made), len(ids), snap.GetTruncated())
	}
}

const daemonMaxSnapshotCheckIns = 64

// A press that was committed and not yet answered when the host stopped is answered after it starts:
// the work to do is in the same commit as the check-in.
func TestAPendingCheckInIsAnsweredAfterARestart(t *testing.T) {
	rig := daemontest.NewRig(t)
	searches := new(search.Store)
	rig.Daemon.Search = searches
	run, _ := beginRun(t, rig)
	searches.Put(run.ID, search.Fold(measure("j1", run.ID, run.CreatedAt.Add(time.Second), "tput", 3)))
	rig.Clock.Advance(time.Minute)
	receipt, err := press(t, viewing(ann), rig, run.ID)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := checkIn(t, rig, receipt.GetCheckInId()).Outcome.(core.CheckInPending); !ok {
		t.Fatalf("before anything carries out the effect the check-in is %T", checkIn(t, rig, receipt.GetCheckInId()).Outcome)
	}
	rig.Search = searches
	rig.Restart(t)
	if err := rig.ReattachErr; err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	if got := ready(t, checkIn(t, rig, receipt.GetCheckInId())); got.JobsMeasured != 1 {
		t.Fatalf("%+v", got)
	}
	// A press repeated under its own id still gets the receipt it had.
	rig.Restart(t)
	again, err := rig.Service.Dispatch(viewing(ann), &pb.DispatchRequest{Command: &pb.Command{
		Id: fmt.Sprintf("press-%d", presses), Body: &pb.Command_RequestCatchUp{RequestCatchUp: &pb.RequestCatchUp{RunId: string(run.ID)}},
	}})
	if err != nil || again.GetReceipt().GetCheckInId() != receipt.GetCheckInId() {
		t.Fatalf("a retry after a restart: %v %v", again, err)
	}
}

// A run that is over can be caught up on: its window ends where it did, no request is sent, and the
// facts are those of the run's own records, so they are the same whenever the viewer looks.
func TestACatchUpOnARunThatIsOverEndsWithTheRun(t *testing.T) {
	rig := daemontest.NewRig(t)
	searches := new(search.Store)
	rig.Daemon.Search = searches
	run, _ := beginRun(t, rig)
	searches.Put(run.ID, search.Fold(measure("j1", run.ID, run.CreatedAt.Add(time.Second), "tput", 3)))
	rig.Clock.Advance(time.Minute)
	if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: "stop", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(run.ID)}}}}); err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	over, _ := rig.Daemon.State().Run(run.ID)
	if !over.Status.Final() {
		t.Fatalf("run is %s", over.Status)
	}
	rig.Clock.Advance(48 * time.Hour)
	receipt, err := press(t, viewing(ann), rig, run.ID)
	if err != nil || receipt.GetMessageId() != "" {
		t.Fatalf("%v %v", receipt, err)
	}
	rig.Settle(t)
	c := checkIn(t, rig, receipt.GetCheckInId())
	if !c.At.Equal(over.EndedAt) || c.Ask != "" || ready(t, c).JobsMeasured != 1 {
		t.Fatalf("check-in %+v of a run that ended at %v", c, over.EndedAt)
	}
}
