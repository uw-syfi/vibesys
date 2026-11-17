package docker_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/fakeresource"
)

const poolOfEight core.PoolID = "gpu:local"

// withPool makes the placement's GPUs a pool of eight devices that jobs are given by number, as the
// wiring does for a placement with a count: a resource that claims and attaches nothing of its own,
// and the role that turns a group into what a container gets.
func withPool(devices *fakeresource.Devices) func(*docker.Config) {
	return func(c *docker.Config) {
		c.Resources = append(c.Resources, &fakeresource.Fake{Of: core.ResourceGPU, Facts: someGPU})
		c.Devices = devices
	}
}

func newDevices(t *testing.T) *fakeresource.Devices {
	t.Helper()
	size, err := core.ParseGPUCount(8, "test")
	if err != nil {
		t.Fatal(err)
	}
	return &fakeresource.Devices{Of: core.ResourceGPU, ID: poolOfEight, Size: size}
}

func grantOf(t *testing.T, pool core.PoolID, devices ...uint32) *core.Grant {
	t.Helper()
	size, _ := core.ParseGPUCount(8, "test")
	g, err := core.ParseDeviceGroup(devices, size, "test")
	if err != nil {
		t.Fatal(err)
	}
	return &core.Grant{Pool: pool, Size: size, Devices: g, Source: core.GrantByCLI}
}

// runAs runs a command of a job given the grant, and returns the container the engine was asked for.
func runAs(t *testing.T, s *scene, grant *core.Grant) (core.Attempt, docker.ContainerSpec) {
	t.Helper()
	var err error
	s.env, err = s.r.provider.Environment(environment.Target{Repository: "/repo", Commit: "0123456789abcdef0123456789abcdef01234567", Task: "task", GPUs: grant})
	if err != nil {
		t.Fatal(err)
	}
	keptContainer(s)
	a, _ := s.run("print", "x")
	return a, onlyContainer(t, s.daemon)
}

// A job given a group of the pool gets exactly those devices by number, the vendor's variables for
// them, and is told which they are in the words every vendor shares; the same group gives the same
// container, and two jobs of disjoint groups get containers that share no device.
func TestAContainerGetsTheDevicesOfTheGroupItsJobWasGiven(t *testing.T) {
	devices := newDevices(t)
	groups := [][]uint32{{2, 3}, {0}, {4, 5, 6, 7}, {1}}
	var seen []string
	for _, g := range groups {
		s := newScene(t, options{configure: withPool(devices)})
		a, spec := runAs(t, s, grantOf(t, poolOfEight, g...))
		if _, ok := a.(core.Unobserved); !ok {
			t.Fatalf("attempt %+v", a)
		}
		list := resource.List(grantOf(t, poolOfEight, g...).Devices)
		if spec.GPUs.NVIDIA != list || spec.GPUs.Devices != "" {
			t.Errorf("group %v: the container is given %+v, want the devices %s by number", g, spec.GPUs, list)
		}
		if spec.Env[fakeresource.FakeVisibleDevices] != list || spec.Env["VIBESYS_GPU_DEVICES"] != list || spec.Env["VIBESYS_GPUS"] != string(rune('0'+len(g))) {
			t.Errorf("group %v: the container's variables are %v", g, spec.Env)
		}
		seen = append(seen, list)
	}
	if got := devices.Asked(); len(got) != len(groups) {
		t.Errorf("the role was asked for %d groups, want %d", len(got), len(groups))
	}
	if seen[0] == seen[1] || seen[1] == seen[2] {
		t.Errorf("groups %v gave one container's devices", seen)
	}
}

// A job that was given no GPUs of a pool gets none of the pool's resource and is told nothing of
// them, and a grant of another pool than the placement's is refused before a container starts.
func TestAJobWithNoGrantGetsNoGPUsAndAGrantOfAnotherPoolIsRefused(t *testing.T) {
	devices := newDevices(t)
	s := newScene(t, options{configure: withPool(devices)})
	a, spec := runAs(t, s, nil)
	if _, ok := a.(core.Unobserved); !ok {
		t.Fatalf("attempt %+v", a)
	}
	if spec.GPUs != (docker.GPUs{}) || spec.Env["VIBESYS_GPUS"] != "" || spec.Env[fakeresource.FakeVisibleDevices] != "" {
		t.Errorf("a job with no grant: GPUs %+v, variables %v", spec.GPUs, spec.Env)
	}

	s = newScene(t, options{configure: withPool(devices)})
	var err error
	s.env, err = s.r.provider.Environment(environment.Target{Repository: "/repo", Commit: "0123456789abcdef0123456789abcdef01234567", Task: "task", GPUs: grantOf(t, "gpu:host-b", 0)})
	if err != nil {
		t.Fatal(err)
	}
	a, _ = s.run("print", "x")
	failure := envtest.NotStarted(t, a)
	if failure.Kind != core.FailureInvalidInput {
		t.Errorf("failure %+v, want invalid input naming both pools", failure)
	}
	if left := s.daemon.Containers(); len(left) != 0 {
		t.Errorf("containers left: %+v", left)
	}
}
