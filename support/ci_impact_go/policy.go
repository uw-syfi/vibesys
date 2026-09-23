package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/BurntSushi/toml"
)

func splitPath(s string) []string  { return strings.Split(s, "/") }
func under(path, root string) bool { return path == root || strings.HasPrefix(path, root+"/") }
func readPolicy(root string) (graph, error) {
	var p policy
	path := filepath.Join(root, "ci-components.toml")
	md, err := toml.DecodeFile(path, &p)
	if err != nil {
		return graph{}, err
	}
	expected := []string{"ignored_roots", "ignored_files", "native_ci_roots", "native_ci_scope_roots", "components", "edges"}
	for _, key := range expected {
		if !md.IsDefined(key) {
			return graph{}, fmt.Errorf("%s: missing key %s", path, key)
		}
	}
	if len(md.Undecoded()) > 0 {
		return graph{}, fmt.Errorf("%s: unknown keys %v", path, md.Undecoded())
	}
	g := graph{Components: map[string]component{}, IgnoredRoots: p.IgnoredRoots, IgnoredFiles: p.IgnoredFiles, NativeLanguages: map[string]string{}}
	for _, group := range [][]string{p.IgnoredRoots, p.IgnoredFiles, p.NativeCIRoots, p.NativeCIScopeRoots} {
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
	if err := g.discoverTach(root); err != nil {
		return g, err
	}
	if err := g.discoverPnpm(root); err != nil {
		return g, err
	}
	if err := g.discoverNative(root, p.NativeCIRoots, p.NativeCIScopeRoots); err != nil {
		return g, err
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
	if err := g.validate(); err != nil {
		return g, err
	}
	if err := g.validateNativeTargets(p.NativeCIRoots, p.NativeCIScopeRoots); err != nil {
		return g, err
	}
	return g, nil
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
		if !contains(jobs, job) {
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
func (g graph) validateNativeTargets(roots, scopes []string) error {
	reverse := g.reverse()
	for _, id := range g.Order {
		if !strings.HasPrefix(id, "native:") {
			continue
		}
		r := strings.TrimPrefix(id, "native:")
		inScope := false
		for _, area := range append(append([]string{}, roots...), scopes...) {
			if under(r, area) {
				inScope = true
			}
		}
		if !inScope {
			continue
		}
		seen := map[string]bool{}
		q := []string{id}
		reaches := false
		for len(q) > 0 {
			v := q[0]
			q = q[1:]
			if seen[v] {
				continue
			}
			seen[v] = true
			if contains(g.Components[v].Jobs, "evaluators") {
				reaches = true
			}
			q = append(q, reverse[v]...)
		}
		if !reaches {
			return fmt.Errorf("native manifest %q does not reach a registered CI target", r)
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

type tachFile struct {
	SourceRoots []string `toml:"source_roots"`
	Modules     []struct {
		Path      string   `toml:"path"`
		DependsOn []string `toml:"depends_on"`
	} `toml:"modules"`
}

func (g *graph) discoverTach(root string) error {
	var t tachFile
	if _, err := toml.DecodeFile(filepath.Join(root, "tach.toml"), &t); err != nil {
		return err
	}
	for _, m := range t.Modules {
		mod := strings.ReplaceAll(m.Path, ".", "/")
		matches := []string{}
		for _, sr := range t.SourceRoots {
			rel := filepath.ToSlash(filepath.Join(sr, mod))
			if s, e := os.Stat(filepath.Join(root, rel)); e == nil && s.IsDir() {
				matches = append(matches, rel)
			} else if s, e := os.Stat(filepath.Join(root, rel+".py")); e == nil && !s.IsDir() {
				matches = append(matches, rel+".py")
			}
		}
		if len(matches) != 1 {
			return fmt.Errorf("Tach module %q has %d source paths", m.Path, len(matches))
		}
		deps := []string{}
		for _, d := range m.DependsOn {
			deps = append(deps, "python:"+d)
		}
		if err := g.add(component{ID: "python:" + m.Path, Roots: matches, DependsOn: deps, Jobs: []string{"python"}}); err != nil {
			return err
		}
	}
	return nil
}
func (g *graph) discoverPnpm(root string) error {
	files, err := filepath.Glob(filepath.Join(root, "clients", "*", "package.json"))
	if err != nil {
		return err
	}
	sort.Strings(files)
	for _, file := range files {
		var p struct {
			Name                 string            `json:"name"`
			Dependencies         map[string]string `json:"dependencies"`
			DevDependencies      map[string]string `json:"devDependencies"`
			OptionalDependencies map[string]string `json:"optionalDependencies"`
		}
		raw, err := os.ReadFile(file)
		if err != nil {
			return err
		}
		if err = json.Unmarshal(raw, &p); err != nil {
			return fmt.Errorf("%s: %w", file, err)
		}
		if p.Name == "" {
			return fmt.Errorf("%s: missing package name", file)
		}
		deps := []string{}
		for _, group := range []map[string]string{p.Dependencies, p.DevDependencies, p.OptionalDependencies} {
			for name, ver := range group {
				if strings.HasPrefix(ver, "workspace:") {
					deps = append(deps, "pnpm:"+name)
				}
			}
		}
		sort.Strings(deps)
		rel, err := filepath.Rel(root, filepath.Dir(file))
		if err != nil {
			return err
		}
		if err := g.add(component{ID: "pnpm:" + p.Name, Roots: []string{filepath.ToSlash(rel)}, DependsOn: deps, Jobs: []string{"tui"}}); err != nil {
			return err
		}
	}
	return nil
}
func (g *graph) discoverNative(root string, ciRoots, scopes []string) error {
	out, err := run(root, "git", "ls-files", "--cached", "--others", "--exclude-standard", "**/Cargo.toml", "**/go.mod")
	if err != nil {
		return err
	}
	seen := map[string]bool{}
	for _, manifest := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		if manifest == "" {
			continue
		}
		r := filepath.ToSlash(filepath.Dir(manifest))
		if seen[r] {
			continue
		}
		seen[r] = true
		language := "go"
		if _, err := os.Stat(filepath.Join(root, r, "Cargo.toml")); err == nil {
			language = "rust"
		}
		g.NativeLanguages[r] = language
		selected := contains(ciRoots, r)
		if !selected {
			for _, s := range scopes {
				if under(r, s) {
					return fmt.Errorf("native evaluator %q has no registered CI target", r)
				}
			}
		}
		jobs := []string{}
		if selected {
			jobs = []string{"evaluators"}
		}
		if err := g.add(component{ID: "native:" + r, Roots: []string{r}, Jobs: jobs}); err != nil {
			return err
		}
	}
	for _, r := range ciRoots {
		if !seen[r] {
			return fmt.Errorf("native_ci_roots: no manifest at %s", r)
		}
	}
	return nil
}
