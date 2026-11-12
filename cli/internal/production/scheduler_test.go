package production

import (
	"errors"
	"maps"
	"slices"
	"strings"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fairshare"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fifo"
)

// test-isolation: schedulerOf is the wiring from the operator config to the daemon's scheduler,
// which RunDaemon reaches only with a real runtime directory and index.

// The config of no file is today's daemon: one slot of the placement "default", which host jobs
// share, and fifo.
func TestTheSchedulerOfNoConfigIsOneSlot(t *testing.T) {
	s, err := schedulerOf(parse(t, ""), nil, nil)
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
	s, err = schedulerOf(parse(t, "[scheduler]\npolicy = \"fair-share\"\nmax_jobs = 4\n"), nil, nil)
	if _, ok := s.Policy.(fairshare.Policy); err != nil || !ok || s.Resources[0].Capacity != 4 {
		t.Fatalf("fair-share with 4 jobs is %+v, %v", s, err)
	}
}

// Each placement is a resource of max_jobs slots, and what the placements' resources claim is one
// with room for every job of the placements that claim it. Host jobs
// with no local placement "default" have a slot of their own.
func TestEachPlacementAndWhatItsResourcesClaimIsAResource(t *testing.T) {
	op := parse(t, "[placements.a]\nkind = \"local\"\nmax_jobs = 4\n[placements.b]\nkind = \"local\"\nmax_jobs = 2\n")
	cluster := core.Claim{Resource: "kubernetes:m/k", Amount: 1}
	offers := []core.PlacementOffer{{Name: "a", Resources: []core.ResourceKind{core.ResourceKubernetes}}, {Name: "b", Resources: []core.ResourceKind{core.ResourceKubernetes}}}
	s, err := schedulerOf(op, offers, map[string][]core.Claim{"a": {cluster}, "b": {cluster}})
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
	if a := d.Placements["a"]; d.Host != hostResource || len(a.Claims) != 1 || a.Claims[0].Resource != "kubernetes:m/k" || !slices.Equal(a.Resources, offers[0].Resources) {
		t.Fatalf("demand %+v", d)
	}
}

func TestAnUnknownPolicyIsRefusedNamingThePolicies(t *testing.T) {
	_, err := schedulerOf(parse(t, "[scheduler]\npolicy = \"lottery\"\n"), nil, nil)
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

// No two jobs that run with one Kubernetes cluster run at once, whatever their kinds, whichever
// placements of the cluster they run on, and whatever max_jobs allows: each deploys into the
// cluster with CPU and memory the scheduler does not see. A job of a placement without the cluster
// runs beside them. The claims are made by the placements' resources, as the daemon makes them,
// and every job leases the cluster, so the daemons that share it take turns too.
//
// Regression: two checks of a Kubernetes task ran at once on one placement of max_jobs 3 and the
// second failed for want of the CPU the first one's pods held.
func TestNoTwoJobsRunWithOneClusterAtOnce(t *testing.T) {
	configs := []string{
		"[placements.local]\nkind = \"local\"\nmax_jobs = 3\n[placements.local.resources.kubernetes]\nkubeconfig = \"/k\"\n",
		"[placements.a]\nkind = \"local\"\nmax_jobs = 2\n[placements.a.resources.kubernetes]\nkubeconfig = \"/k\"\n" +
			"[placements.b]\nkind = \"local\"\nmax_jobs = 2\n[placements.b.resources.kubernetes]\nkubeconfig = \"/k\"\n" +
			"[placements.c]\nkind = \"local\"\nmax_jobs = 2\n",
	}
	kinds := []core.Settings{core.CheckSettings{}, core.CommandSettings{Image: core.ImageHarness, Argv: []string{"true"}}, core.MeasureSettings{Samples: 3}}
	for _, text := range configs {
		op := parse(t, text)
		claims := map[string][]core.Claim{}
		var offers []core.PlacementOffer
		for _, name := range slices.Sorted(maps.Keys(op.GetPlacements())) {
			rs, err := resources(op.GetPlacements()[name].GetResources(), remote.Local{}, "local", "local", clock.System{}, nil)
			if err != nil {
				t.Fatal(err)
			}
			offer := core.PlacementOffer{Name: name}
			for _, r := range rs {
				claims[name] = append(claims[name], r.Claims()...)
				offer.Resources = append(offer.Resources, r.Kind())
			}
			offers = append(offers, offer)
		}
		s, err := schedulerOf(op, offers, claims)
		if err != nil {
			t.Fatal(err)
		}
		book := (Process{UID: 1000}).leasingOf(op, shared, claims, clock.System{}, nil).Book
		onCluster := func(placement string) bool { return len(claims[placement]) > 0 }
		candidate := func(id, placement string, settings core.Settings) schedule.Candidate {
			job := core.Job{Spec: core.JobSpec{Environment: core.EnvironmentDocker, Placement: placement, Settings: settings}}
			if placement != "c" {
				job.Needs = []core.ResourceKind{core.ResourceKubernetes}
			}
			return schedule.Candidate{ID: schedule.ID(id), Options: s.Demand.Options(job), Run: "run-a", Seq: uint64(len(id))}
		}
		for _, first := range slices.Sorted(maps.Keys(op.GetPlacements())) {
			for _, second := range slices.Sorted(maps.Keys(op.GetPlacements())) {
				for _, k1 := range kinds {
					for _, k2 := range kinds {
						a, b := candidate("a", first, k1), candidate("bb", second, k2)
						if len(a.Options) != 1 || len(b.Options) != 1 {
							t.Fatalf("config %q: options %+v and %+v, want one each", text, a.Options, b.Options)
						}
						for _, c := range a.Options[0].Claims {
							if strings.HasPrefix(string(c.Resource), string(core.ResourceKubernetes)+":") {
								if _, leased := book.Place(string(c.Resource)); !c.Exclusive || !leased {
									t.Errorf("config %q: a %s on %s claims the cluster %+v, leased %v", text, k1.Kind(), first, c, leased)
								}
							}
						}
						apart := onCluster(first) && onCluster(second)
						// The jobs arrive while the first one runs, and together.
						running := schedule.State{Resources: s.Resources, Holdings: []schedule.Holding{{ID: a.ID, Claims: a.Options[0].Claims, Run: a.Run}}, Waiting: []schedule.Candidate{b}}
						together := schedule.State{Resources: s.Resources, Waiting: []schedule.Candidate{a, b}}
						for name, state := range map[string]schedule.State{"beside a running one": running, "together": together} {
							started := 0
							for _, d := range schedule.Step(state, schedule.Rules{Policy: s.Policy}) {
								if _, ok := d.(schedule.Start); ok {
									started++
								}
							}
							started += len(state.Holdings)
							shareable := first != second && !apart && k1.Kind() != core.JobMeasure && k2.Kind() != core.JobMeasure
							if apart && started != 1 {
								t.Errorf("config %q: a %s on %s and a %s on %s, %s: %d run on one cluster at once", text, k1.Kind(), first, k2.Kind(), second, name, started)
							}
							if shareable && started != 2 {
								t.Errorf("config %q: a %s on %s and a %s on %s, %s: %d run, want both", text, k1.Kind(), first, k2.Kind(), second, name, started)
							}
						}
					}
				}
			}
		}
	}
}
