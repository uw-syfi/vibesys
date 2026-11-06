package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"

	"repoctl/execution"
)

type localRole int

const (
	roleNone localRole = iota
	roleRun
	roleSkip
)

// localGroupRole decides whether a group runs, is named and skipped, or is
// left out. checksOnly keeps include_in_checks groups and drops include_in_test
// groups and native targets. A local_skip group is named whenever its job is
// selected, in either mode.
func localGroupRole(suite execution.Suite, p plan, checksOnly bool) localRole {
	if suite.LocalSkip != "" {
		if p.Jobs[suite.TriggerJob] {
			return roleSkip
		}
		return roleNone
	}
	always := suite.TriggerJob == "" && len(p.ChangedPaths) > 0
	triggered := suite.TriggerJob != "" && p.Jobs[suite.TriggerJob]
	if suite.IncludeInChecks && (always || triggered) {
		return roleRun
	}
	if !checksOnly && suite.IncludeInTest && p.Jobs[suite.TriggerJob] {
		return roleRun
	}
	return roleNone
}

// groupName is the check group behind a planned command. Package commands label
// themselves "group:package"; the selection contract is the group.
func groupName(label string) string {
	name, _, _ := strings.Cut(label, ":")
	return name
}

func skippedGroupLine(g graph, name string) string {
	if reason := g.CheckGroups[name].LocalSkip; reason != "" {
		return fmt.Sprintf("skipped check group %q: %s, runs in CI only", name, reason)
	}
	return fmt.Sprintf("skipped check group %q: no changed path under its local_paths, runs in CI", name)
}

// selectedTestChecks plans the local test run for a plan. The third result
// lists selected jobs with no local check group or native target; they run in
// CI only, so the caller reports them as skipped instead of failing. The
// fourth lists groups skipped because no changed path is under their
// local_paths, or because local_skip says they cannot run locally. CI still
// runs them. checksOnly is the `--checks-only` flag.
func selectedTestChecks(g graph, p plan, checksOnly bool) ([]execution.Check, []string, []string, []string, error) {
	targets := []string{}
	coveredJobs := map[string]bool{}
	if !checksOnly {
		allTargets := false
		for id := range p.Components {
			allTargets = allTargets || g.Components[id].SelectAllCollections
		}
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
	}
	checks := []execution.Check{}
	skippedGroups := []string{}
	groups := make([]string, 0, len(g.CheckGroups))
	for name := range g.CheckGroups {
		groups = append(groups, name)
	}
	sort.Strings(groups)
	for _, name := range groups {
		suite := g.CheckGroups[name]
		switch localGroupRole(suite, p, checksOnly) {
		case roleSkip:
			skippedGroups = append(skippedGroups, name)
			coveredJobs[suite.TriggerJob] = true
			continue
		case roleNone:
			continue
		}
		if local := g.localSelectionPaths(suite); len(local) > 0 && !anyUnder(p.ChangedPaths, local) {
			skippedGroups = append(skippedGroups, name)
			if suite.TriggerJob != "" {
				coveredJobs[suite.TriggerJob] = true
			}
			continue
		}
		planner, ok := testPlanners[suite.Language]
		if !ok {
			return nil, nil, nil, nil, fmt.Errorf("check group %q: unknown language %q", suite.Name, suite.Language)
		}
		planned, err := planner.Plan(suite, p.Collections[suite.Collection])
		if err != nil {
			return nil, nil, nil, nil, err
		}
		checks = append(checks, planned...)
		if suite.TriggerJob != "" {
			coveredJobs[suite.TriggerJob] = true
		}
	}
	skipped := []string{}
	if !checksOnly {
		for job, selected := range p.Jobs {
			if selected && !coveredJobs[job] {
				skipped = append(skipped, job)
			}
		}
		sort.Strings(skipped)
	}
	return checks, targets, skipped, skippedGroups, nil
}

