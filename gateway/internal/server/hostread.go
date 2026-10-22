package server

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"strings"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"google.golang.org/protobuf/proto"
)

// Read runs one registered catalog operation. The runner deadline is that
// operation's cap from the generated registry. A read is not admission: a
// deadline or cancellation is that category, not an unconfirmed admission.
func (h *host) Read(ctx context.Context, request *apiv1.ReadRequest) (*apiv1.ReadResponse, error) {
	dispatched, ok := hostoperations.DispatchHostRead(request)
	if !ok {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "HostRead.method is required"), nil
	}
	switch dispatched.Executor {
	case hostv1.Executor_EXECUTOR_FILESYSTEM:
		return h.completePaths(ctx, request, dispatched.Deadline)
	case hostv1.Executor_EXECUTOR_CANDIDATE_INSPECTION:
		return h.inspectCandidate(ctx, request, dispatched.Deadline)
	}
	checkout, err := checkoutPath(request.GetCheckout())
	if err != nil {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, err.Error()), nil
	}
	input, err := proto.Marshal(request)
	if err != nil {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, err.Error()), nil
	}
	if len(input) > hostcommand.MaxOutput {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "HostRead request exceeds 1 MiB"), nil
	}
	callCtx, cancel := context.WithTimeout(ctx, dispatched.Deadline)
	defer cancel()
	result, err := h.config.HostRunner.Run(callCtx, hostcommand.Command{
		Executable: filepath.Join(checkout, ".venv", "bin", "vibesys"),
		Args:       []string{"host", dispatched.CLI}, Input: input, StateRoot: h.config.Root,
	})
	if errors.Is(err, context.DeadlineExceeded) {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_DEADLINE_EXCEEDED, "host read deadline exceeded"), nil
	}
	if errors.Is(err, context.Canceled) {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_CANCELLED, "host read cancelled"), nil
	}
	if err != nil || result.ExitCode != 0 {
		message := fmt.Sprintf("vibesys host %s exited %d", dispatched.CLI, result.ExitCode)
		if err != nil {
			message += ": " + err.Error()
		}
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL, message+"\n"+result.Stderr), nil
	}
	response := &apiv1.ReadResponse{}
	if err := proto.Unmarshal(result.Output, response); err != nil || response.Result == nil {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "vibesys host "+dispatched.CLI+" returned invalid protobuf"), nil
	}
	if failure := response.GetFailure(); failure != nil {
		failure.Message = safeDiagnostic(strings.TrimSpace(failure.Message + "\n" + result.Stderr))
		if failure.Message == "" {
			failure.Message = "host read failed"
		}
		return response, nil
	}
	if !hostoperations.HostReadResponseValid(request, response) {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "vibesys host "+dispatched.CLI+" returned a different method or identity"), nil
	}
	return response, nil
}

func readFailure(category hostv1.FailureCategory, message string) *apiv1.ReadResponse {
	text := safeDiagnostic(strings.TrimSpace(message))
	if text == "" {
		text = "host read failed"
	}
	return &apiv1.ReadResponse{Result: &apiv1.ReadResponse_Failure{Failure: &hostv1.OperationFailure{
		Category: category, Message: text,
	}}}
}
