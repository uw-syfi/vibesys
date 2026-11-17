package rocm

import (
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// EnvVisibleDevices is the variable the ROCm runtime reads the devices a process may use from.
// Its numbering is the runtime's own, which is not the numbering of rocm-smi, and not the order of
// the render nodes under /dev/dri either: on a machine of four GPUs, the runtime's device 0 can be
// the card rocm-smi calls GPU 3. A pool's device numbers are the runtime's, so that a group names
// the devices a process sees; the PCI buses behind them are for diagnosis only.
//
// It is the only variable set, on purpose. HIP_VISIBLE_DEVICES counts the devices that remain
// after this one has filtered them, so setting both to the group would filter twice.
const EnvVisibleDevices = "ROCR_VISIBLE_DEVICES"

// DevicesConfig configures NewDevices.
type DevicesConfig struct {
	// Machine names whose GPUs these are, as Config.Machine does.
	Machine string
	// Size is how many GPUs the pool has: the machine's GPUs 0 to Size-1 in the runtime's numbering.
	Size core.GPUCount
}

// AMDDevices gives a job the AMD GPUs of a group. Create it with NewDevices.
//
// Nothing confines a job to its group, in a container or on the host: a container is given the
// kernel driver's devices whole (binding one render node does not hide the others from the ROCm
// runtime, which reads /dev/kfd), and the runtime only honours ROCR_VISIBLE_DEVICES. Two jobs of
// disjoint groups that respect it do not meet; a process that ignores it can use any GPU of the
// machine.
type AMDDevices struct {
	id   core.PoolID
	size core.GPUCount
}

var _ resource.Devices = (*AMDDevices)(nil)

// NewDevices returns the role, or a core.FailureInvalidInput naming what is wrong.
func NewDevices(cfg DevicesConfig) (*AMDDevices, error) {
	id, err := resource.PoolIDOf(core.ResourceROCm, cfg.Machine)
	switch {
	case err != nil:
		return nil, invalid("%v", err)
	case !cfg.Size.IsSet():
		return nil, invalid("the pool of %s has no size", cfg.Machine)
	}
	return &AMDDevices{id: id, size: cfg.Size}, nil
}

// Kind implements resource.Devices.
func (*AMDDevices) Kind() core.ResourceKind { return core.ResourceROCm }

// Pool implements resource.Devices.
func (d *AMDDevices) Pool() (core.PoolID, core.GPUCount) { return d.id, d.size }

// Container implements resource.Devices: the driver's devices, which ROCm's runtime reads whole,
// and the variable that names the group.
func (d *AMDDevices) Container(g core.DeviceGroup) (resource.Attachment, error) {
	if err := resource.CheckGroup(g, d.size); err != nil {
		return resource.Attachment{}, invalid("%v", err)
	}
	return resource.Attachment{GPUs: resource.GPUs{ROCm: true}, GPUEnv: map[string]string{EnvVisibleDevices: resource.List(g)}}, nil
}

// Process implements resource.Devices.
func (d *AMDDevices) Process(g core.DeviceGroup) (map[string]string, error) {
	if err := resource.CheckGroup(g, d.size); err != nil {
		return nil, invalid("%v", err)
	}
	return map[string]string{EnvVisibleDevices: resource.List(g)}, nil
}
