package environment_test

import (
	"errors"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
)

// A registry hands out the environment of a kind it has, and says unavailable for every other
// kind, whether the kind is a real one or not. It never answers with a different kind.
func TestARegistryHasOnlyTheKindsItWasGiven(t *testing.T) {
	host := fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates())
	r := environment.Registry{core.EnvironmentHost: host}
	got, err := r.Environment(core.EnvironmentHost)
	if err != nil || got != environment.Environment(host) {
		t.Fatalf("Environment(host) = %v, %v", got, err)
	}
	for _, kind := range []core.EnvironmentKind{core.EnvironmentDocker, core.EnvironmentAuto, "vm", ""} {
		got, err := r.Environment(kind)
		var f *core.Failure
		if got != nil || !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
			t.Errorf("Environment(%q) = %v, %v; want unavailable", kind, got, err)
		}
	}
	var none environment.Registry
	if _, err := none.Environment(core.EnvironmentHost); err == nil {
		t.Error("an empty registry has a host environment")
	}
	if _, err := (environment.Registry{core.EnvironmentHost: nil}).Environment(core.EnvironmentHost); err == nil {
		t.Error("a registry with a nil environment hands it out")
	}
}
