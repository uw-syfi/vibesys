package docker_test

import (
	"fmt"
	"math/rand/v2"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

func TestAScopeFromAKeyIsStableDistinctAndAcceptedByTheProvider(t *testing.T) {
	rng := rand.New(rand.NewPCG(7, 11))
	seen := map[string]string{}
	w := fakeworld.New()
	for range 500 {
		key := fmt.Sprintf("/run/user/%d/%x", rng.IntN(70000), rng.Uint64())
		scope := docker.ScopeFor(key)
		if again := docker.ScopeFor(key); again != scope {
			t.Fatalf("the scope of %q is %q, then %q", key, scope, again)
		}
		if other, ok := seen[scope]; ok && other != key {
			t.Fatalf("the keys %q and %q share the scope %q", other, key, scope)
		}
		seen[scope] = key
		_, err := docker.New(docker.Config{
			Engine: dockerfake.NewDaemon(w.MemoryGates).Connect(), Source: dockerfake.Fixed(docker.Spec{}), Backend: cpuBackend(),
			Timers: envtest.NewTimers(), IDs: dockerfake.NewIDs(1), Scope: scope,
		})
		if err != nil {
			t.Fatalf("the provider refused the scope %q of %q: %v", scope, key, err)
		}
	}
}
