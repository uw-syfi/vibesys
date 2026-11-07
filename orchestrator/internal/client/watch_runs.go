package client

import (
	"fmt"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// DecodeWatchRuns classifies a frame for test readers, panicking on unknown or missing bodies.
func DecodeWatchRuns(frame *pb.WatchRunsResponse) (*pb.RunsSnapshot, *pb.RunChange, *pb.SearchState) {
	s, e, search, err := classifyWatchRuns(frame)
	if err != nil {
		panic(err)
	}
	return s, e, search
}

func classifyWatchRuns(frame *pb.WatchRunsResponse) (*pb.RunsSnapshot, *pb.RunChange, *pb.SearchState, error) {
	switch body := frame.GetBody().(type) {
	case *pb.WatchRunsResponse_Snapshot:
		return body.Snapshot, nil, nil, nil
	case *pb.WatchRunsResponse_Change:
		return nil, body.Change, nil, nil
	case *pb.WatchRunsResponse_Search:
		return nil, nil, body.Search, nil
	case *pb.WatchRunsResponse_Summary:
		// Only a stream that asked for detail SUMMARY carries these, and its reader uses WatchRunsSummary.
		return nil, nil, nil, nil
	default:
		return nil, nil, nil, fmt.Errorf("unhandled WatchRunsResponse body %T", body)
	}
}

// WatchRunsSnapshot classifies a frame before filtering for its snapshot.
func WatchRunsSnapshot(frame *pb.WatchRunsResponse) *pb.RunsSnapshot {
	s, _, _ := DecodeWatchRuns(frame)
	return s
}

// WatchRunsSummary classifies a frame for the summary of a run it carries, or nil for any other frame.
func WatchRunsSummary(frame *pb.WatchRunsResponse) *pb.RunSummary { return frame.GetSummary() }

// WatchRunsSearch classifies a frame before filtering for its search view.
func WatchRunsSearch(frame *pb.WatchRunsResponse) *pb.SearchState {
	_, _, s := DecodeWatchRuns(frame)
	return s
}
