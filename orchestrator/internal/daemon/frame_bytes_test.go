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
	"google.golang.org/protobuf/proto"
)

func startNamedRun(t *testing.T, r *daemontest.Rig, id string) core.RunID {
	t.Helper()
	resp, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: id, Body: &pb.Command_StartRun{StartRun: &pb.StartRun{Repository: "/repo", Task: "task", Brief: "go", FanoutLimit: 1, Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE}}}}})
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

func TestLargestSnapshotFitsAndOmittedSearchesFollow(t *testing.T) {
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
	for _, f := range frames[1:] {
		if proto.Size(f) > daemon.MaxWatchFrameBytes {
			t.Fatal("oversized frame")
		}
		if s := client.WatchRunsSearch(f); s != nil {
			seen[s.RunId] = true
		}
	}
	if len(seen) != 12 {
		t.Fatal("omitted searches were lost", len(seen))
	}
}
