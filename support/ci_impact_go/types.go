package main

import "fmt"

var jobs = []string{"python", "tui", "examples", "agent_image", "evaluators", "go_prototype"}

type component struct {
	ID        string   `toml:"id"`
	Root      string   `toml:"root"`
	Roots     []string `toml:"roots"`
	Files     []string `toml:"files"`
	DependsOn []string `toml:"depends_on"`
	Job       string   `toml:"job"`
	Jobs      []string `toml:"jobs"`
}
type edge struct {
	From string `toml:"from"`
	To   string `toml:"to"`
}
type policy struct {
	IgnoredRoots       []string    `toml:"ignored_roots"`
	IgnoredFiles       []string    `toml:"ignored_files"`
	NativeCIRoots      []string    `toml:"native_ci_roots"`
	NativeCIScopeRoots []string    `toml:"native_ci_scope_roots"`
	Components         []component `toml:"components"`
	Edges              []edge      `toml:"edges"`
}
type graph struct {
	Components                 map[string]component
	Order                      []string
	IgnoredRoots, IgnoredFiles []string
	NativeLanguages            map[string]string
}
type plan struct {
	Jobs            map[string]bool     `json:"jobs"`
	Components      map[string][]string `json:"components"`
	JobReasons      map[string][]string `json:"job_reasons"`
	ChangedPaths    []string            `json:"changed_paths"`
	NativeTargets   []string            `json:"native_targets"`
	NativeLanguages []string            `json:"native_languages"`
	PnpmPackages    []string            `json:"pnpm_packages"`
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
