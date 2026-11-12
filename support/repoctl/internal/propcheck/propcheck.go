// Package propcheck seeds testing/quick the way the Python tests seed Hypothesis: a pull request
// runs every property from a fixed seed, so its result is a pure function of the code, and the
// nightly run draws fresh seeds. Every quick.Check in this module takes its config from Config;
// TestQuickChecksUseConfig enforces that.
package propcheck

import (
	"hash/fnv"
	"math/rand"
	"os"
	"strconv"
	"testing"
	"testing/quick"
)

// SeedEnv names the variable that picks the seed. Unset or empty: a seed derived from the test's
// name. A decimal integer: that seed, to rerun a failure. "random": a fresh seed.
const SeedEnv = "VIBESYS_PROPERTY_SEED"

// Config returns a copy of base (nil means the default config) whose Rand is seeded as SeedEnv says.
// It logs the seed and the command that reruns it if the test fails.
func Config(t testing.TB, base *quick.Config) *quick.Config {
	t.Helper()
	cfg := quick.Config{}
	if base != nil {
		cfg = *base
	}
	var seed int64
	switch value := os.Getenv(SeedEnv); value {
	case "":
		h := fnv.New64a()
		h.Write([]byte(t.Name()))
		seed = int64(h.Sum64())
	case "random":
		seed = rand.Int63()
	default:
		n, err := strconv.ParseInt(value, 10, 64)
		if err != nil {
			t.Fatalf("%s must be empty, \"random\" or a decimal integer, got %q", SeedEnv, value)
		}
		seed = n
	}
	cfg.Rand = rand.New(rand.NewSource(seed))
	t.Cleanup(func() {
		if t.Failed() {
			t.Logf("property seed %d; rerun with: %s=%d go test -run '^%s$' <package>", seed, SeedEnv, seed, t.Name())
		}
	})
	return &cfg
}
