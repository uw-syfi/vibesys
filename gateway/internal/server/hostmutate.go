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
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Mutate runs one registered unary operation. An admission drops the caller's
// cancellation and keeps the runner until the operation cap, so a disconnect
// does not kill a start that has already been admitted.
func (h *host) Mutate(ctx context.Context, request *apiv1.MutateRequest) (*apiv1.MutateResponse, error) {
	dispatched, ok := hostoperations.DispatchHostMutate(request)
	if !ok {
		return mutateFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "HostMutate.method is required"), nil
	}
	checkout, err := checkoutPath(request.GetCheckout())
	if err != nil {
		return mutateFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, err.Error()), nil
	}
	parent := ctx
	if dispatched.Effect == hostv1.OperationEffect_OPERATION_EFFECT_ADMISSION {
		parent = context.WithoutCancel(ctx)
	}
	callCtx, cancel := context.WithTimeout(parent, dispatched.Deadline)
	defer cancel()
	request = proto.Clone(request).(*apiv1.MutateRequest)
	if terminate := request.GetTerminateRun(); terminate != nil {
		deadline, _ := callCtx.Deadline()
		terminate.Deadline = timestamppb.New(deadline)
	}
	input, err := proto.Marshal(request)
	if err != nil {
		return mutateFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, err.Error()), nil
	}
	if len(input) > hostcommand.MaxOutput {
		return mutateFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "HostMutate request exceeds 1 MiB"), nil
	}
	result, err := h.config.HostRunner.Run(callCtx, hostcommand.Command{
		Executable: filepath.Join(checkout, ".venv", "bin", "vibesys"),
		Args:       []string{"host", dispatched.CLI}, Input: input, StateRoot: h.config.Root,
	})
	if errors.Is(err, context.DeadlineExceeded) {
		return mutateFailure(hostv1.FailureCategory_FAILURE_CATEGORY_DEADLINE_EXCEEDED, "host mutate deadline exceeded"), nil
	}
	if errors.Is(err, context.Canceled) {
		return mutateFailure(hostv1.FailureCategory_FAILURE_CATEGORY_CANCELLED, "host mutate cancelled"), nil
	}
	if err != nil || result.ExitCode != 0 {
		message := fmt.Sprintf("vibesys host %s exited %d", dispatched.CLI, result.ExitCode)
		if err != nil {
			message += ": " + err.Error()
		}
		return mutateFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL, message+"\n"+result.Stderr), nil
	}
	response := &apiv1.MutateResponse{}
	if err := proto.Unmarshal(result.Output, response); err != nil || response.Result == nil {
		return mutateFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "vibesys host "+dispatched.CLI+" returned invalid protobuf"), nil
	}
	if failure := response.GetFailure(); failure != nil {
		failure.Message = safeDiagnostic(strings.TrimSpace(failure.Message + "\n" + result.Stderr))
		if failure.Message == "" {
			failure.Message = "host mutate failed"
		}
		return response, nil
	}
	if !hostoperations.HostMutateResponseValid(request, response) {
		return mutateFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "vibesys host "+dispatched.CLI+" returned a different method or identity"), nil
	}
	return response, nil
}

func mutateFailure(category hostv1.FailureCategory, message string) *apiv1.MutateResponse {
	text := safeDiagnostic(strings.TrimSpace(message))
	if text == "" {
		text = "host mutate failed"
	}
	return &apiv1.MutateResponse{Result: &apiv1.MutateResponse_Failure{Failure: &hostv1.OperationFailure{
		Category: category, Message: text,
	}}}
}
