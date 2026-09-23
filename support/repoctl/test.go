package main

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"repoctl/execution"
	"repoctl/execution/golang"
	"repoctl/execution/python"
	"repoctl/execution/rust"
	"repoctl/execution/typescript"
)

var testPlanners = map[string]execution.Planner{
	"go": golang.Planner{}, "rust": rust.Planner{},
	"python": python.Planner{}, "typescript": typescript.Planner{},
}

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
	jobs := make([]string, 0, len(p.Jobs))
	for job, selected := range p.Jobs {
		if selected {
			jobs = append(jobs, job)
		}
	}
	sort.Strings(jobs)
	checks := []execution.Check{}
	for _, job := range jobs {
		suite, ok := g.TestSuites[job]
		if !ok {
			if coveredJobs[job] {
				continue
			}
			return nil, nil, fmt.Errorf("selected job %q has no test suite", job)
		}
		planner := testPlanners[suite.Language]
		planned, err := planner.Plan(suite, p.Collections[suite.Collection])
		if err != nil {
			return nil, nil, err
		}
		checks = append(checks, planned...)
	}
	return checks, targets, nil
}

func runSelectedTests(root string, g graph, p plan, dryRun bool, runner execution.Runner, nativeRun nativeCommand) error {
	checks, targets, err := selectedTestChecks(g, p)
	if err != nil {
		return err
	}
	for _, check := range checks {
		fmt.Printf("%s (%s): %s\n", check.Label, check.Directory, strings.Join(check.Args, " "))
		if dryRun {
			continue
		}
		if err := runner.Run(context.Background(), root, check); err != nil {
			return err
		}
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
