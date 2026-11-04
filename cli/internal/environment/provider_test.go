package environment_test

import (
	"errors"
	"io"
	"strings"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
)

func wantUnavailable(t *testing.T, err error) {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Fatalf("error %v, want an unavailable failure", err)
	}
}

// The environment of a kind is made for the target it is asked for, and a kind nobody wired is
// unavailable instead of being answered with another kind.
func TestKindsMakeTheEnvironmentOfTheKindForTheTarget(t *testing.T) {
	var got []environment.Target
	kinds := environment.Kinds{
		core.EnvironmentDocker: func(target environment.Target) (environment.Environment, error) {
			got = append(got, target)
			return nil, nil
		},
	}
	want := environment.Target{Repository: "/repo", Commit: core.Commit(strings.Repeat("a", 40)), Task: "bench"}
	if _, err := kinds.Environment(core.EnvironmentDocker, want); err != nil {
		t.Fatalf("docker: %v", err)
	}
	if len(got) != 1 || got[0] != want {
		t.Errorf("the docker function was given %+v, want exactly %+v", got, want)
	}
	for _, kind := range []core.EnvironmentKind{core.EnvironmentHost, core.EnvironmentAuto, "podman"} {
		_, err := kinds.Environment(kind, want)
		wantUnavailable(t, err)
	}
	if len(got) != 1 {
		t.Errorf("a kind with no function reached the docker function")
	}
}

func TestNoneHasNoEnvironment(t *testing.T) {
	for _, kind := range []core.EnvironmentKind{core.EnvironmentHost, core.EnvironmentDocker} {
		_, err := environment.None{}.Environment(kind, environment.Target{})
		wantUnavailable(t, err)
	}
}

type failAfter struct {
	n   int
	buf strings.Builder
}

func (f *failAfter) Write(p []byte) (int, error) {
	if f.n <= 0 {
		return 0, io.ErrClosedPipe
	}
	f.n--
	return f.buf.Write(p)
}

// Several processes write to one output at once: every write is whole, and a failing output is
// reported once, at the end, while the writers are never told.
func TestSinkSerializesWritersAndRemembersTheFirstFailure(t *testing.T) {
	var out strings.Builder
	sink := environment.NewSink(&out)
	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() {
			for range 50 {
				if n, err := sink.Write([]byte("line\n")); n != 5 || err != nil {
					t.Errorf("Write = %d, %v", n, err)
				}
			}
		})
	}
	wg.Wait()
	if got := strings.Count(out.String(), "line\n"); got != 400 || sink.Err() != nil {
		t.Errorf("output holds %d whole lines (err %v), want 400", got, sink.Err())
	}

	failing := &failAfter{n: 1}
	s := environment.NewSink(failing)
	for range 3 {
		if n, err := s.Write([]byte("xy")); n != 2 || err != nil {
			t.Fatalf("Write = %d, %v: the writer must never see the failure", n, err)
		}
	}
	if !errors.Is(s.Err(), io.ErrClosedPipe) {
		t.Errorf("Err = %v, want the output's failure", s.Err())
	}
	if failing.buf.String() != "xy" {
		t.Errorf("the output holds %q: nothing may be written after it failed", failing.buf.String())
	}
}

// A registry hands out the environment of a kind it has, and says unavailable for every other
// kind, whether the kind is a real one or not. It never answers with a different kind.
func TestARegistryHasOnlyTheKindsItWasGiven(t *testing.T) {
	host := fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates())
	r := environment.Registry{core.EnvironmentHost: host}
	got, err := r.Environment(core.EnvironmentHost, environment.Target{})
	if err != nil || got != environment.Environment(host) {
		t.Fatalf("Environment(host) = %v, %v", got, err)
	}
	for _, kind := range []core.EnvironmentKind{core.EnvironmentDocker, core.EnvironmentAuto, "vm", ""} {
		got, err := r.Environment(kind, environment.Target{})
		var f *core.Failure
		if got != nil || !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
			t.Errorf("Environment(%q) = %v, %v; want unavailable", kind, got, err)
		}
	}
	var none environment.Registry
	if _, err := none.Environment(core.EnvironmentHost, environment.Target{}); err == nil {
		t.Error("an empty registry has a host environment")
	}
	if _, err := (environment.Registry{core.EnvironmentHost: nil}).Environment(core.EnvironmentHost, environment.Target{}); err == nil {
		t.Error("a registry with a nil environment hands it out")
	}
}
