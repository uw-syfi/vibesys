package daemon_test

import (
	"fmt"
	"math/rand/v2"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// One directory is one repository however it is named: starts through any chain of links and through
// the real path hold the one run, and a start in another directory is not held.
func TestOneDirectoryIsOneRepositoryWhateverLinkNamesIt(t *testing.T) {
	for seed := range uint64(100) {
		rng := rand.New(rand.NewPCG(seed, seed+1))
		r := daemontest.NewRig(t)
		names := []string{"/real", "/link-a", "/link-b", "/via/link-c"}
		r.Starts.Link("/link-a", "/real")
		r.Starts.Link("/link-b", "/link-a")
		r.Starts.Link("/via/link-c", "/link-b")
		first := dispatch(t, r, startIn("first", names[rng.IntN(len(names))], "task"))
		for i := range 4 {
			again := dispatch(t, r, startIn(fmt.Sprintf("again-%d", i), names[rng.IntN(len(names))], "task"))
			if again.GetRefusal() != pb.Refusal_REFUSAL_REPOSITORY_IN_USE || again.GetRunId() != first.GetRunId() {
				t.Fatalf("seed %d: a start through another name got %v, want a refusal naming %s", seed, again, first.GetRunId())
			}
		}
		if other := dispatch(t, r, startIn("other", "/elsewhere", "task")); other.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
			t.Fatalf("seed %d: a start in another directory got %v", seed, other)
		}
	}
}

// The run is recorded under the canonical repository, so a client that compares paths finds it, and a
// retry of the start through the same link is still the same command.
func TestARunIsRecordedUnderTheCanonicalRepository(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Starts.Link("/link", "/real")
	first := dispatch(t, r, startIn("c1", "/link", "task"))
	run, _ := r.Daemon.State().Run(core.RunID(first.GetRunId()))
	if run.Repository != "/real" {
		t.Fatalf("the run's repository is %q, want /real", run.Repository)
	}
	if again := dispatch(t, r, startIn("c1", "/link", "task")); again.GetRunId() != first.GetRunId() {
		t.Fatalf("a retry answered %v, want %v", again, first)
	}
}
