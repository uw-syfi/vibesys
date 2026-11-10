package daemon_test

import (
	"context"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/propcheck"

	"google.golang.org/protobuf/types/known/timestamppb"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// submitters says the run submitted a job.
func submitters(run core.RunID) []*cli.Submitter {
	return []*cli.Submitter{{RunId: string(run), At: timestamppb.New(time.Unix(1, 0))}}
}

func TestSearchWatchConvergesAcrossLagAndReconnect(t *testing.T) {
	property := func(history []uint8) bool {
		r := daemontest.NewRig(t)
		r.Limits = hub.Limits{MaxItems: 1, MaxBytes: 100}
		r.Restart(t)
		store := new(search.Store)
		r.Daemon.Search = store
		run, _ := beginRun(t, r)
		w, first, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
		if err != nil {
			t.Fatal(err)
		}
		defer w.Close()
		latest := client.WatchRunsSnapshot(first[0]).Searches[0]
		for i, n := range history {
			v := search.Fold(search.Facts{{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j1", Submitters: submitters(run.ID), CancelReason: string(rune(n + 32))}}}})
			if n%2 == 0 {
				v.Reason = core.CLINotInstalled
				v.Detail = "configured CLI is missing"
			}
			store.Put(run.ID, v)
			if _, err := r.Daemon.Step(context.Background()); err != nil {
				t.Fatal(err)
			}
			if i%3 == 0 {
				r.Hub.PublishStored(hub.Stored{Change: &pb.RunChange{Sequence: 1}})
				r.Hub.PublishStored(hub.Stored{Change: &pb.RunChange{Sequence: 2}})
			}
			if n%5 == 0 {
				continue
			}
			msgs, err := w.Poll()
			if err != nil {
				t.Fatal(err)
			}
			for _, m := range msgs {
				if snap := client.WatchRunsSnapshot(m); snap != nil {
					latest = snap.Searches[0]
				} else if s := client.WatchRunsSearch(m); s != nil {
					if s.Revision < latest.Revision {
						return false
					}
					latest = s
				}
			}
		}
		// A reconnect at a nonzero event cursor must receive the current search view too.
		re, frames, err := r.Service.WatchRunsFrom(core.Operator{}, r.Daemon.Last())
		if err != nil {
			t.Fatal(err)
		}
		defer re.Close()
		var current *pb.SearchState
		for _, m := range frames {
			if client.WatchRunsSearch(m) != nil {
				current = client.WatchRunsSearch(m)
			}
		}
		if current == nil || wire.Validate(current) != nil {
			return false
		}
		v, rev := store.Get(run.ID)
		if current.Revision != rev+1 {
			return false
		}
		if rev > 0 && len(current.Jobs) != 1 {
			return false
		}
		if v.Reason == core.CLINotInstalled && current.Health.GetUnreadable().GetReason() != pb.UnreadableReason_UNREADABLE_REASON_CLI_NOT_INSTALLED {
			return false
		}
		msgs, err := w.Poll()
		if err != nil {
			t.Fatal(err)
		}
		for _, m := range msgs {
			if snap := client.WatchRunsSnapshot(m); snap != nil {
				latest = snap.Searches[0]
			} else if s := client.WatchRunsSearch(m); s != nil {
				latest = s
			}
		}
		return latest.Revision == current.Revision
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 30})); err != nil {
		t.Fatal(err)
	}
}

// Every cause that keeps a view from being read reaches clients as its own reason; an oversized
// message used to be reported as a lost stream, hiding that retrying cannot help.
func TestEveryUnreadableCauseReachesClientsAsItsOwnReason(t *testing.T) {
	want := map[core.ViewReason]pb.UnreadableReason{
		core.NoDaemon:            pb.UnreadableReason_UNREADABLE_REASON_NOT_ANSWERING,
		core.Connecting:          pb.UnreadableReason_UNREADABLE_REASON_NO_DAEMON,
		core.StartFailed:         pb.UnreadableReason_UNREADABLE_REASON_START_FAILED,
		core.CLINotInstalled:     pb.UnreadableReason_UNREADABLE_REASON_CLI_NOT_INSTALLED,
		core.ClientTooOld:        pb.UnreadableReason_UNREADABLE_REASON_CLIENT_TOO_OLD,
		core.DaemonTooOld:        pb.UnreadableReason_UNREADABLE_REASON_DAEMON_TOO_OLD,
		core.StreamLost:          pb.UnreadableReason_UNREADABLE_REASON_STREAM_LOST,
		core.MessageTooLarge:     pb.UnreadableReason_UNREADABLE_REASON_MESSAGE_TOO_LARGE,
		core.StoreOwnedElsewhere: pb.UnreadableReason_UNREADABLE_REASON_STORE_OWNED_ELSEWHERE,
		core.StoreAway:           pb.UnreadableReason_UNREADABLE_REASON_STORE_AWAY,
	}
	for reason, expected := range want {
		r := daemontest.NewRig(t)
		store := new(search.Store)
		r.Daemon.Search = store
		run, _ := beginRun(t, r)
		store.Put(run.ID, search.View{Reason: reason, Detail: "x"})
		r.Settle(t)
		w, frames, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
		if err != nil {
			t.Fatal(err)
		}
		got := client.WatchRunsSnapshot(frames[0]).Searches[0].Health.GetUnreadable().GetReason()
		w.Close()
		if got != expected {
			t.Errorf("reason %d reached clients as %v, want %v", reason, got, expected)
		}
	}
}

func TestSearchSnapshotsRetainReleasedViewAndScopeAgents(t *testing.T) {
	r := daemontest.NewRig(t)
	store := new(search.Store)
	r.Daemon.Search = store
	run, lead := beginRun(t, r)
	store.Put(run.ID, search.View{Reason: core.CLINotInstalled, Detail: "configured CLI is missing", Facts: search.Facts{{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j1", Submitters: submitters(run.ID)}}}}})
	r.Settle(t)
	w, first, err := r.AgentService.WatchRunsFrom(core.AgentCaller{Agent: lead.ID}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	view := client.WatchRunsSnapshot(first[0]).Searches
	if len(view) != 1 || view[0].RunId != string(run.ID) || len(view[0].Jobs) != 1 || view[0].Health.GetUnreadable().GetReason() != pb.UnreadableReason_UNREADABLE_REASON_CLI_NOT_INSTALLED {
		t.Fatal(view)
	}
	store.Delete(run.ID)
	r.Settle(t)
	re, frames, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer re.Close()
	retained := client.WatchRunsSnapshot(frames[0]).Searches[0]
	if retained.Revision != view[0].Revision || len(retained.Jobs) != 1 || retained.Health.GetUnreadable().GetReason() != pb.UnreadableReason_UNREADABLE_REASON_CLI_NOT_INSTALLED {
		t.Fatal(retained)
	}
}

// A bound an agent of the run declared reaches the app in the run's view, with the head's percent of
// it, and a revision changes the view: the bound is the daemon's fact, shown as it was declared.
func TestADeclaredBoundReachesTheViewOfItsRun(t *testing.T) {
	r := daemontest.NewRig(t)
	store := new(search.Store)
	r.Daemon.Search = store
	run, _ := beginRun(t, r)
	declared := func(seq uint64, run core.RunID, value float64) *cli.TaskFact {
		return &cli.TaskFact{Fact: &cli.TaskFact_Bound{Bound: &cli.ObjectiveBound{
			Seq: seq, Origin: &cli.RecordOrigin{RunId: string(run), Agent: "lead", At: timestamppb.New(time.Unix(int64(seq), 0))},
			Objective: "tok_s", Direction: cli.Direction_DIRECTION_MAXIMIZE, Value: value, Unit: "tok/s", Note: "bandwidth",
		}}}
	}
	head := &cli.TaskFact{Fact: &cli.TaskFact_Head{Head: &cli.Head{State: &cli.Head_Current{Current: &cli.CurrentResult{
		Head: "a", Result: &cli.Result{Outcome: &cli.Result_Measure{Measure: &cli.MeasureResult{
			Metric: &cli.Metric{Name: "tok_s", Direction: cli.Direction_DIRECTION_MAXIMIZE}, Value: 7800,
		}}},
	}}}}}
	view := func() *pb.SearchState {
		r.Settle(t)
		w, frames, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
		if err != nil {
			t.Fatal(err)
		}
		defer w.Close()
		return client.WatchRunsSnapshot(frames[0]).Searches[0]
	}

	store.Put(run.ID, search.View{Reason: core.Readable, Facts: search.Facts{declared(1, run.ID, 15600), declared(2, "another-run", 1), head}})
	first := view()
	if len(first.Bounds) != 1 || len(first.Bounds[0].Versions) != 1 || first.Bounds[0].PercentOfBound == nil || *first.Bounds[0].PercentOfBound != 50 {
		t.Fatalf("bounds = %v, want this run's one bound with the head at 50%% of it", first.Bounds)
	}
	store.Put(run.ID, search.View{Reason: core.Readable, Facts: search.Facts{declared(1, run.ID, 15600), declared(3, run.ID, 10000), head}})
	second := view()
	if second.Revision <= first.Revision || len(second.Bounds[0].Versions) != 2 || *second.Bounds[0].PercentOfBound != 78 {
		t.Fatalf("after a revision: revision %d then %d, bounds %v; want two versions and the head at 78%% of the standing 10000", first.Revision, second.Revision, second.Bounds)
	}
}

func TestUnchangedTickPublishesNothingIncludingEndedRuns(t *testing.T) {
	r := daemontest.NewRig(t)
	store := new(search.Store)
	r.Daemon.Search = store
	run, _ := beginRun(t, r)
	store.Put(run.ID, search.View{Reason: core.CLINotInstalled, Detail: "missing"})
	r.Settle(t)
	w, _, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	assertQuiet := func() {
		t.Helper()
		moved, err := r.Daemon.Step(context.Background())
		if err != nil || moved {
			t.Fatalf("unchanged tick reports progress: %v %v", moved, err)
		}
		frames, err := w.Poll()
		if err != nil || len(frames) != 0 {
			t.Fatalf("unchanged tick published %d frames: %v", len(frames), err)
		}
	}
	assertQuiet()
	_, err = r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: "stop", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(run.ID)}}}})
	if err != nil {
		t.Fatal(err)
	}
	r.Settle(t)
	if _, err := w.Poll(); err != nil {
		t.Fatal(err)
	}
	assertQuiet()
	// An ended run still publishes a later revision rather than being skipped forever.
	store.Put(run.ID, search.View{Reason: core.StreamLost, Detail: "later"})
	r.Settle(t)
	frames, err := w.Poll()
	if err != nil {
		t.Fatal(err)
	}
	if len(frames) != 1 || client.WatchRunsSearch(frames[0]).GetHealth().GetUnreadable().GetReason() != pb.UnreadableReason_UNREADABLE_REASON_STREAM_LOST {
		t.Fatal("new facts of ended run lost", frames)
	}
	assertQuiet()
}
