// Package python discovers Python modules from a Tach manifest.
package python

import (
	"fmt"
	"os"
	"path/filepath"
	"repoctl/discovery"
	"strings"

	"github.com/BurntSushi/toml"
)

type tachManifest struct {
	SourceRoots []string `toml:"source_roots"`
	Modules     []struct {
		Path      string   `toml:"path"`
		DependsOn []string `toml:"depends_on"`
	} `toml:"modules"`
}

type Adapter struct{}

func (Adapter) Discover(root string, spec discovery.Spec) ([]discovery.Component, error) {
	var manifest tachManifest
	if spec.Config == "" || !discovery.ValidPath(spec.Config) {
		return nil, fmt.Errorf("tach discovery requires a safe config path")
	}
	configPath := filepath.Join(root, filepath.FromSlash(spec.Config))
	if _, err := toml.DecodeFile(configPath, &manifest); err != nil {
		return nil, err
	}

	sourceRoots := spec.SourceRoots
	if len(sourceRoots) == 0 {
		sourceRoots = manifest.SourceRoots
	}
	for _, sourceRoot := range sourceRoots {
		if !discovery.ValidPath(sourceRoot) {
			return nil, fmt.Errorf("unsafe source root %q", sourceRoot)
		}
	}

	components := make([]discovery.Component, 0, len(manifest.Modules))
	for _, module := range manifest.Modules {
		modulePath := strings.ReplaceAll(module.Path, ".", "/")
		if modulePath == "" || !discovery.ValidPath(modulePath) {
			return nil, fmt.Errorf("invalid module path %q", module.Path)
		}

		matches := []string{}
		for _, sourceRoot := range sourceRoots {
			relativePath := filepath.ToSlash(filepath.Join(sourceRoot, modulePath))
			if info, err := os.Stat(filepath.Join(root, relativePath)); err == nil && info.IsDir() {
				matches = append(matches, relativePath)
			} else if info, err := os.Stat(filepath.Join(root, relativePath+".py")); err == nil && !info.IsDir() {
				matches = append(matches, relativePath+".py")
			}
		}
		if len(matches) != 1 {
			return nil, fmt.Errorf("module %q has %d source paths", module.Path, len(matches))
		}

		dependencies := make([]string, 0, len(module.DependsOn))
		for _, dependency := range module.DependsOn {
			dependencies = append(dependencies, spec.IDPrefix+dependency)
		}
		components = append(components, discovery.Component{
			ID:             spec.IDPrefix + module.Path,
			Class:          spec.Class,
			Name:           module.Path,
			Roots:          matches,
			DependsOn:      dependencies,
			Job:            spec.Job,
			OwnershipGroup: spec.OwnershipGroup,
		})
	}
	return components, nil
}
