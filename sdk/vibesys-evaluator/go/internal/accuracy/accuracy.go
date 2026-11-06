// Package accuracy is the accuracy flow of the microservices phase runner: phases of a fixed
// open-loop load against a deployed application with capture on, fault actions between them,
// the accuracy history they leave, and the oracle's verdict on it. microservices.Main's
// `accuracy` command (a Go task's oracle in-process) and the core's `accuracy run` (a language
// SDK's, with the oracle a program) share it.
package accuracy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/phase"
)

// HistoryDir is the history's directory inside the report's.
const HistoryDir = "history"

// Fault is a fault action between two phases: after phase After, Do runs, recorded as a fault of
// Kind on Target.
type Fault struct {
	After        int
	Kind, Target string
	Do           func(context.Context) error
}

// Plan is an accuracy run: Phases phases of Load (an open loop), and the faults.
type Plan struct {
	Phases int
	Load   *pb.PhaseInput
	Faults []Fault
}

// Validate says what is wrong with the plan, if anything.
func (p Plan) Validate() error {
	if p.Phases < 1 {
		return fmt.Errorf("an accuracy run has at least one phase, not %d", p.Phases)
	}
	if p.Load.GetModel() != pb.LoadModel_LOAD_MODEL_OPEN {
		return errors.New("an accuracy run's phases are open loops (a fixed rate)")
	}
	if err := phase.Validate(p.Load); err != nil {
		return err
	}
	for _, f := range p.Faults {
		if f.After < 0 || f.After >= p.Phases-1 {
			return fmt.Errorf("fault %s on %s after phase %d: a fault goes between two of the %d phases (after 0 to %d)",
				f.Kind, f.Target, f.After, p.Phases, p.Phases-2)
		}
		if !phase.FaultKind.MatchString(f.Kind) {
			return fmt.Errorf("fault kind %q must match %s", f.Kind, phase.FaultKind)
		}
		if f.Do == nil {
			return fmt.Errorf("fault %s on %s has no action", f.Kind, f.Target)
		}
	}
	return nil
}

// Run runs the plan on runner, which must capture: each phase, then the faults that follow it in
// order. A phase that is not a measurement (the saturation guard, the validity rules) is logged
// and checked like any other: the oracle judges its responses.
func Run(ctx context.Context, runner *phase.Runner, plan Plan) error {
	if !runner.Capture {
		return errors.New("an accuracy run captures its phases")
	}
	if err := plan.Validate(); err != nil {
		return err
	}
	for i := range plan.Phases {
		ran, err := runner.Run(ctx, plan.Load)
		if err != nil {
			return err
		}
		if ran.Verdict.Failure != "" {
			runner.Log(fmt.Sprintf("phase %d is not a measurement (%s); its history is checked all the same", i, ran.Verdict.Failure))
		}
		for _, f := range plan.Faults {
			if f.After == i {
				if err := runner.Fault(ctx, f.Kind, f.Target, f.Do); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// CheckSpecs judges the history in dir with the specs oracle builds, as an oracle's check.Main
// does: it prints the report to stdout, writes it and the failures' timelines into out, and
// returns the verdict; the reason goes to stderr when it is not PASS.
func CheckSpecs(dir, out string, oracle func(*check.History) ([]check.AnySpec, error), options check.Options, stdout, stderr io.Writer) check.Verdict {
	began := time.Now()
	h, err := check.OpenHistory(dir)
	if err != nil {
		fmt.Fprintf(stderr, "UNKNOWN: the history cannot be read (a capture problem, not the candidate's): %v\n", err)
		return check.Unknown
	}
	specs, err := oracle(h)
	if err != nil {
		fmt.Fprintf(stderr, "UNKNOWN: the oracle cannot be set up (a task problem, not the candidate's): %v\n", err)
		return check.Unknown
	}
	report := check.Check(h, specs, options)
	fmt.Fprint(stdout, report.Text(40))
	paths, err := report.WriteFiles(out)
	if err != nil {
		fmt.Fprintf(stderr, "writing the report to %s: %v\n", out, err)
	}
	fmt.Fprintf(stdout, "report: %v (%v)\n", paths, time.Since(began).Round(time.Millisecond))
	if report.Verdict != check.Pass {
		fmt.Fprintln(stderr, report.Reason)
	}
	return report.Verdict
}

// CheckCommand runs an oracle program (built on check.Main) on the history in dir, with `--history
// DIR --out OUT` after argv, its output to stdout and stderr, and returns the verdict its exit
// status says (anything but 0, 1 and 2 is UNKNOWN).
func CheckCommand(ctx context.Context, argv []string, dir, out string, stdout, stderr io.Writer) check.Verdict {
	if len(argv) == 0 {
		fmt.Fprintln(stderr, "UNKNOWN: no oracle command")
		return check.Unknown
	}
	cmd := exec.CommandContext(ctx, argv[0], append(argv[1:], "--history", dir, "--out", out)...)
	cmd.Stdout, cmd.Stderr = stdout, stderr
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = 30 * time.Second
	err := cmd.Run()
	var exit *exec.ExitError
	switch {
	case err == nil:
		return check.Pass
	case errors.As(err, &exit) && exit.ExitCode() == 1:
		return check.Fail
	case errors.As(err, &exit) && exit.ExitCode() == 2:
		return check.Unknown
	}
	fmt.Fprintf(stderr, "UNKNOWN: the oracle %q failed: %v\n", argv[0], err)
	return check.Unknown
}

// NewHistoryDir makes dir for a history: it must not exist, or hold nothing but the dataset file.
func NewHistoryDir(dir string) error {
	entries, err := os.ReadDir(dir)
	if errors.Is(err, os.ErrNotExist) {
		return os.MkdirAll(dir, 0o755)
	}
	if err != nil {
		return err
	}
	for _, e := range entries {
		if e.Name() != phase.DatasetFile {
			return fmt.Errorf("the history directory %s already holds %s: give each run a new one", dir, e.Name())
		}
	}
	return nil
}

// DatasetPath is the dataset file's path in a history directory.
func DatasetPath(dir string) string { return filepath.Join(dir, phase.DatasetFile) }

// Unsupported is a fault action that system cannot apply: what (restart kv) acts on a Kubernetes
// Deployment.
func Unsupported(system, what string) func(context.Context) error {
	return func(context.Context) error {
		return fmt.Errorf("%s cannot %s: that fault acts on a Kubernetes Deployment (microservices.K8s); "+
			"a fault the task applies itself is FaultFunc", system, what)
	}
}
