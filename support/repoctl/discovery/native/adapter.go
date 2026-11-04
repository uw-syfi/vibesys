// Package native discovers configured Go and Rust manifest directories.
package native

import (
	"bytes"
	"fmt"
	"path/filepath"
	"repoctl/discovery"
	"sort"
	"strings"
)

type Adapter struct{ Run discovery.RunCommand }

func (a Adapter) Discover(root string, spec discovery.Spec) ([]discovery.Component, error) {
	if len(spec.Manifests) == 0 {
		return nil, fmt.Errorf("manifest_directories discovery requires manifests")
	}
	for manifest, language := range spec.Manifests {
		if filepath.Base(manifest) != manifest || language == "" {
			return nil, fmt.Errorf("invalid manifest mapping %q=%q", manifest, language)
		}
	}
	excluded := make([]string, 0, len(spec.ExcludedRoots))
	for excludedRoot, reason := range spec.ExcludedRoots {
		if strings.TrimSpace(reason) == "" {
			return nil, fmt.Errorf("excluded root %q needs a reason", excludedRoot)
		}
		if discovery.Contains(spec.SelectedRoots, excludedRoot) {
			return nil, fmt.Errorf("root %q is both selected and excluded", excludedRoot)
		}
		excluded = append(excluded, excludedRoot)
	}
	sort.Strings(excluded)
	for _, targetRoot := range append(append(append([]string{}, spec.ScopeRoots...), spec.SelectedRoots...), excluded...) {
		if !discovery.ValidPath(targetRoot) {
			return nil, fmt.Errorf("unsafe target root %q", targetRoot)
		}
	}

	args := []string{"ls-files", "-z", "--cached", "--others", "--exclude-standard"}
	for manifest := range spec.Manifests {
		args = append(args, "**/"+manifest)
	}
	output, err := a.Run(root, "git", args...)
	if err != nil {
		return nil, err
	}

	languageByRoot := map[string]string{}
	manifestByRoot := map[string]string{}
	for _, rawPath := range bytes.Split(output, []byte{0}) {
		if len(rawPath) == 0 {
			continue
		}
		path := string(rawPath)
		if !discovery.ValidPath(path) {
			return nil, fmt.Errorf("git returned unsafe manifest path %q", path)
		}
		manifest := filepath.Base(path)
		language, configured := spec.Manifests[manifest]
		if !configured {
			continue
		}

		targetRoot := filepath.ToSlash(filepath.Dir(path))
		if targetRoot == "." {
			return nil, fmt.Errorf("manifest at repository root is unsupported")
		}
		if previous, exists := manifestByRoot[targetRoot]; exists && previous != manifest {
			return nil, fmt.Errorf("directory %q has multiple configured manifests: %s and %s", targetRoot, previous, manifest)
		}
		manifestByRoot[targetRoot] = manifest
		languageByRoot[targetRoot] = language
	}

	for _, selectedRoot := range spec.SelectedRoots {
		if _, found := languageByRoot[selectedRoot]; !found {
			return nil, fmt.Errorf("selected root has no configured manifest: %s", selectedRoot)
		}
	}

	for _, excludedRoot := range excluded {
		if _, found := languageByRoot[excludedRoot]; !found {
			return nil, fmt.Errorf("excluded root has no configured manifest: %s", excludedRoot)
		}
	}

	components := make([]discovery.Component, 0, len(languageByRoot))
	for targetRoot, language := range languageByRoot {
		selected := discovery.Contains(spec.SelectedRoots, targetRoot)
		inScope := false
		for _, scopeRoot := range spec.ScopeRoots {
			if discovery.Under(targetRoot, scopeRoot) {
				inScope = true
				break
			}
		}
		if inScope && !selected && spec.ExcludedRoots[targetRoot] == "" {
			return nil, fmt.Errorf("manifest root %q is in scope but is neither selected for CI nor excluded with a reason", targetRoot)
		}

		jobs := []string{}
		if selected {
			jobs = spec.SelectedJobs
		}
		components = append(components, discovery.Component{
			ID:             spec.IDPrefix + targetRoot,
			Class:          spec.Class,
			Name:           targetRoot,
			Roots:          []string{targetRoot},
			Jobs:           jobs,
			Language:       language,
			Selected:       selected,
			Target:         spec.Target,
			OwnershipGroup: spec.OwnershipGroup,
		})
	}
	sort.Slice(components, func(i, j int) bool {
		return components[i].ID < components[j].ID
	})
	return components, nil
}
