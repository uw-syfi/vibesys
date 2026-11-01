package filestore_test

import (
	"fmt"
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// reference is a run of the script on a filesystem that never fails: what the store shows after
// Open and after each call, and how many write steps the whole run takes.
type reference struct {
	ops    []op
	states []any // states[i] is what the store shows once i calls have returned
	steps  int
}

func referenceRun(t *testing.T) reference {
	t.Helper()
	mem := fsys.NewMemFS()
	s := openOn(t, mem)
	ref := reference{ops: script(), states: []any{storetest.Observe(t, s, runs...)}}
	for _, o := range ref.ops {
		if err := o.run(t.Context(), s); err != nil {
			t.Fatalf("%s: %v", o.name, err)
		}
		ref.states = append(ref.states, storetest.Observe(t, s, runs...))
	}
	ref.steps = mem.Steps()
	return ref
}

// where says what was in flight when a fault fired.
type where struct {
	openFailed bool // Open itself failed
	done       int  // the calls that returned before the one that failed
	failed     bool // a call failed
}

// allowed are the states a store may be in afterwards: the one before the call that was in
// flight and the one after it. Nothing earlier may be lost and nothing later may appear.
func (ref reference) allowed(w where) []int {
	if w.openFailed || !w.failed {
		return []int{w.done}
	}
	return []int{w.done, w.done + 1}
}

// runUntilFault runs the script on a filesystem with a fault armed, until a call fails.
func runUntilFault(t *testing.T, ref reference, mem *fsys.MemFS) (store.Store, where) {
	t.Helper()
	s, err := filestore.Open(stateDir, filestore.WithFS(mem))
	if err != nil {
		return nil, where{openFailed: true}
	}
	w := where{}
	for _, o := range ref.ops {
		if err := o.run(t.Context(), s); err != nil {
			w.failed = true
			return s, w
		}
		w.done++
	}
	return s, w
}

// matches returns the index into the reference states the observation equals, or -1.
func (ref reference) matches(observed any, among []int) int {
	for _, i := range among {
		if reflect.DeepEqual(observed, ref.states[i]) {
			return i
		}
	}
	return -1
}

// reopen opens the image a crash left and checks it shows one of the allowed states. It returns
// the store and the index of the state, so the caller can carry on from there.
func reopen(t *testing.T, ref reference, label string, image *fsys.MemFS, w where) (store.Store, int, bool) {
	t.Helper()
	s, err := filestore.Open(stateDir, filestore.WithFS(image))
	if err != nil {
		t.Errorf("%s: reopening failed: %v", label, err)
		return nil, 0, false
	}
	among := ref.allowed(w)
	got := storetest.Observe(t, s, runs...)
	i := ref.matches(got, among)
	if i < 0 {
		t.Errorf("%s: after reopening the store shows\n%+v\nwhich is neither of the states around call %d (%v)", label, got, w.done, among)
		return nil, 0, false
	}
	return s, i, true
}

// carryOn runs the rest of the script on a store that shows the state after n calls, and checks
// it ends where the uninterrupted run ended.
func carryOn(t *testing.T, ref reference, label string, s store.Store, n int) {
	t.Helper()
	for _, o := range ref.ops[n:] {
		if err := o.run(t.Context(), s); err != nil {
			t.Errorf("%s: %s failed after recovery: %v", label, o.name, err)
			return
		}
	}
	if got := storetest.Observe(t, s, runs...); !reflect.DeepEqual(got, ref.states[len(ref.ops)]) {
		t.Errorf("%s: after recovery and the rest of the script the store shows\n%+v\nwant\n%+v", label, got, ref.states[len(ref.ops)])
	}
}

var faultKinds = []struct {
	name string
	kind fsys.FaultKind
}{
	{"crash", fsys.FaultCrash},
	{"torn write", fsys.FaultTornWrite},
	{"failed call", fsys.FaultFail},
	{"call that failed after it took effect", fsys.FaultFailAfter},
}

// TestKillAtEveryStep stops the program at each write step of the script in turn, in each way
// a step can go wrong, and checks what a restart finds. It runs the same steps every time: the
// step to fail is a number, and the filesystem is in memory.
func TestKillAtEveryStep(t *testing.T) {
	ref := referenceRun(t)
	if ref.steps < 100 {
		t.Fatalf("the script takes %d write steps; the test would cover too little", ref.steps)
	}
	for _, fk := range faultKinds {
		t.Run(fk.name, func(t *testing.T) {
			for k := range ref.steps {
				mem := fsys.NewMemFS()
				mem.SetFault(fsys.Fault{At: k, Kind: fk.kind})
				live, w := runUntilFault(t, ref, mem)
				if !w.failed && !w.openFailed {
					t.Fatalf("a %s at step %d did not stop the script", fk.name, k)
				}
				label := func(what string) string { return fmt.Sprintf("%s at step %d, %s", fk.name, k, what) }

				for _, power := range []bool{false, true} {
					what := map[bool]string{false: "process killed", true: "power lost"}[power]
					s, i, ok := reopen(t, ref, label(what), mem.CrashImage(power), w)
					if ok {
						carryOn(t, ref, label(what), s, i)
					}
				}

				if fk.kind != fsys.FaultFail && fk.kind != fsys.FaultFailAfter {
					continue
				}
				// The process lives on after a failed call. The store it keeps must show what a
				// restart shows, and must work.
				if w.openFailed {
					s, i, ok := reopen(t, ref, label("Open tried again"), mem, w)
					if ok {
						carryOn(t, ref, label("Open tried again"), s, i)
					}
					continue
				}
				got := storetest.Observe(t, live, runs...)
				i := ref.matches(got, ref.allowed(w))
				if i < 0 {
					t.Errorf("%s: the live store shows\n%+v\nwhich is neither of the states around call %d", label("after the failure"), got, w.done)
					continue
				}
				if restart := storetest.Observe(t, openOn(t, mem.CrashImage(false)), runs...); !reflect.DeepEqual(restart, got) {
					t.Errorf("%s: the live store shows\n%+v\nand a restart shows\n%+v", label("after the failure"), got, restart)
				}
				carryOn(t, ref, label("live store"), live, i)
			}
		})
	}
}

// TestCrashDuringRecovery kills the program again while it is opening what the first crash
// left, at each write step Open takes, and checks the second restart finds the same state.
func TestCrashDuringRecovery(t *testing.T) {
	ref := referenceRun(t)
	opened := 0
	for _, fk := range []fsys.FaultKind{fsys.FaultCrash, fsys.FaultTornWrite} {
		for k := range ref.steps {
			mem := fsys.NewMemFS()
			mem.SetFault(fsys.Fault{At: k, Kind: fk})
			_, w := runUntilFault(t, ref, mem)
			for _, power := range []bool{false, true} {
				image := mem.CrashImage(power)
				probe := image.CrashImage(false)
				if _, err := filestore.Open(stateDir, filestore.WithFS(probe)); err != nil {
					t.Fatalf("step %d: reopening failed: %v", k, err)
				}
				for r := range probe.Steps() {
					second := image.CrashImage(false)
					second.SetFault(fsys.Fault{At: r, Kind: fsys.FaultCrash})
					// Open may fail with the crash, or finish: the step that crashed may be a removal of
					// a leftover file that Open does not wait on.
					_, _ = filestore.Open(stateDir, filestore.WithFS(second))
					label := fmt.Sprintf("crash at step %d (power loss %v), then at recovery step %d", k, power, r)
					s, i, ok := reopen(t, ref, label, second.CrashImage(power), w)
					if ok {
						carryOn(t, ref, label, s, i)
					}
					opened++
				}
			}
		}
	}
	if opened == 0 {
		t.Error("no crash left anything for recovery to do, so the test checked nothing")
	}
}
