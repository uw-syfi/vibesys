package trainticket

import (
	"encoding/json"
	"fmt"
	"math"
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
	case expectEntityList, expectEntityAbsent:
		items, ok := data.([]any)
		if !ok {
			return invalid("response_shape", fmt.Sprintf("%s list data must be an array", expectation.service))
		}
		if expectation.kind == expectEntityList && len(items) == 0 {
			return invalid("response_shape", fmt.Sprintf("%s list data must be a non-empty array", expectation.service))
		}
		want := entityFields[expectation.service]
		expected, err := normalizedJSON(expectation.expected)
		if err != nil {
			return invalid("validator", err.Error())
		}
		expectedKey := ""
		if expectation.kind == expectEntityAbsent {
			expectedObject, objectOK := expected.(map[string]any)
			if !objectOK {
				return invalid("validator", fmt.Sprintf("expected %s entity must be an object", expectation.service))
			}
			expectedKey, err = entityKey(expectation.service, expectedObject)
			if err != nil {
				return invalid("validator", err.Error())
			}
		}
		foundExpected := false
		seen := make(map[string]struct{}, len(items))
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
			if valueErr := validateEntityValue(expectation.service, object); valueErr != nil {
				return invalid("response_schema", fmt.Sprintf("%s list item %d: %v", expectation.service, index, valueErr))
			}
			key, keyErr := entityKey(expectation.service, object)
			if keyErr != nil {
				return invalid("response_schema", fmt.Sprintf("%s list item %d: %v", expectation.service, index, keyErr))
			}
			if _, duplicate := seen[key]; duplicate {
				return invalid("response_value", fmt.Sprintf("%s list contains duplicate key %q", expectation.service, key))
			}
			seen[key] = struct{}{}
			if (expectation.kind == expectEntityList && reflect.DeepEqual(item, expected)) ||
				(expectation.kind == expectEntityAbsent && key == expectedKey) {
				foundExpected = true
			}
		}
		if expectation.kind == expectEntityList && !foundExpected {
			return invalid(
				"response_value",
				fmt.Sprintf("%s list omitted or returned stale selected runtime record", expectation.service),
			)
		}
		if expectation.kind == expectEntityAbsent && foundExpected {
			return invalid("response_value", fmt.Sprintf("deleted %s record remains visible in list", expectation.service))
		}
	}
	return api.ValidationResult{Success: true}
}

func validateEntityValue(service string, object map[string]any) error {
	requireString := func(field string) error {
		if _, ok := object[field].(string); !ok {
			return fmt.Errorf("%s must be a string", field)
		}
		return nil
	}
	requireInteger := func(field string) error {
		value, ok := object[field].(float64)
		if !ok || math.Trunc(value) != value {
			return fmt.Errorf("%s must be an integer", field)
		}
		return nil
	}
	stringFields := map[string][]string{
		"config":  {"name", "value", "description"},
		"station": {"id", "name"},
		"train":   {"id"},
		"travel":  {"trainTypeId", "routeId", "startingStationId", "stationsId", "terminalStationId"},
		"route":   {"id", "startStationId", "terminalStationId"},
		"price":   {"id", "trainType", "routeId"},
	}
	for _, field := range stringFields[service] {
		if err := requireString(field); err != nil {
			return err
		}
	}
	switch service {
	case "station":
		return requireInteger("stayTime")
	case "train":
		for _, field := range []string{"economyClass", "confortClass", "averageSpeed"} {
			if err := requireInteger(field); err != nil {
				return err
			}
		}
	case "route":
		stations, stationsOK := object["stations"].([]any)
		distances, distancesOK := object["distances"].([]any)
		if !stationsOK || !distancesOK || len(stations) != len(distances) {
			return fmt.Errorf("stations and distances must be equal-length arrays")
		}
		for _, station := range stations {
			if _, ok := station.(string); !ok {
				return fmt.Errorf("stations must contain strings")
			}
		}
		for _, distance := range distances {
			value, ok := distance.(float64)
			if !ok || math.Trunc(value) != value {
				return fmt.Errorf("distances must contain integers")
			}
		}
	case "price":
		for _, field := range []string{"basicPriceRate", "firstClassPriceRate"} {
			value, ok := object[field].(float64)
			if !ok || math.IsNaN(value) || math.IsInf(value, 0) {
				return fmt.Errorf("%s must be numeric", field)
			}
		}
	case "travel":
		if _, err := entityKey(service, object); err != nil {
			return err
		}
		for _, field := range []string{"startingTime", "endTime"} {
			if err := requireInteger(field); err != nil {
				return err
			}
		}
	}
	return nil
}

func entityKey(service string, object map[string]any) (string, error) {
	field := "id"
	if service == "config" {
		field = "name"
	}
	if service == "travel" {
		trip, ok := object["tripId"].(map[string]any)
		if !ok {
			return "", fmt.Errorf("tripId must be an object")
		}
		if len(trip) != 2 {
			return "", fmt.Errorf("tripId must contain exactly type and number")
		}
		kind, kindOK := trip["type"].(string)
		number, numberOK := trip["number"].(string)
		if !kindOK || (kind != "G" && kind != "D") || !numberOK || number == "" {
			return "", fmt.Errorf("tripId must have type G or D and a non-empty string number")
		}
		return kind + number, nil
	}
	key, ok := object[field].(string)
	if !ok {
		return "", fmt.Errorf("%s must be a string", field)
	}
	return key, nil
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
