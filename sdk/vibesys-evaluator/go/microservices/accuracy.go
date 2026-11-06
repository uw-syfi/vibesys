package microservices

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"maps"
	"os"
	"path/filepath"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/accuracy"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/phase"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/workload"
)

// Accuracy is a task's accuracy check, the `accuracy` command of Main: it deploys and seeds the
// application as a benchmark sample does, runs Phases open-loop phases of Load against it with
// the whole history captured (every response body, the loadgen client and the generator's info of
// every operation), applies the Faults between them, tears the application down, and judges the
// history with the specs Oracle builds (package check). It exits 0 on PASS, 1 on FAIL and 2 on
// UNKNOWN (also when the run could not complete), and writes the report, the failures' timelines
// and the history into --out (/vibesys/artifacts/accuracy, or accuracy/ in
// $VIBESYS_ARTIFACTS_DIR). A check runs at a fixed, modest load, so that the responses are
// judged, not the capacity; no phase is a measurement, so the Task's Validity does not apply.
type Accuracy[D, M any] struct {
	// Phases is how many phases run, each an open loop of Load.
	Phases int
	Load   Open
	// Faults go between phases, each after its phase, in order.
	Faults []Fault
	// Oracle builds the specs that judge the history, as an oracle's check.Main does.
	Oracle func(h *check.History) ([]check.AnySpec, error)
	// Options bound the check; a zero field takes check.DefaultOptions'.
	Options check.Options
	// Workload is the check's own request generator, in this binary (`accuracy-workload
	// generate`); or Generator is its command, as the Task's. Neither: the check's phases run the
	// Task's workload. One task binary so serves a benchmark's mix and a check's.
	Workload  *workload.Workload[D, M]
	Generator []string
}

// Fault is a fault an accuracy run applies between two phases: Restart, RestartAll, Exec and
// Flush act on a Deployment of a Kubernetes application (K8s); FaultFunc is the task's own
// action. Each is recorded in the history as a fault of its kind on its target, with its start
// and end on the clock loadgen stamps its results with, and no operation in flight; the oracle's
// Spec.Fault says what it does to the state.
type Fault struct {
	after        int
	kind, target string
	action       func(d deployment) func(context.Context) error
	// on is what the target must be: one of the App's Deployments (restartable or caches), one
	// of its caches, or anything (FaultFunc).
	on faultTarget
}

type faultTarget int

const (
	onTask faultTarget = iota
	onDeployment
	onCache
)

// Restart deletes one pod of the Deployment name (the first by name) after phase after and waits
// until it serves again: a fault of kind "restart" on the Deployment.
func Restart(after int, name string) Fault {
	return Fault{after, "restart", name, func(d deployment) func(context.Context) error {
		return d.restart(name, false)
	}, onDeployment}
}

// RestartAll deletes every pod of the Deployment name at once after phase after and waits until it
// serves again: a fault of kind "restart_all" on the Deployment.
func RestartAll(after int, name string) Fault {
	return Fault{after, "restart_all", name, func(d deployment) func(context.Context) error {
		return d.restart(name, true)
	}, onDeployment}
}

// Exec runs argv in the first pod of the Deployment name after phase after: a fault of kind "exec" on the
// Deployment. A nonzero exit ends the run.
func Exec(after int, name string, argv ...string) Fault {
	argv = append([]string(nil), argv...)
	return Fault{after, "exec", name, func(d deployment) func(context.Context) error {
		return d.exec(name, argv)
	}, onDeployment}
}

// Flush empties the cache Deployment cache (one of the k8s.App's Caches) in place, in every pod,
// after phase after: memcached's flush_all or redis's FLUSHALL. A fault of kind "flush" on the
// cache.
func Flush(after int, cache string) Fault {
	return Fault{after, "flush", cache, func(d deployment) func(context.Context) error {
		return d.flush(cache)
	}, onCache}
}

// FaultFunc is a fault the task applies itself after phase after: action, recorded as a fault of
// kind (^[a-z][a-z0-9_]{0,62}$) on target. An error of action ends the run.
func FaultFunc(after int, kind, target string, action func(context.Context) error) Fault {
	return Fault{after, kind, target, func(deployment) func(context.Context) error { return action }, onTask}
}

// plan is the run's plan on d; with d nil, its actions do nothing (to validate the plan).
func (a *Accuracy[D, M]) plan(d deployment) (accuracy.Plan, error) {
	load, err := a.Load.input()
	if err != nil {
		return accuracy.Plan{}, err
	}
	plan := accuracy.Plan{Phases: a.Phases, Load: load}
	for _, f := range a.Faults {
		do := func(context.Context) error { return nil }
		if d != nil {
			do = f.action(d)
		}
		plan.Faults = append(plan.Faults, accuracy.Fault{After: f.after, Kind: f.kind, Target: f.target, Do: do})
	}
	return plan, plan.Validate()
}

