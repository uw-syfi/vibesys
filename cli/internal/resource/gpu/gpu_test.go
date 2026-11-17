package gpu_test

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"reflect"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/devicestest"
	"github.com/uw-syfi/vibesys/cli/internal/resource/gpu"
	"github.com/uw-syfi/vibesys/cli/internal/resource/resourcetest"
)

// facts is a Fake gpu.Facts: the facts it holds, until it is broken.
type facts struct {
	mu     sync.Mutex
	of     map[string]string
	broken bool
}

func (f *facts) GPUFacts(context.Context) (map[string]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.broken {
		return nil, errors.New("nvidia-smi exited with 9")
	}
	return maps.Clone(f.of), nil
}

func (f *facts) ProbeFacts(ctx context.Context) (map[string]string, error) { return f.GPUFacts(ctx) }

func (f *facts) brk() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.broken = true
}

func someGPU() *facts {
	return &facts{of: map[string]string{"gpu_model": "A100", "gpu_count": "1", "gpu_driver": "550", "cuda_driver": "12.4"}}
}

func TestGPUsPassTheContract(t *testing.T) {
	for _, cfg := range []gpu.Config{{Devices: "all"}, {Devices: "device=0,1", Runtime: "nvidia"}} {
		t.Run(cfg.Devices, func(t *testing.T) {
			resourcetest.Run(t, func(t *testing.T) resourcetest.Subject {
				f := someGPU()
				cfg.Machine, cfg.Facts = "host-a", f
				g, err := gpu.New(cfg)
				if err != nil {
					t.Fatal(err)
				}
				return resourcetest.Subject{Resource: g, Kind: core.ResourceGPU, Break: f.brk}
			})
		})
	}
}

// With --gpus the devices go as they are; with a runtime they go in the runtime's variable, without
// the device= prefix, and --gpus is not used.
func TestTheDevicesGoByGPUsOrByTheRuntime(t *testing.T) {
	for _, c := range []struct {
		cfg  gpu.Config
		want resource.Attachment
	}{
		{gpu.Config{Devices: "all"}, resource.Attachment{GPUs: resource.GPUs{Devices: "all"}}},
		{gpu.Config{Devices: "device=0", Runtime: "nvidia"}, resource.Attachment{GPUs: resource.GPUs{Devices: "device=0", Runtime: "nvidia"}, GPUEnv: map[string]string{gpu.EnvVisibleDevices: "0"}}},
		{gpu.Config{Devices: "all", Runtime: "nvidia"}, resource.Attachment{GPUs: resource.GPUs{Devices: "all", Runtime: "nvidia"}, GPUEnv: map[string]string{gpu.EnvVisibleDevices: "all"}}},
	} {
		c.cfg.Machine, c.cfg.Facts = "local", someGPU()
		g, err := gpu.New(c.cfg)
		if err != nil {
			t.Fatal(err)
		}
		got, err := g.Attach(t.Context())
		if err != nil || got.GPUs != c.want.GPUs || !maps.Equal(got.Env, c.want.Env) {
			t.Errorf("%+v: %+v, %v; want %+v", c.cfg, got, err, c.want)
		}
	}
}

func TestASettingThatCannotBeIsRefused(t *testing.T) {
	for name, cfg := range map[string]gpu.Config{
		"no devices":         {Machine: "m", Facts: someGPU()},
		"devices with space": {Devices: "all --privileged", Machine: "m", Facts: someGPU()},
		"a runtime with /":   {Devices: "all", Runtime: "a/b", Machine: "m", Facts: someGPU()},
		"no facts":           {Devices: "all", Machine: "m"},
		"no machine":         {Devices: "all", Facts: someGPU()},
	} {
		_, err := gpu.New(cfg)
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
			t.Errorf("%s: %v, want invalid input", name, err)
		}
	}
}

