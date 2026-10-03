package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/BurntSushi/toml"
	"repoctl/execution"
)

func splitPath(s string) []string  { return strings.Split(s, "/") }
func under(path, root string) bool { return path == root || strings.HasPrefix(path, root+"/") }

func readPolicy(root, configPath string) (graph, error) {
	path := configPath
	if !filepath.IsAbs(path) {
		path = filepath.Join(root, path)
	}
	p, err := loadPolicy(path)
	if err != nil {
		return graph{}, err
	}
	if err := unique(p.Jobs, "jobs"); err != nil || len(p.Jobs) == 0 {
		if err != nil {
			return graph{}, err
		}
		return graph{}, fmt.Errorf("jobs must not be empty")
	}
	if strings.TrimSpace(p.DefaultBase) == "" {
		return graph{}, fmt.Errorf("default_base must not be empty")
	}
	for _, job := range p.Jobs {
		if !validOutputName(job) {
			return graph{}, fmt.Errorf("invalid job output name %q", job)
		}
	}
	g := graph{
		Components: map[string]component{}, Jobs: p.Jobs, DefaultBase: p.DefaultBase,
		IgnoredRoots: p.IgnoredRoots, IgnoredFiles: p.IgnoredFiles,
		Collections: p.Collections, NativeTargets: map[string]nativeTarget{},
		NativeChecks:         p.NativeChecks,
		NativeCheckOverrides: map[string]nativeCheckOverride{},
		CheckGroups:          map[string]execution.Suite{},
	}
	for _, group := range [][]string{p.IgnoredRoots, p.IgnoredFiles} {
		for _, x := range group {
			if !validPath(x) {
				return g, fmt.Errorf("unsafe policy path %q", x)
			}
		}
	}
	for _, c := range p.Components {
		if err := g.add(c); err != nil {
			return g, err
		}
	}
	for _, spec := range p.Discoveries {
		adapter, ok := discoveryAdapters[spec.Adapter]
		if !ok {
			return g, fmt.Errorf("unknown discovery adapter %q", spec.Adapter)
		}
		if spec.Class == "" || spec.IDPrefix == "" {
			return g, fmt.Errorf("discovery %q requires class and id_prefix", spec.Adapter)
		}
		if spec.Job != "" && !contains(g.Jobs, spec.Job) {
			return g, fmt.Errorf("discovery %q references unknown job %q", spec.Adapter, spec.Job)
		}
		for _, job := range spec.SelectedJobs {
			if !contains(g.Jobs, job) {
				return g, fmt.Errorf("discovery %q references unknown selected job %q", spec.Adapter, job)
			}
		}
		items, err := adapter.Discover(root, spec)
		if err != nil {
			return g, fmt.Errorf("discovery %s: %w", spec.Adapter, err)
		}
		for _, c := range items {
			if err := g.add(c); err != nil {
				return g, err
			}
		}
	}
	for _, e := range p.Edges {
		if _, ok := g.Components[e.From]; !ok {
			return g, fmt.Errorf("unknown edge source %q", e.From)
		}
		c, ok := g.Components[e.To]
		if !ok {
			return g, fmt.Errorf("unknown edge target %q", e.To)
		}
		c.DependsOn = append(c.DependsOn, e.From)
		g.Components[e.To] = c
	}
	for _, collection := range g.Collections {
		if collection.Name == "" || collection.Class == "" || collection.Field == "" {
			return g, fmt.Errorf("collections require name, class, and field")
		}
		if !validOutputName(collection.Name) {
			return g, fmt.Errorf("invalid collection output name %q", collection.Name)
		}
		if !validCollectionField(collection.Field) {
			return g, fmt.Errorf("unsupported collection field %q", collection.Field)
		}
	}
	if err := g.validateCheckGroups(p.CheckGroups); err != nil {
		return g, err
	}
	if err := g.validate(); err != nil {
		return g, err
	}
	for _, id := range g.Order {
		c := g.Components[id]
		if !c.Target {
			continue
		}
		if c.Language == "" || len(c.Roots) != 1 {
			return g, fmt.Errorf("target component %q must have one root and a language", id)
		}
		r := c.Roots[0]
		g.NativeTargets[id] = nativeTarget{ComponentID: id, Root: r, Language: c.Language, Selected: c.Selected}
	}
	if err := g.validateNativeConfiguration(p.NativeCheckOverrides); err != nil {
		return g, err
	}
	if err := g.validateRunnableJobs(); err != nil {
		return g, err
	}
	return g, nil
}

var componentKeys = []string{"default_base", "jobs", "ignored_roots", "ignored_files", "discoveries", "collections", "components", "edges"}
var checkKeys = []string{"native_checks", "native_check_overrides", "check_groups"}

