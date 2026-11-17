package rocm_test

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"os/exec"
	"reflect"
	"slices"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/devicestest"
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

func (f *facts) ProbeFacts(ctx context.Context) (map[string]string, error) { return f.GPUFacts(ctx) }

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

func TestOffersReadTheFingerprintFacts(t *testing.T) {
	r, err := rocm.New(rocm.Config{Machine: "host-a", Facts: mi210()})
	if err != nil {
		t.Fatal(err)
	}
	offers, err := r.Offers(t.Context())
	if err != nil || len(offers) != 1 || offers[0].Count != 1 || offers[0].Model != core.ModelMI210 {
		t.Fatalf("offers %v, %v", offers, err)
	}
}

func TestAMDDevicesPassTheContract(t *testing.T) {
	for _, size := range []int{1, 2, 4, core.MaxPoolSize} {
		t.Run(fmt.Sprintf("size %d", size), func(t *testing.T) {
			devicestest.Run(t, func(t *testing.T) devicestest.Subject {
				n, err := core.ParseGPUCount(uint32(size), "test")
				if err != nil {
					t.Fatal(err)
				}
				d, err := rocm.NewDevices(rocm.DevicesConfig{Machine: "login-a", Size: n})
				if err != nil {
					t.Fatal(err)
				}
				return devicestest.Subject{Devices: d, Kind: core.ResourceROCm}
			})
		})
	}
}

// The group goes in ROCR_VISIBLE_DEVICES alone: HIP_VISIBLE_DEVICES counts what remains after it,
// so a second variable with the same numbers would filter twice. The driver's devices are passed
// whole, and so nothing confines the job: the attachment says it by giving no per-device flag.
func TestAMDDevicesNameTheGroupInOneVariable(t *testing.T) {
	size, _ := core.ParseGPUCount(4, "test")
	group, err := core.ParseDeviceGroup([]uint32{1, 3}, size, "test")
	if err != nil {
		t.Fatal(err)
	}
	d, err := rocm.NewDevices(rocm.DevicesConfig{Machine: "login-a", Size: size})
	if err != nil {
		t.Fatal(err)
	}
	if id, n := d.Pool(); id != "rocm:login-a" || n != size {
		t.Errorf("Pool = %q, %d", id, n.Int())
	}
	got, err := d.Container(group)
	want := resource.Attachment{GPUs: resource.GPUs{ROCm: true}, GPUEnv: map[string]string{"ROCR_VISIBLE_DEVICES": "1,3"}}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Errorf("Container = %+v, %v, want %+v", got, err, want)
	}
	vars, err := d.Process(group)
	if err != nil || !maps.Equal(vars, map[string]string{"ROCR_VISIBLE_DEVICES": "1,3"}) {
		t.Errorf("Process = %v, %v", vars, err)
	}
}

// A pool's resource claims nothing, and attaches the check only: the GPUs a job gets are its group's.
func TestAPoolOfAMDGPUsClaimsNothingItself(t *testing.T) {
	size, _ := core.ParseGPUCount(4, "test")
	r, err := rocm.New(rocm.Config{Pool: size, Machine: "login-a", Facts: &facts{}})
	if err != nil {
		t.Fatal(err)
	}
	a, err := r.Attach(t.Context())
	if len(r.Claims()) != 0 || err != nil || a.GPUs.Given() || len(a.Check) == 0 {
		t.Errorf("Claims = %v, Attach = %+v, %v", r.Claims(), a, err)
	}
}
