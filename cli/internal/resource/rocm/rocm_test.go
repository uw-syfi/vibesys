package rocm_test

import (
	"context"
	"errors"
	"maps"
	"os/exec"
	"slices"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/resourcetest"
	"github.com/uw-syfi/vibesys/cli/internal/resource/rocm"
)

// facts is a Fake rocm.Facts: the facts it holds, until it is broken.
type facts struct {
	mu     sync.Mutex
	of     map[string]string
	broken bool
}

func (f *facts) GPUFacts(context.Context) (map[string]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.broken {
		return nil, errors.New("rocminfo exited with 1")
	}
	return maps.Clone(f.of), nil
}

func (f *facts) brk() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.broken = true
}

func mi210() *facts {
	return &facts{of: map[string]string{"amd_gpu_model": "AMD Instinct MI210", "amd_gpu_count": "1", "amdgpu_driver": "6.16.13"}}
}

func TestTheAMDGPUsPassTheContract(t *testing.T) {
	resourcetest.Run(t, func(t *testing.T) resourcetest.Subject {
		f := mi210()
		r, err := rocm.New(rocm.Config{Machine: "host-a", Facts: f})
		if err != nil {
			t.Fatal(err)
		}
		return resourcetest.Subject{Resource: r, Kind: core.ResourceROCm, Break: f.brk}
	})
}

// The container gets the AMD GPUs, not a --gpus value, and runs the check first.
func TestTheContainerGetsTheAMDGPUsAndChecksThemFirst(t *testing.T) {
	r, err := rocm.New(rocm.Config{Machine: "host-a", Facts: mi210()})
	if err != nil {
		t.Fatal(err)
	}
	got, err := r.Attach(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if got.GPUs != (resource.GPUs{ROCm: true}) || !slices.Equal(got.Check, rocm.Check) || len(got.Mounts) != 0 || got.Network != "" {
		t.Errorf("attachment %+v, want the AMD GPUs and the check", got)
	}
}

func TestASettingThatCannotBeIsRefused(t *testing.T) {
	for name, cfg := range map[string]rocm.Config{
		"no facts":           {Machine: "m"},
		"no machine":         {Facts: mi210()},
		"a machine with a /": {Machine: "a/b", Facts: mi210()},
	} {
		_, err := rocm.New(cfg)
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
			t.Errorf("%s: %v, want invalid input", name, err)
		}
	}
}

// The check is a POSIX shell script: a shell reads it without a syntax error.
func TestTheCheckIsAShellScript(t *testing.T) {
	if out, err := exec.Command("sh", "-n", "-c", rocm.CheckScript).CombinedOutput(); err != nil {
		t.Fatalf("sh -n: %v: %s", err, out)
	}
}
