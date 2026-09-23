package main

import (
	"fmt"
	"strings"

	"repoctl/execution"
)

func (g *graph) validateCheckGroups(groups []execution.Suite) error {
	collections := map[string]bool{}
	for _, item := range g.Collections {
		collections[item.Name] = true
	}
	for _, suite := range groups {
		if suite.Name == "" {
			return fmt.Errorf("check_groups: name must not be empty")
		}
		if _, exists := g.CheckGroups[suite.Name]; exists {
			return fmt.Errorf("check_groups: duplicate name %q", suite.Name)
		}
		if suite.TriggerJob != "" && !contains(g.Jobs, suite.TriggerJob) {
			return fmt.Errorf("check_groups.%s: unknown trigger_job %q", suite.Name, suite.TriggerJob)
		}
		if suite.IncludeInTest && suite.TriggerJob == "" {
			return fmt.Errorf("check_groups.%s: include_in_test requires trigger_job", suite.Name)
		}
		if suite.Directory != "." && !validPath(suite.Directory) {
			return fmt.Errorf("check_groups.%s: unsafe directory %q", suite.Name, suite.Directory)
		}
		if suite.TimeoutSeconds <= 0 {
			return fmt.Errorf("check_groups.%s: timeout_seconds must be positive", suite.Name)
		}
		for key := range suite.Env {
			if !envKey.MatchString(key) {
				return fmt.Errorf("check_groups.%s: invalid environment key %q", suite.Name, key)
			}
		}
		planner, ok := testPlanners[suite.Language]
		if !ok {
			return fmt.Errorf("check_groups.%s: unknown language %q", suite.Name, suite.Language)
		}
		if err := planner.Validate(suite, collections); err != nil {
			return err
		}
		if len(suite.Commands) == 0 && len(suite.AlwaysCommands) == 0 {
			return fmt.Errorf("check_groups.%s: commands or always_commands must not be empty", suite.Name)
		}
		for _, commands := range [][][]string{suite.AlwaysCommands, suite.Commands, suite.PackageCommands} {
			for _, args := range commands {
				if len(args) == 0 || strings.TrimSpace(args[0]) == "" {
					return fmt.Errorf("check_groups.%s: empty command", suite.Name)
				}
			}
		}
		g.CheckGroups[suite.Name] = suite
	}
	return nil
}

func (g graph) validateRunnableJobs() error {
	covered := map[string]bool{}
	for _, group := range g.CheckGroups {
		if group.TriggerJob != "" {
			covered[group.TriggerJob] = true
		}
	}
	for id, target := range g.NativeTargets {
		if !target.Selected {
			continue
		}
		for _, job := range g.Components[id].Jobs {
			covered[job] = true
		}
	}
	for _, job := range g.Jobs {
		if !covered[job] {
			return fmt.Errorf("job %q has no check group or selected native target", job)
		}
	}
	return nil
}
