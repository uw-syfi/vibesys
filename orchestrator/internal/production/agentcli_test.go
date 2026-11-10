package production

import (
	"math/rand/v2"
	"path/filepath"
	"slices"
	"strings"
	"testing"
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

// The daemon that reads SettingJobTool is the one already running, which keeps its configuration
// until it is restarted; the error says so, not only which setting to change.
func TestNoCLIAnywhereIsAnErrorNamingTheSettingAndNotAGuess(t *testing.T) {
	got, err := ResolveCLI("", []string{"/usr/bin", "relative"}, developerBuild, fakeFiles{"relative/vibesys-cli": true})
	if err == nil || got != "" || !strings.Contains(err.Error(), SettingJobTool) || !strings.Contains(err.Error(), "host restart") {
		t.Fatalf("got %q, %v, want an error naming %s and host restart", got, err, SettingJobTool)
	}
}
