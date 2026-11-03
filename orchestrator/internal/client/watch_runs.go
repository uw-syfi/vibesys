package client

import (
	"fmt"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// DecodeWatchRuns classifies a frame once. Unknown or missing bodies panic rather than being
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
