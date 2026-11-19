package resource

import (
	"fmt"
	"regexp"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Devices is how one vendor's GPUs are given to a job, by device number. It is a role of its own,
// beside Resource: a Resource gives a container what every job of a placement gets, and Devices
// turns the group of a pool's devices that the scheduler chose for one job into what that job is
// given. It knows nothing of scheduling or of the placement's kind, and does no I/O: it is data in,
// data out, so each vendor's numbering and variables are its own package's.
//
// The numbers are those of the pool, 0 to its size less one, in the numbering the vendor's own
// device variable takes (NVIDIA_VISIBLE_DEVICES and CUDA_VISIBLE_DEVICES, ROCR_VISIBLE_DEVICES, or
// NEURON_RT_VISIBLE_CORES, whose devices are NeuronCores), which is not always the numbering of
// the vendor's monitoring tool. A pool whose kind is core.ResourceKind.Contiguous gives a job only a
// contiguous range (core.TakeFor), and its role refuses a group that is not one. How firmly a group is kept
// to its devices belongs to the environment that applies it: Container is what a container
// engine can enforce, Process only what a process is asked to respect.
//
// Adding a vendor is a package under this one that implements Devices and Resource, a line of
// wiring, and nothing else: the scheduler counts a pool by its id and size, and the environments
// apply what Container and Process return. Every implementation passes devicestest.
type Devices interface {
	// Kind is the kind of resource whose GPUs these are: a task that needs it gets the group.
	Kind() core.ResourceKind

	// Pool is the pool's id, which starts with the kind and a colon and names the machine whose
	// GPUs they are, and its size. It is stable.
	Pool() (core.PoolID, core.GPUCount)

	// Container is what a container is given so that it uses exactly the group: the engine's
	// device flags for a vendor whose devices it can pass by number, the variables that name
	// the group, and the vendor's Check of the group, when it has one, as the attachment's Check.
	// It is the same on every call for the same group. It fails with a *core.Failure of kind
	// FailureInvalidInput, naming the device, for a group CheckGroup refuses.
	Container(g core.DeviceGroup) (Attachment, error)

	// Process is the variables a process on the host is given so that it uses exactly the group.
	// Nothing enforces them: a process that ignores them can use any GPU of the machine. It fails
	// as Container does.
	Process(g core.DeviceGroup) (map[string]string, error)

	// Check is a command the environment runs on the machine before the job's commands, a program
	// and its arguments that exits 0 when the group's devices may be used, and otherwise prints why
	// not, naming the device, which is what the job fails with before anything of it runs. It reads
	// the devices as they are, never the scheduler's own record of who holds them. Nil when the
	// vendor checks nothing. It fails as Container does.
	Check(g core.DeviceGroup) ([]string, error)
}

var poolID = regexp.MustCompile(`^[a-z]+:[A-Za-z0-9][A-Za-z0-9_.@-]*$`)

// PoolIDOf is the id of the pool of the kind's GPUs of a machine, kind:machine, which is what the
// scheduler names the resource it counts. It fails with a *core.Failure of kind FailureInvalidInput
// for a machine name a pool cannot be named by.
func PoolIDOf(kind core.ResourceKind, machine string) (core.PoolID, error) {
	id := string(kind) + ":" + machine
	if !poolID.MatchString(id) {
		return "", invalid("the machine %q is not a machine name", machine)
	}
	return core.PoolID(id), nil
}

// CheckGroup refuses a group that no one may be given of a pool of the kind and size: an empty one,
// one with a device outside the pool, and, for a kind whose groups are ranges
// (core.ResourceKind.Contiguous), one with a gap. It names the device.
func CheckGroup(kind core.ResourceKind, g core.DeviceGroup, size core.GPUCount) error {
	if g.Len() == 0 {
		return invalid("the group has no device")
	}
	devices := g.Devices()
	for _, d := range devices {
		if int(d) >= size.Int() {
			return invalid("device %d is not one of the pool's %d (0 to %d)", d, size.Int(), size.Int()-1)
		}
	}
	if kind.Contiguous() && !g.IsRange() {
		for i := 1; i < len(devices); i++ {
			if devices[i] != devices[i-1]+1 {
				return invalid("device %d follows device %d: a group of %s devices is a contiguous range", devices[i], devices[i-1], kind)
			}
		}
	}
	return nil
}

// List writes the group's numbers as the vendors' device variables take them: "0,1,5".
func List(g core.DeviceGroup) string {
	var out []byte
	for i, d := range g.Devices() {
		if i > 0 {
			out = append(out, ',')
		}
		out = fmt.Append(out, int(d))
	}
	return string(out)
}