// runSelectedTests runs the planned groups and native targets. Each group is a unit: its commands
// run in order and stop at the first failure, because a later command may rely on an earlier one.
// Unless failFast, a failed group does not stop the others, and the run ends with a summary naming
// every group's outcome, so one run reports everything that is wrong instead of the first thing.
func runSelectedTests(root string, g graph, p plan, dryRun, checksOnly, failFast bool, runner execution.Runner, nativeRun nativeCommand) error {
	checks, targets, skipped, skippedGroups, err := selectedTestChecks(g, p, checksOnly)
	if err != nil {
		return err
	}
	for _, job := range skipped {
		fmt.Printf("skipped job %q: no local target, runs in CI only\n", job)
	}
	for _, name := range skippedGroups {
		fmt.Println(skippedGroupLine(g, name))
	}
	if dryRun {
		return printSelectedTests(root, g, checks, targets, runner)
	}
	if err := preflight(runner, checks); err != nil {
		return err
	}
	results := runGroups(root, checks, failFast, runner)
	for _, target := range targets {
		if failFast && anyFailed(results) {
			results = append(results, groupResult{Group: "native " + target, Skipped: true})
			continue
		}
		raw, err := json.Marshal([]string{target})
		if err != nil {
			return err
		}
		results = append(results, groupResult{Group: "native " + target, Err: runNativeTargets(root, g, string(raw), nativeRun)})
	}
	return summarize(os.Stdout, results)
}

func printSelectedTests(root string, g graph, checks []execution.Check, targets []string, runner execution.Runner) error {
	if err := runChecks(root, checks, true, runner); err != nil {
		return err
	}
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

// groupResult is the outcome of one group: it ran and passed (Err nil), ran and failed, or was
// not run because an earlier failure stopped a --fail-fast run.
type groupResult struct {
	Group   string
	Err     error
	Skipped bool
}

func anyFailed(results []groupResult) bool {
	for _, r := range results {
		if r.Err != nil {
			return true
		}
	}
	return false
}

// runGroups runs checks grouped by their check group, in order. A group's commands stop at its
// first failure; the next group still runs unless failFast.
func runGroups(root string, checks []execution.Check, failFast bool, runner execution.Runner) []groupResult {
	var order []string
	byGroup := map[string][]execution.Check{}
	for _, check := range checks {
		name := groupName(check.Label)
		if _, seen := byGroup[name]; !seen {
			order = append(order, name)
		}
		byGroup[name] = append(byGroup[name], check)
	}
	results := make([]groupResult, 0, len(order))
	for _, name := range order {
		if failFast && anyFailed(results) {
			results = append(results, groupResult{Group: name, Skipped: true})
			continue
		}
		results = append(results, groupResult{Group: name, Err: runChecks(root, byGroup[name], false, runner)})
	}
	return results
}

// summarize prints each group's outcome and returns an error naming the failed groups.
func summarize(out io.Writer, results []groupResult) error {
	if len(results) == 0 {
		return nil
	}
	var failed []string
	fmt.Fprintln(out, "\nrepoctl test summary:")
	for _, r := range results {
		switch {
		case r.Skipped:
			fmt.Fprintf(out, "  not run  %s (stopped by --fail-fast)\n", r.Group)
		case r.Err != nil:
			failed = append(failed, r.Group)
			fmt.Fprintf(out, "  FAILED   %s: %v\n", r.Group, r.Err)
		default:
			fmt.Fprintf(out, "  passed   %s\n", r.Group)
		}
	}
	if len(failed) == 0 {
		return nil
	}
	return fmt.Errorf("%d of %d groups failed: %s", len(failed), len(results), strings.Join(failed, ", "))
}

// preflight lets a runner refuse the whole plan before any of it runs.
func preflight(runner execution.Runner, checks []execution.Check) error {
	if p, ok := runner.(preflighter); ok {
		return p.Preflight(checks)
	}
	return nil
}

func runChecks(root string, checks []execution.Check, dryRun bool, runner execution.Runner) error {
	if !dryRun {
		if err := preflight(runner, checks); err != nil {
			return err
		}
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
	return nil
}
