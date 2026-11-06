package gatewayroot_test

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
	"github.com/uw-syfi/vibesys/orchestrator/release/releasetest"
)

// getenv answers from a fixed set of settings.
func getenv(settings map[string]string) func(string) string {
	return func(k string) string { return settings[k] }
}

// Over every combination the root follows the one rule: the flag wins; else VIBESYS_HOME; else where
// the program is installed; else ~/.vibesys. When the setting and the install place could differ
// and no flag decides, it is an error printing the rule, never one of the two.
func TestTheRootFollowsTheOneRuleOverEveryCombination(t *testing.T) {
	flags := []string{"", "/f/root"}
	settings := []string{"", "/s/a", "/s/b/", " /s/a "}
	installs := []string{"", "/s/a", "/i/c"}
	homes := []string{"", "/h"}
	for _, flag := range flags {
		for _, setting := range settings {
			for _, installed := range installs {
				for _, home := range homes {
					got, err := gatewayroot.For(flag, getenv(map[string]string{gatewayroot.EnvHome: setting}), home, installed)
					want := expected(flag, strings.TrimSpace(setting), installed, home)
					var refused *gatewayroot.Error
					switch {
					case want == "":
						if !errors.As(err, &refused) || !strings.Contains(err.Error(), gatewayroot.Rule) {
							t.Errorf("flag %q setting %q installed %q home %q: %q, %v; want an error printing the rule", flag, setting, installed, home, got, err)
						}
					case err != nil || got != want:
						t.Errorf("flag %q setting %q installed %q home %q: %q, %v; want %q", flag, setting, installed, home, got, err, want)
					}
				}
			}
		}
	}
}

func expected(flag, setting, installed, home string) string {
	switch {
	case flag != "":
		return flag
	case setting != "" && installed != "" && filepath.Clean(setting) != installed:
		return ""
	case setting != "":
		return filepath.Clean(setting)
	case installed != "":
		return installed
	case home == "":
		return ""
	}
	return filepath.Join(home, ".vibesys")
}

// A setting that is not an absolute path is an error naming it, never a root relative to the working
// directory.
func TestARelativeHomeSettingIsRefusedNamingIt(t *testing.T) {
	_, err := gatewayroot.For("", getenv(map[string]string{gatewayroot.EnvHome: "here"}), "/h", "")
	if err == nil || !strings.Contains(err.Error(), gatewayroot.EnvHome) || !strings.Contains(err.Error(), `"here"`) {
		t.Errorf("got %v", err)
	}
}

// The state of the old default root stays where existing hosts left it. Any other root keeps its
// state inside itself, so two roots never meet on one state directory, whatever the home is.
func TestTheStateDirectoryFollowsTheRoot(t *testing.T) {
	home := t.TempDir()
	home, _ = filepath.EvalSymlinks(home)
	none := getenv(nil)
	if got, want := gatewayroot.StateDirectory(filepath.Join(home, ".vibesys"), home, none), filepath.Join(home, ".local", "state", "vibesys", "orchestrator"); got != want {
		t.Errorf("the default root: %q, want %q", got, want)
	}
	a, b := gatewayroot.StateDirectory("/local/a", home, none), gatewayroot.StateDirectory("/local/b", home, none)
	if a != "/local/a/state" || b != "/local/b/state" {
		t.Errorf("other roots: %q, %q", a, b)
	}
}

// A home reached through a symlink and the same home reached directly are one root, whether or not
// the root directory exists yet, because a host and a client may be started from either path.
func TestTheRootIsTheSameThroughASymlinkedHome(t *testing.T) {
	real := t.TempDir()
	if resolved, err := filepath.EvalSymlinks(real); err == nil {
		real = resolved
	}
	link := filepath.Join(t.TempDir(), "home-link")
	if err := os.Symlink(real, link); err != nil {
		t.Fatal(err)
	}
	for _, exists := range []bool{false, true} {
		if exists {
			if err := os.Mkdir(filepath.Join(real, ".vibesys"), 0o700); err != nil {
				t.Fatal(err)
			}
		}
		direct, err := gatewayroot.For("", getenv(nil), real, "")
		if err != nil {
			t.Fatal(err)
		}
		through, err := gatewayroot.For("", getenv(nil), link, "")
		if err != nil {
			t.Fatal(err)
		}
		if direct != through || direct != filepath.Join(real, ".vibesys") {
			t.Errorf("root exists %v: direct %q, through the link %q", exists, direct, through)
		}
		flagged, err := gatewayroot.For(filepath.Join(link, ".vibesys"), getenv(nil), "/unused", "")
		if err != nil || flagged != direct {
			t.Errorf("root exists %v: --root through the link is %q (%v), want %q", exists, flagged, err, direct)
		}
		setting, err := gatewayroot.For("", getenv(map[string]string{gatewayroot.EnvHome: filepath.Join(link, ".vibesys")}), "/unused", direct)
		if err != nil || setting != direct {
			t.Errorf("root exists %v: %s through the link is %q (%v), want %q", exists, gatewayroot.EnvHome, setting, err, direct)
		}
	}
}

// A program in an installed release belongs to the directory that holds releases/, through whatever
// links reach it, and a bundle unpacked anywhere else belongs to nothing.
func TestAProgramIsInstalledInTheDirectoryThatHoldsItsReleases(t *testing.T) {
	home := t.TempDir()
	home, _ = filepath.EvalSymlinks(home)
	release := filepath.Join(home, "releases", "1.0.0")
	releasetest.Write(t, release)
	if err := os.Symlink(filepath.Join("releases", "1.0.0"), filepath.Join(home, "current")); err != nil {
		t.Fatal(err)
	}
	bin := t.TempDir()
	link := filepath.Join(bin, "vibesys-client")
	if err := os.Symlink(filepath.Join(home, "current", "bin", "vibesys-client"), link); err != nil {
		t.Fatal(err)
	}
	if got := gatewayroot.Installed(link); got != home {
		t.Errorf("through the links: %q, want %q", got, home)
	}
	unpacked := filepath.Join(t.TempDir(), "unpacked")
	releasetest.Write(t, unpacked)
	if got := gatewayroot.Installed(filepath.Join(unpacked, "bin", "vibesys-client")); got != "" {
		t.Errorf("an unpacked bundle: %q, want nothing", got)
	}
	if got := gatewayroot.Installed(filepath.Join(t.TempDir(), "vibesys-client")); got != "" {
		t.Errorf("a build in no release: %q, want nothing", got)
	}
}
