package core_test

import (
	"errors"
	"math/bits"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func gpuCount(t *testing.T, n int) core.GPUCount {
	t.Helper()
	c, err := core.ParseGPUCount(uint32(n), "test")
	if err != nil {
		t.Fatal(err)
	}
	return c
}

// heldOf draws devices of a pool of the size that jobs hold, as groups, and returns them with the
// set of held devices. A few held devices lie beyond the pool, as after its size was lowered.
func heldOf(t *testing.T, r *rand.Rand, size int) ([]core.DeviceGroup, map[core.DeviceIndex]bool) {
	t.Helper()
	busy := map[core.DeviceIndex]bool{}
	var groups []core.DeviceGroup
	var current []uint32
	flush := func() {
		if len(current) > 0 {
			g, err := core.ParseDeviceGroup(current, gpuCount(t, core.MaxPoolSize), "held")
			if err != nil {
				t.Fatal(err)
			}
			groups = append(groups, g)
			current = nil
		}
	}
	density := r.Float64()
	for d := range core.MaxPoolSize {
		if d >= size && r.IntN(8) != 0 {
			continue
		}
		if r.Float64() < density {
			current = append(current, uint32(d))
			if d < size {
				busy[core.DeviceIndex(d)] = true
			}
		}
		if r.IntN(3) == 0 {
			flush()
		}
	}
	flush()
	return groups, busy
}

// alignedFree is the start of the lowest wholly free block of the smallest power of two at least
// n that starts at a multiple of its size, or -1.
func alignedFree(size, n int, busy map[core.DeviceIndex]bool) int {
	block := 1 << bits.Len(uint(n-1))
	for start := 0; start+block <= size; start += block {
		free := true
		for d := start; d < start+block; d++ {
			free = free && !busy[core.DeviceIndex(d)]
		}
		if free {
			return start
		}
	}
	return -1
}

// TakeDevices gives exactly the devices asked for, all of the pool and none held, whenever that
// many are free, and only then; it gives the same group for the same arguments; and it keeps a
// group on an aligned block whenever one is free.
func TestTakeDevicesGivesFreeDevicesOfThePoolAlignedWhenItCan(t *testing.T) {
	r := newRand(41)
	for range properties * 5 {
		size := 1 + r.IntN(core.MaxPoolSize)
		n := 1 + r.IntN(size)
		held, busy := heldOf(t, r, size)
		got, ok := core.TakeDevices(gpuCount(t, size), held, gpuCount(t, n))
		if again, ok2 := core.TakeDevices(gpuCount(t, size), held, gpuCount(t, n)); ok2 != ok || !again.Equal(got) {
			t.Fatalf("size %d, n %d: two calls gave %v and %v", size, n, got.Devices(), again.Devices())
		}
		if free := size - len(busy); ok != (free >= n) {
			t.Fatalf("size %d, %d free, n %d: ok = %v", size, free, n, ok)
		}
		if !ok {
			if got.Len() != 0 {
				t.Fatalf("a refusal gave %v", got.Devices())
			}
			continue
		}
		devices := got.Devices()
		if len(devices) != n || !slices.IsSorted(devices) || len(slices.Compact(slices.Clone(devices))) != n {
			t.Fatalf("size %d, n %d: gave %v", size, n, devices)
		}
		for _, d := range devices {
			if int(d) >= size || busy[d] {
				t.Fatalf("size %d, n %d: gave %v, and %d is held or outside the pool (held %v)", size, n, devices, d, busy)
			}
		}
		for _, h := range held {
			if h.Overlaps(got) {
				t.Fatalf("gave %v, which overlaps the held %v", devices, h.Devices())
			}
		}
		if start := alignedFree(size, n, busy); start >= 0 && int(devices[0]) != start || start >= 0 && int(devices[n-1]) != start+n-1 {
			t.Fatalf("size %d, n %d, held %v: gave %v, and the aligned block at %d is free", size, n, busy, devices, start)
		}
	}
}

// Jobs that take devices one after another, each holding what it took, never share a device and
// are refused only once the pool has fewer free devices than they ask for.
func TestJobsThatTakeDevicesInTurnNeverShareOne(t *testing.T) {
	r := newRand(42)
	for range properties {
		size := 1 + r.IntN(core.MaxPoolSize)
		var held []core.DeviceGroup
		used := 0
		for range 2 * size {
			n := 1 + r.IntN(size)
			g, ok := core.TakeDevices(gpuCount(t, size), held, gpuCount(t, n))
			if ok != (size-used >= n) {
				t.Fatalf("size %d, %d used, n %d: ok = %v", size, used, n, ok)
			}
			if ok {
				held = append(held, g)
				used += n
			}
			if len(held) > 0 && r.IntN(4) == 0 {
				// A job ends and gives its devices back.
				i := r.IntN(len(held))
				used -= held[i].Len()
				held = slices.Delete(held, i, i+1)
			}
		}
		for i := range held {
			for j := range i {
				if held[i].Overlaps(held[j]) {
					t.Fatalf("size %d: %v and %v share a device", size, held[i].Devices(), held[j].Devices())
				}
			}
		}
	}
}

// A group that TakeDevices gave reads back from its stored device numbers as the same group, and a
// stored list that is out of order, repeats a device or names one outside the pool is refused,
// naming the key.
func TestADeviceGroupReadsBackAndABadOneIsRefused(t *testing.T) {
	r := newRand(43)
	for range properties {
		size := 1 + r.IntN(core.MaxPoolSize)
		held, _ := heldOf(t, r, size)
		g, ok := core.TakeDevices(gpuCount(t, size), held, gpuCount(t, 1+r.IntN(size)))
		if !ok {
			continue
		}
		var stored []uint32
		for _, d := range g.Devices() {
			stored = append(stored, uint32(d))
		}
		back, err := core.ParseDeviceGroup(stored, gpuCount(t, size), "job.gpus.devices")
		if err != nil || !back.Equal(g) {
			t.Fatalf("%v read back as %v, %v", stored, back.Devices(), err)
		}
	}
	for name, devices := range map[string][]uint32{
		"out of order": {1, 0}, "a device twice": {2, 2}, "outside the pool": {0, 8},
	} {
		_, err := core.ParseDeviceGroup(devices, gpuCount(t, 8), "job.gpus.devices")
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "job.gpus.devices") {
			t.Errorf("%s %v: %v, want an invalid input naming the key", name, devices, err)
		}
	}
}

