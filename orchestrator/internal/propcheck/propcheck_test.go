package propcheck_test

import (
	"math/rand"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/orchestrator/internal/propcheck"
)

func TestSeedFollowsTheEnvironment(t *testing.T) {
	t.Setenv(propcheck.SeedEnv, "")
	named := propcheck.Config(t, nil).Rand.Int63()
	if again := propcheck.Config(t, nil).Rand.Int63(); again != named {
		t.Errorf("the seed of one test name changed between calls: %d then %d", named, again)
	}
	t.Setenv(propcheck.SeedEnv, "7")
	if got, want := propcheck.Config(t, nil).Rand.Int63(), rand.New(rand.NewSource(7)).Int63(); got != want {
		t.Errorf("seed 7 gave %d, want %d", got, want)
	}
}

func TestConfigKeepsTheBaseFields(t *testing.T) {
	base := &quick.Config{MaxCount: 17}
	cfg := propcheck.Config(t, base)
	if cfg.MaxCount != 17 || cfg.Rand == nil || base.Rand != nil {
		t.Errorf("got MaxCount %d, Rand %v; base Rand %v", cfg.MaxCount, cfg.Rand, base.Rand)
	}
}
