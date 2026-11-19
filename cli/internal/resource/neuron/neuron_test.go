package neuron_test

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"math/rand/v2"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/devicestest"
	"github.com/uw-syfi/vibesys/cli/internal/resource/neuron"
	"github.com/uw-syfi/vibesys/cli/internal/resource/resourcetest"
)

// facts is a Fake neuron.Facts: the facts it holds, until it is broken.
type facts struct {
	mu     sync.Mutex
	of     map[string]string
	broken bool
}

func (f *facts) GPUFacts(context.Context) (map[string]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.broken {
		return nil, errors.New("neuron-ls exited with 1")
	}
	return maps.Clone(f.of), nil
}

func (f *facts) ProbeFacts(ctx context.Context) (map[string]string, error) { return f.GPUFacts(ctx) }

func (f *facts) brk() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.broken = true
}

// trn2 is the facts of a trn2.3xlarge, as backend/neuron reads them.
func trn2() *facts {
	return &facts{of: map[string]string{
		"neuron_model": "Trainium2", "neuron_device_count": "1", "neuron_core_count": "4",
		"neuron_cores_per_device": "4", "neuron_logical_core_config": "2", "neuron_driver": "2.31.7.0",
	}}
}

func count(t *testing.T, n int) core.GPUCount {
	t.Helper()
	c, err := core.ParseGPUCount(uint32(n), "test")
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func group(t *testing.T, size core.GPUCount, devices ...uint32) core.DeviceGroup {
	t.Helper()
	g, err := core.ParseDeviceGroup(devices, size, "test")
	if err != nil {
		t.Fatal(err)
	}
	return g
}

func TestTheNeuronDevicesPassTheContract(t *testing.T) {
	resourcetest.Run(t, func(t *testing.T) resourcetest.Subject {
		f := trn2()
		r, err := neuron.New(neuron.Config{Machine: "local", Facts: f})
		if err != nil {
			t.Fatal(err)
		}
		return resourcetest.Subject{Resource: r, Kind: core.ResourceNeuron, Break: f.brk}
	})
}

// The resource claims nothing and attaches nothing of its own: a job's devices are its range's.
func TestTheResourceClaimsAndAttachesNothingItself(t *testing.T) {
	r, err := neuron.New(neuron.Config{Machine: "local", Facts: trn2()})
	if err != nil {
		t.Fatal(err)
	}
	a, err := r.Attach(t.Context())
	if len(r.Claims()) != 0 || err != nil || !reflect.DeepEqual(a, resource.Attachment{}) {
		t.Errorf("Claims = %v, Attach = %+v, %v", r.Claims(), a, err)
	}
}

// A machine offers its Neuron devices, by chip, of the model the driver names.
func TestOffersAreTheMachinesNeuronDevices(t *testing.T) {
	r, err := neuron.New(neuron.Config{Machine: "local", Facts: trn2()})
	if err != nil {
		t.Fatal(err)
	}
	offers, err := r.Offers(t.Context())
	if err != nil || len(offers) != 1 || offers[0].Vendor != core.VendorAWS || offers[0].Model != core.ModelTrainium2 || offers[0].Count != 1 {
		t.Fatalf("offers %+v, %v", offers, err)
	}
}

func TestASettingThatCannotBeIsRefused(t *testing.T) {
	for name, cfg := range map[string]neuron.Config{
		"no facts":           {Machine: "m"},
		"no machine":         {Facts: trn2()},
		"a machine with a /": {Machine: "a/b", Facts: trn2()},
	} {
		if _, err := neuron.New(cfg); !isInvalid(err) {
			t.Errorf("%s: %v, want invalid input", name, err)
		}
	}
	for name, cfg := range map[string]neuron.DevicesConfig{
		"no size":                      {Machine: "local", CoresPerDevice: 4},
		"no cores per device":          {Machine: "local", Size: count(t, 4)},
		"not a whole number of chips":  {Machine: "local", Size: count(t, 6), CoresPerDevice: 4},
		"a machine that names no pool": {Machine: "a b", Size: count(t, 4), CoresPerDevice: 4},
	} {
		if _, err := neuron.NewDevices(cfg); !isInvalid(err) {
			t.Errorf("%s: %v, want invalid input", name, err)
		}
	}
}

func isInvalid(err error) bool {
	var f *core.Failure
	return errors.As(err, &f) && f.Kind == core.FailureInvalidInput
}

func TestNeuronDevicesPassTheContract(t *testing.T) {
	for _, c := range []struct{ size, perDevice int }{{1, 1}, {4, 4}, {8, 4}, {8, 2}, {16, 8}, {core.MaxPoolSize, 4}, {core.MaxPoolSize, 64}} {
		t.Run(fmt.Sprintf("%d cores of %d per device", c.size, c.perDevice), func(t *testing.T) {
			devicestest.Run(t, func(t *testing.T) devicestest.Subject {
				d, err := neuron.NewDevices(neuron.DevicesConfig{Machine: "local", Size: count(t, c.size), CoresPerDevice: c.perDevice})
				if err != nil {
					t.Fatal(err)
				}
				return devicestest.Subject{Devices: d, Kind: core.ResourceNeuron}
			})
		})
	}
}

// A container is given the devices that hold its range, and only those, with the range in
// NEURON_RT_VISIBLE_CORES and the check of each device it touches; a host process the variable.
func TestARangeIsGivenItsDevicesAndItsCores(t *testing.T) {
	size := count(t, 16)
	d, err := neuron.NewDevices(neuron.DevicesConfig{Machine: "local", Size: size, CoresPerDevice: 4})
	if err != nil {
		t.Fatal(err)
	}
	if id, n := d.Pool(); id != "neuron:local" || n != size {
		t.Errorf("Pool = %q, %d", id, n.Int())
	}
	for _, c := range []struct {
		cores   []uint32
		devices string
		specs   []string
	}{
		{[]uint32{0}, "0", []string{"0:3"}},
		{[]uint32{4, 5, 6, 7}, "1", []string{"1:0"}},
		{[]uint32{2, 3, 4, 5}, "0,1", []string{"0:2", "1:2"}},
		{[]uint32{3, 4, 5, 6, 7, 8}, "0,1,2", []string{"0:3", "1:0", "2:3"}},
	} {
		g := group(t, size, c.cores...)
		list := resource.List(g)
		check := append([]string{"/bin/sh", "-c", neuron.BusyScript, "vibesys-neuron-check", neuron.Sysfs, "4"}, c.specs...)
		want := resource.Attachment{GPUs: resource.GPUs{Neuron: c.devices}, GPUEnv: map[string]string{neuron.EnvVisibleCores: list}, Check: check}
		got, err := d.Container(g)
		if err != nil || !reflect.DeepEqual(got, want) {
			t.Errorf("Container(%v) = %+v, %v\nwant %+v", c.cores, got, err, want)
		}
		vars, err := d.Process(g)
		if err != nil || !maps.Equal(vars, map[string]string{neuron.EnvVisibleCores: list}) {
			t.Errorf("Process(%v) = %v, %v", c.cores, vars, err)
		}
	}
}

// The check is a POSIX shell script: a shell reads it without a syntax error.
func TestTheCheckIsAShellScript(t *testing.T) {
	if out, err := exec.Command("sh", "-n", "-c", neuron.BusyScript).CombinedOutput(); err != nil {
		t.Fatalf("sh -n: %v: %s", err, out)
	}
	if !strings.Contains(neuron.BusyScript, "limit="+strconv.Itoa(neuron.HeldBytes)+"\n") {
		t.Errorf("the script's limit is not HeldBytes (%d)", neuron.HeldBytes)
	}
}

// machine is a driver's sysfs of devices of physical cores, each counting the bytes given.
func machine(t *testing.T, held [][]int) string {
	t.Helper()
	root := t.TempDir()
	for dev, cores := range held {
		dir := filepath.Join(root, "neuron"+strconv.Itoa(dev))
		write(t, filepath.Join(dir, "core_count"), strconv.Itoa(len(cores)))
		for c, bytes := range cores {
			write(t, filepath.Join(dir, "neuron_core"+strconv.Itoa(c), "stats", "memory_usage", "device_mem", "present"), strconv.Itoa(bytes))
		}
	}
	return root
}

func write(t *testing.T, path, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

// runCheck runs the role's check of the group against the sysfs at root, as an environment does.
func runCheck(t *testing.T, d *neuron.NeuronDevices, g core.DeviceGroup, root string) (string, bool) {
	t.Helper()
	argv, err := d.Check(g)
	if err != nil {
		t.Fatal(err)
	}
	i := slices.Index(argv, neuron.Sysfs)
	if i < 0 {
		t.Fatalf("the check %q does not name the driver's sysfs", argv)
	}
	argv[i] = root
	out, err := exec.Command(argv[0], argv[1:]...).CombinedOutput()
	var exit *exec.ExitError
	if err != nil && !errors.As(err, &exit) {
		t.Fatal(err)
	}
	return strings.TrimSpace(string(out)), err == nil
}

// The check reads the memory each physical core holds, never who the scheduler thinks holds it. A
// group is refused, naming its device, exactly when more physical cores of one of its devices hold
// memory than the cores of that device the group was not given are made of, so some of that
// memory must be on the group's own cores; a group of a whole device is refused for any memory on
// it. Drawn over machines of 1 to 3 devices of 1 to 4 logical cores of 1 or 2 physical ones, every
// contiguous group, and memory held on random physical cores, idle ones counting a page.
func TestTheCheckRefusesExactlyTheGroupsWhoseCoresMustHoldMemory(t *testing.T) {
	r := rand.New(rand.NewPCG(13, 17))
	for range 60 {
		devices, perDevice, lnc := 1+r.IntN(3), 1+r.IntN(4), 1+r.IntN(2)
		size := count(t, devices*perDevice)
		d, err := neuron.NewDevices(neuron.DevicesConfig{Machine: "local", Size: size, CoresPerDevice: perDevice})
		if err != nil {
			t.Fatal(err)
		}
		held := make([][]int, devices)
		heldOn := make([]int, devices)
		for dev := range held {
			held[dev] = make([]int, perDevice*lnc)
			for p := range held[dev] {
				held[dev][p] = 4096
				if r.IntN(3) == 0 {
					held[dev][p] = 64 << 20
					heldOn[dev]++
				}
			}
		}
		root := machine(t, held)
		lo := r.IntN(size.Int())
		hi := lo + r.IntN(size.Int()-lo)
		var cores []uint32
		for c := lo; c <= hi; c++ {
			cores = append(cores, uint32(c))
		}
		g := group(t, size, cores...)
		refused, named := false, -1
		for dev := lo / perDevice; dev <= hi/perDevice; dev++ {
			given := 0
			for _, c := range cores {
				if int(c)/perDevice == dev {
					given++
				}
			}
			if heldOn[dev] > (perDevice-given)*lnc {
				refused, named = true, dev
				break
			}
		}
		out, ok := runCheck(t, d, g, root)
		switch {
		case ok == refused:
			t.Fatalf("%d devices of %d cores of %d, memory on %v, group %v: passed %v (%q), want refused %v", devices, perDevice, lnc, held, cores, ok, out, refused)
		case refused && !strings.Contains(out, "/dev/neuron"+strconv.Itoa(named)+" "):
			t.Fatalf("group %v on %v: %q, want the refusal to name /dev/neuron%d", cores, held, out, named)
		}
	}
}

// A machine whose sysfs cannot be read is not taken to be free: the check fails and says so.
func TestACheckThatCannotReadTheDevicesFails(t *testing.T) {
	size := count(t, 4)
	d, err := neuron.NewDevices(neuron.DevicesConfig{Machine: "local", Size: size, CoresPerDevice: 4})
	if err != nil {
		t.Fatal(err)
	}
	for name, root := range map[string]string{
		"no device": t.TempDir(),
		"no core":   machine(t, [][]int{{}}),
	} {
		if name == "no core" {
			write(t, filepath.Join(root, "neuron0", "core_count"), "8")
		}
		out, ok := runCheck(t, d, group(t, size, 0, 1, 2, 3), root)
		if ok || !strings.Contains(out, "cannot read") {
			t.Errorf("%s: passed %v, %q; want a failure saying what it cannot read", name, ok, out)
		}
	}
}
