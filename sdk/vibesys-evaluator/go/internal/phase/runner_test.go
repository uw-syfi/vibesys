package phase

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

func TestAFaultGoesBetweenPhases(t *testing.T) {
	r := &Runner{Log: func(string) {}, Now: time.Now, Dir: t.TempDir()}
	ran := false
	action := func(context.Context) error { ran = true; return nil }
	if err := r.Fault(context.Background(), "restart", "kv", action); err == nil || ran {
		t.Fatalf("a fault before the first phase ran: %v", err)
	}
	r.next = 1
	if err := r.Fault(context.Background(), "Restart!", "kv", action); err == nil || ran {
		t.Fatalf("a fault of a bad kind ran: %v", err)
	}
	failing := func(context.Context) error { return errors.New("no such pod") }
	if err := r.Fault(context.Background(), "restart", "kv", failing); err == nil || !strings.Contains(err.Error(), "no such pod") {
		t.Fatalf("a failed action: %v", err)
	}
	if len(r.faults) != 1 || r.faults[0].GetAfterPhase() != 0 {
		t.Fatalf("the failed action was not recorded: %v", r.faults)
	}
}

// summaryPods is a pod that leaves summary as its phase's output.
type summaryPods struct{ summary *pb.PhaseSummary }

func (p summaryPods) RunPod(_ context.Context, spec PodSpec) (PodRun, error) {
	encoded, err := proto.Marshal(&pb.PhaseOutput{Summary: p.summary})
	if err != nil {
		return PodRun{}, err
	}
	return PodRun{}, os.WriteFile(filepath.Join(spec.Outputs, OutputFile), encoded, 0o644)
}

func TestAnUnjudgedPhaseHasNoVerdictAndLogsItsFailures(t *testing.T) {
	// 4 of 100 operations were refused (status 400), as an accuracy workload's deliberately
	// invalid requests are: no measurement under the validity rules, and none is asked for.
	summary := &pb.PhaseSummary{
		Model: pb.LoadModel_LOAD_MODEL_OPEN, TargetRate: 100, OfferedRate: 100, AchievedRate: 100,
		Sent: 100, Completed: 100, Errors: 4, ErrorsByKind: map[uint32]uint64{uint32(pb.ErrorKind_ERROR_KIND_STATUS): 4},
		OperationNames: []string{"get"}, Operations: []*pb.OperationSummary{{Operation: 0, Completed: 100}},
	}
	load := &pb.PhaseInput{Model: pb.LoadModel_LOAD_MODEL_OPEN, Rate: 100, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, Secs: 1}
	run := func(unjudged bool) (Ran, []string) {
		var lines []string
		r := &Runner{
			Pods: summaryPods{summary}, Command: func(string, string) []string { return nil }, Dir: t.TempDir(),
			Validity: evaluator.DefaultValidity(), Capture: true, Unjudged: unjudged,
			Log: func(line string) { lines = append(lines, line) }, Now: time.Now,
		}
		ran, err := r.Run(context.Background(), load)
		if err != nil {
			t.Fatal(err)
		}
		return ran, lines
	}
	judged, _ := run(false)
	if !strings.Contains(judged.Verdict.Failure, "4 loadgen errors") {
		t.Fatalf("judged: %+v", judged.Verdict)
	}
	unjudged, lines := run(true)
	if unjudged.Verdict != (Verdict{}) || unjudged.Output == nil {
		t.Fatalf("unjudged: %+v", unjudged)
	}
	if last := lines[len(lines)-1]; !strings.Contains(last, "100 operations sent, 4 failed (status 4)") {
		t.Fatalf("log %q", lines)
	}
}
