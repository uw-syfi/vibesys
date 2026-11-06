package production

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

const (
	releaseHost    = "/opt/vs/bin/vibesys-host"
	releasePython  = "/opt/vs/sidecar/.venv/bin/python"
	releaseProject = "/opt/vs/sidecar/pyproject.toml"
)

func releaseFiles(paths ...string) fakeFiles {
	f := fakeFiles{}
	for _, p := range paths {
		f[p] = true
	}
	return f
}

// installedRelease lays out a release's marketplace on disk (PluginFor reads the manifest) and
// returns its root and the manifest path.
func installedRelease(t *testing.T, manifest string) (root, manifestPath string) {
	t.Helper()
	root = t.TempDir()
	manifestPath = filepath.Join(root, "marketplace", ".claude-plugin", "marketplace.json")
	if err := os.MkdirAll(filepath.Dir(manifestPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(manifestPath, []byte(manifest), 0o644); err != nil {
		t.Fatal(err)
	}
	return root, manifestPath
}

// Every combination of the sidecar setting, the release's parts and the host's place: a set value
// is used as it is and never replaced by the release; an unset one falls to the release; and
// without either the error names the setting and the path that was looked at.
func TestTheSidecarCommandIsTheSettingThenTheReleaseThenAnError(t *testing.T) {
	checkout := t.TempDir()
	if err := os.WriteFile(filepath.Join(checkout, "pyproject.toml"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	full := releaseFiles(releasePython, releaseProject)
	cases := []struct {
		name       string
		env        func(string) string
		executable string
		files      fakeFiles
		want       []string
		wantErr    []string
	}{
		{"the release when unset", cliTestEnv(), releaseHost, full, []string{releasePython, "-m", "vs_sidecar"}, nil},
		{"blank counts as unset", cliTestEnv(EnvSidecar, "  "), releaseHost, full, []string{releasePython, "-m", "vs_sidecar"}, nil},
		{"a command wins over the release", cliTestEnv(EnvSidecar, "my-sidecar --flag"), releaseHost, full, []string{"my-sidecar", "--flag"}, nil},
		{"a checkout wins over the release", cliTestEnv(EnvSidecar, checkout), releaseHost, full, []string{"uv", "run", "--frozen", "--quiet", "--project", checkout, "python", "-m", "vs_sidecar"}, nil},
		{"a wrong checkout is not replaced by the release", cliTestEnv(EnvSidecar, t.TempDir()), releaseHost, full, nil, []string{EnvSidecar, "pyproject.toml"}},
		{"unset, no python", cliTestEnv(), releaseHost, releaseFiles(releaseProject), nil, []string{EnvSidecar, "is not set", releasePython}},
		{"unset, no project file", cliTestEnv(), releaseHost, releaseFiles(releasePython), nil, []string{EnvSidecar, releasePython, releaseProject}},
		{"unset, a host built into a bin folder has no sibling layout", cliTestEnv(), "/home/u/.local/bin/vibesys-host", full, nil, []string{EnvSidecar, "/home/u/.local/sidecar/.venv/bin/python"}},
		{"unset, host not in a bin folder", cliTestEnv(), "/opt/vs/vibesys-host", full, nil, []string{EnvSidecar, "not in a release's bin folder"}},
		{"unset, host path unknown", cliTestEnv(), "", full, nil, []string{EnvSidecar, "not absolute"}},
		{"unset, host path relative", cliTestEnv(), "bin/vibesys-host", full, nil, []string{EnvSidecar, "not absolute"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := sidecarCommand(c.env, c.executable, c.files)
			if len(c.wantErr) == 0 {
				if err != nil || strings.Join(got, "\x00") != strings.Join(c.want, "\x00") {
					t.Fatalf("got %q, %v, want %q", got, err, c.want)
				}
				return
			}
			if err == nil {
				t.Fatalf("got %q, want an error with %q", got, c.wantErr)
			}
			for _, w := range c.wantErr {
				if !strings.Contains(err.Error(), w) {
					t.Errorf("error %q lacks %q", err, w)
				}
			}
		})
	}
}

// The marketplace follows the same order: the setting (checked as before), then the release's
// folder, then an error naming both. The release's manifest is validated like any other.
func TestTheMarketplaceIsTheSettingThenTheReleaseThenAnError(t *testing.T) {
	override := marketplace(t, good)
	root, manifest := installedRelease(t, good)
	host := filepath.Join(root, "bin", "vibesys-host")

	t.Run("the release when unset", func(t *testing.T) {
		got, err := PluginFor(cliTestEnv(), host, releaseFiles(manifest))
		if want := (Plugin{Marketplace: filepath.Join(root, "marketplace"), Name: "vibesys"}); err != nil || got != want {
			t.Fatalf("got %+v, %v, want %+v", got, err, want)
		}
	})
	t.Run("the setting wins over the release", func(t *testing.T) {
		got, err := PluginFor(env(override), host, releaseFiles(manifest))
		if want := (Plugin{Marketplace: override, Name: "vibesys"}); err != nil || got != want {
			t.Fatalf("got %+v, %v, want %+v", got, err, want)
		}
	})
	t.Run("a wrong setting is not replaced by the release", func(t *testing.T) {
		_, err := PluginFor(env("relative"), host, releaseFiles(manifest))
		if err == nil || !strings.Contains(err.Error(), EnvMarketplace) || !strings.Contains(err.Error(), "not an absolute path") {
			t.Fatalf("got %v", err)
		}
	})
	t.Run("a release without the manifest names both", func(t *testing.T) {
		_, err := PluginFor(cliTestEnv(), host, releaseFiles())
		if err == nil || !strings.Contains(err.Error(), EnvMarketplace) || !strings.Contains(err.Error(), manifest) {
			t.Fatalf("got %v, want an error naming %s and %s", err, EnvMarketplace, manifest)
		}
	})
	t.Run("a host outside a bin folder is not in a release", func(t *testing.T) {
		_, err := PluginFor(cliTestEnv(), filepath.Join(root, "vibesys-host"), releaseFiles(manifest))
		if err == nil || !strings.Contains(err.Error(), EnvMarketplace) {
			t.Fatalf("got %v", err)
		}
	})
	t.Run("a release whose manifest lacks the plugin is refused", func(t *testing.T) {
		bad, badManifest := installedRelease(t, `{"plugins": []}`)
		_, err := PluginFor(cliTestEnv(), filepath.Join(bad, "bin", "vibesys-host"), releaseFiles(badManifest))
		if err == nil || !strings.Contains(err.Error(), "lists no plugin named vibesys") {
			t.Fatalf("got %v", err)
		}
	})
}

// A host installed in a release starts with no setting at all; one outside a release refuses
// every launch with the reason, naming the setting and the path it looked at.
func TestAHostWithNoSettingsLaunchesFromItsReleaseOrRefusesWithTheReason(t *testing.T) {
	root, manifest := installedRelease(t, good)
	python := filepath.Join(root, "sidecar", ".venv", "bin", "python")
	files := releaseFiles(manifest, python, filepath.Join(root, "sidecar", "pyproject.toml"))
	log := slog.New(slog.NewTextHandler(io.Discard, nil))

	sidecars, err := SidecarsFor(cliTestEnv(), t.TempDir(), filepath.Join(root, "bin", "vibesys-host"), files, log)
	if err != nil {
		t.Fatal(err)
	}
	process, ok := sidecars.(*ProcessSidecars)
	if !ok || strings.Join(process.Command, " ") != python+" -m vs_sidecar" || process.Plugin.Marketplace != filepath.Join(root, "marketplace") {
		t.Fatalf("got %#v, want a launcher of the release's sidecar and marketplace", sidecars)
	}

	outside, err := SidecarsFor(cliTestEnv(), t.TempDir(), "/home/u/.local/bin/vibesys-host", files, log)
	if err != nil {
		t.Fatal(err)
	}
	_, err = outside.Launch(context.Background(), core.LaunchSession{}, nil)
	if err == nil || !strings.Contains(err.Error(), EnvSidecar) || !strings.Contains(err.Error(), "/home/u/.local/sidecar/.venv/bin/python") {
		t.Fatalf("Launch = %v, want a refusal naming %s and the path looked at", err, EnvSidecar)
	}
}
