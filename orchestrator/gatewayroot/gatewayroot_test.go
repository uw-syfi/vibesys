package gatewayroot_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
)

// getenv answers from a fixed set of settings.
func getenv(settings map[string]string) func(string) string {
	return func(k string) string { return settings[k] }
}

// A gateway root is a function of the flag and the state directory alone: the flag wins; else a
// state directory gives a root under it, one per state directory, never the default; else
// ~/.vibesys. So two state directories never share a gateway, whatever the home is.
func TestTheRootFollowsTheFlagThenTheStateDirectoryThenTheDefault(t *testing.T) {
	const stateEnv = "VIBESYS_ORCHESTRATOR_STATE_DIR"
	states := []string{"", "/s/a", "/s/b", "/s/a/", "/s/../s/a", "/h/.vibesys"}
	flags := []string{"", "/f/root", "/s/a/host"}
	homes := []string{"/h", "/other"}
	seen := map[string]string{}
	for _, state := range states {
		for _, flag := range flags {
			for _, home := range homes {
				got, err := gatewayroot.For(flag, getenv(map[string]string{stateEnv: state}), home)
				if err != nil {
					t.Fatalf("flag %q state %q home %q: %v", flag, state, home, err)
				}
				switch {
				case flag != "":
					if got != flag {
						t.Errorf("flag %q state %q: got %q, want the flag", flag, state, got)
					}
				case state == "":
					if want := filepath.Join(home, ".vibesys"); got != want {
						t.Errorf("no state, home %q: got %q, want %q", home, got, want)
					}
				default:
					if got == filepath.Join(home, ".vibesys") || !strings.HasPrefix(got, filepath.Clean(state)+"/") {
						t.Errorf("state %q home %q: root %q is not under the state directory", state, home, got)
					}
					if prev, ok := seen[filepath.Clean(state)]; ok && prev != got {
						t.Errorf("state %q: roots %q and %q for one state directory", state, prev, got)
					}
					seen[filepath.Clean(state)] = got
				}
			}
		}
	}
	if seen["/s/a"] == seen["/s/b"] {
		t.Errorf("two state directories share the root %q", seen["/s/a"])
	}
}

// A state directory that is not absolute is an error naming the setting, never a root relative to the
// working directory; with no state directory and no home there is nothing to default to.
func TestTheRootRefusesWhatItCannotPlaceWithoutGuessing(t *testing.T) {
	_, err := gatewayroot.For("", getenv(map[string]string{"VIBESYS_ORCHESTRATOR_STATE_DIR": "state"}), "/h")
	if err == nil || !strings.Contains(err.Error(), "VIBESYS_ORCHESTRATOR_STATE_DIR") {
		t.Errorf("relative state directory: %v, want an error naming the setting", err)
	}
	if _, err := gatewayroot.For("", getenv(nil), ""); err == nil {
		t.Error("no home and no state directory: want an error")
	}
	if got, err := gatewayroot.For("/f", getenv(nil), ""); err != nil || got != "/f" {
		t.Errorf("a flag needs no home: got %q, %v", got, err)
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
		direct, err := gatewayroot.For("", getenv(nil), real)
		if err != nil {
			t.Fatal(err)
		}
		through, err := gatewayroot.For("", getenv(nil), link)
		if err != nil {
			t.Fatal(err)
		}
		if direct != through || direct != filepath.Join(real, ".vibesys") {
			t.Errorf("root exists %v: direct %q, through the link %q", exists, direct, through)
		}
		flagged, err := gatewayroot.For(filepath.Join(link, ".vibesys"), getenv(nil), "/unused")
		if err != nil || flagged != direct {
			t.Errorf("root exists %v: --root through the link is %q (%v), want %q", exists, flagged, err, direct)
		}
	}
}
