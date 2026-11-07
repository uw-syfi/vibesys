package runtimedir_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

// A run another process holds is live; one nobody holds is dead, held for the caller that proves
// it so until it lets it go, and its lock file is then gone. A run this process holds is live to
// it. Each Runs is a process here: its locks are its own open files, which exclude each other as
// two processes' do.
func TestARunIsLiveWhileAProcessHoldsIt(t *testing.T) {
	ctx := context.Background()
	dir := filepath.Join(shortDir(t), "placement-runs")
	one, other := runtimedir.NewRuns(dir), runtimedir.NewRuns(dir)
	if err := one.Hold(ctx, "run-a"); err != nil {
		t.Fatal(err)
	}
	if err := one.Hold(ctx, "run-a"); err != nil {
		t.Fatalf("holding a run held already: %v", err)
	}
	for name, r := range map[string]*runtimedir.Runs{"another process": other, "its own process": one} {
		if release, dead, err := r.Claim("run-a"); err != nil || dead || release != nil {
			t.Errorf("to %s, the held run is dead=%v (%v)", name, dead, err)
		}
	}

	release, dead, err := other.Claim("run-b")
	if err != nil || !dead {
		t.Fatalf("a run nobody holds: dead=%v (%v)", dead, err)
	}
	if _, dead, _ := one.Claim("run-b"); dead {
		t.Error("a dead run being cleared was claimed twice")
	}
	release()
	if _, err := os.Stat(filepath.Join(dir, "run-b.lock")); !os.IsNotExist(err) {
		t.Errorf("the dead run's lock file is still there (%v)", err)
	}
	release, dead, err = one.Claim("run-b")
	if err != nil || !dead {
		t.Fatalf("the dead run once let go: dead=%v (%v)", dead, err)
	}
	release()
	// A run held after its lock file was removed is held on the file now at its path.
	if err := other.Hold(ctx, "run-b"); err != nil {
		t.Fatal(err)
	}
	if _, dead, _ := one.Claim("run-b"); dead {
		t.Error("a run held again after its lock file was removed is taken to be dead")
	}
}

// A scope that is not a run's is refused, so that no lock is taken outside the directory.
func TestARunsScopeIsAName(t *testing.T) {
	r := runtimedir.NewRuns(filepath.Join(shortDir(t), "placement-runs"))
	for _, scope := range []string{"", "../x", "a/b", ".hidden", "a.b"} {
		if err := r.Hold(context.Background(), scope); err == nil {
			t.Errorf("Hold(%q) took a lock", scope)
		}
		if _, dead, err := r.Claim(scope); err == nil || dead {
			t.Errorf("Claim(%q) = dead %v, %v", scope, dead, err)
		}
	}
}
