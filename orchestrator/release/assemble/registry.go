// Package assemble lays the example tasks into a release and writes the catalogue that lists them. It
// is the builder's half of package release: release defines what a release holds and reads it,
// assemble makes it.
//
// Everything it reads and writes goes through a port, so its tests build a release out of an
// in-memory repository, a Fake card reader and a Fake for the external repositories. The real
// implementations are in os.go.
package assemble

import (
	"bytes"
	"fmt"
	"path"
	"slices"
	"strings"

	"github.com/BurntSushi/toml"
)

// RegistryName is the registry of runnable examples, relative to the repository.
const RegistryName = "examples/registry.toml"

// examplesPrefix is the registry's paths' folder; an example's id is its path without it.
const examplesPrefix = "examples/"

// Entry is one example of the registry.
type Entry struct {
	// Path is the example's folder, relative to the repository: "examples/FAMILY/NAME".
	Path string
	// Tasks are the example's task names; nil is every task its folder holds.
	Tasks []string
	// External is an example whose task files and source come from another repository, which the
	// repository pins as a submodule. Only its own files are carried; the rest is cloned.
	External bool
}

// ID is the example's id: its path under the examples folder.
func (e Entry) ID() string { return strings.TrimPrefix(e.Path, examplesPrefix) }

// registryFile is the TOML of the registry, as far as the builder reads it. known_failing and skips
// are the checks' own concern.
type registryFile struct {
	Version int `toml:"version"`
	Example []struct {
		Path         string           `toml:"path"`
		Tasks        any              `toml:"tasks"`
		ExternalRepo bool             `toml:"external_repo"`
		KnownFailing []map[string]any `toml:"known_failing"`
		Skips        []map[string]any `toml:"skips"`
	} `toml:"example"`
}

// ParseRegistry reads examples/registry.toml. It refuses a key it does not know, naming it, a path
// that is not a folder under examples/, a path listed twice, and a task list that is not "all" or
// a list of names.
func ParseRegistry(data []byte) ([]Entry, error) {
	var file registryFile
	md, err := toml.NewDecoder(bytes.NewReader(data)).Decode(&file)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", RegistryName, err)
	}
	if undecoded := md.Undecoded(); len(undecoded) > 0 {
		return nil, fmt.Errorf("%s: unknown key %s", RegistryName, undecoded[0])
	}
	if file.Version != 1 {
		return nil, fmt.Errorf("%s: version %d is not one this builder reads (1)", RegistryName, file.Version)
	}
	var entries []Entry
	seen := map[string]bool{}
	for i, raw := range file.Example {
		p := raw.Path
		if !strings.HasPrefix(p, examplesPrefix) || path.Clean(p) != p || len(strings.TrimPrefix(p, examplesPrefix)) == 0 || p != strings.TrimSuffix(p, "/") {
			return nil, fmt.Errorf("%s: example %d: path %q is not a folder under %s", RegistryName, i+1, p, examplesPrefix)
		}
		if seen[p] {
			return nil, fmt.Errorf("%s: example %q is listed twice", RegistryName, p)
		}
		seen[p] = true
		e := Entry{Path: p, External: raw.ExternalRepo}
		switch tasks := raw.Tasks.(type) {
		case nil:
		case string:
			if tasks != "all" {
				return nil, fmt.Errorf("%s: example %q: tasks is %q, want \"all\" or a list of task names", RegistryName, p, tasks)
			}
		case []any:
			for _, t := range tasks {
				name, ok := t.(string)
				if !ok || name == "" {
					return nil, fmt.Errorf("%s: example %q: tasks holds %v, want task names", RegistryName, p, t)
				}
				e.Tasks = append(e.Tasks, name)
			}
			if len(e.Tasks) == 0 {
				return nil, fmt.Errorf("%s: example %q: tasks is an empty list", RegistryName, p)
			}
		default:
			return nil, fmt.Errorf("%s: example %q: tasks is %v, want \"all\" or a list of task names", RegistryName, p, tasks)
		}
		entries = append(entries, e)
	}
	slices.SortFunc(entries, func(a, b Entry) int { return strings.Compare(a.Path, b.Path) })
	return entries, nil
}
