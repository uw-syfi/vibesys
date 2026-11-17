package core

import (
	"fmt"
	"math/bits"
	"slices"
)

// MaxPoolSize is the most GPUs one pool has, and so the most one job asks for.
const MaxPoolSize = 64

// GPUCount is a number of GPUs: how many a job asks for, or how many a pool has. It is made only
// by ParseGPUCount, so a count is always 1 to MaxPoolSize. Its zero value is no count at all: a job
// that asked for none, or a placement without a pool.
type GPUCount struct{ n uint8 }

// ParseGPUCount returns the count n, 1 to MaxPoolSize. It fails with a *Failure of kind
// FailureInvalidInput that names key, the flag or the key of the file the count was written in.
func ParseGPUCount(n uint32, key string) (GPUCount, error) {
	if n < 1 || n > MaxPoolSize {
		return GPUCount{}, failf(FailureInvalidInput, "%s = %d: a number of GPUs is 1 to %d", key, n, MaxPoolSize)
	}
	return GPUCount{n: uint8(n)}, nil
}

// IsSet says whether the count is one, as opposed to the zero value, which is no count.
func (c GPUCount) IsSet() bool { return c.n != 0 }

// Int is the count as a number, 0 for no count.
func (c GPUCount) Int() int { return int(c.n) }

// AllowedGPUCounts is how a refusal says the counts a pool of the size lets a job ask for, such as
// "1 to 8", or "1" for a pool of one GPU. A job may ask for any count up to the pool's size.
func AllowedGPUCounts(pool GPUCount) string {
	if pool.n <= 1 {
		return "1"
	}
	return fmt.Sprintf("1 to %d", pool.n)
}

// CheckPoolCount refuses a pool whose declared size is not the number of GPUs the machine has, as
// the machine's facts read it: a pool larger than the machine would hand out GPUs that do not
// exist, and a smaller one would leave GPUs no job is told about while a measure believes it holds
// the machine whole. key names the declaration (placements.<name>.resources.gpu.count) and machine
// the machine whose GPUs were read. It fails with a *Failure of kind FailureInvalidInput.
func CheckPoolCount(key string, declared GPUCount, machine string, probed int) error {
	if declared.Int() == probed {
		return nil
	}
	return failf(FailureInvalidInput, "%s = %d, and %s has %d GPUs", key, declared.Int(), machine, probed)
}

// DeviceIndex is a device number of a pool, 0 to its size less one.
type DeviceIndex uint8

// DeviceGroup is a set of distinct device numbers of one pool, in ascending order. It is made only
// by TakeDevices, which picks a fresh one, and by ParseDeviceGroup, which checks a stored one
// against its pool's size, so no code that gives GPUs to a job sees a list that is unsorted, holds
// a device twice or names one outside its pool. Its zero value is the empty group.
type DeviceGroup struct{ idx []DeviceIndex }

// ParseDeviceGroup returns the group of the devices, which must be in ascending order, each once,
// and each below size. It fails with a *Failure of kind FailureInvalidInput that names key.
func ParseDeviceGroup(devices []uint32, size GPUCount, key string) (DeviceGroup, error) {
	var out []DeviceIndex
	for i, d := range devices {
		switch {
		case d >= uint32(size.n):
			return DeviceGroup{}, failf(FailureInvalidInput, "%s: device %d is not one of the pool's %d (0 to %d)", key, d, size.n, int(size.n)-1)
		case i > 0 && d <= devices[i-1]:
			return DeviceGroup{}, failf(FailureInvalidInput, "%s: device %d after %d: the devices are in ascending order, each once", key, d, devices[i-1])
		}
		out = append(out, DeviceIndex(d))
	}
	return DeviceGroup{idx: out}, nil
}

// Devices returns the group's device numbers in ascending order, as a copy.
func (g DeviceGroup) Devices() []DeviceIndex { return slices.Clone(g.idx) }

// Len is how many devices the group has.
func (g DeviceGroup) Len() int { return len(g.idx) }

// Has says whether the group holds the device.
func (g DeviceGroup) Has(d DeviceIndex) bool {
	_, ok := slices.BinarySearch(g.idx, d)
	return ok
}

// Overlaps says whether the two groups share a device.
func (g DeviceGroup) Overlaps(other DeviceGroup) bool {
	return slices.ContainsFunc(g.idx, other.Has)
}

// Equal says whether the two groups hold the same devices.
func (g DeviceGroup) Equal(other DeviceGroup) bool { return slices.Equal(g.idx, other.idx) }

