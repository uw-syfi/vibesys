package production

import (
	"context"
	"log/slog"
	"math/rand/v2"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
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
	got, err := ResolveCLI(cliTestEnv(EnvCLI, "/pick/vibesys-cli", "PATH", "/usr/bin"), "/install/vibesysd", files)
	if err != nil || got != "/pick/vibesys-cli" {
		t.Fatalf("got %q, %v, want the chosen build", got, err)
	}
}

// An explicit choice that cannot be honoured is an error that names the setting. It is never
// answered with another build, which is the failure this exists to prevent.
func TestAnExplicitCLIThatCannotBeUsedIsRefusedAndNeverReplaced(t *testing.T) {
	files := fakeFiles{"/install/vibesys-cli": true, "/usr/bin/vibesys-cli": true}
	for _, setting := range []string{"vibesys-cli", "./vibesys-cli", "/missing/vibesys-cli", "/usr/bin"} {
		got, err := ResolveCLI(cliTestEnv(EnvCLI, setting, "PATH", "/usr/bin"), "/install/vibesysd", files)
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
		got, err := ResolveCLI(cliTestEnv("PATH", path), "/inst/vibesysd", files)
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
	got, err := ResolveCLI(cliTestEnv("PATH", "/usr/bin:relative"), "/install/vibesysd", fakeFiles{"relative/vibesys-cli": true})
	if err == nil || got != "" || !strings.Contains(err.Error(), EnvCLI) {
		t.Fatalf("got %q, %v, want an error naming %s", got, err, EnvCLI)
	}
}

// Whatever environment the daemon itself runs in, a sidecar's holds exactly the CLI the daemon
// chose under the name, or no value at all when it chose none, and everything else is the daemon's.
func TestASidecarsEnvironmentHoldsOnlyTheChosenCLI(t *testing.T) {
	names := []string{"PATH=/usr/bin", "HOME=/h", "VIBESYS_CLI=/old/vibesys-cli", "VIBESYS_CLIX=1", "VIBESYS_CLI=/older", "TOKEN=VIBESYS_CLI=x"}
	for seed := uint64(1); seed <= 200; seed++ {
		rng := rand.New(rand.NewPCG(seed, 5))
		var base []string
		for _, kv := range names {
			if rng.IntN(2) == 0 {
				base = append(base, kv)
			}
		}
		launch := map[string]string{"VIBESYS_ORCHESTRATOR_TOKEN": "secret"}
		chosen := ""
		if rng.IntN(2) == 0 {
			chosen = "/opt/vibesys-cli"
			launch[daemon.EnvCLI] = chosen
		}
		got := sessionEnv(base, launch, daemon.EnvCLI)
		var values, others []string
		for _, kv := range got {
			if v, ok := strings.CutPrefix(kv, daemon.EnvCLI+"="); ok {
				values = append(values, v)
			} else {
				others = append(others, kv)
			}
		}
		var wantOthers []string
		for _, kv := range base {
			if !strings.HasPrefix(kv, daemon.EnvCLI+"=") {
				wantOthers = append(wantOthers, kv)
			}
		}
		if chosen == "" && len(values) != 0 || chosen != "" && !slices.Equal(values, []string{chosen}) {
			t.Fatalf("seed %d: %v gave %v under the name, chosen %q", seed, base, values, chosen)
		}
		if !slices.Equal(others, wantOthers) {
			t.Fatalf("seed %d: the rest of the environment changed: %v, want %v", seed, others, wantOthers)
		}
		for _, kv := range got {
			if strings.Contains(kv, "secret") {
				t.Fatalf("seed %d: the launch environment's token reached the sidecar's: %v", seed, got)
			}
		}
	}
}

// The process that starts a sidecar carries that environment.
func TestTheSidecarProcessCarriesTheChosenCLI(t *testing.T) {
	t.Setenv(daemon.EnvCLI, "/inherited/vibesys-cli")
	s := &ProcessSidecars{Root: t.TempDir(), Command: []string{"python", "-m", "vs_sidecar"}, Log: slog.New(slog.DiscardHandler)}
	cmd := s.command(context.Background(), "/s/a/1", daemon.LaunchEnv{daemon.EnvCLI: "/opt/vibesys-cli"})
	var got []string
	for _, kv := range cmd.Env {
		if v, ok := strings.CutPrefix(kv, daemon.EnvCLI+"="); ok {
			got = append(got, v)
		}
	}
	if !slices.Equal(got, []string{"/opt/vibesys-cli"}) {
		t.Fatalf("the sidecar's environment holds %v under %s, want only the chosen build", got, daemon.EnvCLI)
	}
}
