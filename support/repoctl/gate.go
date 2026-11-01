package main

import (
	"bytes"
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"repoctl/execution"
	"repoctl/internal/gitrepo"
)

// The pre-push gate is the fast subset of the checks CI runs for a change: each check
// group may list gate_commands (and gate_package_commands) in .repoctl/checks.toml, the
// part of it worth running before a push. The gate selects groups the way `repoctl test`
// does, runs each selected group's gate commands as one lane, and runs the lanes
// concurrently, so its wall time is the slowest lane rather than the sum. CI still runs
// every check in full; the gate only moves the common failures to before the push.
const (
	// gateSkipEnv names the logged escape: set it to the reason and the gate passes.
	gateSkipEnv = "VIBESYS_SKIP_GATE"
	// gateSkipLog is appended to in the repository's shared git directory.
	gateSkipLog = "vibesys-gate-skips.log"
	// gateHooksDir is the tracked directory whose pre-push hook runs the gate.
	gateHooksDir = ".githooks"
	// gateTimeout is a hang guard, not a target: a lane that exceeds it fails the push.
	gateTimeout = 4 * time.Minute
	// gateTarget is the wall time the selection aims for on a developer's machine.
	gateTarget = 60 * time.Second
)

// gateLane is one check group's gate commands, run in order.
type gateLane struct {
	Group  string
	Checks []execution.Check
}

type laneResult struct {
	Group    string
	Duration time.Duration
	Output   string
	Err      error
}

// selectedGateLanes returns one lane per selected group that has gate commands, in group
// name order. A group is selected as `repoctl test` selects it: its trigger job is
// selected (or it has none and something changed), and any local_paths narrowing holds.
func selectedGateLanes(g graph, p plan) ([]gateLane, error) {
	names := make([]string, 0, len(g.CheckGroups))
	for name := range g.CheckGroups {
		names = append(names, name)
	}
	sort.Strings(names)
	lanes := []gateLane{}
	for _, name := range names {
		suite := g.CheckGroups[name]
		if len(suite.GateCommands) == 0 && len(suite.GatePackageCommands) == 0 {
			continue
		}
		triggered := suite.TriggerJob == "" && len(p.ChangedPaths) > 0 || suite.TriggerJob != "" && p.Jobs[suite.TriggerJob]
		if !triggered {
			continue
		}
		if local := g.localSelectionPaths(suite); len(local) > 0 && !anyUnder(p.ChangedPaths, local) {
			continue
		}
		planner, ok := testPlanners[suite.Language]
		if !ok {
			return nil, fmt.Errorf("check group %q: unknown language %q", suite.Name, suite.Language)
		}
		gate := suite.ForGate()
		checks, err := planner.Plan(gate, p.Collections[gate.Collection])
		if err != nil {
			return nil, err
		}
		lanes = append(lanes, gateLane{Group: name, Checks: checks})
	}
	return lanes, nil
}

// runLanes runs the lanes concurrently, each lane's checks in order with output captured.
// The first failure cancels the other lanes. Results come back in lane order.
func runLanes(ctx context.Context, root string, lanes []gateLane, newRunner func(io.Writer) execution.Runner) []laneResult {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	results := make([]laneResult, len(lanes))
	var wg sync.WaitGroup
	for i, lane := range lanes {
		wg.Add(1)
		go func() {
			defer wg.Done()
			var buffer bytes.Buffer
			runner := newRunner(&buffer)
			started := time.Now()
			var err error
			for _, check := range lane.Checks {
				fmt.Fprintf(&buffer, "%s (%s): %s\n", check.Label, check.Directory, strings.Join(check.Args, " "))
				if err = runner.Run(ctx, root, check); err != nil {
					break
				}
			}
			results[i] = laneResult{Group: lane.Group, Duration: time.Since(started), Output: buffer.String(), Err: err}
			if err != nil {
				cancel()
			}
		}()
	}
	wg.Wait()
	return results
}

// reportGate prints each lane's time, the output of every lane that really failed (a
// lane cancelled because another failed has nothing to say), and the verdict.
func reportGate(out io.Writer, results []laneResult, wall time.Duration, parent context.Context) error {
	var failed []string
	for _, r := range results {
		status := "ok"
		switch {
		case r.Err == nil:
		case parent.Err() == nil && strings.Contains(r.Err.Error(), "context canceled"):
			status = "cancelled"
		default:
			status = "FAILED"
			failed = append(failed, r.Group)
			fmt.Fprintf(out, "\n==> %s\n%s\n%v\n", r.Group, strings.TrimRight(r.Output, "\n"), r.Err)
		}
		fmt.Fprintf(out, "gate: %-28s %6.1fs  %s\n", r.Group, r.Duration.Seconds(), status)
	}
	if len(failed) == 0 && parent.Err() == nil {
		fmt.Fprintf(out, "gate: passed in %.1fs (target about %.0fs)\n", wall.Seconds(), gateTarget.Seconds())
		return nil
	}
	if parent.Err() != nil {
		fmt.Fprintf(out, "gate: stopped after %.0fs (%v)\n", wall.Seconds(), parent.Err())
	}
	fmt.Fprintf(out, "gate: push refused. Fix the failure above; CI runs the same checks.\n")
	fmt.Fprintf(out, "gate: for an emergency, %s='<reason>' git push (the reason is logged in the repository's git directory).\n", gateSkipEnv)
	return fmt.Errorf("pre-push gate failed: %s", strings.Join(failed, ", "))
}

