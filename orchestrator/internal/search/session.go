package search

import pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/cli/v1"

// Facts and Request name the callee-owned messages at the wiring boundary.
type Facts = []*pb.TaskFact

func Request(repository, task string) *pb.WatchTaskRequest {
	return &pb.WatchTaskRequest{Context: &pb.CallContext{Repository: repository, Task: task}}
}
