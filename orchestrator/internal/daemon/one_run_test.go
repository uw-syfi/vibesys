package daemon_test

import (
	"fmt"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

func startIn(id, repository, task string) *pb.Command {
	cmd := startRun(id)
	cmd.GetStartRun().Repository, cmd.GetStartRun().Task = repository, task
	return cmd
}

// A second start in a repository is refused, with the id of the run that is there, whatever its task.
func TestASecondStartInARepositoryIsRefusedNamingTheRunThere(t *testing.T) {
	r := daemontest.NewRig(t)
	first := dispatch(t, r, startIn("c1", "/repo", "task"))
	for i, task := range []string{"task", "other"} {
		second := dispatch(t, r, startIn(fmt.Sprintf("again-%d", i), "/repo", task))
		if second.GetRefusal() != pb.Refusal_REFUSAL_REPOSITORY_IN_USE || second.GetRunId() != first.GetRunId() {
			t.Fatalf("second start of %q: %v, want a refusal naming %s", task, second, first.GetRunId())
		}
	}
}