// skipGate records an emergency skip and lets the push through.
func skipGate(root, reason string, now time.Time, out io.Writer) error {
	dir, err := gitrepo.CommonDir(root)
	if err != nil {
		return err
	}
	branch, sha := gitrepo.Head(root)
	line := fmt.Sprintf("%s\t%s\t%s\t%s\t%s\n", now.UTC().Format(time.RFC3339), os.Getenv("USER"), branch, sha, strings.Join(strings.Fields(reason), " "))
	f, err := os.OpenFile(filepath.Join(dir, gateSkipLog), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.WriteString(line); err != nil {
		return err
	}
	fmt.Fprintf(out, "gate: SKIPPED by %s (%s); logged in %s. CI and the merge queue still run every check.\n", gateSkipEnv, reason, filepath.Join(dir, gateSkipLog))
	return nil
}

// ensureHooks points this checkout at the tracked pre-push gate, once, quietly after
// that, and warns loudly when another hooks directory is in force. CI and runs that set
// REPOCTL_NO_HOOKS leave the repository's configuration alone.
func ensureHooks(root string, out io.Writer) {
	if os.Getenv("CI") != "" || os.Getenv("GITHUB_ACTIONS") != "" || os.Getenv("REPOCTL_NO_HOOKS") != "" {
		return
	}
	state, current, err := gitrepo.EnsureHooksPath(root, gateHooksDir)
	switch {
	case err != nil:
		fmt.Fprintf(out, "repoctl: could not enable the pre-push gate: %v\n", err)
	case state == gitrepo.HooksInstalled:
		fmt.Fprintf(out, "repoctl: enabled the pre-push gate (git config core.hooksPath %s); every git push now runs `repoctl gate`.\n", current)
	case state == gitrepo.HooksForeign:
		fmt.Fprintf(out, "repoctl: WARNING: core.hooksPath is %q, so the pre-push gate in %s does not run and a push is not checked. Unset it with `git config --unset core.hooksPath`, or call `./support/repoctl/repoctl gate` from your own hook.\n", current, gateHooksDir)
	}
}

func runGate(root, configPath string, g graph, args []string, out io.Writer) error {
	newRunner := func(w io.Writer) execution.Runner {
		return execution.OSRunner{Stdout: w, Stderr: w, OwnProcessGroup: true}
	}
	// Each lane runs in its own process group, which a terminal's Ctrl-C does not reach, so
	// the interrupt cancels the lanes instead.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return runGateWith(ctx, root, configPath, g, args, out, newRunner, time.Now)
}

func runGateWith(parent context.Context, root, configPath string, g graph, args []string, out io.Writer, newRunner func(io.Writer) execution.Runner, now func() time.Time) error {
	fs := flag.NewFlagSet("gate", flag.ContinueOnError)
	base := fs.String("base", "origin/"+g.DefaultBase, "Base revision")
	head := fs.String("head", "HEAD", "Head revision")
	dryRun := fs.Bool("dry-run", false, "Print the selected gate commands without running them")
	timeout := fs.Duration("timeout", gateTimeout, "Hang guard for the whole gate")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return fmt.Errorf("unexpected gate arguments")
	}
	if reason := strings.TrimSpace(os.Getenv(gateSkipEnv)); reason != "" {
		return skipGate(root, reason, now(), out)
	}
	g = g.forLocalTest()
	p, err := localChangePlan(root, configPath, g, *base, *head, "pull_request")
	if err != nil {
		return err
	}
	lanes, err := selectedGateLanes(g, p)
	if err != nil {
		return err
	}
	if len(lanes) == 0 {
		fmt.Fprintln(out, "gate: no checks selected for this change")
		return nil
	}
	if *dryRun {
		for _, lane := range lanes {
			for _, check := range lane.Checks {
				fmt.Fprintf(out, "%s (%s): %s\n", lane.Group, check.Directory, strings.Join(check.Args, " "))
			}
		}
		return nil
	}
	ctx, cancel := context.WithTimeout(parent, *timeout)
	defer cancel()
	started := now()
	results := runLanes(ctx, root, lanes, newRunner)
	return reportGate(out, results, now().Sub(started), ctx)
}
