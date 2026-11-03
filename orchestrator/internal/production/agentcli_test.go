package production

import (
	"math/rand/v2"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// fakeFiles is a file system of executable paths.
type fakeFiles map[string]bool

func (f fakeFiles) Executable(path string) bool { return f[path] }

func cliTestEnv(kv ...string) func(string) string {
	m := map[string]string{}
	for i := 0; i < len(kv); i += 2 {
		m[kv[i]] = kv[i+1]
	}
	return func(k string) string { return m[k] }
}

func TestAnExplicitCLIWinsOverEveryOtherBuild(t *testing.T) {
	files := fakeFiles{"/pick/vibesys-cli": true, "/install/vibesys-cli": true, "/usr/bin/vibesys-cli": true}
	got, err := ResolveCLI(cliTestEnv(EnvCLI, "/pick/vibesys-cli", "PATH", "/usr/bin"), "/install/vibesys-host", files)
	if err != nil || got != "/pick/vibesys-cli" {
		t.Fatalf("got %q, %v, want the chosen build", got, err)
	}
}

// An explicit choice that cannot be honoured is an error that names the setting. It is never
// answered with another build, which is the failure this exists to prevent.
func TestAnExplicitCLIThatCannotBeUsedIsRefusedAndNeverReplaced(t *testing.T) {
	files := fakeFiles{"/install/vibesys-cli": true, "/usr/bin/vibesys-cli": true}
	for _, setting := range []string{"vibesys-cli", "./vibesys-cli", "/missing/vibesys-cli", "/usr/bin"} {
		got, err := ResolveCLI(cliTestEnv(EnvCLI, setting, "PATH", "/usr/bin"), "/install/vibesys-host", files)
		if err == nil || got != "" || !strings.Contains(err.Error(), EnvCLI) {
			t.Errorf("%q gave %q, %v, want an error naming %s", setting, got, err, EnvCLI)
		}
	}
}

// Without a setting the build installed beside the daemon beats any on PATH, and the first
// absolute PATH entry beats the later ones; a relative entry never counts.
func TestTheCLIBesideTheDaemonThenTheFirstOnPATHIsChosen(t *testing.T) {
	dirs := []string{"/a", "/b", "/c", "/d/e"}
	for seed := uint64(1); seed <= 200; seed++ {
		rng := rand.New(rand.NewPCG(seed, 3))
		files := fakeFiles{}
		var have []string
		for _, d := range dirs {
			if rng.IntN(2) == 0 {
				files[filepath.Join(d, "vibesys-cli")] = true
				have = append(have, d)
			}
		}
		files["bin/vibesys-cli"] = true // reachable only through a relative PATH entry
		beside := rng.IntN(2) == 0
		if beside {
			files["/inst/vibesys-cli"] = true
		}
		order := slices.Clone(dirs)
		rng.Shuffle(len(order), func(i, j int) { order[i], order[j] = order[j], order[i] })
		path := strings.Join(slices.Concat([]string{"bin"}, order), ":")
		got, err := ResolveCLI(cliTestEnv("PATH", path), "/inst/vibesys-host", files)
		want := ""
		switch {
		case beside:
			want = "/inst/vibesys-cli"
		default:
			for _, d := range filepath.SplitList(path) {
				if filepath.IsAbs(d) && files[filepath.Join(d, "vibesys-cli")] {
					want = filepath.Join(d, "vibesys-cli")
					break
				}
			}
		}
		if got != want || (want == "") != (err != nil) {
			t.Fatalf("seed %d: PATH %s, beside %v, executables in %v: got %q, %v, want %q", seed, path, beside, have, got, err, want)
		}
		if got != "" && !filepath.IsAbs(got) {
			t.Fatalf("seed %d: %q is not absolute", seed, got)
		}
	}
}

func TestNoCLIAnywhereIsAnErrorNamingTheSettingAndNotAGuess(t *testing.T) {
	got, err := ResolveCLI(cliTestEnv("PATH", "/usr/bin:relative"), "/install/vibesys-host", fakeFiles{"relative/vibesys-cli": true})
	if err == nil || got != "" || !strings.Contains(err.Error(), EnvCLI) {
		t.Fatalf("got %q, %v, want an error naming %s", got, err, EnvCLI)
	}
}

// The location a run records is exactly the daemon's own absolute settings: a value the
// vibesys-cli would treat as unset (empty or relative) is recorded as unset, and an absolute one
// is kept as it is (cleaned), so an agent resolves each place as the daemon would.
func TestTheLocationRecordsEachAbsoluteSettingAndNothingElse(t *testing.T) {
	values := []string{"", "relative/dir", "/abs/dir", "/abs//dir/", "/"}
	for seed := uint64(1); seed <= 200; seed++ {
		rng := rand.New(rand.NewPCG(seed, 9))
		pick := func() string { return values[rng.IntN(len(values))] }
		env := map[string]string{"XDG_RUNTIME_DIR": pick(), "XDG_STATE_HOME": pick(), "HOME": "/ignored", "PATH": "/bin"}
		home, cli := pick(), pick()
		got := LocationFor(func(k string) string { return env[k] }, home, cli)
		want := func(v string) string {
			if v == "" || v[0] != '/' {
				return ""
			}
			return filepath.Clean(v)
		}
		wantLocation := core.CliLocation{
			CLI: want(cli), RuntimeDir: want(env["XDG_RUNTIME_DIR"]), StateHome: want(env["XDG_STATE_HOME"]), Home: want(home),
		}
		if got != wantLocation {
			t.Fatalf("seed %d: env %v home %q cli %q gave %+v, want %+v", seed, env, home, cli, got, wantLocation)
		}
	}
}
