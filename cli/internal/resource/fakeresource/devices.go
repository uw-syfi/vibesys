package fakeresource

import (
	"slices"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// FakeVisibleDevices is the variable a Devices Fake sets, beside the GPUs it attaches by number.
const FakeVisibleDevices = "FAKE_VISIBLE_DEVICES"

// Devices is a resource.Devices of any kind and size that gives NVIDIA-style numbered GPUs and a
// variable, and records the groups it was asked for, so that a test of what applies a group sees
// which one reached it. It passes the contract in devicestest.
type Devices struct {
	Of   core.ResourceKind
	ID   core.PoolID
	Size core.GPUCount

	mu    sync.Mutex
	asked []core.DeviceGroup
}

var _ resource.Devices = (*Devices)(nil)

// Kind implements resource.Devices.
func (d *Devices) Kind() core.ResourceKind { return d.Of }

// Pool implements resource.Devices.
func (d *Devices) Pool() (core.PoolID, core.GPUCount) { return d.ID, d.Size }

// Container implements resource.Devices.
func (d *Devices) Container(g core.DeviceGroup) (resource.Attachment, error) {
	if err := resource.CheckGroup(g, d.Size); err != nil {
		return resource.Attachment{}, &core.Failure{Kind: core.FailureInvalidInput, Message: "fakeresource: " + err.Error()}
	}
	d.record(g)
	return resource.Attachment{GPUs: resource.GPUs{NVIDIA: resource.List(g)}, GPUEnv: map[string]string{FakeVisibleDevices: resource.List(g)}}, nil
}

// Process implements resource.Devices.
func (d *Devices) Process(g core.DeviceGroup) (map[string]string, error) {
	if err := resource.CheckGroup(g, d.Size); err != nil {
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: "fakeresource: " + err.Error()}
	}
	d.record(g)
	return map[string]string{FakeVisibleDevices: resource.List(g)}, nil
}

func (d *Devices) record(g core.DeviceGroup) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.asked = append(d.asked, g)
}

// Asked returns the groups Container and Process were called for, in order.
func (d *Devices) Asked() []core.DeviceGroup {
	d.mu.Lock()
	defer d.mu.Unlock()
	return slices.Clone(d.asked)
}
