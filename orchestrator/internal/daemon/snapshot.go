package daemon

import (
	"slices"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
	"google.golang.org/protobuf/proto"
)

// MaxSnapshotCheckIns is how many of a viewer's check-ins in one run a snapshot holds, the newest. A
// viewer presses Catch me up now and then, so the cut is for a viewer who pressed it very many times.
const MaxSnapshotCheckIns = 64

// MaxSnapshotBytes leaves room for the response wrapper below the receive limit.
// Historical run bundles are optional; omitted search views follow separately.
const MaxSnapshotBytes = 1 << 20

// snapshotReserve is what truncated and runs_left_out add to a snapshot once its runs are known, at their
// widest, which the budget keeps free before it lists a run.
const snapshotReserve = 8

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

// addDetail adds the history of a run to its bundle as far as the byte budget allows, leaving out first
// its search view (which follows as a response of its own), then its check-ins, then its turns and
// messages, and sets truncated on the snapshot for each that it leaves out. used is what the bundles
// before it take. A run whose history cannot be read is listed without it and sets truncated: it costs
// the snapshot nothing else.
func (s *Service) addDetail(bundle *pb.RunsSnapshot, state core.State, run core.RunID, used int, snap *pb.RunsSnapshot) {
	over := func() bool { return used+proto.Size(bundle) > MaxSnapshotBytes-snapshotReserve }
	bundle.Searches = []*pb.SearchState{s.e.searchState(run)}
	if err := s.addHistory(bundle, run); err != nil {
		s.e.Log.Error("cannot read the history of a run; it is listed without", "run", run, "err", err)
		bundle.Turns, bundle.Messages = nil, nil
		snap.Truncated = true
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
}
