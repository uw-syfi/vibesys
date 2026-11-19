package production

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// facts is a Fake of what a vendor's tool reads of a machine.
type facts struct {
	count, perDevice string
	err              error
}

func (f facts) ProbeFacts(context.Context) (map[string]string, error) {
	return map[string]string{"gpu_count": f.count, "per_device": f.perDevice}, f.err
}

// A pool of NeuronCores is refused when its count or its cores_per_device is not the machine's,
// naming the key; a machine that cannot be read gives no verdict.
func TestAPoolOfNeuronCoresMustBeTheMachines(t *testing.T) {
	op := parse(t, "[placements.trn]\nkind = \"local\"\n[placements.trn.resources.neuron]\ncount = 4\ncores_per_device = 4\n")
	pools, err := poolsOf(op)
	if err != nil || pools["trn"] == nil || pools["trn"].kind != core.ResourceNeuron {
		t.Fatalf("poolsOf: %v, %v", pools, err)
	}
	if id, size := pools["trn"].devices.Pool(); id != "neuron:local" || size.Int() != 4 {
		t.Errorf("the pool is %s of %d", id, size.Int())
	}
	count := func(n, per string) countOf {
		return countOf{facts: facts{count: n, perDevice: per}, fact: "gpu_count", unit: "NeuronCores", perDevice: "per_device"}
	}
	for _, c := range []struct{ n, per, want string }{
		{"4", "4", ""},
		{"8", "4", "placements.trn.resources.neuron.count = 4, and local has 8 NeuronCores"},
		{"4", "2", "placements.trn.resources.neuron.cores_per_device = 4, and each device of local has 2 NeuronCores"},
		{"4", "", ""},
	} {
		err := checkPool(t.Context(), pools["trn"], count(c.n, c.per), 4)
		if c.want == "" && err != nil || c.want != "" && (err == nil || !strings.Contains(err.Error(), c.want)) {
			t.Errorf("a machine of %s cores, %s per device: %v, want %q", c.n, c.per, err, c.want)
		}
	}
}

// A pool whose size is not the machine's GPU count is refused naming the key and the machine; a
// machine that cannot be read gives no verdict.
func TestAPoolThatIsNotTheMachinesGPUsIsRefusedNamingItsKey(t *testing.T) {
	op := parse(t, "[placements.pod]\nkind = \"local\"\n[placements.pod.resources.gpu]\ncount = 8\n")
	pools, err := poolsOf(op)
	if err != nil || pools["pod"] == nil {
		t.Fatalf("poolsOf: %v, %v", pools, err)
	}
	for count, want := range map[string]string{"8": "", "4": "placements.pod.resources.gpu.count = 8, and local has 4 GPUs", "16": "has 16 GPUs", "": "", "x": ""} {
		err := checkPool(t.Context(), pools["pod"], countOf{facts: facts{count: count}, fact: "gpu_count", unit: "GPUs"}, 0)
		if want == "" && err != nil || want != "" && (err == nil || !strings.Contains(err.Error(), want)) {
			t.Errorf("a machine with %q GPUs: %v, want %q", count, err, want)
		}
	}
	if err := checkPool(t.Context(), pools["pod"], countOf{facts: facts{err: errors.New("no nvidia-smi")}, fact: "gpu_count", unit: "GPUs"}, 0); err != nil {
		t.Errorf("a machine that cannot be read: %v, want no verdict", err)
	}
}

// The scheduler counts a pool as a resource of that many devices, claimed by the placement's jobs
// and, for the host jobs of this machine, by the local placement that declares it, whose slot they
// also take; a config whose two local placements both declare the machine's GPUs, one of each
// vendor, is refused, since a host job cannot say which it uses.
func TestThePoolOfALocalPlacementIsCountedAndTakenByHostJobs(t *testing.T) {
	op := parse(t, "[placements.pod]\nkind = \"local\"\n[placements.pod.resources.gpu]\ncount = 8\n")
	s, err := schedulerOf(op, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	var pool *schedule.Resource
	for i, r := range s.Resources {
		if r.ID == "gpu:local" {
			pool = &s.Resources[i]
		}
		if r.ID == "host" {
			t.Errorf("host jobs have a resource of their own beside a local placement that declares the GPUs")
		}
	}
	if pool == nil || pool.Capacity != 8 || !pool.Devices {
		t.Fatalf("the pool is %+v, want 8 numbered devices", pool)
	}
	d := s.Demand.(schedule.PlacementDemand)
	if d.Host != schedule.PlacementOf("pod") || d.HostPool == nil || d.HostPool.Resource != "gpu:local" || d.Placements["pod"].Pool == nil {
		t.Errorf("the demand is %+v", d)
	}
	for _, r := range s.Resources {
		if r.ID == schedule.PlacementOf("pod") && r.Capacity != 8 {
			t.Errorf("the placement has %d slots, want one per GPU", r.Capacity)
		}
	}

	two := parse(t, "[placements.a]\nkind = \"local\"\n[placements.a.resources.gpu]\ncount = 2\n[placements.b]\nkind = \"local\"\n[placements.b.resources.rocm]\ncount = 2\n")
	if _, err := schedulerOf(two, nil, nil); err == nil || !strings.Contains(err.Error(), "a host job cannot say which") {
		var f *core.Failure
		t.Errorf("two local placements that declare GPUs: %v (%v)", err, errors.As(err, &f))
	}
}
