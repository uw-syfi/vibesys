package production

import (
	"fmt"
	"path/filepath"
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

// PluginFor chooses the plugin marketplace, the setting when there is one and otherwise the release's.
// It does not read the marketplace: the agent's own harness defines that format and is its one reader,
// so one that cannot be loaded comes back as the session's start failure, naming it.
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
	return Plugin{Marketplace: filepath.Clean(path), Name: PluginName}, nil
}
