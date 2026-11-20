// Package neuron reads what identifies a machine's AWS Neuron devices (Trainium), for an
// environment's fingerprint and for the check of a pool against the machine: their model, from the
// driver's sysfs (/sys/class/neuron_device/neuron0/info/architecture/device_name), how many devices
// and logical NeuronCores there are, from neuron-ls --json-output, and the version of the node's
// neuron kernel driver, from /sys/module/neuron/version. Script prints what Parse reads, so the
// facts can be read through a shell on this machine or on an ssh host (Devices).
package neuron

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// The fingerprint facts this package reports. They are not the names another vendor's facts have.
const (
	// FactModel is the devices' model as the driver names it, such as Trainium2.
	FactModel = "neuron_model"
	// FactDevices is how many Neuron devices (chips) the machine has.
	FactDevices = "neuron_device_count"
	// FactCores is how many logical NeuronCores the machine has, every device's: the unit of a pool.
	FactCores = "neuron_core_count"
	// FactCoresPerDevice is how many logical NeuronCores each device has.
	FactCoresPerDevice = "neuron_cores_per_device"
	// FactLogicalCoreConfig is how many physical cores make one logical NeuronCore (LNC).
	FactLogicalCoreConfig = "neuron_logical_core_config"
	// FactDriver is the version of the neuron kernel driver.
	FactDriver = "neuron_driver"
)

// Timeout bounds the reading of the facts.
const Timeout = 2 * time.Minute

// Script is the POSIX shell script whose output Parse reads: the driver's version, the model the
// first device names, then neuron-ls's listing as JSON. A machine with no driver or no neuron-ls
// prints empty sections, which Parse refuses.
const Script = `echo '==driver=='
cat /sys/module/neuron/version 2>/dev/null
echo '==model=='
cat /sys/class/neuron_device/neuron0/info/architecture/device_name 2>/dev/null
echo '==neuron-ls=='
n=$(command -v neuron-ls 2>/dev/null || ls /opt/aws/neuron/bin/neuron-ls 2>/dev/null)
[ -n "$n" ] && "$n" --json-output 2>/dev/null
exit 0`

// device is the part of one entry of neuron-ls --json-output that Parse reads.
type device struct {
	Index *int `json:"neuron_device"`
	Cores *int `json:"nc_count"`
	LNC   *int `json:"logical_neuroncore_config"`
}

// Parse reads the facts from Script's output. Devices of unlike core counts are refused, since a
// pool numbers the cores of each device from cores_per_device times the device's number, and so is a
// machine whose output names no device or no driver, or devices that are not numbered 0 to n-1.
func Parse(out string) (map[string]string, error) {
	unavailable := func(format string, args ...any) error {
		return &core.Failure{Kind: core.FailureUnavailable, Message: "neuron: " + fmt.Sprintf(format, args...)}
	}
	s := sections(out)
	driver, model := first(s["driver"]), first(s["model"])
	if driver == "" {
		return nil, unavailable("the neuron driver's version is not in /sys/module/neuron/version")
	}
	listing := strings.TrimSpace(strings.Join(s["neuron-ls"], "\n"))
	if listing == "" {
		return nil, unavailable("neuron-ls --json-output printed nothing (is neuron-ls on PATH or in /opt/aws/neuron/bin?)")
	}
	var devices []device
	if err := json.Unmarshal([]byte(listing), &devices); err != nil {
		return nil, unavailable("neuron-ls --json-output is not a list of devices: %v", err)
	}
	if len(devices) == 0 {
		return nil, unavailable("neuron-ls lists no Neuron device")
	}
	seen := make([]bool, len(devices))
	perDevice, lnc := 0, 0
	for _, d := range devices {
		switch {
		case d.Index == nil || d.Cores == nil || d.LNC == nil:
			return nil, unavailable("neuron-ls lists a device without neuron_device, nc_count or logical_neuroncore_config")
		case *d.Index < 0 || *d.Index >= len(devices) || seen[*d.Index]:
			return nil, unavailable("neuron-ls lists device %d of %d: the devices are not numbered 0 to %d", *d.Index, len(devices), len(devices)-1)
		case *d.Cores < 1 || *d.LNC < 1:
			return nil, unavailable("neuron-ls says device %d has %d NeuronCores of %d physical cores each", *d.Index, *d.Cores, *d.LNC)
		case perDevice != 0 && (*d.Cores != perDevice || *d.LNC != lnc):
			return nil, unavailable("neuron-ls lists devices of %d and of %d NeuronCores (logical core configs %d and %d); a pool needs every device alike", perDevice, *d.Cores, lnc, *d.LNC)
		}
		seen[*d.Index] = true
		perDevice, lnc = *d.Cores, *d.LNC
	}
	if model == "" {
		model = "unknown"
	}
	return map[string]string{
		FactModel: model, FactDevices: strconv.Itoa(len(devices)), FactCores: strconv.Itoa(len(devices) * perDevice),
		FactCoresPerDevice: strconv.Itoa(perDevice), FactLogicalCoreConfig: strconv.Itoa(lnc), FactDriver: driver,
	}, nil
}

// sections splits Script's output by its ==name== lines.
func sections(out string) map[string][]string {
	got := map[string][]string{}
	name := ""
	for _, line := range strings.Split(out, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "==") && strings.HasSuffix(trimmed, "==") && len(trimmed) > 4 {
			name = strings.Trim(trimmed, "=")
			got[name] = got[name]
			continue
		}
		if name != "" && trimmed != "" {
			got[name] = append(got[name], line)
		}
	}
	return got
}

// first is the first line of a section, trimmed, or "".
func first(lines []string) string {
	if len(lines) == 0 {
		return ""
	}
	return strings.TrimSpace(lines[0])
}

// Devices reads the Neuron facts of the machine the shell reaches, once for the fingerprint: they
// do not change while the program runs.
type Devices struct {
	Shell  remote.Shell
	Timers clock.Timers

	mu    sync.Mutex
	facts map[string]string
}

// GPUFacts returns the facts, reading them the first time.
func (d *Devices) GPUFacts(ctx context.Context) (map[string]string, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.facts != nil {
		return maps.Clone(d.facts), nil
	}
	facts, err := d.ProbeFacts(ctx)
	if err != nil {
		return nil, err
	}
	d.facts = facts
	return maps.Clone(facts), nil
}

// ProbeFacts reads a fresh observation without changing the fingerprint cache.
func (d *Devices) ProbeFacts(ctx context.Context) (map[string]string, error) {
	out, err := remote.Within(ctx, d.Timers, d.Shell, Timeout, []string{"sh", "-c", Script}, "")
	if err != nil {
		return nil, fmt.Errorf("reading the Neuron devices: %w", err)
	}
	return Parse(out)
}
