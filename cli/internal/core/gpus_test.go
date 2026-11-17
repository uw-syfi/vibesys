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

// A grant is the grant of a job only when it is what the job asked for: a counted job gets as many
// devices as its count, a job that took the pool gets every device of it, and a job that asked for
// no count is given the whole pool or nothing. The move to running records it, for no other move,
// and the move back to the queue forgets it.
func TestAGrantIsHeldToTheJobsSpecAndRecordedWithItsStart(t *testing.T) {
	size := gpuCount(t, 8)
	group := func(devices ...uint32) core.DeviceGroup {
		g, err := core.ParseDeviceGroup(devices, size, "test")
		if err != nil {
			t.Fatal(err)
		}
		return g
	}
	grant := func(whole bool, devices ...uint32) *core.Grant {
		return &core.Grant{Pool: "gpu:local", Size: size, Devices: group(devices...), Whole: whole, Source: core.GrantByCLI}
	}
	whole := grant(true, 0, 1, 2, 3, 4, 5, 6, 7)
	cases := []struct {
		name  string
		asked core.GPUCount
		grant *core.Grant
		ok    bool
	}{
		{"as many as asked", gpuCount(t, 2), grant(false, 3, 4), true},
		{"fewer than asked", gpuCount(t, 3), grant(false, 3, 4), false},
		{"more than asked", gpuCount(t, 1), grant(false, 3, 4), false},
		{"a count of a job that asked none", core.GPUCount{}, grant(false, 3, 4), false},
		{"the whole pool of a job that asked none", core.GPUCount{}, whole, true},
		{"the whole pool of a job that asked a count", gpuCount(t, 8), whole, false},
		{"a whole grant short of devices", core.GPUCount{}, grant(true, 0, 1), false},
		{"no devices", gpuCount(t, 1), grant(false), false},
	}
	for _, c := range cases {
		for _, kind := range jobKinds {
			job := newJob(kind, core.JobQueued)
			job.Spec.GPUs = c.asked
			fp := hostEnv
			moved, err := job.Apply(core.Transition{Job: job.ID, From: core.JobQueued, To: core.JobRunning, At: at(1), Environment: &fp, GPUs: c.grant})
			if (err == nil) != c.ok {
				t.Fatalf("%s (%s job): %v, want it accepted = %v", c.name, kind, err, c.ok)
			}
			if err != nil {
				continue
			}
			if moved.GPUs == nil || !moved.GPUs.Devices.Equal(c.grant.Devices) || moved.GPUs.Whole != c.grant.Whole {
				t.Fatalf("%s (%s job): the job holds %+v, want the grant recorded", c.name, kind, moved.GPUs)
			}
			// The grant is the running job's alone: nothing else moves it, and a cut attempt forgets it.
			if _, err := moved.Apply(core.Transition{Job: job.ID, From: core.JobRunning, To: core.JobCancelled, At: at(2), GPUs: c.grant}); err == nil {
				t.Errorf("%s: a grant was accepted with a move to cancelled", c.name)
			}
			back, err := moved.Apply(core.Transition{Job: job.ID, From: core.JobRunning, To: core.JobQueued, At: at(2), Cut: core.CutBeforeStart{By: core.CutRebooted}})
			if err != nil || back.GPUs != nil {
				t.Errorf("%s: after the attempt was cut off the job holds %+v, %v", c.name, back.GPUs, err)
			}
		}
	}
}

// A count is checked against the pools of the places the job may run on, for every kind of job, in
// every combination of pools: a refusal always names --gpus, and the verdict is the one the rules
// say. The rules are restated here from the outside, not copied from the code: a measure takes no
// count; a count fits when some place has a pool of at least that size; a task that needs GPUs
// must say how many when some place has a pool.
func TestAGPUCountIsCheckedAgainstThePoolsOfThePlacesTheJobMayRunOn(t *testing.T) {
	r := rand.New(rand.NewPCG(7, 11))
	kinds := []core.JobKind{core.JobCheck, core.JobCommand, core.JobMeasure}
	for range 20000 {
		in := core.GPUAsk{Kind: kinds[r.IntN(len(kinds))], Task: "serve"}
		maxPool := 0
		for i := range 1 + r.IntN(3) {
			place := core.GPUPlace{Name: string(rune('a' + i))}
			if r.IntN(3) > 0 {
				size := 1 + r.IntN(16)
				place.Pool = gpuCount(t, size)
				maxPool = max(maxPool, size)
			}
			in.Where = append(in.Where, place)
		}
		asked := 0
		if r.IntN(3) > 0 {
			asked = 1 + r.IntN(20)
			in.Asked = gpuCount(t, asked)
		}
		in.NeedsKnown = r.IntN(4) > 0
		if r.IntN(2) == 0 {
			in.Needs = []core.ResourceKind{core.ResourceGPU}
		}
		needsGPU := len(in.Needs) > 0
		var want bool // refused
		switch {
		case in.Kind == core.JobMeasure:
			want = asked > 0
		case asked == 0:
			want = in.NeedsKnown && needsGPU && maxPool > 0
		default:
			want = (in.NeedsKnown && !needsGPU) || asked > maxPool
		}
		err := core.CheckGPUAsk(in)
		if (err != nil) != want {
			t.Fatalf("%+v: %v, refused = %v", in, err, want)
		}
		var f *core.Failure
		if err != nil && (!errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "--gpus")) {
			t.Fatalf("%+v: %v, want an invalid-input refusal that names --gpus", in, err)
		}
	}
}

// The refusals say the sizes the pools allow, so that a caller picks one without guessing.
func TestARefusedGPUCountNamesThePlacementsAndTheirSizes(t *testing.T) {
	in := core.GPUAsk{
		Kind: core.JobCommand, Task: "serve", NeedsKnown: true, Needs: []core.ResourceKind{core.ResourceGPU},
		Where: []core.GPUPlace{{Name: "local", Pool: gpuCount(t, 8)}, {Name: "box", Pool: gpuCount(t, 4)}},
	}
	err := core.CheckGPUAsk(in)
	if err == nil || !strings.Contains(err.Error(), "--gpus N") || !strings.Contains(err.Error(), "local has 8: 1 to 8") || !strings.Contains(err.Error(), "box has 4: 1 to 4") {
		t.Errorf("no count: %v", err)
	}
	in.Asked = gpuCount(t, 16)
	if err := core.CheckGPUAsk(in); err == nil || !strings.Contains(err.Error(), "--gpus 16") || !strings.Contains(err.Error(), "box has 4: 1 to 4") {
		t.Errorf("16 GPUs: %v", err)
	}
}