func (a *Accuracy[D, M]) validate(system System) error {
	if a.Oracle == nil {
		return errors.New("microservices.Accuracy needs an Oracle")
	}
	if a.Workload != nil && len(a.Generator) > 0 {
		return errors.New("microservices.Accuracy takes at most one of Workload and Generator")
	}
	if a.Workload != nil {
		if err := checkOperationNames(a.Workload); err != nil {
			return fmt.Errorf("microservices.Accuracy Workload: %w", err)
		}
	}
	if ks, ok := system.(kubeSystem); ok {
		caches := map[string]bool{}
		for name := range ks.app.Caches {
			caches[name] = true
		}
		deployments := maps.Clone(caches)
		for _, name := range ks.app.Restartable {
			deployments[name] = true
		}
		for _, f := range a.Faults {
			switch {
			case f.on == onDeployment && !deployments[f.target]:
				return fmt.Errorf("fault %s on %s: not one of the k8s.App's Restartable Deployments or Caches", f.kind, f.target)
			case f.on == onCache && !caches[f.target]:
				return fmt.Errorf("fault %s on %s: not one of the k8s.App's Caches", f.kind, f.target)
			}
		}
	}
	_, err := a.plan(nil)
	return err
}

// reportDir is where an accuracy check writes by default: accuracy/ in the job's artifacts.
func reportDir() string {
	if artifacts := os.Getenv(evaluator.ArtifactsEnv); artifacts != "" {
		return filepath.Join(artifacts, "accuracy")
	}
	return check.DefaultReportDir
}

// accuracyCommand is `accuracy [--project-root DIR] [--out DIR] [--seed N] [--history DIR]`.
func accuracyCommand[D, M any](ctx context.Context, task Task[D, M], args []string) int {
	set := flag.NewFlagSet("accuracy", flag.ContinueOnError)
	set.SetOutput(io.Discard)
	root := set.String("project-root", "", "the candidate's checkout")
	out := set.String("out", reportDir(), "where the report, the timelines and the history go")
	seedText := set.String("seed", "", "the run's seed (drawn when not given)")
	history := set.String("history", "", "judge this history instead of running")
	if err := set.Parse(args); err != nil || set.NArg() > 0 {
		usage("accuracy takes [--project-root DIR] [--out DIR] [--seed N] [--history DIR]")
	}
	a := task.Accuracy
	if a == nil {
		usage("this task has no Accuracy")
	}
	if *history != "" {
		return accuracy.CheckSpecs(*history, *out, a.Oracle, a.Options, os.Stdout, os.Stderr).ExitCode()
	}
	given := false
	set.Visit(func(f *flag.Flag) { given = given || f.Name == "seed" })
	seed, source, err := evaluator.SampleSeed(*seedText, given, os.Getenv(evaluator.SampleSeedEnv))
	if err != nil {
		usage("%v", err)
	}
	dir := filepath.Join(*out, accuracy.HistoryDir)
	evaluator.Log("accuracy check, seed %d (%s): %d phases, the history in %s", seed, source, a.Phases, dir)
	if err := runAccuracy(ctx, task, *root, *out, dir, seed); err != nil {
		fmt.Fprintf(os.Stderr, "UNKNOWN: the accuracy run did not complete: %v\n", err)
		return check.Unknown.ExitCode()
	}
	return accuracy.CheckSpecs(dir, *out, a.Oracle, a.Options, os.Stdout, os.Stderr).ExitCode()
}

// runAccuracy deploys the application, runs the plan with the history going into dir, and tears
// the application down.
func runAccuracy[D, M any](ctx context.Context, task Task[D, M], root, out, dir string, seed uint64) error {
	exe, err := executable()
	if err != nil {
		return err
	}
	if err := accuracy.NewHistoryDir(dir); err != nil {
		return err
	}
	encoded, digest, err := encodeDataset(task)
	if err != nil {
		return err
	}
	if err := os.WriteFile(accuracy.DatasetPath(dir), encoded, 0o644); err != nil {
		return err
	}
	logf := func(line string) { evaluator.Log("%s", line) }
	var seedCommand []string
	if task.Seed != nil {
		seedCommand = []string{exe, "seed", "--dataset-digest", digest}
	}
	// The deploy's logs go to the job's artifacts directory, which also names the job in the
	// namespace's labels; out when the run is not a job's.
	artifacts := os.Getenv(evaluator.ArtifactsEnv)
	if artifacts == "" {
		artifacts = out
	}
	deployed, err := task.System.deploy(ctx, deployRequest{projectRoot: root, artifacts: artifacts, seed: seedCommand, log: logf})
	if err != nil {
		return fmt.Errorf("deploy: %w", err)
	}
	defer func() {
		if err := deployed.close(context.WithoutCancel(ctx)); err != nil {
			evaluator.Log("teardown: %v", err)
		}
	}()
	plan, err := task.Accuracy.plan(deployed)
	if err != nil {
		return err
	}
	runner := &phase.Runner{
		Pods: deployed, Command: phaseCommand(exe, true), Seed: seed, Dir: dir,
		Capture: true, Unjudged: true, Log: logf, Now: time.Now,
	}
	return accuracy.Run(ctx, runner, plan)
}
