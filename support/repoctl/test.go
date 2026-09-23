package main

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"repoctl/execution"
)

func selectedTestChecks(g graph, p plan) ([]execution.Check, []string, error) {
	allTargets := false
	for id := range p.Components {
		allTargets = allTargets || g.Components[id].SelectAllCollections
	}
	targets := []string{}
	coveredJobs := map[string]bool{}
	for id, target := range g.NativeTargets {
		if !target.Selected {
			continue
		}
		if _, selected := p.Components[id]; !selected && !allTargets {
			continue
		}
		targets = append(targets, target.Root)
		for _, job := range g.Components[id].Jobs {
			coveredJobs[job] = true
		}
	}
	sort.Strings(targets)
	checks := []execution.Check{}
	groups := make([]string, 0, len(g.CheckGroups))
	for name := range g.CheckGroups {
		groups = append(groups, name)
	}
	sort.Strings(groups)
	for _, name := range groups {
		suite := g.CheckGroups[name]
		if !suite.IncludeInTest || !p.Jobs[suite.TriggerJob] {
			continue
		}
		planner, ok := testPlanners[suite.Language]
		if !ok {
			return nil, nil, fmt.Errorf("check group %q: unknown language %q", suite.Name, suite.Language)
		}
		planned, err := planner.Plan(suite, p.Collections[suite.Collection])
		if err != nil {
			return nil, nil, err
		}
		checks = append(checks, planned...)
		coveredJobs[suite.TriggerJob] = true
	}
	for job, selected := range p.Jobs {
		if selected && !coveredJobs[job] {
			return nil, nil, fmt.Errorf("selected job %q has no runnable check group or native target", job)
		}
	}
	return checks, targets, nil
}

func runSelectedTests(root string, g graph, p plan, dryRun bool, runner execution.Runner, nativeRun nativeCommand) error {
	checks, targets, err := selectedTestChecks(g, p)
	if err != nil {
		return err
	}
	if err := runChecks(root, checks, dryRun, runner); err != nil {
		return err
	}
	if len(targets) == 0 {
		return nil
	}
	raw, err := json.Marshal(targets)
	if err != nil {
		return err
	}
	if dryRun {
		registered, err := selectedNativeTargets(g)
		if err != nil {
			return err
		}
		for _, target := range targets {
			item, ok := registered[target]
			if !ok {
				return fmt.Errorf("unregistered native target %q", target)
			}
			for _, args := range g.NativeChecks.Commands[item.Language] {
				fmt.Printf("%s: %s\n", target, strings.Join(args, " "))
			}
		}
		return nil
	}
	return runNativeTargets(root, g, string(raw), nativeRun)
}

func runChecks(root string, checks []execution.Check, dryRun bool, runner execution.Runner) error {
	for _, check := range checks {
		fmt.Printf("%s (%s): %s\n", check.Label, check.Directory, strings.Join(check.Args, " "))
		if dryRun {
			continue
		}
		if err := runner.Run(context.Background(), root, check); err != nil {
			return err
		}
	}
	return nil
}
