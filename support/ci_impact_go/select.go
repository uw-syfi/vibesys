package main

import (
	"fmt"
	"sort"
	"strings"
)

func (g graph) owners(path string) []string {
	owners := []string{}
	for _, id := range g.Order {
		c := g.Components[id]
		if contains(c.Files, path) {
			owners = append(owners, id)
			continue
		}
		for _, r := range c.Roots {
			if under(path, r) {
				owners = append(owners, id)
				break
			}
		}
	}
	for _, prefix := range []string{"python:", "pnpm:", "native:"} {
		deepest := -1
		for _, id := range owners {
			if strings.HasPrefix(id, prefix) && len(g.Components[id].Roots[0]) > deepest {
				deepest = len(g.Components[id].Roots[0])
			}
		}
		if deepest < 0 {
			continue
		}
		kept := owners[:0]
		for _, id := range owners {
			if !strings.HasPrefix(id, prefix) || len(g.Components[id].Roots[0]) == deepest {
				kept = append(kept, id)
			}
		}
		owners = kept
	}
	return owners
}
func (g graph) selectPaths(paths []string) (plan, error) {
	p := plan{Jobs: map[string]bool{}, Components: map[string][]string{}, JobReasons: map[string][]string{}, ChangedPaths: paths, NativeTargets: []string{}, NativeLanguages: []string{}, PnpmPackages: []string{}}
	for _, job := range jobs {
		p.Jobs[job] = false
		p.JobReasons[job] = []string{}
	}
	unknown := []string{}
	reasonOrder := []string{}
	for _, path := range paths {
		owners := g.owners(path)
		if len(owners) == 0 {
			ignored := contains(g.IgnoredFiles, path)
			for _, r := range g.IgnoredRoots {
				if under(path, r) {
					ignored = true
				}
			}
			if !ignored {
				unknown = append(unknown, path)
			}
			continue
		}
		for _, id := range owners {
			if _, seen := p.Components[id]; !seen {
				reasonOrder = append(reasonOrder, id)
			}
			p.Components[id] = append(p.Components[id], "changed "+path)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return p, fmt.Errorf("unowned changed paths: %s", strings.Join(unknown, ", "))
	}
	reverse := g.reverse()
	queue := append([]string{}, reasonOrder...)
	for len(queue) > 0 {
		source := queue[0]
		queue = queue[1:]
		for _, dependent := range reverse[source] {
			if _, ok := p.Components[dependent]; ok {
				continue
			}
			p.Components[dependent] = []string{fmt.Sprintf("depends on %s: %s", source, p.Components[source][0])}
			reasonOrder = append(reasonOrder, dependent)
			queue = append(queue, dependent)
		}
	}
	for _, id := range reasonOrder {
		reason, ok := p.Components[id]
		if !ok {
			continue
		}
		c := g.Components[id]
		for _, job := range c.Jobs {
			p.Jobs[job] = true
			p.JobReasons[job] = append(p.JobReasons[job], id+": "+reason[0])
		}
		if strings.HasPrefix(id, "native:") && len(c.Jobs) > 0 {
			p.NativeTargets = append(p.NativeTargets, strings.TrimPrefix(id, "native:"))
		}
		if strings.HasPrefix(id, "pnpm:") {
			p.PnpmPackages = append(p.PnpmPackages, strings.TrimPrefix(id, "pnpm:"))
		}
	}
	if _, ok := p.Components["ci_policy"]; ok {
		p.NativeTargets = []string{}
		for _, id := range g.Order {
			c := g.Components[id]
			if strings.HasPrefix(id, "native:") && len(c.Jobs) > 0 {
				p.NativeTargets = append(p.NativeTargets, strings.TrimPrefix(id, "native:"))
			}
		}
	}
	sort.Strings(p.NativeTargets)
	languages := map[string]bool{}
	for _, target := range p.NativeTargets {
		language, ok := g.NativeLanguages[target]
		if !ok {
			return p, fmt.Errorf("native target %q has no Cargo.toml or go.mod", target)
		}
		languages[language] = true
	}
	for language := range languages {
		p.NativeLanguages = append(p.NativeLanguages, language)
	}
	sort.Strings(p.NativeLanguages)
	sort.Strings(p.PnpmPackages)
	return p, nil
}
