// Package rocm is the resource of AMD GPUs passed into the container through the ROCm kernel
// driver: under docker the driver's devices (/dev/kfd, /dev/dri) and the video group, under
// Apptainer --rocm. It is what [placements.<name>.resources.rocm] configures, and what a task of the
// rocm backend needs.
//
// A container whose image's ROCm runtime cannot use the node's driver fails in ways that say little
// (a server that never answers, a "no GPU" deep in a framework's log), so the container first runs
// Check: it exits 0 when the runtime sees a GPU, and otherwise says so, naming the image's ROCm
// version and the node's driver version, which is what the run fails with.
package rocm

import (
	"context"
	"fmt"
	"maps"
	"regexp"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// CheckScript is what Check runs in the container, as the container's user, with /bin/sh: the
// device must be there and open to the user, and the image's rocminfo, when it has one, must list a
// GPU agent. Its output when it fails is one line that says what did not work.
const CheckScript = `driver=$(cat /sys/module/amdgpu/version 2>/dev/null)
[ -n "$driver" ] || driver=unknown
rocm=$(cat /opt/rocm/.info/version 2>/dev/null)
[ -n "$rocm" ] || rocm=$(ls -d /opt/rocm-[0-9]* 2>/dev/null | sed -n '1s|.*/rocm-||p')
[ -n "$rocm" ] || rocm="unknown (no /opt/rocm/.info/version)"
fail() {
  echo "the container's ROCm runtime does not see the GPU: $1 (the image's ROCm $rocm, the node's amdgpu driver $driver)"
  exit 1
}
[ -e /dev/kfd ] || fail "/dev/kfd is not in the container"
{ [ -r /dev/kfd ] && [ -w /dev/kfd ]; } || fail "/dev/kfd cannot be opened by the container's user"
info=$(command -v rocminfo 2>/dev/null || ls /opt/rocm/bin/rocminfo 2>/dev/null)
if [ -n "$info" ]; then
  out=$("$info" 2>&1)
  code=$?
  if [ "$code" -ne 0 ] || ! printf '%s\n' "$out" | grep -q 'Device Type:[[:space:]]*GPU'; then
    fail "rocminfo exited with $code and lists no GPU agent: $(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' | tail -n 3 | tr '\n' ' ')"
  fi
fi
exit 0`

// Check is the command a container that gets the GPUs runs first.
var Check = []string{"/bin/sh", "-c", CheckScript, "vibesys-rocm-check"}

// Facts reads what identifies the AMD GPUs a container gets: their model, count and the driver's
// version, as fingerprint facts. It fails with core.FailureUnavailable when it cannot read them.
type Facts interface {
	GPUFacts(ctx context.Context) (map[string]string, error)
	ProbeFacts(ctx context.Context) (map[string]string, error)
}

// Config configures New.
type Config struct {
	// Machine names whose GPUs these are, so that the GPUs of two are two claims: the engine's
	// machine, or, for a placement whose jobs each get the GPUs of an allocation of their own, the
	// placement on its cluster.
	Machine string
	// Facts reads the GPUs for the fingerprint.
	Facts Facts
}

var machine = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.@-]*$`)

// ROCm is the resource. Create it with New.
type ROCm struct{ cfg Config }

var _ resource.Resource = (*ROCm)(nil)

// New returns the resource, or a core.FailureInvalidInput naming what is wrong.
func New(cfg Config) (*ROCm, error) {
	switch {
	case !machine.MatchString(cfg.Machine):
		return nil, invalid("the machine %q is not a machine name", cfg.Machine)
	case cfg.Facts == nil:
		return nil, invalid("no reader of the GPUs' facts")
	}
	return &ROCm{cfg: cfg}, nil
}

// Kind implements resource.Resource.
func (*ROCm) Kind() core.ResourceKind { return core.ResourceROCm }

// Claims implements resource.Resource: the GPUs of the machine.
func (r *ROCm) Claims() []core.Claim {
	return []core.Claim{{Resource: string(core.ResourceROCm) + ":" + r.cfg.Machine, Amount: 1}}
}

// Fingerprint implements resource.Resource: the facts the reader gives, as they are.
func (r *ROCm) Fingerprint(ctx context.Context) (map[string]string, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	facts, err := r.cfg.Facts.GPUFacts(ctx)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		f := core.AsFailure(err)
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: "rocm: " + f.Message}
	}
	for name, value := range facts {
		if !resource.FactName(name) || value == "" {
			return nil, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("rocm: the reader named a fact %q=%q", name, value)}
		}
	}
	if len(facts) == 0 {
		return nil, &core.Failure{Kind: core.FailureInternal, Message: "rocm: the reader named no fact"}
	}
	return maps.Clone(facts), nil
}

// Attach implements resource.Resource: the AMD GPUs, and the check that the image's runtime sees
// them.
func (r *ROCm) Attach(ctx context.Context) (resource.Attachment, error) {
	if err := ctx.Err(); err != nil {
		return resource.Attachment{}, err
	}
	return resource.Attachment{GPUs: resource.GPUs{ROCm: true}, Check: append([]string(nil), Check...)}, nil
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: "rocm: " + fmt.Sprintf(format, args...)}
}

// Offers implements resource.Resource using the same facts as the fingerprint.
func (r *ROCm) Offers(ctx context.Context) ([]core.Accelerator, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	facts, err := r.cfg.Facts.ProbeFacts(ctx)
	if err != nil {
		return nil, err
	}
	offer, err := core.ParseAccelerator(core.VendorAMD, facts["amd_gpu_model"], facts["amd_gpu_count"])
	if err != nil {
		return nil, err
	}
	return []core.Accelerator{offer}, nil
}
