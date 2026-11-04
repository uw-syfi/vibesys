package production

import (
	"errors"
	"slices"
	"strings"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fairshare"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fifo"
)

// test-isolation: schedulerOf is the wiring from the operator config to the daemon's scheduler,
// which RunDaemon reaches only with a real runtime directory and index.

// The config of no file is today's daemon: one slot of the placement "default", which host jobs
// share, and fifo.
func TestTheSchedulerOfNoConfigIsOneSlot(t *testing.T) {
	s, err := schedulerOf(parse(t, ""), nil)
	if err != nil {
		t.Fatal(err)
	}
	want := []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 1}}
	if _, ok := s.Policy.(fifo.Policy); !ok || !slices.Equal(s.Resources, want) {
		t.Fatalf("the default scheduler is %+v", s)
	}
	if d, ok := s.Demand.(schedule.PlacementDemand); !ok || d.Host != schedule.DefaultPlacement {
		t.Fatalf("the default demand is %+v", s.Demand)
	}
	s, err = schedulerOf(parse(t, "[scheduler]\npolicy = \"fair-share\"\nmax_jobs = 4\n"), nil)
	if _, ok := s.Policy.(fairshare.Policy); err != nil || !ok || s.Resources[0].Capacity != 4 {
		t.Fatalf("fair-share with 4 jobs is %+v, %v", s, err)
	}
}

// Each placement is a resource of max_jobs slots, and what the placements' resources claim is one
// with room for every job of the placements that claim it, so it bounds only a measure. Host jobs
// with no local placement "default" have a slot of their own.
func TestEachPlacementAndWhatItsResourcesClaimIsAResource(t *testing.T) {
	op := parse(t, "[placements.a]\nkind = \"local\"\nmax_jobs = 4\n[placements.b]\nkind = \"local\"\nmax_jobs = 2\n")
	cluster := core.Claim{Resource: "kubernetes:m/k", Amount: 1}
	s, err := schedulerOf(op, map[string][]core.Claim{"a": {cluster}, "b": {cluster}})
	if err != nil {
		t.Fatal(err)
	}
	want := []schedule.Resource{
		{ID: "placement:a", Capacity: 4}, {ID: "placement:b", Capacity: 2}, {ID: "kubernetes:m/k", Capacity: 6}, {ID: hostResource, Capacity: 1},
	}
	if !slices.Equal(s.Resources, want) {
		t.Fatalf("resources %+v, want %+v", s.Resources, want)
	}
	d := s.Demand.(schedule.PlacementDemand)
	if d.Host != hostResource || len(d.Placements["a"]) != 1 || d.Placements["a"][0].Resource != "kubernetes:m/k" {
		t.Fatalf("demand %+v", d)
	}
}

func TestAnUnknownPolicyIsRefusedNamingThePolicies(t *testing.T) {
	_, err := schedulerOf(parse(t, "[scheduler]\npolicy = \"lottery\"\n"), nil)
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "scheduler.policy") || !strings.Contains(f.Message, "fair-share, fifo") {
		t.Fatalf("an unknown policy: %v", err)
	}
}

func parse(t *testing.T, text string) *cliv1.OperatorConfig {
	t.Helper()
	c, err := operator.Parse([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	return c
}
