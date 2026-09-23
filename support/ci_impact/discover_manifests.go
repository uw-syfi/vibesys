package main

import (
	"bytes"
	"fmt"
	"path/filepath"
	"sort"
)

type manifestDirectoryAdapter struct{}

func (manifestDirectoryAdapter) Discover(root string, spec discoverySpec) ([]component, error) {
	if len(spec.Manifests) == 0 {
		return nil, fmt.Errorf("manifest_directories discovery requires manifests")
	}
	for manifest, language := range spec.Manifests {
		if filepath.Base(manifest) != manifest || language == "" {
			return nil, fmt.Errorf("invalid manifest mapping %q=%q", manifest, language)
		}
	}
	for _, targetRoot := range append(append([]string{}, spec.ScopeRoots...), spec.SelectedRoots...) {
		if !validPath(targetRoot) {
			return nil, fmt.Errorf("unsafe target root %q", targetRoot)
		}
	}

	args := []string{"ls-files", "-z", "--cached", "--others", "--exclude-standard"}
	for manifest := range spec.Manifests {
		args = append(args, "**/"+manifest)
	}
	output, err := run(root, "git", args...)
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
		if !validPath(path) {
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

	components := make([]component, 0, len(languageByRoot))
	for targetRoot, language := range languageByRoot {
		selected := contains(spec.SelectedRoots, targetRoot)
		inScope := false
		for _, scopeRoot := range spec.ScopeRoots {
			if under(targetRoot, scopeRoot) {
				inScope = true
				break
			}
		}
		if inScope && !selected {
			return nil, fmt.Errorf("manifest root %q is in scope but has no selected CI target", targetRoot)
		}

		jobs := []string{}
		if selected {
			jobs = spec.SelectedJobs
		}
		components = append(components, component{
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
