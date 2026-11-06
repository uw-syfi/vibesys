package production

import (
	"errors"
	"math/rand/v2"
	"os"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
)

// test-isolation: daemonPlaces is the rule RunDaemon starts from, and RunDaemon reaches it only with a
// real daemon, which the real tier covers once (TestRealAnAgentReachesTheDaemonItWasTold).

// envName is the variable of JobToolPlace, from the message.
func envName(t *testing.T) string {
	t.Helper()
	names, err := envcodec.Names(&cliv1.JobToolPlace{})
	if err != nil || len(names) != 1 {
		t.Fatalf("JobToolPlace declares %v, %v", names, err)
	}
	return names[0]
}

// someEnvironment is whatever an operator's shell may say about the places, in any combination.
func someEnvironment(r *rand.Rand) map[string]string {
	values := []string{"", "/", "/x", "/y/z", "relative", "./dot", "/run/user/7"}
	env := map[string]string{}
	for _, name := range []string{"XDG_RUNTIME_DIR", filestore.EnvStateHome, "XDG_CONFIG_HOME", "XDG_CACHE_HOME"} {
		if value := values[r.IntN(len(values))]; value != "" {
			env[name] = value
		}
	}
	return env
}

func processIn(env map[string]string, home string) Process {
	return Process{Getenv: func(name string) string { return env[name] }, UID: os.Getuid(), Home: home}
}

// For any environment, a process told which runtime directory to use uses that one, and a process
// told none resolves the user's from its environment as it always did: the variable is the only thing
// that moves a daemon's place, and a person's plain defaults do not see it.
func TestTheVariableAloneChoosesTheRuntimeDirectory(t *testing.T) {
	name := envName(t)
	r := rand.New(rand.NewPCG(3, 11))
	for range 500 {
		env := someEnvironment(r)
		plain := processIn(env, "/home/u")
		want := runtimedir.Resolve(plain.Getenv, plain.UID)
		if got, err := plain.RuntimePath(); err != nil || got != want {
			t.Fatalf("%v: %q, %v; want the default %q", env, got, err, want)
		}
		env[name] = "/chosen/rt"
		told := processIn(env, "/home/u")
		if got, err := told.RuntimePath(); err != nil || got != "/chosen/rt" {
			t.Fatalf("%v: %q, %v; want the chosen directory", env, got, err)
		}
	}
}

// A value that cannot be a directory is refused naming the variable, and never read as unset: that
// would reach the operator's daemon in place of the host's.
func TestAVariableThatCannotBeADirectoryIsRefusedNamingIt(t *testing.T) {
	name := envName(t)
	for _, bad := range []string{"relative", "./x", "x/y", "/nul\x00byte"} {
		_, err := processIn(map[string]string{name: bad}, "/home/u").RuntimePath()
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
			t.Fatalf("%q: %v", bad, err)
		}
	}
}

// Whatever order and environments starts come in, a directory's daemon keeps one state home: the one
// the first start that may record chose. A process told to use a directory that records nothing
// records nothing itself and is refused, however its own environment reads.
func TestADirectoryKeepsTheStateHomeOfItsFirstStart(t *testing.T) {
	name := envName(t)
	r := rand.New(rand.NewPCG(5, 23))
	for range 100 {
		rt := filepath.Join(t.TempDir(), "rt")
		dir, err := runtimedir.Open(rt, os.Getuid(), fake{})
		if err != nil {
			t.Fatal(err)
		}
		var first string
		for range 8 {
			env := someEnvironment(r)
			if _, told := map[bool]string{true: ""}[r.IntN(2) == 0]; told {
				env[name] = rt
			}
			home := "/home/" + string(rune('a'+r.IntN(3)))
			p := processIn(env, home)
			places, err := p.daemonPlaces(dir)
			switch {
			case env[name] != "" && first == "":
				if err == nil || core.AsFailure(err).Kind != core.FailureUnavailable {
					t.Fatalf("told, with nothing recorded: %v, %v", places, err)
				}
				if recorded, err := dir.Places(); err != nil || recorded != nil {
					t.Fatalf("a refused start recorded %v, %v", recorded, err)
				}
			case err != nil:
				t.Fatalf("%v: %v", env, err)
			case first == "":
				first = places.GetStateHome()
			case places.GetStateHome() != first:
				t.Fatalf("%v: the state home moved from %q to %q", env, first, places.GetStateHome())
			}
		}
	}
}

// `daemon ensure` is how whoever chose a directory sets it up, also from a process told to use it, and
// it changes nothing once the directory records its places.
func TestEnsureSetsUpADirectoryAndNeverMovesIt(t *testing.T) {
	name := envName(t)
	rt := filepath.Join(t.TempDir(), "rt")
	dir, err := runtimedir.Open(rt, os.Getuid(), fake{})
	if err != nil {
		t.Fatal(err)
	}
	host := processIn(map[string]string{name: rt, filestore.EnvStateHome: "/host/state"}, "/home/u")
	if places, err := host.recordPlaces(dir); err != nil || places.GetStateHome() != "/host/state" {
		t.Fatalf("%v, %v", places, err)
	}
	other := processIn(map[string]string{name: rt, filestore.EnvStateHome: "/other/state"}, "/home/v")
	if places, err := other.recordPlaces(dir); err != nil || places.GetStateHome() != "/host/state" {
		t.Fatalf("%v, %v", places, err)
	}
	if places, err := other.daemonPlaces(dir); err != nil || places.GetStateHome() != "/host/state" {
		t.Fatalf("%v, %v", places, err)
	}
}

// With no place to name a state home from, nothing is recorded: a relative path would be a store in
// whatever directory the command ran in.
func TestAStateHomeThatIsNotAbsoluteIsNotRecorded(t *testing.T) {
	rt := filepath.Join(t.TempDir(), "rt")
	dir, err := runtimedir.Open(rt, os.Getuid(), fake{})
	if err != nil {
		t.Fatal(err)
	}
	if places, err := processIn(map[string]string{}, "").daemonPlaces(dir); err == nil {
		t.Fatalf("recorded %v with no home", places)
	}
	if recorded, err := dir.Places(); err != nil || recorded != nil {
		t.Fatalf("recorded %v, %v", recorded, err)
	}
}

// A file that is not the message is an error naming it, and never read as nothing recorded.
func TestAPlacesFileThatCannotBeReadIsNamed(t *testing.T) {
	rt := filepath.Join(t.TempDir(), "rt")
	dir, err := runtimedir.Open(rt, os.Getuid(), fake{})
	if err != nil {
		t.Fatal(err)
	}
	for _, text := range []string{"", "not json", `{"stateHome":"relative"}`, `{"somethingElse":"/x"}`} {
		if err := os.WriteFile(filepath.Join(rt, "daemon-places.json"), []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
		if places, err := dir.Places(); err == nil {
			t.Fatalf("%q read as %v", text, places)
		}
	}
}

// fake says the directory is on local disk.
type fake struct{}

func (fake) IsNetwork(string) (bool, error) { return false, nil }
