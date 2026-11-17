package gpu

import (
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// The variables the devices of a group are named in.
const (
	// EnvCUDAVisibleDevices is the variable CUDA reads the devices a process may use from, in the
	// numbering of EnvCUDADeviceOrder.
	EnvCUDAVisibleDevices = "CUDA_VISIBLE_DEVICES"
	// EnvCUDADeviceOrder is the order CUDA numbers the devices in. Its default, fastest first, is
	// not the order nvidia-smi and NVIDIA_VISIBLE_DEVICES number them in, which is the order of
	// the PCI bus, so a process on the host is told to use that one: its device 2 is then the
	// nvidia-smi's GPU 2. A container needs no such variable: the container runtime renumbers the
	// devices it passes from 0, and CUDA in it sees only those.
	EnvCUDADeviceOrder = "CUDA_DEVICE_ORDER"
	// PCIBusOrder is the value of EnvCUDADeviceOrder that numbers the devices as nvidia-smi does.
	PCIBusOrder = "PCI_BUS_ID"
)

// DevicesConfig configures NewDevices.
type DevicesConfig struct {
	// Machine names the machine whose GPUs these are, as Config.Machine does, so that the pools of
	// two machines are two.
	Machine string
	// Size is how many GPUs the pool has: the machine's GPUs 0 to Size-1.
	Size core.GPUCount
	// Runtime, when set, is the container runtime that passes the devices, with the group in
	// NVIDIA_VISIBLE_DEVICES, instead of --gpus.
	Runtime string
}

// NVIDIADevices gives a job the NVIDIA GPUs of a group. Create it with NewDevices.
//
// A container gets exactly its group: docker passes the devices by number, so the others do not
// exist in it. A process on the host is told its group in CUDA_VISIBLE_DEVICES and nothing keeps
// it from using another. The numbers are those of nvidia-smi (the order of the PCI bus).
type NVIDIADevices struct {
	id      core.PoolID
	size    core.GPUCount
	runtime string
}

var _ resource.Devices = (*NVIDIADevices)(nil)

// NewDevices returns the role, or a core.FailureInvalidInput naming what is wrong.
func NewDevices(cfg DevicesConfig) (*NVIDIADevices, error) {
	id, err := resource.PoolIDOf(core.ResourceGPU, cfg.Machine)
	switch {
	case err != nil:
		return nil, invalid("%v", err)
	case !cfg.Size.IsSet():
		return nil, invalid("the pool of %s has no size", cfg.Machine)
	case !runtime.MatchString(cfg.Runtime):
		return nil, invalid("runtime: %q is not a runtime name", cfg.Runtime)
	}
	return &NVIDIADevices{id: id, size: cfg.Size, runtime: cfg.Runtime}, nil
}

// Kind implements resource.Devices.
func (*NVIDIADevices) Kind() core.ResourceKind { return core.ResourceGPU }

// Pool implements resource.Devices.
func (d *NVIDIADevices) Pool() (core.PoolID, core.GPUCount) { return d.id, d.size }

// Container implements resource.Devices: the group by number, to docker's --gpus (or the runtime's
// variable), and no variable of CUDA's, since the container's devices are numbered from 0.
func (d *NVIDIADevices) Container(g core.DeviceGroup) (resource.Attachment, error) {
	if err := resource.CheckGroup(g, d.size); err != nil {
		return resource.Attachment{}, invalid("%v", err)
	}
	a := resource.Attachment{GPUs: resource.GPUs{NVIDIA: resource.List(g), Runtime: d.runtime}}
	if d.runtime != "" {
		// The runtime's own hook passes the devices the variable names.
		a.GPUEnv = map[string]string{EnvVisibleDevices: resource.List(g)}
	}
	return a, nil
}

// Process implements resource.Devices: CUDA_VISIBLE_DEVICES, in the PCI bus order.
func (d *NVIDIADevices) Process(g core.DeviceGroup) (map[string]string, error) {
	if err := resource.CheckGroup(g, d.size); err != nil {
		return nil, invalid("%v", err)
	}
	return map[string]string{EnvCUDAVisibleDevices: resource.List(g), EnvCUDADeviceOrder: PCIBusOrder}, nil
}
