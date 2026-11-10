package hostconfig_test

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
	"testing/quick"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
	"github.com/uw-syfi/vibesys/orchestrator/internal/propcheck"
)

func configWith(t *testing.T, directory string) []byte {
	t.Helper()
	data, err := json.Marshal(map[string]string{"repositoriesDirectory": directory})
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// repositories_directory is absolute or ~-relative and nothing else: the contract refuses every other
// value naming the field, and an unknown key is still refused beside it.
func TestRepositoriesDirectoryIsAbsoluteOrHomeRelative(t *testing.T) {
	for _, ok := range []string{"/srv/repos", "~", "~/work/repos", "/"} {
		if _, err := hostconfig.Parse(configWith(t, ok)); err != nil {
			t.Errorf("%q refused: %v", ok, err)
		}
	}
	for _, bad := range []string{"repos", "./repos", "~other/repos", "~repos", " /x"} {
		if _, err := hostconfig.Parse(configWith(t, bad)); err == nil || !strings.Contains(err.Error(), "repositories_directory") {
			t.Errorf("%q: error %v, want a refusal naming repositories_directory", bad, err)
		}
	}
	if _, err := hostconfig.Parse([]byte(`{"repositoriesDirectory": "/x", "repositoriesDir": "/y"}`)); err == nil {
		t.Error("an unknown key was accepted")
	}
}

// The default is ~/.vibesys/repos, a configured directory wins, and whatever the contract accepts the
// host's directory is an absolute, clean path.
func TestTheHostsRepositoriesDirectoryIsTheConfiguredOneOrTheDefault(t *testing.T) {
	for config, want := range map[string]string{"": "/home/user/.vibesys/repos", "/srv/r": "/srv/r", "~": "/home/user", "~/r/": "/home/user/r"} {
		if got := hostconfig.RepositoriesDirectory(&pb.HostConfig{RepositoriesDirectory: config}, "/home/user"); got != want {
			t.Errorf("%q gives %q, want %q", config, got, want)
		}
	}
	if err := quick.Check(func(rest string) bool {
		for _, written := range []string{"/" + rest, "~/" + rest} {
			if _, err := hostconfig.Parse(configWith(t, written)); err != nil {
				continue
			}
			got := hostconfig.RepositoriesDirectory(&pb.HostConfig{RepositoriesDirectory: written}, "/home/user")
			if !filepath.IsAbs(got) || got != filepath.Clean(got) {
				return false
			}
		}
		return true
	}, propcheck.Config(t, nil)); err != nil {
		t.Fatal(err)
	}
}