// A count is 1 to MaxPoolSize; any other number is refused, naming the key it was written in.
func TestAGPUCountIsOneToTheLargestPool(t *testing.T) {
	for n := range uint32(2 * core.MaxPoolSize) {
		c, err := core.ParseGPUCount(n, "placements.local.resources.gpu.count")
		if valid := n >= 1 && n <= core.MaxPoolSize; valid {
			if err != nil || !c.IsSet() || c.Int() != int(n) {
				t.Errorf("%d: %v, %v", n, c.Int(), err)
			}
			continue
		}
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "placements.local.resources.gpu.count") || c.IsSet() {
			t.Errorf("%d: %v, %v; want a refusal naming the key", n, c.Int(), err)
		}
	}
	if (core.GPUCount{}).IsSet() {
		t.Errorf("the zero count is a count")
	}
	if _, ok := core.TakeDevices(gpuCount(t, 8), nil, core.GPUCount{}); ok {
		t.Errorf("no count took devices")
	}
	if _, ok := core.TakeDevices(core.GPUCount{}, nil, gpuCount(t, 1)); ok {
		t.Errorf("no pool gave devices")
	}
}

func TestTheAllowedCountsAreOneToThePoolsSize(t *testing.T) {
	for size, want := range map[int]string{1: "1", 2: "1 to 2", 8: "1 to 8", core.MaxPoolSize: "1 to 64"} {
		if got := core.AllowedGPUCounts(gpuCount(t, size)); got != want {
			t.Errorf("a pool of %d: %q, want %q", size, got, want)
		}
	}
}

// A declared pool is accepted only at the number of GPUs the machine has, and a refusal names the
// key and the machine.
func TestAPoolMustBeTheMachinesGPUs(t *testing.T) {
	for declared := 1; declared <= 16; declared++ {
		for probed := range 17 {
			err := core.CheckPoolCount("placements.box.resources.gpu.count", gpuCount(t, declared), "host-a", probed)
			if (err == nil) != (declared == probed) {
				t.Fatalf("declared %d, probed %d: %v", declared, probed, err)
			}
			var f *core.Failure
			if err != nil && (!errors.As(err, &f) || f.Kind != core.FailureInvalidInput ||
				!strings.Contains(f.Message, "placements.box.resources.gpu.count") || !strings.Contains(f.Message, "host-a")) {
				t.Fatalf("declared %d, probed %d: %v, want a refusal naming the key and the machine", declared, probed, err)
			}
		}
	}
}