func TestOffersCountTheDevicesASelectionNames(t *testing.T) {
	for _, tc := range []struct {
		devices, runtime string
		want             uint32 // zero: unreadable
	}{
		{"all", "", 1}, {"device=0", "", 1}, {"device=0,0", "", 1}, {"0", "nvidia", 1}, {"GPU-1234", "nvidia", 1},
		{"device=0,1", "", 0}, {"0", "", 0}, {"2", "", 0}, {"count=1", "", 0}, {"device=", "", 0},
		{"device=0,", "", 0}, {"none", "nvidia", 0}, {"void", "nvidia", 0}, {"device=none", "", 0},
	} {
		g, err := gpu.New(gpu.Config{Devices: tc.devices, Runtime: tc.runtime, Machine: "host-a", Facts: someGPU()})
		if err != nil {
			t.Fatal(err)
		}
		offers, err := g.Offers(t.Context())
		if tc.want == 0 {
			if err == nil {
				t.Errorf("%s: got %v, want unreadable", tc.devices, offers)
			}
			continue
		}
		if err != nil || len(offers) != 1 || offers[0].Count != tc.want || offers[0].Model != core.ModelA100 {
			t.Errorf("%s: %v, %v", tc.devices, offers, err)
		}
	}
}

func TestNVIDIADevicesPassTheContract(t *testing.T) {
	for _, runtime := range []string{"", "nvidia"} {
		for _, size := range []int{1, 2, 8, core.MaxPoolSize} {
			t.Run(fmt.Sprintf("size %d, runtime %q", size, runtime), func(t *testing.T) {
				devicestest.Run(t, func(t *testing.T) devicestest.Subject {
					n, err := core.ParseGPUCount(uint32(size), "test")
					if err != nil {
						t.Fatal(err)
					}
					d, err := gpu.NewDevices(gpu.DevicesConfig{Machine: "host-a", Size: n, Runtime: runtime})
					if err != nil {
						t.Fatal(err)
					}
					return devicestest.Subject{Devices: d, Kind: core.ResourceGPU}
				})
			})
		}
	}
}

// What docker is given is pinned exactly: the numbers, for the engine to write as the --gpus value
// "device=2,3" with its quotes, and for a runtime the variable; and what a host process is told.
func TestNVIDIADevicesGiveTheGroupByNumber(t *testing.T) {
	size, _ := core.ParseGPUCount(8, "test")
	group, err := core.ParseDeviceGroup([]uint32{2, 3}, size, "test")
	if err != nil {
		t.Fatal(err)
	}
	d, err := gpu.NewDevices(gpu.DevicesConfig{Machine: "host-a", Size: size})
	if err != nil {
		t.Fatal(err)
	}
	if id, n := d.Pool(); id != "gpu:host-a" || n != size {
		t.Errorf("Pool = %q, %d", id, n.Int())
	}
	got, err := d.Container(group)
	if err != nil || !reflect.DeepEqual(got, resource.Attachment{GPUs: resource.GPUs{NVIDIA: "2,3"}}) {
		t.Errorf("Container = %+v, %v: want the numbers 2 and 3 and no variable (the container numbers its devices from 0)", got, err)
	}
	vars, err := d.Process(group)
	if want := map[string]string{"CUDA_VISIBLE_DEVICES": "2,3", "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}; err != nil || !maps.Equal(vars, want) {
		t.Errorf("Process = %v, %v, want %v", vars, err, want)
	}
	viaRuntime, _ := gpu.NewDevices(gpu.DevicesConfig{Machine: "host-a", Size: size, Runtime: "nvidia"})
	got, err = viaRuntime.Container(group)
	want := resource.Attachment{GPUs: resource.GPUs{NVIDIA: "2,3", Runtime: "nvidia"}, GPUEnv: map[string]string{"NVIDIA_VISIBLE_DEVICES": "2,3"}}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Errorf("Container with a runtime = %+v, %v, want %+v", got, err, want)
	}
}

// A pool's resource claims nothing and attaches nothing of its own: what a job gets depends on its group.
func TestAPoolOfGPUsClaimsAndAttachesNothingItself(t *testing.T) {
	size, _ := core.ParseGPUCount(4, "test")
	g, err := gpu.New(gpu.Config{Pool: size, Machine: "host-a", Facts: someGPU()})
	if err != nil {
		t.Fatal(err)
	}
	a, err := g.Attach(t.Context())
	if len(g.Claims()) != 0 || err != nil || !reflect.DeepEqual(a, resource.Attachment{}) {
		t.Errorf("Claims = %v, Attach = %+v, %v", g.Claims(), a, err)
	}
	if _, err := gpu.New(gpu.Config{Pool: size, Devices: "all", Machine: "host-a", Facts: someGPU()}); err == nil {
		t.Error("a pool with a --gpus value was accepted")
	}
}