// TakeDevices picks n devices of a pool of the size that no group of held has. It takes the lowest
// block of the smallest power of two at least n that is wholly free and starts at a multiple of its
// own size, and of it the n lowest devices, so that a job of 2, 4 or 8 GPUs gets neighbouring
// devices where the machine's topology most often joins them; when no such block is free it takes
// the n lowest free devices, so that how jobs came and went never keeps a job from devices that are
// free. A held device outside the pool is no device of it. It reports false, and the empty group,
// only when fewer than n devices are free, or when the size or n is no count. It is pure and
// deterministic: the same arguments give the same group.
func TakeDevices(size GPUCount, held []DeviceGroup, n GPUCount) (DeviceGroup, bool) {
	if !size.IsSet() || !n.IsSet() || n.n > size.n {
		return DeviceGroup{}, false
	}
	var busy uint64
	for _, g := range held {
		for _, d := range g.idx {
			if d < DeviceIndex(size.n) {
				busy |= 1 << d
			}
		}
	}
	block := uint8(1) << bits.Len8(n.n-1)
	for start := uint8(0); block <= size.n && start <= size.n-block; start += block {
		mask := (uint64(1)<<block - 1) << start
		if block == 64 {
			mask = ^uint64(0)
		}
		if busy&mask == 0 {
			return contiguous(start, n.n), true
		}
	}
	out := make([]DeviceIndex, 0, n.n)
	for d := range DeviceIndex(size.n) {
		if busy&(1<<d) == 0 {
			out = append(out, d)
			if len(out) == int(n.n) {
				return DeviceGroup{idx: out}, true
			}
		}
	}
	return DeviceGroup{}, false
}

// contiguous is the group of the n devices from start.
func contiguous(start, n uint8) DeviceGroup {
	out := make([]DeviceIndex, n)
	for i := range out {
		out[i] = DeviceIndex(start) + DeviceIndex(i)
	}
	return DeviceGroup{idx: out}
}

// PoolID names a pool by the machine whose GPUs it is, with the vendor's resource kind first, such
// as gpu:local or rocm:<placement>@<login host>, as the scheduler names the resource it counts.
type PoolID string

// GrantSource is who chose the devices of a grant.
type GrantSource string

const (
	// GrantByCLI is a grant whose devices TakeDevices chose from the pool's free ones.
	GrantByCLI GrantSource = "cli"
	// GrantByCluster is a grant whose devices the cluster's scheduler chose for the job's
	// allocation, read back after the job started; its devices are empty until then.
	GrantByCluster GrantSource = "cluster"
)

// Grant is the GPUs of one pool a job was given. It is recorded with the job's start, to say where
// the job ran; it never takes part in a cache key.
type Grant struct {
	Pool PoolID
	// Size is the pool's size when the job started.
	Size GPUCount
	// Devices are the devices given, within the pool's size.
	Devices DeviceGroup
	// Whole is set when the job took the whole pool (a measure) rather than a count it asked for.
	Whole  bool
	Source GrantSource
}

// CheckFor refuses a grant that cannot be the grant of a job with this spec: one that names a
// device twice or none, a counted grant of a job that asked for another number of GPUs or for none,
// and a grant of the whole pool to a job that asked for a count or does not hold every device of
// it. It fails with a *Failure of kind FailureInvalidInput naming the disagreement.
func (g Grant) CheckFor(spec JobSpec) error {
	switch {
	case g.Source == GrantByCLI && g.Devices.Len() == 0:
		return failf(FailureInvalidInput, "gpus: a grant the CLI chose names its devices, and this names none")
	case g.Whole && spec.GPUs.IsSet():
		return failf(FailureInvalidInput, "gpus: the job asked for %d GPUs, and was given the whole pool", spec.GPUs.Int())
	case g.Whole && g.Source == GrantByCLI && g.Devices.Len() != g.Size.Int():
		return failf(FailureInvalidInput, "gpus: the whole pool of %d has %d devices in this grant", g.Size.Int(), g.Devices.Len())
	case !g.Whole && !spec.GPUs.IsSet():
		return failf(FailureInvalidInput, "gpus: the job asked for no count of GPUs, and was given %d of a pool", g.Devices.Len())
	case !g.Whole && g.Source == GrantByCLI && g.Devices.Len() != spec.GPUs.Int():
		return failf(FailureInvalidInput, "gpus: the job asked for %d GPUs, and was given %d", spec.GPUs.Int(), g.Devices.Len())
	}
	return nil
}
