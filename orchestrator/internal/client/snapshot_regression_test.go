package client

import (
	"context"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"google.golang.org/grpc"
)

type snapshotService struct{ pb.OrchestratorServiceClient }

func (snapshotService) WatchRuns(context.Context, *pb.WatchRunsRequest, ...grpc.CallOption) (grpc.ServerStreamingClient[pb.WatchRunsResponse], error) {
	return snapshotStream{}, nil
}

type snapshotStream struct {
	grpc.ServerStreamingClient[pb.WatchRunsResponse]
}

func (snapshotStream) Recv() (*pb.WatchRunsResponse, error) { return &pb.WatchRunsResponse{}, nil }

func TestSnapshotRejectsMissingBody(t *testing.T) {
	c := &Client{svc: snapshotService{}}
	_, err := c.Snapshot(context.Background())
	if err == nil || err.Error() != "the orchestrator began a stream of runs with something other than a snapshot" {
		t.Fatalf("Snapshot error = %v", err)
	}
}
