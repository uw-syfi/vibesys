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

// MaxSnapshotBytes leaves room for the response wrapper below the receive limit.
// Historical run bundles are optional; omitted search views follow separately.
const MaxSnapshotBytes = 1 << 20

func (s *Service) runsSnapshot(state core.State, last uint64, run core.RunID, scoped bool, seen map[string]uint64) (*pb.WatchRunsResponse, error) {
	clear(seen)
	snap := &pb.RunsSnapshot{Sequence: last}
	var bundles []*pb.RunsSnapshot
	used := proto.Size(snap)
	runs := state.Runs()
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
	for _, r := range runs {
		if scoped && r.ID != run {
			continue
		}
		bundle := &pb.RunsSnapshot{Runs: []*pb.Run{wire.RunMessage(r)}, Searches: []*pb.SearchState{s.e.searchState(r.ID)}}
		for _, a := range state.Agents(r.ID) {
			bundle.Agents = append(bundle.Agents, wire.AgentMessage(a))
		}
		if err := s.addHistory(bundle, r.ID); err != nil {
			return nil, err
		}
		if used+proto.Size(bundle) > MaxSnapshotBytes-2 {
			bundle.Searches = nil
			snap.Truncated = true
		}
		if used+proto.Size(bundle) > MaxSnapshotBytes-2 {
			// Keep run identity and current agents in preference to its older history.
			bundle.Turns, bundle.Messages = nil, nil
			snap.Truncated = true
		}
		if used+proto.Size(bundle) > MaxSnapshotBytes-2 {
			snap.Truncated = true
			continue
		}
		bundles = append(bundles, bundle)
		used += proto.Size(bundle)
	}
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
		for _, search := range bundle.Searches {
			seen[search.RunId] = search.Revision
		}
	}
	return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Snapshot{Snapshot: snap}}, nil
}
