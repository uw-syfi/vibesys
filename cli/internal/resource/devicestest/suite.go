// Package devicestest is the contract every resource.Devices passes: the pool it declares, and, for
// every group of devices of the pool, that what it gives a container and a host process names
// exactly the group, the same way on every call, and that a group outside the pool or an empty one
// is refused naming the device. Each implementation's tests run it, the Fake's included, so a test
// that uses the Fake in place of a vendor's role tests what the vendor's does.
//
// The suite does not know a vendor's variables or flags. It reads the device numbers an attachment
// or a set of variables names the one way every vendor writes them: a list "0,1,5" in a variable,
// or the numbers of the attachment's GPUs.
package devicestest

import (
	"errors"
	"math/rand/v2"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// Subject is one role under the contract, made fresh for each case.
type Subject struct {
	Devices resource.Devices
	// Kind is the kind it must report.
	Kind core.ResourceKind
}

// Groups is how many groups of a pool a case draws.
const Groups = 200

// Run runs the contract against the roles make makes.
func Run(t *testing.T, make func(t *testing.T) Subject) {
	t.Run("its pool is of its kind, has a size, and is the same on every call", func(t *testing.T) {
		s := make(t)
		if got := s.Devices.Kind(); got != s.Kind || !slices.Contains(core.ResourceKinds(), got) {
			t.Fatalf("Kind = %q, want %q", got, s.Kind)
		}
		id, size := s.Devices.Pool()
		if !strings.HasPrefix(string(id), string(s.Kind)+":") || len(id) == len(s.Kind)+1 {
			t.Errorf("pool id %q: want %q:<machine>", id, s.Kind)
		}
		if !size.IsSet() || size.Int() > core.MaxPoolSize {
			t.Errorf("pool size %d: want 1 to %d", size.Int(), core.MaxPoolSize)
		}
		if id2, size2 := s.Devices.Pool(); id2 != id || size2 != size {
			t.Errorf("Pool changed between calls: %s %d, then %s %d", id, size.Int(), id2, size2.Int())
		}
	})
	t.Run("a container is given exactly the devices of the group, the same way on every call", func(t *testing.T) {
		s := make(t)
		_, size := s.Devices.Pool()
		for _, g := range groupsOf(t, size) {
			a, err := s.Devices.Container(g)
			if err != nil {
				t.Fatalf("Container(%v): %v", g.Devices(), err)
			}
			if err := a.Validate(); err != nil {
				t.Fatalf("Container(%v) is not valid: %v", g.Devices(), err)
			}
			if _, err := resource.Combine([]resource.Attachment{a}); err != nil {
				t.Fatalf("Combine of Container(%v): %v", g.Devices(), err)
			}
			if !a.GPUs.Given() {
				t.Errorf("Container(%v) gives no GPU", g.Devices())
			}
			named := namedIn(a)
			if len(named) == 0 || !allEqual(named, g) {
				t.Errorf("Container(%v) names the devices %v, want exactly the group's", g.Devices(), named)
			}
			if again, err := s.Devices.Container(g); err != nil || !reflect.DeepEqual(a, again) {
				t.Errorf("Container(%v) changed between calls: %+v then %+v, %v", g.Devices(), a, again, err)
			}
		}
	})
	t.Run("a process is told exactly the devices of the group, the same way on every call", func(t *testing.T) {
		s := make(t)
		_, size := s.Devices.Pool()
		for _, g := range groupsOf(t, size) {
			vars, err := s.Devices.Process(g)
			if err != nil {
				t.Fatalf("Process(%v): %v", g.Devices(), err)
			}
			a := resource.Attachment{Env: vars}
			if err := a.Validate(); err != nil {
				t.Fatalf("Process(%v) gives variables that are not valid: %v", g.Devices(), err)
			}
			named := listsIn(vars)
			if len(named) == 0 || !allEqual(named, g) {
				t.Errorf("Process(%v) names the devices %v, want exactly the group's", g.Devices(), named)
			}
			if again, err := s.Devices.Process(g); err != nil || !reflect.DeepEqual(vars, again) {
				t.Errorf("Process(%v) changed between calls: %v then %v, %v", g.Devices(), vars, again, err)
			}
		}
	})
	t.Run("two groups that share no device give nothing in common", func(t *testing.T) {
		s := make(t)
		_, size := s.Devices.Pool()
		if size.Int() < 2 {
			return // a pool of one device has no two groups that share none
		}
		r := rand.New(rand.NewPCG(7, 11))
		for range Groups {
			a, b := disjointPair(t, r, size)
			ca, err1 := s.Devices.Container(a)
			cb, err2 := s.Devices.Container(b)
			if err1 != nil || err2 != nil {
				t.Fatalf("Container: %v, %v", err1, err2)
			}
			for _, d := range namedIn(ca) {
				if slices.Contains(namedIn(cb), d) {
					t.Fatalf("the containers of %v and %v both name device %d", a.Devices(), b.Devices(), d)
				}
			}
			pa, _ := s.Devices.Process(a)
			pb, _ := s.Devices.Process(b)
			for _, d := range listsIn(pa) {
				if slices.Contains(listsIn(pb), d) {
					t.Fatalf("the processes of %v and %v both name device %d", a.Devices(), b.Devices(), d)
				}
			}
		}
	})
	t.Run("a group outside the pool, or with no device, is refused and the device named", func(t *testing.T) {
		s := make(t)
		_, size := s.Devices.Pool()
		empty := core.DeviceGroup{}
		if _, err := s.Devices.Container(empty); !isInvalid(err) {
			t.Errorf("Container of no device: %v, want an invalid-input failure", err)
		}
		if _, err := s.Devices.Process(empty); !isInvalid(err) {
			t.Errorf("Process of no device: %v, want an invalid-input failure", err)
		}
		if size.Int() == core.MaxPoolSize {
			return // every device number a group can hold is in the pool
		}
		beyond, err := core.ParseDeviceGroup([]uint32{0, uint32(size.Int())}, widest(t), "test")
		if err != nil {
			t.Fatal(err)
		}
		want := strconv.Itoa(size.Int())
		if _, err := s.Devices.Container(beyond); !isInvalid(err) || !strings.Contains(err.Error(), want) {
			t.Errorf("Container with device %s outside the pool: %v, want an invalid-input failure naming it", want, err)
		}
		if _, err := s.Devices.Process(beyond); !isInvalid(err) || !strings.Contains(err.Error(), want) {
			t.Errorf("Process with device %s outside the pool: %v, want an invalid-input failure naming it", want, err)
		}
	})
}

func widest(t *testing.T) core.GPUCount {
	t.Helper()
	c, err := core.ParseGPUCount(core.MaxPoolSize, "test")
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func isInvalid(err error) bool {
	var f *core.Failure
	return errors.As(err, &f) && f.Kind == core.FailureInvalidInput
}

// groupsOf draws the groups a case checks: every single device, the whole pool, and random ones
// of the sizes TakeDevices hands out.
func groupsOf(t *testing.T, size core.GPUCount) []core.DeviceGroup {
	t.Helper()
	r := rand.New(rand.NewPCG(3, uint64(size.Int())))
	var out []core.DeviceGroup
	for d := range size.Int() {
		g, err := core.ParseDeviceGroup([]uint32{uint32(d)}, size, "test")
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, g)
	}
	for range Groups {
		n := 1 + r.IntN(size.Int())
		want, err := core.ParseGPUCount(uint32(n), "test")
		if err != nil {
			t.Fatal(err)
		}
		held := randomHeld(t, r, size, size.Int()-n)
		g, ok := core.TakeDevices(size, held, want)
		if !ok {
			t.Fatalf("TakeDevices(%d, %d) found no group in a pool with %d devices free", size.Int(), n, n)
		}
		out = append(out, g)
	}
	return out
}

// randomHeld holds up to most devices of the pool, as groups of one.
func randomHeld(t *testing.T, r *rand.Rand, size core.GPUCount, most int) []core.DeviceGroup {
	t.Helper()
	var held []core.DeviceGroup
	for _, d := range r.Perm(size.Int())[:max(0, r.IntN(most+1))] {
		g, err := core.ParseDeviceGroup([]uint32{uint32(d)}, size, "test")
		if err != nil {
			t.Fatal(err)
		}
		held = append(held, g)
	}
	return held
}

// disjointPair draws two groups of a pool of two devices or more that share no device.
func disjointPair(t *testing.T, r *rand.Rand, size core.GPUCount) (core.DeviceGroup, core.DeviceGroup) {
	t.Helper()
	perm := r.Perm(size.Int())
	cut := 1 + r.IntN(size.Int()-1)
	split := func(devices []int) core.DeviceGroup {
		numbers := make([]uint32, len(devices))
		for i, d := range devices {
			numbers[i] = uint32(d)
		}
		slices.Sort(numbers)
		g, err := core.ParseDeviceGroup(numbers, size, "test")
		if err != nil {
			t.Fatal(err)
		}
		return g
	}
	return split(perm[:cut]), split(perm[cut:])
}

// namedIn is the device numbers an attachment names: those of its GPUs, and the lists in the
// variables set for the GPUs.
func namedIn(a resource.Attachment) []int {
	return append(listsIn(map[string]string{"gpus": a.GPUs.NVIDIA}), listsIn(a.GPUEnv)...)
}

// listsIn is the device numbers the variables name: the ones whose value is a list of numbers
// such as "0,1,5". A value that is not one, such as an ordering, names none.
func listsIn(vars map[string]string) []int {
	var out []int
	for _, v := range vars {
		var list []int
		ok := v != ""
		for part := range strings.SplitSeq(v, ",") {
			n, err := strconv.Atoi(part)
			if err != nil || n < 0 {
				ok = false
				break
			}
			list = append(list, n)
		}
		if ok {
			out = append(out, list...)
		}
	}
	return out
}

// allEqual says whether every number named is one of the group's, and each of its devices is named.
func allEqual(named []int, g core.DeviceGroup) bool {
	for _, n := range named {
		if !g.Has(core.DeviceIndex(n)) {
			return false
		}
	}
	for _, d := range g.Devices() {
		if !slices.Contains(named, int(d)) {
			return false
		}
	}
	return true
}
