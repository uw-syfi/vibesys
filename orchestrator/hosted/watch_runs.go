package hosted

import (
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
)

// DecodeWatchRuns exposes the shared test-facing reader to gateway consumers.
func DecodeWatchRuns(frame *pb.WatchRunsResponse) (*pb.RunsSnapshot, *pb.RunChange, *pb.SearchState) {
	return client.DecodeWatchRuns(frame)
}
