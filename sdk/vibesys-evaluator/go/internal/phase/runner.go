package phase

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

// HistoryFile is the accuracy history's manifest, beside the phase directories and the dataset
// file (docs/spec.md, "Accuracy history").
const HistoryFile = "history.binpb"

// FaultKind is what a fault's kind must match: a name the task's oracle gives meaning.
var FaultKind = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)

// Pods runs the task's commands next to a deployed application: a phase pod on Kubernetes, a
// child process for a local application.
type Pods interface {
	RunPod(ctx context.Context, spec PodSpec) (PodRun, error)
}

// PodSpec is one pod: its command, given the work directory and the target as the pod sees them;
// the files written into the work directory first; the directory the files it leaves go to.
type PodSpec struct {
	Name    string
	Command func(workDir, target string) []string
	Inputs  map[string][]byte
	Outputs string
	Timeout time.Duration
}

// PodRun is a finished pod. A nonzero exit is a result; a pod that could not run is an error.
type PodRun struct {
	ExitCode int
	Log      string
	Seconds  float64
}

// Runner runs a sample's phases in order on a deployed application, each in a pod (Pods) whose
// command is `Command` (the phase pod's: Pod.Run), and judges each; it records the faults run
// between them and, under capture, the accuracy history's manifest.
type Runner struct {
	Pods    Pods
	Command func(workDir, target string) []string
	// Seed is the sample's seed; every phase's streams derive from it and the phase's index.
	Seed uint64
	// Dir gets a directory per phase (phase-<i>) and, under capture, HistoryFile.
	Dir      string
	Validity evaluator.Validity
	// Capture records every phase's whole history for an accuracy check.
	Capture bool
	// Unjudged: the phases are not measurements and get no verdict (an accuracy run's, whose
	// oracle judges every response); Validity is not applied, and each phase logs what it sent
	// and how many of its operations failed.
	Unjudged bool
	Log      func(string)
	Now      func() time.Time

	next   int
	faults []*pb.FaultEvent
	// windowEnd is the end of the last phase's window on the loadgen clock: a fault that starts
	// before it shows clocks that disagree.
	windowEnd int64
}

// Ran is a phase that ran to its end: what it was asked, what it left, and the judge's verdict
// (whose Failure says why it is not a measurement, if it is not; zero when the Runner is
// Unjudged).
type Ran struct {
	Index   int
	Input   *pb.PhaseInput
	Output  *pb.PhaseOutput
	Verdict Verdict
}

// Phases is how many phases have started.
func (r *Runner) Phases() int { return r.next }

// Run runs the next phase with the load asked (a PhaseInput's model, clients or rate and
// arrivals, secs and warmup) and judges it, unless the Runner is Unjudged. An error means it left nothing to judge: its pod could
// not run, exited nonzero or wrote no usable output.
func (r *Runner) Run(ctx context.Context, asked *pb.PhaseInput) (Ran, error) {
	index := r.next
	r.next++
	in := Input(r.Seed, index, asked, r.Capture)
	name := fmt.Sprintf("phase-%d", index)
	dir, err := evaluator.PhaseDir(r.Dir, name)
	if err != nil {
		return Ran{}, err
	}
	r.Log(fmt.Sprintf("phase %d: %s", index, Describe(in)))
	encoded, err := proto.Marshal(in)
	if err != nil {
		return Ran{}, err
	}
	ran, err := r.Pods.RunPod(ctx, PodSpec{
		Name: name, Command: r.Command, Outputs: dir, Timeout: Timeout(in),
		Inputs: map[string][]byte{InputFile: encoded},
	})
	if err != nil {
		_ = os.WriteFile(filepath.Join(dir, LogFile), []byte(err.Error()), 0o644)
		return Ran{}, fmt.Errorf("phase %d (%s): %w", index, Describe(in), err)
	}
	_ = os.WriteFile(filepath.Join(dir, LogFile), []byte(ran.Log), 0o644)
	if ran.ExitCode != 0 {
		return Ran{}, fmt.Errorf("%s exited %d after %.0f s; log tail:\n%s", name, ran.ExitCode, ran.Seconds, Tail(ran.Log, 1500))
	}
	output, err := ReadOutput(dir)
	if err != nil {
		return Ran{}, fmt.Errorf("phase %d (%s) left no usable output: %w", index, Describe(in), err)
	}
	r.windowEnd = output.GetSummary().GetWindowEndUnixNanos()
	if r.Capture {
		if err := r.writeHistory(); err != nil {
			return Ran{}, err
		}
	}
	if r.Unjudged {
		for _, line := range Counts(in, output) {
			r.Log(line)
		}
		return Ran{Index: index, Input: in, Output: output}, nil
	}
	verdict, err := Judge(output, r.Validity)
	if err != nil {
		return Ran{}, fmt.Errorf("phase %d: %w", index, err)
	}
	for _, line := range Log(in, output, verdict) {
		r.Log(line)
	}
	return Ran{Index: index, Input: in, Output: output, Verdict: verdict}, nil
}

// Fault runs action between two phases, after at least one, and records it as a fault of kind on
// target, with its start and end on this machine's clock (which must be the one loadgen stamps
// its results with). The fault is recorded, and under capture written into the history, even when
// action fails; its error is returned, wrapped.
func (r *Runner) Fault(ctx context.Context, kind, target string, action func(context.Context) error) error {
	if !FaultKind.MatchString(kind) {
		return fmt.Errorf("fault kind %q must match %s", kind, FaultKind)
	}
	if r.next == 0 {
		return fmt.Errorf("fault %s: a fault goes between phases; run a phase first", kind)
	}
	after := r.next - 1
	start := r.Now()
	if start.UnixNano() < r.windowEnd {
		r.Log(fmt.Sprintf("fault %s starts at %s, before phase %d's window ended on loadgen's clock (%s): "+
			"this machine's clock and the phase pod's disagree, and the fault's times are off by as much",
			kind, start.UTC().Format(time.RFC3339Nano), after, time.Unix(0, r.windowEnd).UTC().Format(time.RFC3339Nano)))
	}
	err := action(ctx)
	end := r.Now()
	r.faults = append(r.faults, &pb.FaultEvent{
		Kind: kind, Target: target, StartUnixNanos: start.UnixNano(), EndUnixNanos: end.UnixNano(), AfterPhase: uint32(after),
	})
	if r.Capture {
		if werr := r.writeHistory(); werr != nil && err == nil {
			err = werr
		}
	}
	if err != nil {
		return fmt.Errorf("fault %s on %s after phase %d: %w", kind, target, after, err)
	}
	r.Log(fmt.Sprintf("fault %s on %s after phase %d: %.1f s", kind, target, after, end.Sub(start).Seconds()))
	return nil
}

// writeHistory writes the accuracy history's manifest: the phases run so far, the faults, the
// dataset file.
func (r *Runner) writeHistory() error {
	manifest := &pb.HistoryManifest{Phases: uint32(r.next), Faults: r.faults, Dataset: DatasetFile}
	encoded, err := proto.Marshal(manifest)
	if err != nil {
		return err
	}
	path := filepath.Join(r.Dir, HistoryFile)
	if err := os.WriteFile(path+".tmp", encoded, 0o644); err != nil {
		return err
	}
	return os.Rename(path+".tmp", path)
}

// Tail is the last n bytes of text, trimmed.
func Tail(text string, n int) string {
	text = strings.TrimSpace(text)
	if len(text) <= n {
		return text
	}
	return text[len(text)-n:]
}
