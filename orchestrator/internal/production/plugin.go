package production

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// EnvMarketplace names the local checkout of the published plugin marketplace: the directory that
// holds .claude-plugin/marketplace.json. Every agent session loads the vibesys plugin from it, and
// none of the operator's own plugins. It has no default: a daemon never guesses where its skills are.
const EnvMarketplace = "VIBESYS_ORCHESTRATOR_MARKETPLACE"

// PluginName is the plugin every session loads, as the marketplace names it.
const PluginName = "vibesys"

// Plugin is the plugin a launched session loads, from a local marketplace checkout.
type Plugin struct {
	// Marketplace is the absolute path of the checkout.
	Marketplace string
	// Name is the plugin's name in that marketplace.
	Name string
}

// PluginFor reads the plugin from the environment and checks the checkout holds it, so a wrong path
// is refused when the daemon starts and named, instead of when an agent is launched.
func PluginFor(getenv func(string) string) (Plugin, error) {
	path := strings.TrimSpace(getenv(EnvMarketplace))
	switch {
	case path == "":
		return Plugin{}, fmt.Errorf("%s is not set, so no agent session can start: set it to the checkout of the vibesys plugin marketplace (the directory holding .claude-plugin/marketplace.json)", EnvMarketplace)
	case !filepath.IsAbs(path):
		return Plugin{}, fmt.Errorf("%s=%s is not an absolute path", EnvMarketplace, path)
	}
	manifest := filepath.Join(path, ".claude-plugin", "marketplace.json")
	data, err := os.ReadFile(manifest)
	if err != nil {
		return Plugin{}, fmt.Errorf("%s=%s is not a marketplace checkout: %w", EnvMarketplace, path, err)
	}
	var parsed struct {
		Plugins []struct {
			Name string `json:"name"`
		} `json:"plugins"`
	}
	if err := json.Unmarshal(data, &parsed); err != nil {
		return Plugin{}, fmt.Errorf("%s=%s: %s is not a marketplace manifest: %w", EnvMarketplace, path, manifest, err)
	}
	names := make([]string, 0, len(parsed.Plugins))
	for _, p := range parsed.Plugins {
		names = append(names, p.Name)
	}
	if !slices.Contains(names, PluginName) {
		return Plugin{}, fmt.Errorf("%s=%s: %s lists no plugin named %s", EnvMarketplace, path, manifest, PluginName)
	}
	return Plugin{Marketplace: filepath.Clean(path), Name: PluginName}, nil
}
