// Package wire reaches into package workload for the module's other packages: the proto message
// of a workload.Step, which workload keeps unexported. workload sets StepMessage when it is
// initialized.
package wire

import (
	"bytes"
	"fmt"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
)

// StepMessage is the message of a workload.Step; nil for an empty Step.
var StepMessage func(step any) *pb.Step

// ProbePlan is the ready steps as a request stream for `loadgen probe`, one operation per step
// (Python's probe_plan writes the same).
func ProbePlan(steps []*pb.Step, writer string) ([]byte, error) {
	var out bytes.Buffer
	w, err := files.NewWriter(&out, files.Binpb, files.Header(pb.FileKind_FILE_KIND_REQUESTS, writer))
	if err != nil {
		return nil, err
	}
	names := make([]string, len(steps))
	for n := range steps {
		names[n] = fmt.Sprintf("ready_%d", n)
	}
	if err := w.Write(&pb.RequestsHeader{OperationNames: names}); err != nil {
		return nil, err
	}
	for n, step := range steps {
		if step == nil {
			return nil, fmt.Errorf("ready step %d is empty (build steps with workload.HTTP, Get, Post or Put)", n)
		}
		if err := w.Write(&pb.PlannedOperation{Operation: uint32(n), Steps: []*pb.Step{step}}); err != nil {
			return nil, err
		}
	}
	if err := w.Flush(); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}
