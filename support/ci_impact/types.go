package main

import "fmt"

func validOutputName(s string) bool {
	if s == "" {
		return false
	}
	for i, r := range s {
		valid := r == '_' || r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z'
		if i > 0 && (r == '-' || r >= '0' && r <= '9') {
			valid = true
		}
		if !valid {
			return false
		}
	}
	return true
}

type component struct {
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
type edge struct {
	From string `toml:"from"`
	To   string `toml:"to"`
}
type discoverySpec struct {
	Adapter          string            `toml:"adapter"`
	Config           string            `toml:"config"`
	Class            string            `toml:"class"`
	IDPrefix         string            `toml:"id_prefix"`
	Job              string            `toml:"job"`
	OwnershipGroup   string            `toml:"ownership_group"`
	SourceRoots      []string          `toml:"source_roots"`
	ManifestGlob     string            `toml:"manifest_glob"`
	DependencyFields []string          `toml:"dependency_fields"`
	WorkspacePrefix  string            `toml:"workspace_prefix"`
	Manifests        map[string]string `toml:"manifests"`
	ScopeRoots       []string          `toml:"scope_roots"`
	SelectedRoots    []string          `toml:"selected_roots"`
	SelectedJobs     []string          `toml:"selected_jobs"`
	Target           bool              `toml:"target"`
}
type collectionSpec struct {
	Name         string `toml:"name"`
	Class        string `toml:"class"`
	Field        string `toml:"field"`
	SelectedOnly bool   `toml:"selected_only"`
}
type nativeChecks struct {
	Commands       map[string][][]string `toml:"commands"`
	TimeoutSeconds int                   `toml:"timeout_seconds"`
}
type nativeAssertion struct {
	File       string `toml:"file"`
	LinePrefix string `toml:"line_prefix"`
	Equals     string `toml:"equals"`
}
type nativeCheckOverride struct {
	Root       string            `toml:"root"`
	Env        map[string]string `toml:"env"`
	Assertions []nativeAssertion `toml:"assertions"`
}
type policy struct {
	DefaultBase          string                `toml:"default_base"`
	Jobs                 []string              `toml:"jobs"`
	IgnoredRoots         []string              `toml:"ignored_roots"`
	IgnoredFiles         []string              `toml:"ignored_files"`
	Discoveries          []discoverySpec       `toml:"discoveries"`
	Collections          []collectionSpec      `toml:"collections"`
	NativeChecks         nativeChecks          `toml:"native_checks"`
	NativeCheckOverrides []nativeCheckOverride `toml:"native_check_overrides"`
	Components           []component           `toml:"components"`
	Edges                []edge                `toml:"edges"`
}
type nativeTarget struct {
	ComponentID string
	Root        string
	Language    string
	Selected    bool
}
type graph struct {
	DefaultBase          string
	Components           map[string]component
	Order                []string
	Jobs                 []string
	IgnoredRoots         []string
	IgnoredFiles         []string
	Collections          []collectionSpec
	NativeTargets        map[string]nativeTarget
	NativeChecks         nativeChecks
	NativeCheckOverrides map[string]nativeCheckOverride
}
type plan struct {
	Jobs         map[string]bool     `json:"jobs"`
	Components   map[string][]string `json:"components"`
	JobReasons   map[string][]string `json:"job_reasons"`
	ChangedPaths []string            `json:"changed_paths"`
	Collections  map[string][]string `json:"collections"`
}

func validPath(s string) bool {
	if s == "" || s[0] == '/' {
		return false
	}
	for _, part := range splitPath(s) {
		if part == "." || part == ".." || part == "" {
			return false
		}
	}
	return true
}
func unique(items []string, label string) error {
	seen := map[string]bool{}
	for _, item := range items {
		if item == "" || seen[item] {
			return fmt.Errorf("%s has an empty or duplicate entry %q", label, item)
		}
		seen[item] = true
	}
	return nil
}
func contains(items []string, item string) bool {
	for _, x := range items {
		if x == item {
			return true
		}
	}
	return false
}
