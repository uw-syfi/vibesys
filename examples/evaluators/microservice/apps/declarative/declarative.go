package declarative

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"vibesys/microservice-evaluator/api"
)

type Application struct{}

func New(workload api.Workload) (api.Application, error) {
	if len(workload.ApplicationConfig) != 0 {
		return nil, fmt.Errorf("declarative application does not accept application_config fields")
	}
	targetProtocols := make(map[string]string, len(workload.Targets))
	for _, target := range workload.Targets {
		targetProtocols[target.Name] = target.Protocol
	}
	for _, operation := range workload.Operations {
		if operation.HTTP == nil {
			return nil, fmt.Errorf("operation %q requires an [operations.http] table", operation.Name)
		}
		if targetProtocols[operation.Target] != "http" {
			return nil, fmt.Errorf("operation %q uses an HTTP request with non-HTTP target %q", operation.Name, operation.Target)
		}
	}
	return &Application{}, nil
}

func (a *Application) Name() string {
	return "declarative"
}

func (a *Application) Prepare(context.Context, api.Runtime, api.TrialContext) (any, error) {
	return nil, nil
}

func (a *Application) Reset(context.Context, api.Runtime, api.TrialContext) error {
	return nil
}

func (a *Application) BuildInvocation(operation api.Operation, sample api.Sample, _ any) (api.Invocation, error) {
	if operation.HTTP == nil {
		return api.Invocation{}, fmt.Errorf("operation %q has no HTTP request", operation.Name)
	}
	request := *operation.HTTP
	request.Path = expand(request.Path, sample)
	request.Body = expand(request.Body, sample)
	request.Query = expandMap(request.Query, sample)
	request.Headers = expandMap(request.Headers, sample)
	request.Form = expandMap(request.Form, sample)
	return api.Invocation{Target: operation.Target, Operation: operation.Name, Payload: request}, nil
}

func (a *Application) Validate(operation api.Operation, result api.ProtocolResult) api.ValidationResult {
	if !result.TransportSuccess {
		return api.ValidationResult{ErrorCategory: result.ErrorCategory, ErrorMessage: result.ErrorMessage}
	}
	response, ok := result.Payload.(api.HTTPResponse)
	if !ok {
		return invalid("invalid_response", fmt.Sprintf("expected HTTP response, got %T", result.Payload))
	}
	if !containsStatus(operation.Expect.Statuses, response.StatusCode) {
		return invalid("http_status", fmt.Sprintf("unexpected HTTP status %d", response.StatusCode))
	}
	if operation.Expect.TextContains != "" && !strings.Contains(string(response.Body), operation.Expect.TextContains) {
		return invalid("response_text", fmt.Sprintf("response does not contain %q", operation.Expect.TextContains))
	}
	if operation.Expect.JSON || operation.Expect.JSONStatusIfPresent != nil {
		var payload any
		if err := json.Unmarshal(response.Body, &payload); err != nil {
			return invalid("response_json", err.Error())
		}
		if operation.Expect.JSONObjectRequiresStatusOrData {
			if object, objectOK := payload.(map[string]any); objectOK {
				_, hasStatus := object["status"]
				_, hasData := object["data"]
				if !hasStatus && !hasData {
					return invalid("response_shape", "JSON object contains neither status nor data")
				}
			}
		}
		if expected := operation.Expect.JSONStatusIfPresent; expected != nil {
			if object, objectOK := payload.(map[string]any); objectOK {
				if value, exists := object["status"]; exists {
					number, numberOK := value.(float64)
					if !numberOK || number != float64(*expected) {
						return invalid("application_status", fmt.Sprintf("response status is %v, want %d", value, *expected))
					}
				}
			}
		}
	}
	custom, err := captureHeaders(operation, result.Metadata)
	if err != nil {
		return invalid("timing_header", err.Error())
	}
	return api.ValidationResult{Success: true, CustomTimings: custom}
}

func invalid(category string, message string) api.ValidationResult {
	return api.ValidationResult{ErrorCategory: category, ErrorMessage: message}
}

func containsStatus(statuses []int, status int) bool {
	for _, candidate := range statuses {
		if candidate == status {
			return true
		}
	}
	return false
}

func expand(value string, sample api.Sample) string {
	value = strings.ReplaceAll(value, "${counter}", strconv.FormatInt(sample.Counter, 10))
	return strings.ReplaceAll(value, "${random}", strconv.FormatUint(sample.Random, 10))
}

func expandMap(values map[string]string, sample api.Sample) map[string]string {
	if values == nil {
		return nil
	}
	result := make(map[string]string, len(values))
	for key, value := range values {
		result[key] = expand(value, sample)
	}
	return result
}

func captureHeaders(operation api.Operation, metadata map[string][]string) (map[string]time.Duration, error) {
	if len(operation.CaptureHeaders) == 0 {
		return nil, nil
	}
	result := make(map[string]time.Duration, len(operation.CaptureHeaders))
	for _, capture := range operation.CaptureHeaders {
		values := metadata[http.CanonicalHeaderKey(capture.Header)]
		if len(values) == 0 {
			continue
		}
		milliseconds, err := strconv.ParseFloat(values[0], 64)
		if err != nil {
			return nil, fmt.Errorf("header %s is not numeric: %w", capture.Header, err)
		}
		result[capture.Name] = time.Duration(milliseconds * float64(time.Millisecond))
	}
	return result, nil
}
