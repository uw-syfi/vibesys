package gpu_test

import (
	"context"
	"errors"
	"maps"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
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
