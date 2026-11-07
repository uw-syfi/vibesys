package hostconfig

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/BurntSushi/toml"
)

// HostSettingsName is the machine-wide settings file's name. The Python engine reads the same file,
// ~/.config/vibesys/host.toml, for the same key, so the models a host allows are one fact kept in one
// place; tests/corpus/host_settings holds the cases both readers agree on.
const HostSettingsName = "host.toml"

// allowedModelsKey is the one key the file has.
const allowedModelsKey = "allowed_models"

// HostSettingsPath is where the settings of the user whose home directory is home are.
func HostSettingsPath(home string) string {
	return filepath.Join(home, ".config", "vibesys", HostSettingsName)
}

// LoadAllowedModels reads the models the host allows from the settings file in home. A missing file,
// and an empty list, allow every model: it returns none. A file that cannot be read or parsed, that
// has a key other than allowed_models, or that lists a blank model is an error naming the file or the
// key, and the caller must treat the host as allowing nothing, never as having no policy.
func LoadAllowedModels(home string) ([]string, error) {
	data, err := os.ReadFile(HostSettingsPath(home))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("%s: %w", HostSettingsName, err)
	}
	return ParseAllowedModels(string(data))
}

// ParseAllowedModels reads the settings file's text.
func ParseAllowedModels(text string) ([]string, error) {
	var document map[string]any
	if _, err := toml.Decode(text, &document); err != nil {
		return nil, fmt.Errorf("%s: %w", HostSettingsName, err)
	}
	var unknown []string
	for key := range document {
		if key != allowedModelsKey {
			unknown = append(unknown, key)
		}
	}
	slices.Sort(unknown)
	if len(unknown) > 0 {
		return nil, fmt.Errorf("%s: %s", HostSettingsName, strings.Join(unknown, ", "))
	}
	raw, ok := document[allowedModelsKey]
	if !ok {
		return nil, nil
	}
	list, ok := raw.([]any)
	if !ok {
		return nil, fmt.Errorf("%s: %s must be a list of model ids", HostSettingsName, allowedModelsKey)
	}
	var models []string
	for _, entry := range list {
		model, ok := entry.(string)
		if !ok || strings.TrimSpace(model) == "" {
			return nil, fmt.Errorf("%s: %s entries must be non-empty model ids", HostSettingsName, allowedModelsKey)
		}
		models = append(models, model)
	}
	return models, nil
}
