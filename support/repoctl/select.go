package main

import (
	"fmt"
	"sort"
	"strings"

	"repoctl/internal/gitrepo"
)

func appendUniquePaths(paths, added []string) []string {
	seen := make(map[string]bool, len(paths)+len(added))
	for _, path := range paths {
		seen[path] = true
	}
	for _, path := range added {
		if !seen[path] {
			paths = append(paths, path)
			seen[path] = true
		}
	}
	return paths
}

func selectChangedRecords(baseGraph, headGraph graph, changes []gitrepo.Change) (plan, error) {
	basePaths, headPaths, allPaths := []string{}, []string{}, []string{}
	for _, change := range changes {
		switch change.Status[0] {
		case 'A':
			headPaths = append(headPaths, change.Path)
			allPaths = append(allPaths, change.Path)
		case 'C':
			headPaths = append(headPaths, change.NewPath)
			allPaths = append(allPaths, change.NewPath)
		case 'D':
			basePath := change.Path
			basePaths = append(basePaths, basePath)
			allPaths = append(allPaths, basePath)
		default:
			if change.Status[0] == 'R' {
				basePaths = append(basePaths, change.OldPath)
				headPaths = append(headPaths, change.NewPath)
				allPaths = append(allPaths, change.OldPath, change.NewPath)
			} else {
				basePaths = append(basePaths, change.Path)
				headPaths = append(headPaths, change.Path)
				allPaths = append(allPaths, change.Path)
			}
		}
	}
	basePaths = appendUniquePaths(nil, basePaths)
	headPaths = appendUniquePaths(nil, headPaths)
	allPaths = appendUniquePaths(nil, allPaths)

	basePlan := plan{Jobs: map[string]bool{}}
	if len(basePaths) > 0 && len(baseGraph.Jobs) > 0 {
		var err error
		basePlan, err = baseGraph.selectPaths(basePaths)
		if err != nil {
			return plan{}, fmt.Errorf("base revision: %w", err)
		}
	}
	// A deleted path can still select the current version of its component when
	// the component remains configured, keeping current package checks runnable.
	for _, path := range basePaths {
		if len(headGraph.owners(path)) > 0 {
			headPaths = append(headPaths, path)
		}
	}
	headPaths = appendUniquePaths(nil, headPaths)
	headPlan, err := headGraph.selectPaths(headPaths)
	if err != nil {
		return plan{}, err
	}
	for job, selected := range basePlan.Jobs {
		if selected {
			if _, exists := headPlan.Jobs[job]; exists {
				headPlan.Jobs[job] = true
				for _, reason := range basePlan.JobReasons[job] {
					headPlan.JobReasons[job] = append(headPlan.JobReasons[job], "base revision: "+reason)
				}
			}
		}
	}
	headPlan.ChangedPaths = allPaths
	return headPlan, nil
}

func mergePlans(primary, additional plan) plan {
	for job, selected := range additional.Jobs {
		primary.Jobs[job] = primary.Jobs[job] || selected
		primary.JobReasons[job] = appendUniquePaths(primary.JobReasons[job], additional.JobReasons[job])
	}
	for component, reasons := range additional.Components {
		primary.Components[component] = appendUniquePaths(primary.Components[component], reasons)
	}
	for name, values := range additional.Collections {
		primary.Collections[name] = appendUniquePaths(primary.Collections[name], values)
		sort.Strings(primary.Collections[name])
	}
	primary.ChangedPaths = appendUniquePaths(primary.ChangedPaths, additional.ChangedPaths)
	return primary
}

func matchingRootLength(c component, path string) int {
	longest := -1
	for _, root := range c.Roots {
		if under(path, root) && len(root) > longest {
			longest = len(root)
		}
	}
	return longest
}

func (g graph) owners(path string) []string {
	owners := []string{}
	for _, id := range g.Order {
		c := g.Components[id]
		if contains(c.Files, path) || matchingRootLength(c, path) >= 0 {
			owners = append(owners, id)
		}
	}
	groups := map[string]int{}
	for _, id := range owners {
		c := g.Components[id]
		if c.OwnershipGroup == "" || matchingRootLength(c, path) < 0 {
			continue
		}
		if n := matchingRootLength(c, path); n > groups[c.OwnershipGroup] {
			groups[c.OwnershipGroup] = n
		}
	}
	kept := owners[:0]
	for _, id := range owners {
		c := g.Components[id]
		rootLength := matchingRootLength(c, path)
		if c.OwnershipGroup != "" && rootLength >= 0 && rootLength < groups[c.OwnershipGroup] {
			continue
		}
		kept = append(kept, id)
	}
	return kept
}

func collectionValue(c component, field string) (string, bool) {
	switch field {
	case "id":
		return c.ID, true
	case "name":
		return c.Name, c.Name != ""
	case "root":
		if c.Root != "" {
			return c.Root, true
		}
		if len(c.Roots) > 0 {
			return c.Roots[0], true
		}
	case "language":
		return c.Language, c.Language != ""
	case "class":
		return c.Class, c.Class != ""
	}
	return "", false
}

func validCollectionField(field string) bool {
	switch field {
	case "id", "name", "root", "language", "class":
		return true
	default:
		return false
	}
}

func (g graph) selectPaths(paths []string) (plan, error) {
	p := plan{Jobs: map[string]bool{}, Components: map[string][]string{}, JobReasons: map[string][]string{}, ChangedPaths: paths, Collections: map[string][]string{}}
	for _, job := range g.Jobs {
		p.Jobs[job] = false
		p.JobReasons[job] = []string{}
	}
	for _, item := range g.Collections {
		p.Collections[item.Name] = []string{}
	}
	unknown := []string{}
	reasonOrder := []string{}
	for _, path := range paths {
		owners := g.owners(path)
		if len(owners) == 0 {
			ignored := contains(g.IgnoredFiles, path)
			for _, root := range g.IgnoredRoots {
				if under(path, root) {
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
	allJobs, allCollections := false, false
	selectedIDs := map[string]bool{}
	for _, id := range reasonOrder {
		reason := p.Components[id]
		c := g.Components[id]
		selectedIDs[id] = true
		allJobs = allJobs || c.SelectAllJobs
		allCollections = allCollections || c.SelectAllCollections
		for _, job := range c.Jobs {
			p.Jobs[job] = true
			p.JobReasons[job] = append(p.JobReasons[job], id+": "+reason[0])
		}
	}
	if allJobs {
		for _, job := range g.Jobs {
			p.Jobs[job] = true
			for _, id := range reasonOrder {
				if g.Components[id].SelectAllJobs {
					p.JobReasons[job] = append(p.JobReasons[job], id+": all jobs selected by policy")
					break
				}
			}
		}
	}
	for _, selector := range g.Collections {
		seen := map[string]bool{}
		for _, id := range g.Order {
			c := g.Components[id]
			if c.Class != selector.Class {
				continue
			}
			if selector.SelectedOnly && !c.Selected {
				continue
			}
			if !allCollections && !selectedIDs[id] {
				continue
			}
			value, ok := collectionValue(c, selector.Field)
			if !ok {
				return p, fmt.Errorf("component %q has no collection field %q", id, selector.Field)
			}
			if !seen[value] {
				p.Collections[selector.Name] = append(p.Collections[selector.Name], value)
				seen[value] = true
			}
		}
		sort.Strings(p.Collections[selector.Name])
	}
	return p, nil
}
