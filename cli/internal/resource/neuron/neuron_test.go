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
}

// machine is}

// device is what the driver's sysfs says of one Neuron device: per physical core, the memory live
// runtimes hold on it (device_mem/total, the signal) and the counter beside it that is no signal
// (device_mem/present), which a real driver leaves at a page while a runtime holds the core, or at
// 64 MiB after one has exited.
type device struct{ total, present []int }

// machine is a driver's sysfs of the devices.
func machine(t *testing.T, devices []device) string {
	t.Helper()
	root := t.TempDir()
	for d, dev := range devices {
		dir := filepath.Join(root, "neuron"+strconv.Itoa(d))
		write(t, filepath.Join(dir, "core_count"), strconv.Itoa(len(dev.total)))
		for c := range dev.total {
			mem := filepath.Join(dir, "neuron_core"+strconv.Itoa(c), "stats", "memory_usage", "device_mem")
			write(t, filepath.Join(mem, "total"), strconv.Itoa(dev.total[c]))
			write(t, filepath.Join(mem, "present"), strconv.Itoa(dev.present[c]))
		}
	}
	return root
}

// heldBy is a device of logical cores of lnc physical ones each, on which runtimes hold the logical
// cores held says, as a real driver shows it: a runtime holds memory on every physical core of the
// logical cores it opened, and the logical cores lie on the physical ones in an order sysfs does not
// say (mapping, a permutation of the logical cores). The present counters are noise.
func heldBy(r *rand.Rand, held []bool, lnc int) device {
	mapping := r.Perm(len(held))
	dev := device{total: make([]int, len(held)*lnc), present: make([]int, len(held)*lnc)}
	for logical, on := range held {
		for p := range lnc {
			physical := mapping[logical]*lnc + p
			dev.present[physical] = []int{4096, 28672, 64 << 20}[r.IntN(3)]
			if on {
				dev.total[physical] = 1 + r.IntN(2<<30)
			}
		}
	}
	return dev
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
func runCheck(t *testing.T, d *neuron.NeuronDevices, g core.DeviceGroup, root string, env ...string) (string, bool) {
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
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Env = append(os.Environ(), env...)
	out, err := cmd.CombinedOutput()
	var exit *exec.ExitError
	if err != nil && !errors.As(err, &exit) {
		t.Fatal(err)
	}
	return strings.TrimSpace(string(out)), err == nil
}

// The check reads the memory live runtimes hold on each physical core, never who the scheduler thinks
// holds it, and never the present counters. A group is refused, naming its device, exactly when more
// logical cores of one of its devices are held than the cores of that device the group was not given,
// so that some held core is the group's; a group of a whole device is refused for any held core.
// Drawn over machines of 1 to 3 devices of 1 to 4 logical cores of 1 or 2 physical ones, laid on
// the physical cores in a random order, every contiguous group, and random held cores.
func TestTheCheckRefusesExactlyTheGroupsWhoseCoresMustBeHeld(t *testing.T) {
	r := rand.New(rand.NewPCG(13, 17))
	for range 80 {
		devices, perDevice, lnc := 1+r.IntN(3), 1+r.IntN(4), 1+r.IntN(2)
		size := count(t, devices*perDevice)
		d, err := neuron.NewDevices(neuron.DevicesConfig{Machine: "local", Size: size, CoresPerDevice: perDevice})
		if err != nil {
			t.Fatal(err)
		}
		sysfs := make([]device, devices)
		heldOn := make([]int, devices)
		for dev := range sysfs {
			held := make([]bool, perDevice)
			for c := range held {
				if held[c] = r.IntN(3) == 0; held[c] {
					heldOn[dev]++
				}
			}
			sysfs[dev] = heldBy(r, held, lnc)
		}
		root := machine(t, sysfs)
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
			if heldOn[dev] > perDevice-given {
				refused, named = true, dev
				break
			}
		}
		out, ok := runCheck(t, d, g, root, "PATH=/usr/bin:/bin")
		switch {
		case ok == refused:
			t.Fatalf("%d devices of %d cores of %d, held %v, group %v: passed %v (%q), want refused %v", devices, perDevice, lnc, sysfs, cores, ok, out, refused)
		case refused && !strings.Contains(out, "/dev/neuron"+strconv.Itoa(named)+" "):
			t.Fatalf("group %v on %v: %q, want the refusal to name /dev/neuron%d", cores, sysfs, out, named)
		}
	}
}

// A refusal names the processes neuron-ls lists on the refused device, and only those (from inside a
// container neuron-ls sees a holder's pid and not its command), and says when neuron-ls shows none.
func TestARefusalNamesTheHoldersNeuronLsSees(t *testing.T) {
	size := count(t, 8)
	d, err := neuron.NewDevices(neuron.DevicesConfig{Machine: "local", Size: size, CoresPerDevice: 4})
	if err != nil {
		t.Fatal(err)
	}
	r := rand.New(rand.NewPCG(3, 4))
	root := machine(t, []device{heldBy(r, []bool{false, false, false, false}, 2), heldBy(r, []bool{true, false, false, false}, 2)})
	bin := t.TempDir()
	write(t, filepath.Join(bin, "neuron-ls"), `#!/bin/sh
cat <<'EOF'
[
    {
        "neuron_device": 0,
        "nc_count": 4,
        "neuron_processes": [
            {
                "pid": 111,
                "command": "python other.py",
                "neuron_runtime_version": "2.35.30"
            }
        ]
    },
    {
        "neuron_device": 1,
        "nc_count": 4,
        "neuron_processes": [
            {
                "pid": 4242,
                "command": "/opt/venv/bin/python holder.py",
                "neuron_runtime_version": "2.35.30"
            },
            {
                "pid": 77,
                "command": "",
                "neuron_runtime_version": "2.35.30"
            }
        ]
    }
]
EOF`)
	if err := os.Chmod(filepath.Join(bin, "neuron-ls"), 0o755); err != nil {
		t.Fatal(err)
	}
	path := "PATH=" + bin + ":/usr/bin:/bin"
	out, ok := runCheck(t, d, group(t, size, 4, 5, 6, 7), root, path)
	if ok || !strings.Contains(out, "/dev/neuron1 is in use") || !strings.Contains(out, "held by pid 4242 (/opt/venv/bin/python holder.py), pid 77") || strings.Contains(out, "111") || strings.Contains(out, "()") {
		t.Errorf("passed %v, %q; want /dev/neuron1 refused, naming pids 4242 and 77 (whose command neuron-ls could not read) alone", ok, out)
	}
	if out, ok := runCheck(t, d, group(t, size, 0, 1, 2, 3), root, path); !ok {
		t.Errorf("a free device was refused: %q", out)
	}
	out, ok = runCheck(t, d, group(t, size, 4, 5, 6, 7), root, "PATH=/usr/bin:/bin")
	if ok || !strings.Contains(out, "no process neuron-ls can see") {
		t.Errorf("without neuron-ls: passed %v, %q", ok, out)
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
		"no core":   machine(t, []device{{}}),
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
