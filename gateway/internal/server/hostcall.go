package server

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// HostCall dispatches typed methods and carries opaque protobuf across stdin and
// stdout. Python owns all validation and product policy, including inbox writes.
// Method names, runner caps and response correlations come from the registry.
func (h *host) HostCall(ctx context.Context, request *apiv1.HostCallRequest) (*apiv1.HostCallResponse, error) {
	dispatched, ok := hostoperations.DispatchHostCall(request)
	if !ok {
		return hostFailure("invalid_method", "HostCall.method is required", -1, ""), nil
	}
	checkout, err := checkoutPath(request.GetCheckout())
	if err != nil {
		return hostFailure("invalid_checkout", err.Error(), -1, ""), nil
	}
	callCtx, cancel := context.WithTimeout(ctx, dispatched.Deadline)
	defer cancel()
	request = proto.Clone(request).(*apiv1.HostCallRequest)
	deadline, _ := callCtx.Deadline()
	request.Deadline = timestamppb.New(deadline)
	input, err := proto.Marshal(request)
	if err != nil {
		return hostFailure("invalid_input", err.Error(), -1, ""), nil
	}
	if len(input) > hostcommand.MaxOutput {
		return hostFailure("invalid_input", "HostCall request exceeds 1 MiB", -1, ""), nil
	}
	result, err := h.config.HostRunner.Run(callCtx, hostcommand.Command{
		Executable: filepath.Join(checkout, ".venv", "bin", "vibesys"),
		Args:       []string{"host", dispatched.CLI}, Input: input, StateRoot: h.config.Root,
	})
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		return hostFailure("admission_unconfirmed", "The host did not confirm admission. Check the run status or retry with the same request id.", result.ExitCode, result.Stderr), nil
	}
	if request.GetRelaunch() != nil && errors.Is(err, os.ErrNotExist) {
		return &apiv1.HostCallResponse{Result: &apiv1.HostCallResponse_Relaunch{Relaunch: &apiv1.RelaunchResponse{Outcome: &apiv1.RelaunchResponse_Refusal{Refusal: apiv1.RelaunchRefusal_RELAUNCH_REFUSAL_CHECKOUT_MISSING}}}}, nil
	}
	if err != nil || result.ExitCode != 0 {
		if exitFailureCategory(dispatched.CLI, result, err) == hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED {
			return hostFailure("host_unsupported", unsupportedMessage(dispatched.CLI), result.ExitCode, ""), nil
		}
		message := fmt.Sprintf("vibesys host %s exited %d", dispatched.CLI, result.ExitCode)
		if err != nil {
			message += ": " + err.Error()
		}
		return hostFailure("host_command_failed", message, result.ExitCode, result.Stderr), nil
	}
	response := &apiv1.HostCallResponse{}
	if err := proto.Unmarshal(result.Output, response); err != nil || response.Result == nil {
		return hostFailure("invalid_host_response", "vibesys host "+dispatched.CLI+" returned invalid protobuf", result.ExitCode, result.Stderr), nil
	}
	if failure := response.GetConfigurationFailed(); failure != nil {
		failure.Stderr = safeDiagnostic(strings.TrimSpace(failure.Stderr + "\n" + result.Stderr))
	} else if !hostoperations.HostCallResponseValid(request, response) {
		return hostFailure("invalid_host_response", "vibesys host "+dispatched.CLI+" returned a different method or identity", result.ExitCode, result.Stderr), nil
	}
	return response, nil
}

func hostFailure(code, message string, exitCode int, stderr string) *apiv1.HostCallResponse {
	return &apiv1.HostCallResponse{Result: &apiv1.HostCallResponse_ConfigurationFailed{ConfigurationFailed: &apiv1.ConfigurationFailed{
		Code: code, Stage: "host_call", Message: safeDiagnostic(message), ExitCode: int32(exitCode), Stderr: safeDiagnostic(stderr),
	}}}
}

func safeDiagnostic(value string) string {
	value = strings.ToValidUTF8(value, "�")
	if len(value) > hostcommand.MaxStderr {
		value = value[:hostcommand.MaxStderr]
	}
	for !utf8.ValidString(value) {
		value = value[:len(value)-1]
	}
	return value
}

func checkoutPath(value string) (string, error) {
	if value == "~" || strings.HasPrefix(value, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		value = filepath.Join(home, strings.TrimPrefix(strings.TrimPrefix(value, "~"), "/"))
	}
	if !filepath.IsAbs(value) || strings.ContainsAny(value, "\x00\n\r") || len(value) > 4096 {
		return "", fmt.Errorf("checkout must be an absolute path or ~/ path")
	}
	return filepath.Clean(value), nil
}
