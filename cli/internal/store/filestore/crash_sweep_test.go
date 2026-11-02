//go:build !race

// This file is a sweep: it walks a large space of cases on one goroutine, so the race detector has
// nothing to check and only slows it down. check.sh runs it in its step without the detector, and
// archcheck holds every file here to that: named *_sweep_test.go, built only without -race, and
// free of goroutines.
package filestore_test

import (
	"fmt"
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

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
