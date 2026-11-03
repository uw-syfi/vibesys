package daemon_test

import (
	"fmt"
	"math/rand"
	"strings"
	"testing"
	"testing/quick"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"google.golang.org/protobuf/proto"
)

func TestMultipleRunsConvergeAcrossLagReconnectAndSnapshotOmissions(t *testing.T) {
	property := func(history []uint8) bool {
		r := daemontest.NewRig(t)
		r.Limits = hub.Limits{MaxItems: 1, MaxBytes: 1}
		r.Restart(t)
		store := new(search.Store)
		r.Daemon.Search = store
		payload := strings.Repeat("x", search.MaxStateBytes-2048)
		put := func(run core.RunID, detail string) {
			store.Put(run, search.View{Reason: search.StreamLost, Detail: detail, Facts: search.Facts{{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: 1, Body: payload}}}}})
		}
		var runs []core.RunID
		for i := 0; i < 6; i++ {
			run := startNamedRun(t, r, fmt.Sprint(i))
			runs = append(runs, run)
			put(run, "initial")
		}
		r.Settle(t)
		views := map[string]*pb.SearchState{}
		greatest := map[string]uint64{}
		feed := func(frames []*pb.WatchRunsResponse) bool {
			accept := func(s *pb.SearchState) bool {
				if s.Revision < greatest[s.RunId] {
					return false
				}
				greatest[s.RunId] = s.Revision
				views[s.RunId] = s
				return true
			}
			for _, f := range frames {
				if proto.Size(f) > daemon.MaxWatchFrameBytes {
					return false
				}
				if snap := f.GetSnapshot(); snap != nil {
					clear(views)
					for _, s := range snap.Searches {
						if !accept(s) {
							return false
						}
					}
				}
				if s := f.GetSearch(); s != nil {
					if !accept(s) {
						return false
					}
				}
			}
			return true
		}
		w, frames, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { w.Close() }()
		if !feed(frames) || len(views) != 6 {
			return false
		}
		for i, n := range history {
			put(runs[int(n)%len(runs)], fmt.Sprint(i, n))
			r.Settle(t)
			if i%3 == 0 {
				r.Hub.PublishStored(hub.Stored{Event: &pb.Event{Sequence: 1}})
			}
			if n%2 == 0 {
				frames, err = w.Poll()
				if err != nil {
					t.Fatal(err)
				}
				if !feed(frames) {
					return false
				}
			}
			if i%5 == 0 {
				w.Close()
				after := uint64(0)
				if n%2 == 1 {
					after = r.Daemon.Last()
				}
				w, frames, err = r.Service.WatchRunsFrom(core.Operator{}, after)
				if err != nil {
					t.Fatal(err)
				}
				if !feed(frames) {
					return false
				}
			}
		}
		frames, err = w.Poll()
		if err != nil {
			t.Fatal(err)
		}
		if !feed(frames) {
			return false
		}
		for _, run := range runs {
			s := views[string(run)]
			v, rev := store.Get(run)
			if s == nil || s.Revision != rev+1 || s.Health.GetUnreadable().Detail != v.Detail || len(s.Plans) != 1 || s.Plans[0].Body != payload {
				return false
			}
		}
		return len(views) == 6
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 15, Rand: rand.New(rand.NewSource(33))}); err != nil {
		t.Fatal(err)
	}
}
