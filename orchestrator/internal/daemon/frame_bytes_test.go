package daemon_test

import (
	"context"
	"fmt"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"math/rand"
	"strings"
	"testing"
	"testing/quick"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"google.golang.org/protobuf/proto"
)

func startNamedRun(t *testing.T, r *daemontest.Rig, id string) core.RunID {
	t.Helper()
	resp, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: id, Body: &pb.Command_StartRun{StartRun: &pb.StartRun{Repository: "/repo-" + id, Task: "task", Brief: "go", FanoutLimit: 1, Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE}}}}})
	if err != nil {
		t.Fatal(err)
	}
	return core.RunID(resp.GetReceipt().GetRunId())
}

func TestEverySearchFrameFitsForGeneratedRunsAndViews(t *testing.T) {
	property := func(sizes []uint16) bool {
		if len(sizes) > 16 {
			sizes = sizes[:16]
		}
		r := daemontest.NewRig(t)
		store := new(search.Store)
		r.Daemon.Search = store
		for i, n := range sizes {
			run := startNamedRun(t, r, fmt.Sprint(i))
			store.Put(run, search.Fold(search.Facts{{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: 1, Body: strings.Repeat("x", int(n)*8)}}}}))
		}
		r.Settle(t)
		w, frames, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
		if err != nil {
			t.Fatal(err)
		}
		defer w.Close()
		check := func(frames []*pb.WatchRunsResponse) bool {
			for _, f := range frames {
				if proto.Size(f) > daemon.MaxWatchFrameBytes {
					return false
				}
				if snap := client.WatchRunsSnapshot(f); snap != nil && proto.Size(snap) > daemon.MaxSnapshotBytes {
					return false
				}
			}
			return true
		}
		if !check(frames) {
			return false
		}
		for i, run := range r.Daemon.State().Runs() {
			store.Put(run.ID, search.Fold(search.Facts{{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: 1, Body: strings.Repeat("y", int(sizes[i])*8)}}}}))
		}
		r.Settle(t)
		frames, err = w.Poll()
		if err != nil {
			t.Fatal(err)
		}
		if !check(frames) {
			return false
		}
		re, frames, err := r.Service.WatchRunsFrom(core.Operator{}, r.Daemon.Last())
		if err != nil {
			t.Fatal(err)
		}
		defer re.Close()
		return check(frames)
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 20, Rand: rand.New(rand.NewSource(8))}); err != nil {
		t.Fatal(err)
	}
}

func TestLargestSnapshotFitsAndOmittedSearchesFollowWithinTheBurstBudget(t *testing.T) {
	r := daemontest.NewRig(t)
	store := new(search.Store)
	r.Daemon.Search = store
	for i := 0; i < 12; i++ {
		run := startNamedRun(t, r, fmt.Sprint(i))
		store.Put(run, search.Fold(search.Facts{{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: 1, Body: strings.Repeat("x", search.MaxStateBytes-1024)}}}}))
	}
	r.Settle(t)
	w, frames, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	snap := client.WatchRunsSnapshot(frames[0])
	if !snap.Truncated || proto.Size(snap) > daemon.MaxSnapshotBytes || proto.Size(snap) < daemon.MaxSnapshotBytes/2 {
		t.Fatal("snapshot budget not exercised", proto.Size(snap))
	}
	seen := map[string]bool{}
	for _, s := range snap.Searches {
		seen[s.RunId] = true
	}
	burst := 0
	for _, f := range frames[1:] {
		if proto.Size(f) > daemon.MaxWatchFrameBytes {
			t.Fatal("oversized frame")
		}
		if s := client.WatchRunsSearch(f); s != nil {
			seen[s.RunId] = true
			burst += proto.Size(f)
		}
	}
	// What the snapshot left out follows, but only as much as one burst holds: the rest is sent when it changes.
	if burst > daemon.MaxFollowUpBytes || len(seen) <= len(snap.Searches) {
		t.Fatalf("%d bytes followed the snapshot, and %d of 12 views were sent against the snapshot's %d", burst, len(seen), len(snap.Searches))
	}
}

