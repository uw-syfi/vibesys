package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os/signal"
	"path/filepath"
	"slices"
	"sync"
	"syscall"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/accuracy"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/kube"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/phase"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
)

// The `accuracy` group: a language SDK's accuracy check, the same flow as a Go task's
// microservices.Main `accuracy` (proto/vibesys/evaluator/v1/microservices.proto).
func init() {
	register("accuracy", "run", accuracyRun)
}

// target is a deployed application as the accuracy flow uses it.
type target struct {
	pods    phase.Pods
	restart func(deployment string, every bool) func(context.Context) error
	exec    func(deployment string, argv []string) func(context.Context) error
	close   func(context.Context) error
}

// accuracyRun deploys and seeds the application, runs the phases with the faults between them
// into history_dir, tears the application down and runs the oracle: {"event": PhaseEvent} lines,
// then the AccuracyRunResponse. The first SIGTERM, SIGINT or SIGHUP stops the run (the
// application is still torn down).
func accuracyRun(rw IO) error {
	in, err := io.ReadAll(rw.Stdin)
	if err != nil {
		return err
	}
	req := &pb.AccuracyRunRequest{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(in, req); err != nil {
		return fail("bad_request", "the request is not a valid message: %v", err)
	}
	if len(req.GetPhaseCommand()) == 0 || req.GetHistoryDir() == "" || req.GetSystem() == nil {
		return fail("bad_request", "phase_command, history_dir and a system (k8s or local) are required")
	}
	v := req.GetValidity()
	validity := evaluator.Validity{
		MaxErrorRate: v.GetMaxErrorRate(), MinSustainedRatio: v.GetMinSustainedRatio(),
		MinOpsPerOperation: int(v.GetMinOpsPerOperation()),
	}
	if v == nil {
		validity = evaluator.DefaultValidity()
	}
	if err := validity.Validate(); err != nil {
		return fail("bad_request", "%v", err)
	}
	plan, err := planOf(req, nil)
	if err != nil {
		return fail("bad_request", "%v", err)
	}
	if app := req.GetK8S().GetApp(); app != nil {
		// A fault acts on a Deployment the App lets it touch: refuse the others before deploying.
		allowed := map[string]bool{}
		for _, name := range app.GetRestartable() {
			allowed[name] = true
		}
		for name := range app.GetCaches() {
			allowed[name] = true
		}
		for _, f := range plan.Faults {
			if !allowed[f.Target] {
				return fail("bad_request", "fault %s on %s: not one of the App's restartable Deployments or caches", f.Kind, f.Target)
			}
		}
	}
	if err := accuracy.NewHistoryDir(req.GetHistoryDir()); err != nil {
		return fail("bad_request", "%v", err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT, syscall.SIGHUP)
	defer stop()
	var mu sync.Mutex
	emit := func(stage, message string) {
		mu.Lock()
		defer mu.Unlock()
		line, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(
			&pb.PhaseEvent{Stage: stage, Message: message, UnixNanos: time.Now().UnixNano()})
		if err == nil {
			fmt.Fprintf(rw.Stdout, "{\"event\":%s}\n", line)
		}
	}
	log := func(line string) { emit("log", line) }

	t, err := deployTarget(ctx, req, log)
	if err != nil {
		if ctx.Err() != nil {
			return fail("cancelled", "the run was stopped by a signal")
		}
		return fail("deploy", "%v", err)
	}
	plan, _ = planOf(req, &t)
	runner := &phase.Runner{
		Pods: t.pods, Seed: req.GetSampleSeed(), Dir: req.GetHistoryDir(), Validity: validity,
		Capture: true, Log: log, Now: time.Now,
		Command: func(work, target string) []string {
			return append(slices.Clone(req.GetPhaseCommand()), "--work", work, "--target", target)
		},
	}
	runErr := accuracy.Run(ctx, runner, plan)
	if err := t.close(context.WithoutCancel(ctx)); err != nil {
		log(fmt.Sprintf("teardown: %v", err))
	}
	switch {
	case runErr != nil && ctx.Err() != nil:
		return fail("cancelled", "the run was stopped by a signal")
	case runErr != nil:
		return fail("run", "%v", runErr)
	}
	resp := &pb.AccuracyRunResponse{Phases: uint32(runner.Phases()), Faults: uint32(len(plan.Faults))}
	if len(req.GetOracle()) > 0 {
		out := req.GetOutDir()
		if out == "" {
			out = check.DefaultReportDir
		}
		emit("check", fmt.Sprintf("the oracle judges the history: %q", req.GetOracle()))
		// The oracle's report goes to standard error: standard output holds the events and the
		// response.
		verdict := accuracy.CheckCommand(ctx, req.GetOracle(), req.GetHistoryDir(), out, rw.Stderr, rw.Stderr)
		resp.Verdict, resp.ExitCode = string(verdict), int32(verdict.ExitCode())
	}
	emit("done", "the accuracy run ended")
	return writeMessage(rw.Stdout, resp)
}

// planOf is the request's plan; with t nil, its fault actions do nothing (to validate it).
func planOf(req *pb.AccuracyRunRequest, t *target) (accuracy.Plan, error) {
	plan := accuracy.Plan{Phases: int(req.GetPhases()), Load: req.GetLoad()}
	for _, f := range req.GetFaults() {
		kind := f.GetKind()
		do := func(context.Context) error { return nil }
		switch f.GetAction() {
		case pb.AccuracyFaultAction_ACCURACY_FAULT_ACTION_RESTART, pb.AccuracyFaultAction_ACCURACY_FAULT_ACTION_RESTART_ALL:
			every := f.GetAction() == pb.AccuracyFaultAction_ACCURACY_FAULT_ACTION_RESTART_ALL
			if kind == "" {
				kind = map[bool]string{false: "restart", true: "restart_all"}[every]
			}
			if t != nil {
				do = t.restart(f.GetDeployment(), every)
			}
		case pb.AccuracyFaultAction_ACCURACY_FAULT_ACTION_EXEC:
			if len(f.GetArgv()) == 0 {
				return plan, fmt.Errorf("an exec fault on %s has no argv", f.GetDeployment())
			}
			if kind == "" {
				kind = "exec"
			}
			if t != nil {
				do = t.exec(f.GetDeployment(), f.GetArgv())
			}
		default:
			return plan, fmt.Errorf("fault on %s: action must be restart, restart_all or exec", f.GetDeployment())
		}
		if f.GetDeployment() == "" {
			return plan, fmt.Errorf("a %s fault names no deployment", kind)
		}
		plan.Faults = append(plan.Faults, accuracy.Fault{After: int(f.GetAfterPhase()), Kind: kind, Target: f.GetDeployment(), Do: do})
	}
	return plan, plan.Validate()
}

// deployTarget brings the request's application up: a namespace of its own on Kubernetes, or
// the local application after its seed.
func deployTarget(ctx context.Context, req *pb.AccuracyRunRequest, log func(string)) (target, error) {
	if local := req.GetLocal(); local != nil {
		if local.GetTarget() == "" {
			return target{}, errors.New("local.target is required")
		}
		if len(local.GetSeed()) > 0 {
			if err := phase.LocalSeed(ctx, local.GetSeed(), filepath.Join(filepath.Dir(req.GetHistoryDir()), "seed.log")); err != nil {
				return target{}, err
			}
		}
		log(fmt.Sprintf("local system at %s", local.GetTarget()))
		return target{
			pods: phase.LocalPods{Target: local.GetTarget()},
			restart: func(name string, _ bool) func(context.Context) error {
				return accuracy.Unsupported("a local system", "restart "+name)
			},
			exec: func(name string, _ []string) func(context.Context) error {
				return accuracy.Unsupported("a local system", "exec in "+name)
			},
			close: func(context.Context) error { return nil },
		}, nil
	}
	dr := req.GetK8S()
	c, err := cluster(verbContext{ctx: ctx, stderr: logWriter(log)}, dr.GetWorkDir(), dr.GetArtifactsDir())
	if err != nil {
		return target{}, err
	}
	src, err := sourceOf(dr.GetApp(), dr.GetTaskDir(), dr.GetProjectRoot(), dr.GetProbePlanFile())
	if err != nil {
		return target{}, err
	}
	var seed *k8s.PodCommand
	if dr.Seed != nil {
		command, err := podCommandOf(dr.Seed)
		if err != nil {
			return target{}, err
		}
		seed = &command
	}
	d, err := k8s.Deploy(ctx, src, seed, c)
	if err != nil {
		return target{}, err
	}
	log(fmt.Sprintf("serving in namespace %s", d.Namespace))
	return target{
		pods:    kube.Pods{D: d},
		restart: func(name string, every bool) func(context.Context) error { return kube.Restart(d, name, every) },
		exec:    func(name string, argv []string) func(context.Context) error { return kube.Exec(d, name, argv) },
		close:   func(ctx context.Context) error { return d.Close(ctx, k8s.CloseOptions{}) },
	}, nil
}

// logWriter is an io.Writer of lines for log.
type logWriter func(string)

func (w logWriter) Write(p []byte) (int, error) {
	w(string(trimNewline(p)))
	return len(p), nil
}

func trimNewline(p []byte) []byte {
	for len(p) > 0 && (p[len(p)-1] == '\n' || p[len(p)-1] == '\r') {
		p = p[:len(p)-1]
	}
	return p
}
