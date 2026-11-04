package daemon_test

import (
	"context"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"math/rand"
	"testing"
	"testing/quick"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

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
			v := search.Fold(search.Facts{{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j1", CancelReason: string(rune(n + 32))}}}})
			if n%2 == 0 {
				v.Reason = search.CLINotInstalled
				v.Detail = "configured CLI is missing"
			}
			store.Put(run.ID, v)
			if _, err := r.Daemon.Step(context.Background()); err != nil {
				t.Fatal(err)
			}
			if i%3 == 0 {
				r.Hub.PublishStored(hub.Stored{Event: &pb.Event{Sequence: 1}})
				r.Hub.PublishStored(hub.Stored{Event: &pb.Event{Sequence: 2}})
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
		if v.Reason == search.CLINotInstalled && current.Health.GetUnreadable().GetReason() != pb.UnreadableReason_UNREADABLE_REASON_CLI_NOT_INSTALLED {
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
	if err := quick.Check(property, &quick.Config{MaxCount: 30, Rand: rand.New(rand.NewSource(1))}); err != nil {
		t.Fatal(err)
	}
}

func TestSearchSnapshotsRetainReleasedViewAndScopeAgents(t *testing.T) {
	r := daemontest.NewRig(t)
	store := new(search.Store)
	r.Daemon.Search = store
	run, lead := beginRun(t, r)
	store.Put(run.ID, search.View{Reason: search.CLINotInstalled, Detail: "configured CLI is missing", Facts: search.Facts{{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j1"}}}}})
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

func TestUnchangedTickPublishesNothingIncludingEndedRuns(t *testing.T) {
	r := daemontest.NewRig(t)
	store := new(search.Store)
	r.Daemon.Search = store
	run, _ := beginRun(t, r)
	store.Put(run.ID, search.View{Reason: search.CLINotInstalled, Detail: "missing"})
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
	store.Put(run.ID, search.View{Reason: search.StreamLost, Detail: "later"})
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