func loadPolicy(path string) (policy, error) {
	info, err := os.Stat(path)
	if err != nil {
		return policy{}, fmt.Errorf("configuration %q: %w", path, err)
	}
	if !info.IsDir() {
		return decodePolicyFile(path, append(append([]string{}, componentKeys...), checkKeys...), componentKeys)
	}
	componentsPath := filepath.Join(path, "components.toml")
	checksPath := filepath.Join(path, "checks.toml")
	components, err := decodePolicyFile(componentsPath, componentKeys, componentKeys)
	if err != nil {
		return policy{}, err
	}
	checks, err := decodePolicyFile(checksPath, checkKeys, nil)
	if err != nil {
		return policy{}, err
	}
	components.NativeChecks = checks.NativeChecks
	components.NativeCheckOverrides = checks.NativeCheckOverrides
	components.CheckGroups = checks.CheckGroups
	return components, nil
}

func decodePolicyFile(path string, allowed, required []string) (policy, error) {
	var p policy
	md, err := toml.DecodeFile(path, &p)
	if err != nil {
		return policy{}, err
	}
	for _, key := range required {
		if !md.IsDefined(key) {
			return policy{}, fmt.Errorf("%s: missing key %s", path, key)
		}
	}
	if len(md.Undecoded()) > 0 {
		return policy{}, fmt.Errorf("%s: unknown keys %v", path, md.Undecoded())
	}
	for _, key := range md.Keys() {
		if !contains(allowed, key[0]) {
			return policy{}, fmt.Errorf("%s: key %s belongs in another config file", path, key)
		}
	}
	return p, nil
}

func (g *graph) add(c component) error {
	if c.ID == "" {
		return fmt.Errorf("component has empty id")
	}
	if _, ok := g.Components[c.ID]; ok {
		return fmt.Errorf("duplicate component id %q", c.ID)
	}
	if c.Root != "" {
		c.Roots = append(c.Roots, c.Root)
	}
	for _, x := range append(append([]string{}, c.Roots...), c.Files...) {
		if !validPath(x) {
			return fmt.Errorf("%s has unsafe path %q", c.ID, x)
		}
	}
	for label, group := range map[string][]string{"roots": c.Roots, "files": c.Files, "dependencies": c.DependsOn, "jobs": c.Jobs} {
		if err := unique(group, c.ID+"."+label); err != nil {
			return err
		}
	}
	if c.Job != "" {
		c.Jobs = append(c.Jobs, c.Job)
	}
	for _, job := range c.Jobs {
		if !contains(g.Jobs, job) {
			return fmt.Errorf("%s has unknown job %q", c.ID, job)
		}
	}
	g.Components[c.ID] = c
	g.Order = append(g.Order, c.ID)
	return nil
}

func (g graph) validate() error {
	state := map[string]int{}
	var visit func(string) error
	visit = func(id string) error {
		if state[id] == 1 {
			return fmt.Errorf("component dependency cycle at %s", id)
		}
		if state[id] == 2 {
			return nil
		}
		state[id] = 1
		for _, dep := range g.Components[id].DependsOn {
			if _, ok := g.Components[dep]; !ok {
				return fmt.Errorf("%s: unknown dependency %q", id, dep)
			}
			if err := visit(dep); err != nil {
				return err
			}
		}
		state[id] = 2
		return nil
	}
	for _, id := range g.Order {
		if err := visit(id); err != nil {
			return err
		}
	}
	return nil
}

func (g graph) reverse() map[string][]string {
	r := map[string][]string{}
	for _, id := range g.Order {
		r[id] = []string{}
	}
	for _, id := range g.Order {
		for _, dep := range g.Components[id].DependsOn {
			r[dep] = append(r[dep], id)
		}
	}
	return r
}

func (g *graph) validateNativeConfiguration(overrides []nativeCheckOverride) error {
	if len(g.NativeTargets) > 0 {
		if g.NativeChecks.TimeoutSeconds <= 0 {
			return fmt.Errorf("native_checks.timeout_seconds must be positive")
		}
		for id, target := range g.NativeTargets {
			commands := g.NativeChecks.Commands[target.Language]
			if len(commands) == 0 {
				return fmt.Errorf("native target %q has no commands for language %q", id, target.Language)
			}
			for _, command := range commands {
				if len(command) == 0 || command[0] == "" {
					return fmt.Errorf("native_checks.commands.%s contains an empty command", target.Language)
				}
			}
		}
	}
	for _, item := range overrides {
		if !validPath(item.Root) {
			return fmt.Errorf("native_check_overrides: unsafe root %q", item.Root)
		}
		if _, ok := g.findNativeTarget(item.Root); !ok {
			return fmt.Errorf("native_check_overrides: unknown target root %q", item.Root)
		}
		if _, exists := g.NativeCheckOverrides[item.Root]; exists {
			return fmt.Errorf("native_check_overrides: duplicate root %q", item.Root)
		}
		for key := range item.Env {
			if !envKey.MatchString(key) {
				return fmt.Errorf("native_check_overrides: invalid environment key %q", key)
			}
		}
		for _, assertion := range item.Assertions {
			if !validPath(assertion.File) || assertion.LinePrefix == "" {
				return fmt.Errorf("native_check_overrides: invalid assertion for %q", item.Root)
			}
		}
		g.NativeCheckOverrides[item.Root] = item
	}
	return nil
}
func (g graph) findNativeTarget(root string) (nativeTarget, bool) {
	for _, target := range g.NativeTargets {
		if target.Root == root {
			return target, true
		}
	}
	return nativeTarget{}, false
}
