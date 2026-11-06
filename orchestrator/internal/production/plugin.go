package production

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// SettingSkills is the field of the host's configuration that names the local checkout of the
// published plugin marketplace: the directory that holds .claude-plugin/marketplace.json. Every agent
// session loads the vibesys plugin from it, and none of the operator's own plugins. It is a developer
// override; without it the marketplace of the release the host is installed in is used (see
// release.Release.SkillsRoot).
const SettingSkills = "skills_directory"

// PluginName is the plugin every session loads, as the marketplace names it.
const PluginName = "vibesys"

// Plugin is the plugin a launched session loads, from a local marketplace checkout.
type Plugin struct {
	// Marketplace is the absolute path of the checkout.
	Marketplace string
	// Name is the plugin's name in that marketplace.
	Name string
}

// PluginFor chooses the plugin marketplace, the setting when there is one and otherwise the
// release's, and checks the checkout holds the plugin, so a wrong path is refused when the daemon
// starts and named, instead of when an agent is launched.
func PluginFor(setting string, find release.Finder) (Plugin, error) {
	path := strings.TrimSpace(setting)
	switch {
	case path == "":
		rel, err := find()
		if err == nil {
			path, err = rel.SkillsRoot()
		}
		if err != nil {
			return Plugin{}, fmt.Errorf("the host's configuration names no %s and %w, so no agent session can start: choose the checkout of the vibesys plugin marketplace (the directory holding .claude-plugin/marketplace.json) with `vibesys-client host configure --skills-directory`", SettingSkills, err)
		}
	case !filepath.IsAbs(path):
		return Plugin{}, fmt.Errorf("%s=%s is not an absolute path", SettingSkills, path)
	}
	manifest := filepath.Join(path, ".claude-plugin", "marketplace.json")
	data, err := os.ReadFile(manifest)
	if err != nil {
		return Plugin{}, fmt.Errorf("%s=%s is not a marketplace checkout: %w", SettingSkills, path, err)
	}
	var parsed struct {
		Plugins []struct {
			Name string `json:"name"`
		} `json:"plugins"`
	}
	if err := json.Unmarshal(data, &parsed); err != nil {
		return Plugin{}, fmt.Errorf("%s=%s: %s is not a marketplace manifest: %w", SettingSkills, path, manifest, err)
	}
	names := make([]string, 0, len(parsed.Plugins))
	for _, p := range parsed.Plugins {
		names = append(names, p.Name)
	}
	if !slices.Contains(names, PluginName) {
		return Plugin{}, fmt.Errorf("%s=%s: %s lists no plugin named %s", SettingSkills, path, manifest, PluginName)
	}
	return Plugin{Marketplace: filepath.Clean(path), Name: PluginName}, nil
}
