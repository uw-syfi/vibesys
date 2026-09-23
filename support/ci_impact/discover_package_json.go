package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type packageJSONAdapter struct{}

func (packageJSONAdapter) Discover(root string, spec discoverySpec) ([]component, error) {
	if spec.ManifestGlob == "" || spec.WorkspacePrefix == "" {
		return nil, fmt.Errorf("package_json discovery requires manifest_glob and workspace_prefix")
	}
	if filepath.IsAbs(spec.ManifestGlob) || !validPath(filepath.ToSlash(spec.ManifestGlob)) {
		return nil, fmt.Errorf("unsafe manifest glob %q", spec.ManifestGlob)
	}

	files, err := filepath.Glob(filepath.Join(root, filepath.FromSlash(spec.ManifestGlob)))
	if err != nil {
		return nil, err
	}
	sort.Strings(files)

	dependencyFields := spec.DependencyFields
	if len(dependencyFields) == 0 {
		dependencyFields = []string{"dependencies", "devDependencies", "optionalDependencies"}
	}
	components := make([]component, 0, len(files))
	for _, file := range files {
		raw, err := os.ReadFile(file)
		if err != nil {
			return nil, err
		}
		var manifest map[string]json.RawMessage
		if err := json.Unmarshal(raw, &manifest); err != nil {
			return nil, fmt.Errorf("%s: %w", file, err)
		}
		var name string
		if err := json.Unmarshal(manifest["name"], &name); err != nil || name == "" {
			return nil, fmt.Errorf("%s: missing package name", file)
		}

		dependencies := []string{}
		for _, field := range dependencyFields {
			var declared map[string]string
			if len(manifest[field]) == 0 {
				continue
			}
			if err := json.Unmarshal(manifest[field], &declared); err != nil {
				return nil, fmt.Errorf("%s: invalid %s: %w", file, field, err)
			}
			for dependency, version := range declared {
				if strings.HasPrefix(version, spec.WorkspacePrefix) {
					dependencies = append(dependencies, spec.IDPrefix+dependency)
				}
			}
		}
		sort.Strings(dependencies)

		relativePath, err := filepath.Rel(root, filepath.Dir(file))
		if err != nil {
			return nil, err
		}
		components = append(components, component{
			ID:             spec.IDPrefix + name,
			Class:          spec.Class,
			Name:           name,
			Roots:          []string{filepath.ToSlash(relativePath)},
			DependsOn:      dependencies,
			Job:            spec.Job,
			OwnershipGroup: spec.OwnershipGroup,
		})
	}
	return components, nil
}
