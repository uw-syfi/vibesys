package runtimedir_test

import (
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

// networkAt says that the paths it holds are on a network file system, and nothing else is.
type networkAt map[string]bool

func (n networkAt) IsNetwork(path string) (bool, error) { return n[path], nil }

// parentKind is what a candidate parent of the directory of leases is.
type parentKind int

const (
	missing parentKind = iota
	aFile
	private       // a directory only its owner may add to
	openUnsticky  // writable by everyone, but anyone may remove another's file
	sharedNetwork // sticky and writable by everyone, on a network file system
	sharedLocal   // sticky and writable by everyone, on local disk: the only kind that may hold leases
	kinds
)

// makeParents makes a parent of each kind under root and says which are on a network.
func makeParents(t *testing.T, root string, ks []parentKind) ([]string, networkAt) {
	t.Helper()
	var paths []string
	network := networkAt{}
	for i, k := range ks {
		p := filepath.Join(root, fmt.Sprintf("p%d", i))
		paths = append(paths, p)
		switch k {
		case missing:
			continue
		case aFile:
			if err := os.WriteFile(p, nil, 0o644); err != nil {
				t.Fatal(err)
			}
			continue
		}
		mode := map[parentKind]os.FileMode{private: 0o755, openUnsticky: 0o777, sharedNetwork: os.ModeSticky | 0o777, sharedLocal: os.ModeSticky | 0o777}[k]
		if err := os.Mkdir(p, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(p, mode); err != nil {
			t.Fatal(err)
		}
		network[p] = k == sharedNetwork
	}
	return paths, network
}

// For every list of parents, the directory of leases is in the first one that is sticky, writable
// by everyone and on local disk, whatever comes before or after it. With none, the failure is a
// typed unavailable one that names every parent, and says of one on a network file system that it
// is. The answer depends on nothing but the parents, so every user's daemon finds the same one.
func TestTheLeasesAreInTheFirstSharedLocalParent(t *testing.T) {
	rng := rand.New(rand.NewPCG(7, 11))
	for round := range 300 {
		ks := make([]parentKind, 1+rng.IntN(5))
		for i := range ks {
			ks[i] = parentKind(rng.IntN(int(kinds)))
		}
		paths, network := makeParents(t, t.TempDir(), ks)
		got, err := runtimedir.ResolveLeasesIn(network, paths)
		want := ""
		for i, k := range ks {
			if k == sharedLocal {
				want = filepath.Join(paths[i], "vibesys-leases")
				break
			}
		}
		if want != "" {
			if err != nil || got != want {
				t.Fatalf("round %d, parents %v: resolved %q, %v; want %q", round, ks, got, err, want)
			}
			continue
		}
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
			t.Fatalf("round %d, parents %v: resolved %q, %v; want an unavailable failure", round, ks, got, err)
		}
		for i, p := range paths {
			if !strings.Contains(f.Message, p) {
				t.Fatalf("round %d: the failure %q does not name %s", round, f.Message, p)
			}
			if ks[i] == sharedNetwork && !strings.Contains(f.Message, p+" is on a network file system") {
				t.Fatalf("round %d: the failure %q does not say %s is on a network file system", round, f.Message, p)
			}
		}
	}
}

// The parents are tried in the order the file system hierarchy gives them, the same on every
// machine and for every user.
func TestTheParentsOfTheLeasesAreFixed(t *testing.T) {
	want := []string{"/run/lock", "/var/tmp", "/dev/shm"}
	if fmt.Sprint(runtimedir.LeaseParents) != fmt.Sprint(want) {
		t.Fatalf("the parents are %v, want %v", runtimedir.LeaseParents, want)
	}
}

// On this machine the directory of leases is on local disk, whatever /var/tmp is: on a machine
// whose root is NFS, /var/tmp is not.
func TestThisMachinesLeasesAreOnLocalDisk(t *testing.T) {
	dir, err := runtimedir.ResolveLeases(runtimedir.System())
	if err != nil {
		t.Fatalf("this machine has no directory of leases: %v", err)
	}
	network, err := runtimedir.System().IsNetwork(filepath.Dir(dir))
	if err != nil || network {
		t.Fatalf("the directory of leases %s is on a network file system (%v, %v)", dir, network, err)
	}
	t.Logf("this machine's leases are in %s", dir)
}
