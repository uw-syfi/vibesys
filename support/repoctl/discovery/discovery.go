// Package discovery defines the contract between manifest adapters and the
// repository graph. Adapters parse files under root and return components; they
// do not select jobs or walk dependency edges.
package discovery

import "strings"

// Component is a graph node emitted by an adapter or declared in policy.
// Paths are slash-separated and relative to the repository root. DependsOn
// contains component IDs, including any adapter-specific ID prefix.
type Component struct {
	ID                   string   `toml:"id"`
	Class                string   `toml:"class"`
	Root                 string   `toml:"root"`
	Roots                []string `toml:"roots"`
	Files                []string `toml:"files"`
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
