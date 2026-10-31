// Package discovery defines the contract between manifest adapters and the
// repository graph. Adapters parse files under root and return components; they
// do not select jobs or walk dependency edges.
package discovery

import (
	"fmt"
	"strings"
)

// Component is a graph node emitted by an adapter or declared in policy.
// Paths are slash-separated and relative to the repository root. DependsOn
// contains component IDs, including any adapter-specific ID prefix.
type Component struct {
	ID    string `toml:"id"`
	Class string `toml:"class"`
	Root  string `toml:"root"`
	// Roots and Files are the paths that select this component's jobs. In policy
	// they are written as `roots` and `files`, each entry a string or a table
	// (see Entry); `add` splits them into these and the key-only lists.
	Roots       []string `toml:"-"`
	Files       []string `toml:"-"`
	RootEntries []Entry  `toml:"roots"`
	FileEntries []Entry  `toml:"files"`
	// KeyOnlyRoots and KeyOnlyFiles are entries written `{path = "...", selects = false}`.
	// The component owns them for whoever reads its list (a content key), but a
	// change under one selects no job, in CI or in `repoctl test`.
	KeyOnlyRoots         []string `toml:"-"`
	KeyOnlyFiles         []string `toml:"-"`
	DependsOn            []string `toml:"depends_on"`
	Job                  string   `toml:"job"`
	Jobs                 []string `toml:"jobs"`
	Language             string   `toml:"language"`
	Name                 string   `toml:"name"`
	Selected             bool     `toml:"selected"`
	Target               bool     `toml:"target"`
	OwnershipGroup       string   `toml:"ownership_group"`
	SelectAllJobs        bool     `toml:"select_all_jobs"`
	SelectAllCollections bool     `toml:"select_all_collections"`
	// LocalJobs replaces SelectAllJobs and SelectAllCollections in `repoctl test`
	// (never in `plan`, which CI uses): the component selects only these jobs there.
	LocalJobs []string `toml:"local_jobs"`
	// LocalIgnoreRoots are roots CI still owns. `repoctl test` treats a change
	// under one of them as ignored, so it selects no job from this component.
	// Each entry must also be one of Roots.
	LocalIgnoreRoots []string `toml:"local_ignore_roots"`
}

// Entry is one path of a component's roots or files in policy: a string, which
// selects, or a table `{path = "dir", selects = false}`, which the component
// lists without selecting anything from it. One list carries both tiers, so a
// path is never written twice.
type Entry struct {
	Path    string
	KeyOnly bool
}

// UnmarshalTOML accepts a string or a table with exactly the keys path and
// selects, and rejects anything else naming what it found.
func (e *Entry) UnmarshalTOML(data any) error {
	switch value := data.(type) {
	case string:
		*e = Entry{Path: value}
	case map[string]any:
		path, ok := value["path"].(string)
		if !ok || path == "" {
			return fmt.Errorf("a roots or files entry written as a table needs a string path, got %v", value)
		}
		for key := range value {
			if key != "path" && key != "selects" {
				return fmt.Errorf("entry %q has unknown key %q; only path and selects are allowed", path, key)
			}
		}
		selects := true
		if raw, present := value["selects"]; present {
			if selects, ok = raw.(bool); !ok {
				return fmt.Errorf("entry %q: selects must be true or false, got %v", path, raw)
			}
		}
		*e = Entry{Path: path, KeyOnly: !selects}
	default:
		return fmt.Errorf("a roots or files entry must be a string or a table, got %T", data)
	}
	return nil
}

// Spec is one policy discovery entry. Adapter selects the parser. Class,
// IDPrefix, Job, and OwnershipGroup describe every emitted component. The
// remaining fields configure individual parsers; each adapter validates the
// fields it consumes and ignores fields owned by other adapters.
type Spec struct {
	// Common output fields.
	Adapter        string `toml:"adapter"`
	Class          string `toml:"class"`
	IDPrefix       string `toml:"id_prefix"`
	Job            string `toml:"job"`
	OwnershipGroup string `toml:"ownership_group"`
	// Python: Tach configuration path and optional source root override.
	Config      string   `toml:"config"`
	SourceRoots []string `toml:"source_roots"`
	// TypeScript: package.json glob, local dependency fields, and workspace
	// version prefix used to identify local edges.
	ManifestGlob     string   `toml:"manifest_glob"`
	DependencyFields []string `toml:"dependency_fields"`
	WorkspacePrefix  string   `toml:"workspace_prefix"`
	// Native: manifest filename to language mapping, roots requiring CI
	// selection, and jobs assigned to those selected roots.
	Manifests     map[string]string `toml:"manifests"`
	ScopeRoots    []string          `toml:"scope_roots"`
	SelectedRoots []string          `toml:"selected_roots"`
	SelectedJobs  []string          `toml:"selected_jobs"`
	Target        bool              `toml:"target"`
}

// Adapter reads one manifest format from root. It returns components in a
// stable order or an error that identifies invalid configuration or input.
type Adapter interface {
	Discover(root string, spec Spec) ([]Component, error)
}

// RunCommand allows an adapter to invoke an external command in root. The
// caller owns command timeouts and error reporting.
type RunCommand func(root, program string, args ...string) ([]byte, error)

// ValidPath reports whether s is a safe slash-separated repository path.
func ValidPath(s string) bool {
	if s == "" || s[0] == '/' {
		return false
	}
	for _, part := range strings.Split(s, "/") {
		if part == "." || part == ".." || part == "" {
			return false
		}
	}
	return true
}

// Under includes root itself and all descendants of root.
func Under(path, root string) bool { return path == root || strings.HasPrefix(path, root+"/") }

// Contains reports exact membership in a string slice.
func Contains(items []string, item string) bool {
	for _, x := range items {
		if x == item {
			return true
		}
	}
	return false
}
