// Package neuron is the resource of AWS Neuron devices (Trainium) a placement gives, shared by
// logical NeuronCore: what [placements.<name>.resources.neuron] configures, and what a task of the
// trainium backend needs. The machine's NeuronCores 0 to count-1 are a pool, and each job is given a
// contiguous range of them (core.TakeFor) by NeuronDevices, its role: the /dev/neuron* devices that
// hold the range under docker, and NEURON_RT_VISIBLE_CORES, in a container and on the host.
//
// Before a job's commands run, its environment runs the role's check (BusyScript), which reads the
// device memory live runtimes hold per physical core in sysfs, never the scheduler's record of who
// holds the cores, and refuses to start the job, naming the device and the holders neuron-ls can
// see, when some held core must be the job's: a process the scheduler does not know of holds it.
package neuron

import (
	"context"
	"fmt"
	"maps"
	"regexp"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// The facts the resource reads from Facts: the devices' model and count, for its offer. They are
// the names the backend/neuron package reports.
const (
	factModel   = "neuron_model"
	factDevices = "neuron_device_count"
)

// Facts reads what identifies the Neuron devices a job gets: their model, count and the driver's
// version, as fingerprint facts. It fails with core.FailureUnavailable when it cannot read them.
type Facts interface {
	GPUFacts(ctx context.Context) (map[string]string, error)
	ProbeFacts(ctx context.Context) (map[string]string, error)
}

// Config configures New.
type Config struct {
	// Machine names whose devices these are: this machine ("local"), or an ssh placement's host.
	Machine string
	// Facts reads the devices for the fingerprint.
	Facts Facts
}

var machine = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.@-]*$`)

// Neuron is the resource. Create it with New. Its devices are always a pool, which the scheduler
// counts by its size (NeuronDevices): it claims nothing itself and attaches nothing, since the
// devices and the check a job gets depend on its cores.
type Neuron struct{ cfg Config }

var _ resource.Resource = (*Neuron)(nil)

// New returns the resource, or a core.FailureInvalidInput naming what is wrong.
func New(cfg Config) (*Neuron, error) {
	switch {
	case !machine.MatchString(cfg.Machine):
		return nil, invalid("the machine %q is not a machine name", cfg.Machine)
	case cfg.Facts == nil:
		return nil, invalid("no reader of the Neuron devices' facts")
	}
	return &Neuron{cfg: cfg}, nil
}

// Kind implements resource.Resource.
func (*Neuron) Kind() core.ResourceKind { return core.ResourceNeuron }

// Claims implements resource.Resource: none, since the pool is counted by its size.
func (*Neuron) Claims() []core.Claim { return nil }

// Fingerprint implements resource.Resource: the facts the reader gives, as they are.
func (r *Neuron) Fingerprint(ctx context.Context) (map[string]string, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	facts, err := r.cfg.Facts.GPUFacts(ctx)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		f := core.AsFailure(err)
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: "neuron: " + f.Message}
	}
	for name, value := range facts {
		if !resource.FactName(name) || value == "" {
			return nil, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("neuron: the reader named a fact %q=%q", name, value)}
		}
	}
	if len(facts) == 0 {
		return nil, &core.Failure{Kind: core.FailureInternal, Message: "neuron: the reader named no fact"}
	}
	return maps.Clone(facts), nil
}

// Attach implements resource.Resource: nothing. A job's devices, variable and check are its
// group's (NeuronDevices.Container).
func (*Neuron) Attach(ctx context.Context) (resource.Attachment, error) {
	if err := ctx.Err(); err != nil {
		return resource.Attachment{}, err
	}
	return resource.Attachment{}, nil
}

// Offers implements resource.Resource: the machine's Neuron devices (chips), of the model the
// driver names, from the same reader as the fingerprint.
func (r *Neuron) Offers(ctx context.Context) ([]core.Accelerator, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	facts, err := r.cfg.Facts.ProbeFacts(ctx)
	if err != nil {
		return nil, err
	}
	offer, err := core.ParseAccelerator(core.VendorAWS, facts[factModel], facts[factDevices])
	if err != nil {
		return nil, err
	}
	return []core.Accelerator{offer}, nil
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: "neuron: " + fmt.Sprintf(format, args...)}
}
