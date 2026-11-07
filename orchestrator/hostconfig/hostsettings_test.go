package hostconfig_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
)

// corpus is the cases the Python engine's reader of the same file passes too.
const corpus = "../../tests/corpus/host_settings/cases.json"

type settingsCase struct {
	Content string   `json:"content"`
	Allowed []string `json:"allowed"`
	Error   string   `json:"error"`
}

func settingsCases(t *testing.T) map[string]settingsCase {
	t.Helper()
	data, err := os.ReadFile(corpus)
	if err != nil {
		t.Fatal(err)
	}
	var cases map[string]settingsCase
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatal("the corpus is empty")
	}
	return cases
}

func home(t *testing.T, content string) string {
	t.Helper()
	home := t.TempDir()
	path := hostconfig.HostSettingsPath(home)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return home
}

// Every case of the corpus reads as the Python reader reads it: the same models, or an error that
// names the same key or file.
func TestTheHostSettingsReaderAgreesWithTheCorpus(t *testing.T) {
	for name, c := range settingsCases(t) {
		t.Run(name, func(t *testing.T) {
			got, err := hostconfig.LoadAllowedModels(home(t, c.Content))
			if c.Error != "" {
				if err == nil || !strings.Contains(err.Error(), c.Error) {
					t.Fatalf("got %v, %v; want an error naming %q", got, err, c.Error)
				}
				return
			}
			if err != nil || !slices.Equal(got, c.Allowed) {
				t.Fatalf("got %v, %v; want %v", got, err, c.Allowed)
			}
		})
	}
}

// No file means no policy, and no policy is the one way a host allows every model.
func TestAMissingHostSettingsFileAllowsEveryModel(t *testing.T) {
	got, err := hostconfig.LoadAllowedModels(t.TempDir())
	if err != nil || len(got) != 0 {
		t.Fatalf("got %v, %v; want no models and no error", got, err)
	}
}