// A build before event-size bounds could persist a report larger than the link can carry. Replaying
// that event must advance by replacing it with a snapshot; returning RESOURCE_EXHAUSTED leaves every
// client at the old cursor and makes each reconnect replay the same event forever (#3878).
func TestWatchRunsRecoversPastEveryOversizedLegacyEvent(t *testing.T) {
	property := func(extra uint16) bool {
		rig := daemontest.NewRig(t)
		run := startNamedRun(t, rig, "legacy")
		lead := rig.Daemon.State().Agents(run)[0].ID
		after := rig.Daemon.Last()
		legacy := core.Message{
			ID: "legacy-oversized", Run: run, From: lead, Delivery: core.DeliveryConsumed,
			At: rig.Clock.Now(), Body: core.Report{
				Kind: core.ReportProgress, Scope: core.ReportScopeRun, Sequence: 1,
				Headline: "legacy", Changed: strings.Repeat("x", daemon.MaxWatchFrameBytes+1+int(extra)),
			},
		}
		if _, err := rig.Store.Commit(store.Result{Events: []core.Event{{Run: run, At: rig.Clock.Now(), Body: legacy}}}); err != nil {
			t.Fatal(err)
		}
		rig.Restart(t)

		watch, frames, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, after, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
		if err != nil {
			t.Fatalf("resume after an oversized event: %v", err)
		}
		defer watch.Close()
		if len(frames) == 0 {
			t.Fatal("resume returned no recovery snapshot")
		}
		snapshot := client.WatchRunsSnapshot(frames[0])
		if snapshot == nil || snapshot.GetSequence() != rig.Daemon.Last() {
			t.Fatalf("resume returned a non-snapshot at %d, want a snapshot at %d", snapshot.GetSequence(), rig.Daemon.Last())
		}
		for _, frame := range frames {
			if proto.Size(frame) > daemon.MaxWatchFrameBytes {
				t.Fatalf("recovery frame is %d bytes", proto.Size(frame))
			}
		}

		if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
			Id: "after-recovery", Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead), Text: "still live"}},
		}}); err != nil {
			t.Fatal(err)
		}
		rig.Settle(t)
		live, err := watch.Poll()
		if err != nil {
			t.Fatalf("poll after recovery: %v", err)
		}
		for _, frame := range live {
			_, change, _ := client.DecodeWatchRuns(frame)
			if change.GetSequence() > snapshot.GetSequence() {
				return true
			}
		}
		return false
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 5, Rand: rand.New(rand.NewSource(3878))}); err != nil {
		t.Fatal(err)
	}
}

func TestWatchRunsReplacesAnOversizedLiveEventWithASnapshot(t *testing.T) {
	rig := daemontest.NewRig(t)
	run := startNamedRun(t, rig, "live")
	rig.Settle(t)
	lead := rig.Daemon.State().Agents(run)[0].ID
	watch, first, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatal(err)
	}
	defer watch.Close()
	before := client.WatchRunsSnapshot(first[0]).GetSequence()

	post(t, rig, lead, "oversized-live", bigReport(pb.ReportKind_REPORT_KIND_PROGRESS, daemon.MaxWatchFrameBytes+1))
	frames, err := watch.Poll()
	if err != nil {
		t.Fatalf("poll after an oversized live event: %v", err)
	}
	if len(frames) == 0 {
		t.Fatal("poll returned no recovery snapshot")
	}
	snapshot := client.WatchRunsSnapshot(frames[0])
	if snapshot == nil || snapshot.GetSequence() <= before {
		t.Fatalf("poll recovered at %d, want a snapshot after %d", snapshot.GetSequence(), before)
	}
	for _, frame := range frames {
		if proto.Size(frame) > daemon.MaxWatchFrameBytes {
			t.Fatalf("recovery frame is %d bytes", proto.Size(frame))
		}
	}
}
