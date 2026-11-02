package environment_test

import (
	"errors"
	"math/rand/v2"
	"path"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

func execution(checkout, scratch string) core.Execution {
	return core.Execution{
		Checkout: core.Checkout{Dir: checkout}, Scratch: scratch, Argv: []string{"true"}, Timeout: time.Second,
	}
}

func isInvalid(err error) bool {
	var f *core.Failure
	return errors.As(err, &f) && f.Kind == core.FailureInvalidInput
}

func TestScratchIsAnAbsoluteDirectoryApartFromTheCheckout(t *testing.T) {
	for _, c := range []struct {
		checkout, scratch string
		ok                bool
	}{
		{"/work/co", "", true},
		{"/work/co", "/work/scratch", true},
		{"/work/co", "/work/co-scratch", true}, // a longer name is not inside
		{"/work/co", "/work/co", false},
		{"/work/co", "/work/co/scratch", false},
		{"/work/co/inner", "/work/co", false},
		{"/work/co", "/work/co/../co/x", false}, // unclean paths mean what they resolve to
		{"/work/co", "scratch", false},
		{"/", "/scratch", false}, // the root contains everything
	} {
		err := environment.Validate(execution(c.checkout, c.scratch))
		if c.ok && err != nil {
			t.Errorf("checkout %q, scratch %q: %v, want it accepted", c.checkout, c.scratch, err)
		}
		if !c.ok && !isInvalid(err) {
			t.Errorf("checkout %q, scratch %q: %v, want it refused as invalid input", c.checkout, c.scratch, err)
		}
	}
}

// Over random directories, the refusal is exactly "one is the other or inside it", whatever the
// order, and the verdict does not depend on a trailing slash.
func TestScratchRefusalMatchesContainmentOverRandomPaths(t *testing.T) {
	segments := []string{"a", "b", "ab", "c"}
	for seed := range uint64(500) {
		rng := rand.New(rand.NewPCG(seed, 3))
		random := func() string {
			var parts []string
			for range 1 + rng.IntN(3) {
				parts = append(parts, segments[rng.IntN(len(segments))])
			}
			return "/" + strings.Join(parts, "/")
		}
		checkout, scratch := random(), random()
		inside := func(p, dir string) bool { return p == dir || strings.HasPrefix(p, dir+"/") }
		want := inside(scratch, checkout) || inside(checkout, scratch)
		for _, suffix := range []string{"", "/"} {
			err := environment.Validate(execution(path.Clean(checkout)+suffix, scratch+suffix))
			if refused := err != nil; refused != want {
				t.Fatalf("seed %d: checkout %q, scratch %q: refused=%v (%v), want %v", seed, checkout, scratch, refused, err, want)
			}
		}
	}
}
