package server_test

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// No head writes or lock probes can accidentally wake this subscription: an
// index unlink alone must invalidate the scan, even while its target survives.
func TestRunsIndexUnlinkWithoutTargetChange(t *testing.T) {
	f := start(t)
	stream, err := subscribeRuns(f.ctx, client(t, f, false))
	if err != nil {
		t.Fatal(err)
	}
	if got := watchEntry(t, stream); got.RunId != "run-1" || got.Removed {
		t.Fatal(got)
	}
	if got := watchEntry(t, stream); !got.InitialComplete {
		t.Fatal(got)
	}
	for i := range 4 {
		id := fmt.Sprintf("run-%d", i+1)
		if err := os.Remove(filepath.Join(f.root, "runs", id)); err != nil {
			t.Fatal(err)
		}
		if got := watchEntry(t, stream); got.RunId != id || !got.Removed {
			t.Fatal(got)
		}
		if i == 3 {
			break
		}
		// Recreate the index too: watches belong to directory identities, not
		// path strings. Coalescing these mutations must still converge.
		if err := os.Remove(filepath.Join(f.root, "runs")); err != nil {
			t.Fatal(err)
		}
		if err := os.Mkdir(filepath.Join(f.root, "runs"), 0700); err != nil {
			t.Fatal(err)
		}
		next := fmt.Sprintf("run-%d", i+2)
		if err := os.Symlink(f.run, filepath.Join(f.root, "runs", next)); err != nil {
			t.Fatal(err)
		}
		if got := watchEntry(t, stream); got.RunId != next || got.Removed {
			t.Fatal(got)
		}
	}
}
