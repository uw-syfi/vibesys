package production

import (
	"context"
	"fmt"
	"maps"
	"slices"
	"strconv"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/backend/amd"
	neuronfacts "github.com/uw-syfi/vibesys/cli/internal/backend/neuron"
	"github.com/uw-syfi/vibesys/cli/internal/backend/nvidia"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/gpu"
	"github.com/uw-syfi/vibesys/cli/internal/resource/neuron"
	"github.com/uw-syfi/vibesys/cli/internal/resource/rocm"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// pooled is a placement whose GPUs are a pool the daemon counts and hands out by number: a local
// placement or one over ssh, whose file declares a count. (A cluster's pool is the cluster's:
// Slurm keeps two allocations off one GPU, and a job's allocation asks it for the job's count.)
type pooled struct {
	name string
	pool operator.Pool
	// kind is the resource a task needs to use it, and machine names the machine whose GPUs they
	// are: this one, or the host of an ssh placement.
	kind    core.ResourceKind
	machine string
	// Devices says how a job is given a group of the pool, by the vendor's own role.
	devices resource.Devices
	counted schedule.Pool
}

// machineOf names the machine a placement's jobs run on, which the pool's id and the claims of its
// GPUs name: "local" for this machine, and the host of an ssh placement.
func machineOf(pl *cliv1.Placement) string {
	if pl.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_LOCAL {
		return "local"
	}
	return pl.GetRemote().GetHost()
}

// pooledOf is the pool of a placement that declares one and whose kind lets the daemon count it, or
// nil. The vendor's package makes its role (resource.Devices): this is where the vendors are named.
func pooledOf(name string, pl *cliv1.Placement) (*pooled, error) {
	if k := pl.GetKind(); k != cliv1.PlacementKind_PLACEMENT_KIND_LOCAL && k != cliv1.PlacementKind_PLACEMENT_KIND_SSH {
		return nil, nil
	}
	pool, err := operator.PoolOf(name, pl)
	if err != nil || !pool.Size.IsSet() {
		return nil, err
	}
	machine := machineOf(pl)
	p := &pooled{name: name, pool: pool, machine: machine}
	switch pool.Vendor {
	case core.VendorNVIDIA:
		p.kind = core.ResourceGPU
		p.devices, err = gpu.NewDevices(gpu.DevicesConfig{Machine: machine, Size: pool.Size, Runtime: pl.GetResources().GetGpu().GetRuntime()})
	case core.VendorAMD:
		p.kind = core.ResourceROCm
		p.devices, err = rocm.NewDevices(rocm.DevicesConfig{Machine: machine, Size: pool.Size})
	case core.VendorAWS:
		p.kind = core.ResourceNeuron
		p.devices, err = neuron.NewDevices(neuron.DevicesConfig{Machine: machine, Size: pool.Size, CoresPerDevice: int(pl.GetResources().GetNeuron().GetCoresPerDevice())})
	default:
		return nil, fmt.Errorf("%s: no GPUs of the vendor %q are given by number", pool.Key, pool.Vendor)
	}
	if err != nil {
		return nil, fmt.Errorf("%s: %w", pool.Key, err)
	}
	id, size := p.devices.Pool()
	p.counted = schedule.Pool{Resource: schedule.ResourceID(id), Size: size.Int(), Kind: p.kind}
	return p, nil
}

// poolsOf are the pools of the config, by placement.
func poolsOf(op *cliv1.OperatorConfig) (map[string]*pooled, error) {
	out := map[string]*pooled{}
	for _, name := range slices.Sorted(maps.Keys(op.GetPlacements())) {
		p, err := pooledOf(name, op.GetPlacements()[name])
		if err != nil {
			return nil, err
		}
		if p != nil {
			out[name] = p
		}
	}
	return out, nil
}

// hostPoolOf is the pool of this machine's GPUs, which host jobs take their GPUs from so that they
// never share a GPU with a docker job of a local placement: the pool of the one local placement
// that declares one. A config whose local placements declare two (one of each vendor) is refused,
// since a host job cannot say which it uses.
func hostPoolOf(pools map[string]*pooled) (*pooled, error) {
	var found []string
	for _, name := range slices.Sorted(maps.Keys(pools)) {
		if pools[name].machine == "local" {
			found = append(found, name)
		}
	}
	switch len(found) {
	case 0:
		return nil, nil
	case 1:
		return pools[found[0]], nil
	}
	return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
		"the operator config: placements %s are local placements that each declare GPUs of this machine, and a host job cannot say which it uses: declare this machine's GPUs in one", found)}
}

