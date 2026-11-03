package client

import (
	"fmt"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// DecodeWatchRuns classifies a frame once for client and test readers. Unknown or missing bodies panic rather than being
// mistaken for events. Callers may ignore search views, which carry no event sequence.
func DecodeWatchRuns(frame *pb.WatchRunsResponse) (*pb.RunsSnapshot, *pb.Event, *pb.SearchState) {
	switch body := frame.GetBody().(type) {
	case *pb.WatchRunsResponse_Snapshot:
		return body.Snapshot, nil, nil
	case *pb.WatchRunsResponse_Event:
		return nil, body.Event, nil
	case *pb.WatchRunsResponse_Search:
		return nil, nil, body.Search
	default:
		panic(fmt.Sprintf("unhandled WatchRunsResponse body %T", body))
	}
}

// WatchRunsSnapshot classifies a frame before filtering for its snapshot.
func WatchRunsSnapshot(frame *pb.WatchRunsResponse) *pb.RunsSnapshot {
	s, _, _ := DecodeWatchRuns(frame)
	return s
}

// WatchRunsSearch classifies a frame before filtering for its search view.
func WatchRunsSearch(frame *pb.WatchRunsResponse) *pb.SearchState {
	_, _, s := DecodeWatchRuns(frame)
	return s
}
