package main

import (
	"encoding/json"
	"fmt"

	"repoctl/execution"
)

func collectionValues(g graph, name string) (map[string]bool, error) {
	for _, spec := range g.Collections {
		if spec.Name != name {
			continue
		}
		values := map[string]bool{}
		for _, id := range g.Order {
			component := g.Components[id]
			if component.Class != spec.Class || spec.SelectedOnly && !component.Selected {
				continue
			}
			value, ok := collectionValue(component, spec.Field)
			if !ok {
				return nil, fmt.Errorf("component %q has no collection field %q", id, spec.Field)
			}
			values[value] = true
		}
		return values, nil
	}
	return nil, fmt.Errorf("unknown collection %q", name)
}

func selectedGroupChecks(g graph, name, rawCollection string) ([]execution.Check, error) {
	group, ok := g.CheckGroups[name]
	if !ok {
		return nil, fmt.Errorf("unknown check group %q", name)
	}
	var selected []string
	if rawCollection != "" {
		if group.Collection == "" {
			return nil, fmt.Errorf("check group %q does not use a collection", name)
		}
		if err := json.Unmarshal([]byte(rawCollection), &selected); err != nil || selected == nil {
			return nil, fmt.Errorf("check group %q: collection-json must be a JSON array of strings", name)
		}
		allowed, err := collectionValues(g, group.Collection)
		if err != nil {
			return nil, err
		}
		seen := map[string]bool{}
		for _, value := range selected {
			if !allowed[value] {
				return nil, fmt.Errorf("check group %q: unregistered collection value %q", name, value)
			}
			if seen[value] {
				return nil, fmt.Errorf("check group %q: duplicate collection value %q", name, value)
			}
			seen[value] = true
		}
	}
	planner, ok := testPlanners[group.Language]
	if !ok {
		return nil, fmt.Errorf("check group %q: unknown language %q", name, group.Language)
	}
	return planner.Plan(group, selected)
}

func runCheckGroup(root string, g graph, name, rawCollection string, dryRun bool, runner execution.Runner) error {
	checks, err := selectedGroupChecks(g, name, rawCollection)
	if err != nil {
		return err
	}
	if len(checks) == 0 {
		return fmt.Errorf("check group %q has no runnable checks", name)
	}
	return runChecks(root, checks, dryRun, runner)
}
