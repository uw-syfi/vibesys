// Package gpu is the resource of GPUs passed into the container: with docker run --gpus, or with
// a container runtime of the NVIDIA kind that reads the devices from NVIDIA_VISIBLE_DEVICES. It is
// what [placements.<name>.resources.gpu] configures, and what gpus and runtime in the [docker]
// section of the earlier operator config shape did. A task of the cuda backend needs it.
package gpu

import (
	"context"
	"fmt"
	"maps"
	"regexp"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// EnvVisibleDevices is the variable a GPU runtime reads the devices from.
const EnvVisibleDevices = "NVIDIA_VISIBLE_DEVICES"

// Facts reads what identifies the GPUs of the engine's machine: their model, count, driver and the
// driver's CUDA version, as fingerprint facts. It fails with core.FailureUnavailable when it
// cannot read them.
type Facts interface {
	GPUFacts(ctx context.Context) (map[string]string, error)
	ProbeFacts(ctx context.Context) (map[string]string, error)
}

// Config configures New.
type Config struct {
	// Devices is a docker --gpus value, such as "all" or "device=0,1". It is required.
	Devices string
	// Runtime, when set, is the container runtime that passes the devices, with the devices in
	// EnvVisibleDevices, instead of --gpus.
	Runtime string
	// Machine names the engine's machine, so that the GPUs of two machines are two claims.
	Machine string
	// Facts reads the GPUs for the fingerprint.
	Facts Facts
}

var (
	devices = regexp.MustCompile(`^[A-Za-z0-9=,:._/+-]+$`)
	runtime = regexp.MustCompile(`^[a-z0-9._-]*$`)
	machine = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.@-]*$`)
)

// GPU is the resource. Create it with New.
type GPU struct{ cfg Config }

var _ resource.Resource = (*GPU)(nil)

// New returns the resource, or a core.FailureInvalidInput naming the setting that is wrong.
func New(cfg Config) (*GPU, error) {
	switch {
	case !devices.MatchString(cfg.Devices):
		return nil, invalid("devices: %q is not a --gpus value", cfg.Devices)
	case !runtime.MatchString(cfg.Runtime):
		return nil, invalid("runtime: %q is not a runtime name", cfg.Runtime)
	case !machine.MatchString(cfg.Machine):
		return nil, invalid("the machine %q is not a machine name", cfg.Machine)
	case cfg.Facts == nil:
		return nil, invalid("no reader of the GPUs' facts")
	}
	return &GPU{cfg: cfg}, nil
}

// Kind implements resource.Resource.
func (*GPU) Kind() core.ResourceKind { return core.ResourceGPU }

// Claims implements resource.Resource: the devices of the machine.
func (g *GPU) Claims() []core.Claim {
	return []core.Claim{{Resource: string(core.ResourceGPU) + ":" + g.cfg.Machine + "/" + g.cfg.Devices, Amount: 1}}
}

// Fingerprint implements resource.Resource: the facts the reader gives, as they are.
func (g *GPU) Fingerprint(ctx context.Context) (map[string]string, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	facts, err := g.cfg.Facts.GPUFacts(ctx)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		f := core.AsFailure(err)
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: "gpu: " + f.Message}
	}
	for name := range facts {
		if !resource.FactName(name) {
			return nil, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("gpu: the reader named a fact %q", name)}
		}
	}
	return maps.Clone(facts), nil
}

// Attach implements resource.Resource.
func (g *GPU) Attach(ctx context.Context) (resource.Attachment, error) {
	if err := ctx.Err(); err != nil {
		return resource.Attachment{}, err
	}
	a := resource.Attachment{GPUs: resource.GPUs{Devices: g.cfg.Devices, Runtime: g.cfg.Runtime}}
	if g.cfg.Runtime != "" {
		// The runtime's own hook passes the devices the variable names; CDI is not used.
		a.GPUEnv = map[string]string{EnvVisibleDevices: strings.TrimPrefix(g.cfg.Devices, "device=")}
	}
	return a, nil
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: "gpu: " + fmt.Sprintf(format, args...)}
}

// Offers implements resource.Resource: the machine's GPU model, counted by the selection. A
// selection that does not list its devices, such as a count, is unreadable.
func (r *GPU) Offers(ctx context.Context) ([]*cliv1.AcceleratorOffer, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	facts, err := r.cfg.Facts.ProbeFacts(ctx)
	if err != nil {
		return nil, err
	}
	offer, err := resource.AcceleratorOffer(cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, facts["gpu_model"], facts["gpu_count"])
	if err != nil {
		return nil, err
	}
	selected, err := selectedDevices(r.cfg.Devices, r.cfg.Runtime != "", offer.GetCount())
	if err != nil {
		return nil, err
	}
	offer.Count = selected
	return []*cliv1.AcceleratorOffer{offer}, nil
}

// selectedDevices counts the devices of a selection out of the machine's total: all of them for
// "all", and the listed ones for "device=0,1" (for a runtime, the variable's list "0,1"). Any other
// value, such as a count, does not say which devices.
func selectedDevices(selection string, viaRuntime bool, total uint32) (uint32, error) {
	if selection == "all" {
		return total, nil
	}
	list, listed := strings.CutPrefix(selection, "device=")
	if !listed && !viaRuntime {
		return 0, fmt.Errorf("GPU selection %q does not list its devices", selection)
	}
	seen := map[string]bool{}
	for id := range strings.SplitSeq(list, ",") {
		if id == "" || id == "none" || id == "void" || strings.Contains(id, "=") {
			return 0, fmt.Errorf("GPU selection %q does not list its devices", selection)
		}
		seen[id] = true
	}
	if uint32(len(seen)) > total {
		return 0, fmt.Errorf("GPU selection %q lists more devices than the machine's %d", selection, total)
	}
	return uint32(len(seen)), nil
}