// countOf reads how many devices of a vendor a machine has.
type countOf struct {
	facts interface {
		ProbeFacts(ctx context.Context) (map[string]string, error)
	}
	fact string
	// unit is what the pool counts, for the refusal.
	unit string
	// perDevice, when set, is the fact of how many of the pool's units each device has, which
	// must be the declared one.
	perDevice string
}

// machineCounts are the readers of the device count of a machine each vendor's facts come from.
func machineCounts(vendor core.AcceleratorVendor, shell remote.Shell, clk Clocks) (countOf, bool) {
	switch vendor {
	case core.VendorNVIDIA:
		return countOf{facts: &nvidia.GPUs{Shell: shell, Timers: clk}, fact: nvidia.FactCount, unit: "GPUs"}, true
	case core.VendorAMD:
		return countOf{facts: &amd.GPUs{Shell: shell, Timers: clk}, fact: amd.FactCount, unit: "GPUs"}, true
	case core.VendorAWS:
		return countOf{facts: &neuronfacts.Devices{Shell: shell, Timers: clk}, fact: neuronfacts.FactCores, unit: "NeuronCores", perDevice: neuronfacts.FactCoresPerDevice}, true
	}
	return countOf{}, false
}

// checkPool refuses a pool whose declared size is not the number of devices its machine has: it
// hands out numbers that name no device, or leaves devices that no job is given while a measure
// believes it holds the machine whole. For a pool of NeuronCores it also refuses a cores_per_device
// that is not the machine's, which would give a job the wrong /dev/neuron* devices. The refusal
// names the key of the file. A machine that cannot be read says nothing: the check is made again
// when it can.
func checkPool(ctx context.Context, p *pooled, count countOf, perDevice uint32) error {
	facts, err := count.facts.ProbeFacts(ctx)
	if err != nil {
		return nil
	}
	n, err := strconv.Atoi(facts[count.fact])
	if err != nil {
		return nil
	}
	if err := core.CheckPoolCount(p.pool.Key, p.pool.Size, p.machine, n, count.unit); err != nil {
		return err
	}
	if count.perDevice == "" {
		return nil
	}
	if per, err := strconv.Atoi(facts[count.perDevice]); err == nil && per != int(perDevice) {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"%s = %d, and each device of %s has %d %s", strings.TrimSuffix(p.pool.Key, ".count")+".cores_per_device", perDevice, p.machine, per, count.unit)}
	}
	return nil
}

// checkPools refuses a config whose pool is not the number of GPUs its machine has, before the
// daemon takes a job: for each pool it reads the machine's count through the vendor's own facts.
// A machine that cannot be read (no nvidia-smi here, an ssh host that does not answer) gives no
// verdict, and its pool is believed. The refusal names the key of the file.
func (p Process) checkPools(ctx context.Context, op *cliv1.OperatorConfig, clk Clocks) error {
	pools, err := poolsOf(op)
	if err != nil {
		return err
	}
	for _, name := range slices.Sorted(maps.Keys(pools)) {
		pool := pools[name]
		var shell remote.Shell = remote.Local{}
		if r := op.GetPlacements()[name].GetRemote(); pool.machine != "local" {
			shell = remote.SSH{Host: r.GetHost(), Program: orDefault(r.GetSshCommand(), "ssh")}
		}
		count, ok := machineCounts(pool.pool.Vendor, shell, clk)
		if !ok {
			continue
		}
		if err := checkPool(ctx, pool, count, op.GetPlacements()[name].GetResources().GetNeuron().GetCoresPerDevice()); err != nil {
			return fmt.Errorf("the operator config %s: %w", operator.Path(p.Getenv, p.Home), err)
		}
	}
	return nil
}
