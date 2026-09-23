package main

import (
	"fmt"
	"sort"
	"strings"
)

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
