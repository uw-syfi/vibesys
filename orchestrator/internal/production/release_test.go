package production

import (
	"context"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/release"
	"github.com/uw-syfi/vibesys/orchestrator/release/releasetest"
)

// developerBuild is the Finder of a host that stands in no release.
var developerBuild release.Finder = func() (*release.Release, error) {
	return nil, &release.NotInstalledError{Program: "/home/u/.local/bin/vibesys-host", Manifest: "/home/u/.local/release.json", Err: fs.ErrNotExist}
}

// memoryRelease is a complete installed release at dir, in memory, without the files named in
// missing (relative to the release).
func memoryRelease(t *testing.T, dir string, missing ...string) release.Finder {
	t.Helper()
	tree := releasetest.Tree(t, dir, releasetest.Without(missing...))
	return func() (*release.Release, error) { return release.Open(dir, tree) }
}

// diskRelease lays a release out in a folder of the real file system, with the given marketplace
// file, and returns its folder and Finder.
func diskRelease(t *testing.T, marketplaceJSON string) (string, release.Finder) {
	t.Helper()
	dir := t.TempDir()
	releasetest.Write(t, dir, releasetest.Content("marketplace/.claude-plugin/marketplace.json", marketplaceJSON))
	return dir, func() (*release.Release, error) { return release.OpenDir(dir) }
}

// Every combination of the sidecar setting and the release: a set value is used as it is and never
// replaced by the release; an unset one falls to the release; and without either the error names
// the setting and the file that is missing.
func TestTheSidecarCommandIsTheSettingThenTheReleaseThenAnError(t *testing.T) {
	checkout := t.TempDir()
	if err := os.WriteFile(filepath.Join(checkout, "pyproject.toml"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	python := "/opt/vs/sidecar/.venv/bin/python"
	cases := []struct {
		name    string
		setting string
		find    release.Finder
		want    []string
		wantErr []string
	}{
		{"the release when unset", "", memoryRelease(t, "/opt/vs"), []string{python, "-m", "vs_sidecar"}, nil},
		{"blank counts as unset", "  ", memoryRelease(t, "/opt/vs"), []string{python, "-m", "vs_sidecar"}, nil},
		{"a command wins over the release", "my-sidecar --flag", memoryRelease(t, "/opt/vs"), []string{"my-sidecar", "--flag"}, nil},
		{"a checkout wins over the release", checkout, memoryRelease(t, "/opt/vs"), []string{"uv", "run", "--frozen", "--quiet", "--project", checkout, "python", "-m", "vs_sidecar"}, nil},
		{"a wrong checkout is not replaced by the release", t.TempDir(), memoryRelease(t, "/opt/vs"), nil, []string{SettingSessionHolder, "pyproject.toml"}},
		{"unset, a release without its environment", "", memoryRelease(t, "/opt/vs", "sidecar/.venv/bin/python"), nil, []string{SettingSessionHolder, "names no", "sidecar/.venv/bin/python"}},
		{"unset, a host in no release", "", developerBuild, nil, []string{SettingSessionHolder, "names no", "/home/u/.local/release.json"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := sidecarCommand(c.setting, c.find)
			if len(c.wantErr) == 0 {
				if err != nil || !slices.Equal(got, c.want) {
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
// skills, then an error naming both. What is in the marketplace file is for the harness to read.
func TestTheMarketplaceIsTheSettingThenTheReleaseThenAnError(t *testing.T) {
	override := marketplace(t, good)
	root, find := diskRelease(t, good)

	t.Run("the release when unset", func(t *testing.T) {
		got, err := PluginFor("", find)
		if want := (Plugin{Marketplace: filepath.Join(root, "marketplace"), Name: "vibesys"}); err != nil || got != want {
			t.Fatalf("got %+v, %v, want %+v", got, err, want)
		}
	})
	t.Run("the setting wins over the release", func(t *testing.T) {
		got, err := PluginFor(override, find)
		if want := (Plugin{Marketplace: override, Name: "vibesys"}); err != nil || got != want {
			t.Fatalf("got %+v, %v, want %+v", got, err, want)
		}
	})
	t.Run("a wrong setting is not replaced by the release", func(t *testing.T) {
		_, err := PluginFor("relative", find)
		if err == nil || !strings.Contains(err.Error(), SettingSkills) || !strings.Contains(err.Error(), "not an absolute path") {
			t.Fatalf("got %v", err)
		}
	})
	t.Run("a release without its skills names both", func(t *testing.T) {
		_, err := PluginFor("", memoryRelease(t, "/opt/vs", "marketplace/.claude-plugin/marketplace.json"))
		if err == nil || !strings.Contains(err.Error(), SettingSkills) || !strings.Contains(err.Error(), "marketplace") {
			t.Fatalf("got %v, want an error naming %s and the skills folder", err, SettingSkills)
		}
	})
	t.Run("a host in no release has no skills", func(t *testing.T) {
		_, err := PluginFor("", developerBuild)
		if err == nil || !strings.Contains(err.Error(), SettingSkills) {
			t.Fatalf("got %v", err)
		}
	})
}

// A host installed in a release starts with no setting at all; one outside a release refuses
// every launch with the reason, naming the setting and the file it looked for.
func TestAHostWithNoSettingsLaunchesFromItsReleaseOrRefusesWithTheReason(t *testing.T) {
	root, find := diskRelease(t, good)
	log := slog.New(slog.NewTextHandler(io.Discard, nil))

	sidecars, err := SidecarsFor(Parts{Find: find}, t.TempDir(), filepath.Join(root, "bin", "vibesys-host"), log)
	if err != nil {
		t.Fatal(err)
	}
	python := filepath.Join(root, "sidecar", ".venv", "bin", "python")
	command, _, err := sidecars.Parts.SessionHolder()
	plugin, _, perr := sidecars.Parts.Skills()
	if err != nil || perr != nil || strings.Join(command, " ") != python+" -m vs_sidecar" || plugin.Marketplace != filepath.Join(root, "marketplace") {
		t.Fatalf("got %q, %v and %+v, %v; want the release's sidecar and marketplace", command, err, plugin, perr)
	}

	outside, err := SidecarsFor(Parts{Find: developerBuild}, t.TempDir(), "/home/u/.local/bin/vibesys-host", log)
	if err != nil {
		t.Fatal(err)
	}
	_, err = outside.Launch(context.Background(), daemon.SessionStart{})
	if err == nil || !strings.Contains(err.Error(), SettingSessionHolder) || !strings.Contains(err.Error(), "/home/u/.local/release.json") {
		t.Fatalf("Launch = %v, want a refusal naming %s and the manifest looked for", err, SettingSessionHolder)
	}
}
