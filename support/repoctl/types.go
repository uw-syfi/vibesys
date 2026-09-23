package main

import (
	"fmt"

	"repoctl/discovery"
	"repoctl/execution"
)

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

type component = discovery.Component
type edge struct {
	From string `toml:"from"`
	To   string `toml:"to"`
}
type discoverySpec = discovery.Spec
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
	CheckGroups          []execution.Suite     `toml:"check_groups"`
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
	CheckGroups          map[string]execution.Suite
}
type plan struct {
	Jobs         map[string]bool     `json:"jobs"`
	Components   map[string][]string `json:"components"`
	JobReasons   map[string][]string `json:"job_reasons"`
	ChangedPaths []string            `json:"changed_paths"`
	Collections  map[string][]string `json:"collections"`
}

func validPath(s string) bool {
	return discovery.ValidPath(s)
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
