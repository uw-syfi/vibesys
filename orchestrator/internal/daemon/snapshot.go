package daemon

import (
	"slices"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
	"google.golang.org/protobuf/proto"
)

// MaxWatchFrameBytes is the receive limit used on the orchestrator link.
const MaxWatchFrameBytes = 4 << 20

// MaxSnapshotCheckIns is how many of a viewer's check-ins in one run a snapshot holds, the newest. A
// viewer presses Catch me up now and then, so the cut is for a viewer who pressed it very many times.
const MaxSnapshotCheckIns = 64

// MaxSnapshotBytes leaves room for the response wrapper below the receive limit.
// Historical run bundles are optional; omitted search views follow separately.
const MaxSnapshotBytes = 1 << 20

// newestCheckIns keeps, of a run's check-ins in the order they were made, the newest MaxSnapshotCheckIns
// of each viewer, and says whether any was left out.
func newestCheckIns(all []core.CheckIn) (kept []core.CheckIn, cut bool) {
	seen := map[core.ViewerID]int{}
	for i := len(all) - 1; i >= 0; i-- {
		viewer := all[i].Viewer.ID
		if seen[viewer]++; seen[viewer] > MaxSnapshotCheckIns {
			cut = true
			continue
		}
		kept = append(kept, all[i])
	}
	slices.Reverse(kept)
	return kept, cut
}

// listingOrder is the order a snapshot lists runs in when it cannot list them all: live runs first,
// then the newest started. What does not fit is the end of this order, so a live run is never left out
// for a finished one and the oldest finished runs go first.
func listingOrder(runs []core.Run) {
	slices.SortFunc(runs, func(a, b core.Run) int {
		if a.Status.Live() != b.Status.Live() {
			if a.Status.Live() {
				return -1
			}
			return 1
		}
		if c := a.CreatedAt.Compare(b.CreatedAt); c != 0 {
			return -c
		}
		if a.ID < b.ID {
			return -1
		}
		if a.ID > b.ID {
			return 1
		}
		return 0
	})
}

// runsSnapshot builds the snapshot of runs. Whatever the detail, a run is listed with its summary or
// counted in runs_left_out, never dropped without a word, and what is cut of a listed run's history sets
// truncated. With detail SUMMARY nothing of a run's history is in it, so its size follows the number of
// runs and not the length of their mailboxes; the summaries and runs are the only part the byte budget
// has to bound.
//
// seen, held and left are the stream's account of what the client was told, which a snapshot starts again:
// the revision of each search view in it, the summary of each run in it, and the runs it left out.
func (s *Service) runsSnapshot(state core.State, last uint64, run core.RunID, scoped bool, detail pb.SnapshotDetail, seen map[string]uint64, held map[string]*pb.RunSummary, left map[core.RunID]bool) (*pb.WatchRunsResponse, error) {
	clear(seen)
	clear(held)
	clear(left)
	snap := &pb.RunsSnapshot{Sequence: last}
	var bundles []*pb.RunsSnapshot
	used := proto.Size(snap)
	runs := state.Runs()
	listingOrder(runs)
	agentsOf := state.AgentsByRun()
	var wanted []core.Run
	for _, r := range runs {
		if !scoped || r.ID == run {
			wanted = append(wanted, r)
		}
	}
	for _, r := range wanted {
		summary, err := s.e.runSummary(state, r, agentsOf[r.ID])
		if err != nil {
			return nil, err
		}
		bundle := &pb.RunsSnapshot{Runs: []*pb.Run{wire.RunMessage(r)}, Summaries: []*pb.RunSummary{summary}}
		if detail != pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY {
			for _, a := range agentsOf[r.ID] {
				bundle.Agents = append(bundle.Agents, wire.AgentMessage(a))
			}
		}
		// A run's identity, summary and agents are listed in preference to anything else of it: if even
		// those do not fit, this run and the ones after it in listing order are left out.
		if used+proto.Size(bundle) > MaxSnapshotBytes-2 {
			break
		}
		if detail != pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY {
			if err := s.addDetail(bundle, state, r.ID, used, snap); err != nil {
				return nil, err
			}
		}
		bundles = append(bundles, bundle)
		used += proto.Size(bundle)
	}
	for _, r := range wanted[len(bundles):] {
		left[r.ID] = true
	}
	snap.RunsLeftOut = uint32(len(wanted) - len(bundles))
	snap.Truncated = snap.Truncated || snap.RunsLeftOut > 0
	slices.SortFunc(bundles, func(a, b *pb.RunsSnapshot) int {
		if a.Runs[0].Id < b.Runs[0].Id {
			return -1
		}
		if a.Runs[0].Id > b.Runs[0].Id {
			return 1
		}
		return 0
	})
	for _, bundle := range bundles {
		snap.Runs = append(snap.Runs, bundle.Runs...)
		snap.Agents = append(snap.Agents, bundle.Agents...)
		snap.Turns = append(snap.Turns, bundle.Turns...)
		snap.Messages = append(snap.Messages, bundle.Messages...)
		snap.Searches = append(snap.Searches, bundle.Searches...)
		snap.CheckIns = append(snap.CheckIns, bundle.CheckIns...)
		snap.Summaries = append(snap.Summaries, bundle.Summaries...)
		for _, search := range bundle.Searches {
			seen[search.RunId] = search.Revision
		}
		for _, summary := range bundle.Summaries {
			held[summary.RunId] = summary
		}
	}
	return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Snapshot{Snapshot: snap}}, nil
}

// addDetail adds the history of a run to its bundle as far as the byte budget allows, leaving out first
// its search view (which follows as a response of its own), then its check-ins, then its turns and
// messages, and sets truncated on the snapshot for each that it leaves out. used is what the bundles
// before it take.
func (s *Service) addDetail(bundle *pb.RunsSnapshot, state core.State, run core.RunID, used int, snap *pb.RunsSnapshot) error {
	over := func() bool { return used+proto.Size(bundle) > MaxSnapshotBytes-2 }
	bundle.Searches = []*pb.SearchState{s.e.searchState(run)}
	if err := s.addHistory(bundle, run); err != nil {
		return err
	}
	checkIns, cut := newestCheckIns(state.CheckIns(run))
	for _, c := range checkIns {
		bundle.CheckIns = append(bundle.CheckIns, wire.CheckInMessage(c))
	}
	snap.Truncated = snap.Truncated || cut
	if over() {
		bundle.Searches = nil
		snap.Truncated = true
	}
	if over() {
		bundle.CheckIns = nil
		snap.Truncated = true
	}
	if over() {
		bundle.Turns, bundle.Messages = nil, nil
		snap.Truncated = true
	}
	return nil
}
