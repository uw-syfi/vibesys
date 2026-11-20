package neuron

import (
	"strconv"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// EnvVisibleCores is the variable the Neuron runtime reads the logical NeuronCores a process may use
// from, as a list ("0,1") or a range ("0-1") that must be contiguous: the runtime refuses "0,2"
// with "Logical Neuron cores are not contiguous". A pool's numbers are the runtime's: core c of the
// machine is core c mod cores_per_device of /dev/neuron<c / cores_per_device>.
const EnvVisibleCores = "NEURON_RT_VISIBLE_CORES"

// Sysfs is where the neuron driver lists its devices, each as neuron<d>, with its physical core count
// (core_count) and, per physical core p, the device memory live runtimes hold on it
// (neuron_core<p>/stats/memory_usage/device_mem/total).
const Sysfs = "/sys/class/neuron_device"

// BusyScript is the check a job's environment runs before its commands (NeuronDevices.Check), with
// /bin/sh: its first argument is the driver's sysfs directory (Sysfs), its second the logical
// NeuronCores of a device, and each one after them is "<device>:<cores of that device the job was
// not given>", one per device that holds the job's cores.
//
// The signal is the device memory the driver counts as held on each physical core,
// device_mem/total. A Neuron runtime that opens a logical core holds memory on each of its physical
// cores (a runtime that only initialised holds 2.5 MB on each of the two), and the driver frees it
// when the process exits, cleanly or killed. On a trn2.3xlarge with driver 2.31.7, every core read
// 0 when no runtime was open. The neighbouring files are no signal: device_mem/present stayed at
// 4096 bytes while another process held 1.2 GB of tensors on the core, and the per-category present
// counters kept 64 MiB after their process was killed.
//
// sysfs does not say which physical cores make up which logical NeuronCore: on that host logical
// core 0 is physical cores 4 and 5, and 3 is 0 and 1. So the check counts. A held logical core holds
// memory on at most core_count / cores_per_device physical cores, so when more physical cores of a
// device are held than the cores the job was not given are made of, some are the job's. The check
// then fails, naming the device, the held cores and the processes neuron-ls lists on the device when
// it can see them. The count is exact for a job given a whole device, as a measure is, which any
// held core refuses; for a job given part of one it refuses only what the count proves, and a
// process that holds exactly the job's own cores is found by the job's runtime instead ("Logical
// Neuron Core(s) not available"). A check that cannot read sysfs fails, since then nothing is known.
const BusyScript = `root=$1
per=$2
shift 2
for spec in "$@"; do
  dev=${spec%%:*}
  others=${spec#*:}
  dir=$root/neuron$dev
  physical=$(cat "$dir/core_count" 2>/dev/null)
  if [ -z "$physical" ]; then
    echo "neuron: cannot read $dir/core_count, so whether another process holds cores of /dev/neuron$dev is not known"
    exit 1
  fi
  lnc=$((physical / per))
  [ "$lnc" -ge 1 ] || lnc=1
  held=""
  n=0
  c=0
  while [ "$c" -lt "$physical" ]; do
    f=$dir/neuron_core$c/stats/memory_usage/device_mem/total
    v=$(cat "$f" 2>/dev/null)
    case "$v" in
      '' | *[!0-9]*)
        echo "neuron: cannot read $f, so whether another process holds cores of /dev/neuron$dev is not known"
        exit 1
        ;;
    esac
    if [ "$v" -gt 0 ]; then
      held="$held neuron_core$c=$v"
      n=$((n + 1))
    fi
    c=$((c + 1))
  done
  if [ "$n" -gt $((others * lnc)) ]; then
    who=""
    ls=$(command -v neuron-ls 2>/dev/null || ls /opt/aws/neuron/bin/neuron-ls 2>/dev/null)
    if [ -n "$ls" ] && command -v awk >/dev/null 2>&1; then
      who=$("$ls" --json-output 2>/dev/null | awk -v d="$dev" '
        /"neuron_device":/ { v = $2; gsub(/[^0-9]/, "", v); cur = v }
        /"pid":/ { v = $2; gsub(/[^0-9]/, "", v); pid = v }
        /"command":/ && cur == d { c = $0; sub(/^[^:]*: *"/, "", c); sub(/",? *$/, "", c); out = out (out == "" ? "" : ", ") "pid " pid (c == "" ? "" : " (" c ")") }
        END { print out }')
    fi
    [ -n "$who" ] || who="no process neuron-ls can see from here (it may run in another container or as another user)"
    echo "neuron: /dev/neuron$dev is in use: $n of its $physical physical cores hold device memory (bytes:$held), more than its $others NeuronCores this job was not given account for, so another process holds some of this job's NeuronCores; held by $who"
    exit 1
  fi
done
exit 0`

// DevicesConfig configures NewDevices.
type DevicesConfig struct {
	// Machine names whose devices these are, as Config.Machine does.
	Machine string
	// Size is how many logical NeuronCores the pool has: every core of the machine, 0 to Size-1.
	Size core.GPUCount
	// CoresPerDevice is how many logical NeuronCores each device has, which says the device of each
	// core. Size is a whole number of devices of it.
	CoresPerDevice int
}

// NeuronDevices gives a job a contiguous range of the machine's logical NeuronCores. Create it with
// NewDevices.
//
// A container is given the /dev/neuron* devices that hold the range, and so cannot open another
// device; the cores of its devices that are not its own are kept from it only by
// NEURON_RT_VISIBLE_CORES, which the runtime honours. A host process is given the variable alone.
type NeuronDevices struct {
	id      core.PoolID
	size    core.GPUCount
	perChip int
}

var _ resource.Devices = (*NeuronDevices)(nil)

// NewDevices returns the role, or a core.FailureInvalidInput naming what is wrong.
func NewDevices(cfg DevicesConfig) (*NeuronDevices, error) {
	id, err := resource.PoolIDOf(core.ResourceNeuron, cfg.Machine)
	switch {
	case err != nil:
		return nil, invalid("%v", err)
	case !cfg.Size.IsSet():
		return nil, invalid("the pool of %s has no size", cfg.Machine)
	case cfg.CoresPerDevice < 1 || cfg.CoresPerDevice > core.MaxPoolSize:
		return nil, invalid("cores_per_device = %d: a device has 1 to %d NeuronCores", cfg.CoresPerDevice, core.MaxPoolSize)
	case cfg.Size.Int()%cfg.CoresPerDevice != 0:
		return nil, invalid("count = %d is not a whole number of devices of %d NeuronCores (cores_per_device)", cfg.Size.Int(), cfg.CoresPerDevice)
	}
	return &NeuronDevices{id: id, size: cfg.Size, perChip: cfg.CoresPerDevice}, nil
}

// Kind implements resource.Devices.
func (*NeuronDevices) Kind() core.ResourceKind { return core.ResourceNeuron }

// Pool implements resource.Devices.
func (d *NeuronDevices) Pool() (core.PoolID, core.GPUCount) { return d.id, d.size }

// Container implements resource.Devices: the devices that hold the range, the variable that names
// it, and the check that its cores hold no one else's memory.
func (d *NeuronDevices) Container(g core.DeviceGroup) (resource.Attachment, error) {
	check, err := d.Check(g)
	if err != nil {
		return resource.Attachment{}, err
	}
	var devices []uint32
	for _, dev := range d.devicesOf(g) {
		devices = append(devices, uint32(dev))
	}
	list, err := core.ParseDeviceGroup(devices, d.size, "neuron devices")
	if err != nil {
		return resource.Attachment{}, invalid("%v", err)
	}
	return resource.Attachment{
		GPUs:   resource.GPUs{Neuron: resource.List(list)},
		GPUEnv: map[string]string{EnvVisibleCores: resource.List(g)},
		Check:  check,
	}, nil
}

// Process implements resource.Devices: NEURON_RT_VISIBLE_CORES.
func (d *NeuronDevices) Process(g core.DeviceGroup) (map[string]string, error) {
	if err := resource.CheckGroup(core.ResourceNeuron, g, d.size); err != nil {
		return nil, invalid("%v", err)
	}
	return map[string]string{EnvVisibleCores: resource.List(g)}, nil
}

// Check implements resource.Devices: BusyScript, for each device that holds a core of the range,
// with the number of that device's cores the job was not given.
func (d *NeuronDevices) Check(g core.DeviceGroup) ([]string, error) {
	if err := resource.CheckGroup(core.ResourceNeuron, g, d.size); err != nil {
		return nil, invalid("%v", err)
	}
	argv := []string{"/bin/sh", "-c", BusyScript, "vibesys-neuron-check", Sysfs, strconv.Itoa(d.perChip)}
	for _, dev := range d.devicesOf(g) {
		given := 0
		for _, c := range g.Devices() {
			if int(c)/d.perChip == dev {
				given++
			}
		}
		argv = append(argv, strconv.Itoa(dev)+":"+strconv.Itoa(d.perChip-given))
	}
	return argv, nil
}

// devicesOf is the devices that hold the group's cores, in ascending order, each once.
func (d *NeuronDevices) devicesOf(g core.DeviceGroup) []int {
	var out []int
	for _, c := range g.Devices() {
		dev := int(c) / d.perChip
		if len(out) == 0 || out[len(out)-1] != dev {
			out = append(out, dev)
		}
	}
	return out
}
