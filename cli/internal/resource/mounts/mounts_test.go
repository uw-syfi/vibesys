package mounts_test

import (
	"errors"
	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"maps"
	"math/rand/v2"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/mounts"
	"github.com/uw-syfi/vibesys/cli/internal/resource/resourcetest"
)

func TestMountsPassTheContract(t *testing.T) {
	resourcetest.Run(t, func(t *testing.T) resourcetest.Subject {
		m, err := mounts.New(map[string]string{"hf-cache": "/data/hf", "models": "/data/models"})
		if err != nil {
			t.Fatal(err)
		}
		return resourcetest.Subject{Resource: m, Kind: core.ResourceMounts}
	})
}

// Every directory is mounted read-only at its name, the names are listed in order, and two sets of
// mounts fingerprint differently exactly when their names differ.
func TestEachDirectoryIsMountedReadOnlyAtItsName(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 4))
	seen := map[string]map[string]string{}
	for range 300 {
		dirs := map[string]string{}
		for range 1 + r.IntN(4) {
			dirs[[]string{"a", "b", "hf-cache", "m_1"}[r.IntN(4)]] = []string{"/x", "/data/y", "/z/w"}[r.IntN(3)]
		}
		m, err := mounts.New(dirs)
		if err != nil {
			t.Fatal(err)
		}
		a, err := m.Attach(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		names := slices.Sorted(maps.Keys(dirs))
		var job cliv1.JobEnvironment
		if err := envcodec.Decode(func(n string) string { return a.Env[n] }, &job); err != nil {
			t.Fatal(err)
		}
		if !slices.Equal(job.GetMounts(), names) || len(a.Env) != 1 || len(a.Mounts) != len(names) {
			t.Fatalf("%v: %+v", dirs, a)
		}
		for i, n := range names {
			if a.Mounts[i] != (resource.Mount{Host: dirs[n], Container: mounts.Dir + "/" + n, ReadOnly: true}) {
				t.Fatalf("%v: mount %d is %+v", dirs, i, a.Mounts[i])
			}
		}
		fp, err := m.Fingerprint(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		if other, ok := seen[fp[mounts.Fact]]; ok && !slices.Equal(slices.Sorted(maps.Keys(other)), names) {
			t.Fatalf("%v and %v fingerprint alike: %v", dirs, other, fp)
		}
		seen[fp[mounts.Fact]] = dirs
	}
}

func TestAMountThatCannotBeIsRefusedNamingIt(t *testing.T) {
	for name, dirs := range map[string]map[string]string{
		"none":               {},
		"a name with /":      {"a/b": "/x"},
		"an upper-case name": {"HF": "/x"},
		"a relative dir":     {"hf": "data"},
		"a dir with a comma": {"hf": "/a,b"},
	} {
		_, err := mounts.New(dirs)
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
			t.Errorf("%s: %v, want invalid input", name, err)
		}
	}
}
