package server

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"google.golang.org/protobuf/proto"
)

const hostCallDeadline = 30 * time.Second

// HostCall dispatches typed methods and carries opaque protobuf across stdin and
// stdout. Python owns all validation and product policy, including inbox writes.
func (h *host) HostCall(ctx context.Context, request *apiv1.HostCallRequest) (*apiv1.HostCallResponse, error) {
	method := ""
	requestID := ""
	switch {
	case request.GetPostIntent() != nil:
		method, requestID = "post-intent", request.GetPostIntent().GetRequestId()
	case request.GetChatMessage() != nil:
		method, requestID = "chat-message", request.GetChatMessage().GetRequestId()
	default:
		return hostFailure("invalid_method", "HostCall.method is required", -1, ""), nil
	}
	checkout, err := checkoutPath(request.GetCheckout())
	if err != nil {
		return hostFailure("invalid_checkout", err.Error(), -1, ""), nil
	}
	input, err := proto.Marshal(request)
	if err != nil {
		return hostFailure("invalid_input", err.Error(), -1, ""), nil
	}
	if len(input) > hostcommand.MaxOutput {
		return hostFailure("invalid_input", "HostCall request exceeds 1 MiB", -1, ""), nil
	}
	callCtx, cancel := context.WithTimeout(ctx, hostCallDeadline)
	defer cancel()
	result, err := h.config.HostRunner.Run(callCtx, hostcommand.Command{
		Executable: filepath.Join(checkout, ".venv", "bin", "vibesys"),
		Args:       []string{"host", method}, Input: input, StateRoot: h.config.Root,
	})
	if err != nil || result.ExitCode != 0 {
		message := fmt.Sprintf("vibesys host %s exited %d", method, result.ExitCode)
		if err != nil {
			message += ": " + err.Error()
		}
		return hostFailure("host_command_failed", message, result.ExitCode, result.Stderr), nil
	}
	response := &apiv1.HostCallResponse{}
	if err := proto.Unmarshal(result.Output, response); err != nil || response.Result == nil {
		return hostFailure("invalid_host_response", "vibesys host "+method+" returned invalid protobuf", result.ExitCode, result.Stderr), nil
	}
	if failure := response.GetConfigurationFailed(); failure != nil {
		failure.Stderr = safeDiagnostic(strings.TrimSpace(failure.Stderr + "\n" + result.Stderr))
	} else {
		valid := method == "post-intent" && response.GetPostIntent() != nil && response.GetPostIntent().GetRequestId() == requestID
		if method == "chat-message" {
			valid = response.GetChatMessage() != nil && response.GetChatMessage().GetRequestId() == requestID && response.GetChatMessage().GetSessionId() == request.GetChatMessage().GetSessionId()
		}
		if !valid {
			return hostFailure("invalid_host_response", "vibesys host "+method+" returned a different method or identity", result.ExitCode, result.Stderr), nil
		}
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
		return "", fmt.Errorf("HostCall.checkout must be an absolute path or ~/ path")
	}
	return filepath.Clean(value), nil
}
