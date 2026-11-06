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

func TestAnExplicitCLIWinsOverEveryOtherBuild(t *testing.T) {
	files := fakeFiles{"/pick/vibesys-cli": true, "/install/vibesys-cli": true, "/usr/bin/vibesys-cli": true}
	got, err := ResolveCLI("/pick/vibesys-cli", []string{"/usr/bin"}, developerBuild, files)
	if err != nil || got != "/pick/vibesys-cli" {
		t.Fatalf("got %q, %v, want the chosen build", got, err)
	}
}

// An explicit choice that cannot be honoured is an error that names the setting. It is never
// answered with another build, which is the failure this exists to prevent.
func TestAnExplicitCLIThatCannotBeUsedIsRefusedAndNeverReplaced(t *testing.T) {
	files := fakeFiles{"/install/vibesys-cli": true, "/usr/bin/vibesys-cli": true}
	for _, setting := range []string{"vibesys-cli", "./vibesys-cli", "/missing/vibesys-cli", "/usr/bin"} {
		got, err := ResolveCLI(setting, []string{"/usr/bin"}, developerBuild, files)
		if err == nil || got != "" || !strings.Contains(err.Error(), SettingJobTool) {
			t.Errorf("%q gave %q, %v, want an error naming %s", setting, got, err, SettingJobTool)
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
		find := developerBuild
		if beside {
			find = memoryRelease(t, "/inst")
		}
		order := slices.Clone(dirs)
		rng.Shuffle(len(order), func(i, j int) { order[i], order[j] = order[j], order[i] })
		path := slices.Concat([]string{"bin"}, order)
		got, err := ResolveCLI("", path, find, files)
		want := ""
		switch {
		case beside:
			want = "/inst/bin/vibesys-cli"
		default:
			for _, d := range path {
				if filepath.IsAbs(d) && files[filepath.Join(d, "vibesys-cli")] {
					want = filepath.Join(d, "vibesys-cli")
					break
				}
			}
		}
		if got != want || (want == "") != (err != nil) {
			t.Fatalf("seed %d: tool path %v, beside %v, executables in %v: got %q, %v, want %q", seed, path, beside, have, got, err, want)
		}
		if got != "" && !filepath.IsAbs(got) {
			t.Fatalf("seed %d: %q is not absolute", seed, got)
		}
	}
}

// A release is the daemon's own build: when its tool is missing the error names the file, and the
// build on PATH is not taken instead.
func TestAReleaseWithoutItsToolIsAnErrorAndNeverAnotherBuild(t *testing.T) {
	files := fakeFiles{"/usr/bin/vibesys-cli": true}
	got, err := ResolveCLI("", []string{"/usr/bin"}, memoryRelease(t, "/inst", "bin/vibesys-cli"), files)
	if err == nil || got != "" || !strings.Contains(err.Error(), "bin/vibesys-cli") {
		t.Fatalf("got %q, %v, want an error naming the release's tool", got, err)
	}
}

func TestNoCLIAnywhereIsAnErrorNamingTheSettingAndNotAGuess(t *testing.T) {
	got, err := ResolveCLI("", []string{"/usr/bin", "relative"}, developerBuild, fakeFiles{"relative/vibesys-cli": true})
	if err == nil || got != "" || !strings.Contains(err.Error(), SettingJobTool) {
		t.Fatalf("got %q, %v, want an error naming %s", got, err, SettingJobTool)
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
