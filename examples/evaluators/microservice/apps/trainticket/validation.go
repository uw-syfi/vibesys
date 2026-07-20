package trainticket

import (
	"encoding/json"
	"fmt"
	"reflect"
	"sort"
	"strings"

	"vibesys/microservice-evaluator/api"
)

var entityFields = map[string][]string{
	"config":  {"description", "name", "value"},
	"station": {"id", "name", "stayTime"},
	"train":   {"averageSpeed", "confortClass", "economyClass", "id"},
	"travel":  {"endTime", "routeId", "startingStationId", "startingTime", "stationsId", "terminalStationId", "trainTypeId", "tripId"},
	"route":   {"distances", "id", "startStationId", "stations", "terminalStationId"},
	"price":   {"basicPriceRate", "firstClassPriceRate", "id", "routeId", "trainType"},
}

func (a *Application) ValidateOperation(operation api.Operation, plan api.OperationPlan, results []api.ProtocolResult) api.ValidationResult {
	state, ok := plan.State.(*operationState)
	if !ok {
		return invalid("invalid_plan", fmt.Sprintf("unexpected Train Ticket plan state %T", plan.State))
	}
	if len(results) != len(state.expectations) {
		return invalid("result_count", fmt.Sprintf("got %d protocol results, want %d", len(results), len(state.expectations)))
	}
	for index, expectation := range state.expectations {
		validation := validateStep(results[index], expectation)
		if index == 0 && state.commit != nil && validation.Success {
			state.commit()
			state.commit = nil
		}
		if !validation.Success {
			isReadYourWrite := strings.HasPrefix(operation.Name, "update_read_") && index == 1
			isEphemeralRead := operation.Name == "create_read_delete_config" && (index == 1 || index == 3)
			if isReadYourWrite || isEphemeralRead {
				validation.ErrorCategory = "read_your_write"
			}
			validation.ErrorMessage = fmt.Sprintf("step %d: %s", index+1, validation.ErrorMessage)
			return validation
		}
	}
	return api.ValidationResult{Success: true}
}

func validateStep(result api.ProtocolResult, expectation stepExpectation) api.ValidationResult {
	base := validateEnvelopeResult(result, expectation.status, expectation.appStatus)
	if !base.Success {
		return base
	}
	response := result.Payload.(api.HTTPResponse)
	var envelope map[string]any
	_ = json.Unmarshal(response.Body, &envelope)
	data := envelope["data"]
	switch expectation.kind {
	case expectExactData:
		expected, err := normalizedJSON(expectation.expected)
		if err != nil {
			return invalid("validator", err.Error())
		}
		if !reflect.DeepEqual(data, expected) {
			return invalid("response_value", fmt.Sprintf("data mismatch: got %v, want %v", data, expected))
		}
	case expectEntityList:
		items, ok := data.([]any)
		if !ok || len(items) == 0 {
			return invalid("response_shape", fmt.Sprintf("%s list data must be a non-empty array", expectation.service))
		}
		want := entityFields[expectation.service]
		for index, item := range items {
			object, objectOK := item.(map[string]any)
			if !objectOK {
				return invalid("response_shape", fmt.Sprintf("%s list item %d is not an object", expectation.service, index))
			}
			keys := make([]string, 0, len(object))
			for key := range object {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			if !reflect.DeepEqual(keys, want) {
				return invalid("response_schema", fmt.Sprintf("%s list item %d fields %v, want %v", expectation.service, index, keys, want))
			}
		}
	}
	return api.ValidationResult{Success: true}
}

func validateEnvelopeResult(result api.ProtocolResult, httpStatus, appStatus int) api.ValidationResult {
	if !result.TransportSuccess {
		return invalid(result.ErrorCategory, result.ErrorMessage)
	}
	response, ok := result.Payload.(api.HTTPResponse)
	if !ok {
		return invalid("invalid_response", fmt.Sprintf("expected HTTP response, got %T", result.Payload))
	}
	if response.StatusCode != httpStatus {
		return invalid("http_status", fmt.Sprintf("HTTP %d, want %d", response.StatusCode, httpStatus))
	}
	var envelope map[string]any
	if err := json.Unmarshal(response.Body, &envelope); err != nil {
		return invalid("response_json", err.Error())
	}
	keys := make([]string, 0, len(envelope))
	for key := range envelope {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	if !reflect.DeepEqual(keys, []string{"data", "msg", "status"}) {
		return invalid("response_schema", fmt.Sprintf("envelope fields %v, want [data msg status]", keys))
	}
	status, ok := envelope["status"].(float64)
	if !ok || status != float64(appStatus) {
		return invalid("application_status", fmt.Sprintf("application status %v, want %d", envelope["status"], appStatus))
	}
	if _, ok := envelope["msg"].(string); !ok {
		return invalid("response_schema", "envelope msg must be a string")
	}
	return api.ValidationResult{Success: true}
}

func normalizedJSON(value any) (any, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return nil, fmt.Errorf("encode expected value: %w", err)
	}
	var normalized any
	if err := json.Unmarshal(encoded, &normalized); err != nil {
		return nil, fmt.Errorf("normalize expected value: %w", err)
	}
	return normalized, nil
}

func invalid(category, message string) api.ValidationResult {
	return api.ValidationResult{ErrorCategory: category, ErrorMessage: message}
}
