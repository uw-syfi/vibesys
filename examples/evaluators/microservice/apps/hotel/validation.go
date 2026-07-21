package hotel

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"reflect"
	"sort"
	"strconv"

	"vibesys/microservice-evaluator/api"
)

const (
	loginSuccess       = "Login successfully!"
	loginFailure       = "Failed. Please check your username and password. "
	reservationSuccess = "Reserve successfully!"
	reservationFailure = "Failed. Already reserved. "
)

func (a *Application) ValidateOperation(
	operation api.Operation,
	plan api.OperationPlan,
	results []api.ProtocolResult,
) api.ValidationResult {
	state, ok := plan.State.(*operationState)
	if !ok || state == nil {
		return invalid("invalid_plan", fmt.Sprintf("unexpected Hotel plan state %T", plan.State))
	}
	if state.kind != operation.Name {
		return invalid("invalid_plan", fmt.Sprintf("plan kind %q does not match operation %q", state.kind, operation.Name))
	}
	wantResults := 1
	if operation.Name == operationReserveCapacity {
		wantResults = 2
	}
	if len(results) != wantResults {
		return invalid("result_count", fmt.Sprintf("got %d protocol results, want %d", len(results), wantResults))
	}

	switch operation.Name {
	case operationSearch, operationRecommendDistance, operationRecommendRate, operationRecommendPrice:
		return validateGeoJSON(results[0], state.expectedIDs)
	case operationLoginValid:
		return validateMessage(results[0], loginSuccess)
	case operationLoginInvalid:
		return validateMessage(results[0], loginFailure)
	case operationReserveCapacity:
		if validation := validateMessage(results[0], reservationSuccess); !validation.Success {
			validation.ErrorMessage = "capacity-fill step: " + validation.ErrorMessage
			return validation
		}
		validation := validateMessage(results[1], reservationFailure)
		if !validation.Success {
			validation.ErrorMessage = "over-capacity rejection step: " + validation.ErrorMessage
		}
		return validation
	default:
		return invalid("invalid_plan", fmt.Sprintf("unknown Hotel operation %q", operation.Name))
	}
}

func validateMessage(result api.ProtocolResult, expected string) api.ValidationResult {
	value, validation := decodeHTTPJSON(result)
	if !validation.Success {
		return validation
	}
	object, ok := value.(map[string]any)
	if !ok {
		return invalid("response_schema", fmt.Sprintf("message response must be an object, got %T", value))
	}
	if keys := sortedKeys(object); !reflect.DeepEqual(keys, []string{"message"}) {
		return invalid("response_schema", fmt.Sprintf("message response fields %v, want [message]", keys))
	}
	message, ok := object["message"].(string)
	if !ok {
		return invalid("response_schema", "message must be a string")
	}
	if message != expected {
		return invalid("response_value", fmt.Sprintf("message = %q, want %q", message, expected))
	}
	return api.ValidationResult{Success: true}
}

func validateGeoJSON(
	result api.ProtocolResult,
	expectedIDs []string,
) api.ValidationResult {
	value, validation := decodeHTTPJSON(result)
	if !validation.Success {
		return validation
	}
	root, ok := value.(map[string]any)
	if !ok {
		return invalid("response_schema", fmt.Sprintf("GeoJSON root must be an object, got %T", value))
	}
	if keys := sortedKeys(root); !reflect.DeepEqual(keys, []string{"features", "type"}) {
		return invalid("response_schema", fmt.Sprintf("GeoJSON root fields %v, want [features type]", keys))
	}
	if root["type"] != "FeatureCollection" {
		return invalid("response_schema", fmt.Sprintf("GeoJSON type = %v, want FeatureCollection", root["type"]))
	}
	features, ok := root["features"].([]any)
	if !ok {
		return invalid("response_schema", "GeoJSON features must be an array")
	}
	if len(features) != len(expectedIDs) {
		return invalid(
			"response_value",
			fmt.Sprintf("GeoJSON has %d features, want exactly %d", len(features), len(expectedIDs)),
		)
	}
	seen := make(map[string]struct{}, len(features))
	actualIDs := make([]string, 0, len(features))
	for index, raw := range features {
		id, err := validateFeature(raw)
		if err != nil {
			return invalid("response_schema", fmt.Sprintf("feature %d: %v", index, err))
		}
		if _, duplicate := seen[id]; duplicate {
			return invalid("response_value", fmt.Sprintf("GeoJSON contains duplicate hotel ID %q", id))
		}
		seen[id] = struct{}{}
		actualIDs = append(actualIDs, id)
	}
	sort.Strings(actualIDs)
	want := append([]string(nil), expectedIDs...)
	sort.Strings(want)
	if !reflect.DeepEqual(actualIDs, want) {
		return invalid("response_value", fmt.Sprintf("hotel IDs %v, want exact set %v", actualIDs, want))
	}
	return api.ValidationResult{Success: true}
}

func validateFeature(raw any) (string, error) {
	feature, ok := raw.(map[string]any)
	if !ok {
		return "", fmt.Errorf("must be an object, got %T", raw)
	}
	if keys := sortedKeys(feature); !reflect.DeepEqual(keys, []string{"geometry", "id", "properties", "type"}) {
		return "", fmt.Errorf("fields %v, want [geometry id properties type]", keys)
	}
	if feature["type"] != "Feature" {
		return "", fmt.Errorf("type = %v, want Feature", feature["type"])
	}
	id, ok := feature["id"].(string)
	if !ok {
		return "", fmt.Errorf("id must be a string")
	}
	expected, err := profileForID(id)
	if err != nil {
		return "", err
	}
	properties, ok := feature["properties"].(map[string]any)
	if !ok {
		return "", fmt.Errorf("properties must be an object")
	}
	if keys := sortedKeys(properties); !reflect.DeepEqual(keys, []string{"name", "phone_number"}) {
		return "", fmt.Errorf("property fields %v, want [name phone_number]", keys)
	}
	if properties["name"] != expected.name || properties["phone_number"] != expected.phoneNumber {
		return "", fmt.Errorf("properties %v do not match catalog for hotel %s", properties, id)
	}
	geometry, ok := feature["geometry"].(map[string]any)
	if !ok {
		return "", fmt.Errorf("geometry must be an object")
	}
	if keys := sortedKeys(geometry); !reflect.DeepEqual(keys, []string{"coordinates", "type"}) {
		return "", fmt.Errorf("geometry fields %v, want [coordinates type]", keys)
	}
	if geometry["type"] != "Point" {
		return "", fmt.Errorf("geometry type = %v, want Point", geometry["type"])
	}
	coordinates, ok := geometry["coordinates"].([]any)
	if !ok || len(coordinates) != 2 {
		return "", fmt.Errorf("coordinates must be a two-element array")
	}
	lon, lonOK := coordinates[0].(float64)
	lat, latOK := coordinates[1].(float64)
	if !lonOK || !latOK || math.IsNaN(lon) || math.IsInf(lon, 0) || math.IsNaN(lat) || math.IsInf(lat, 0) {
		return "", fmt.Errorf("coordinates must contain finite numbers")
	}
	wantLon := encodedFloat32(expected.lon)
	wantLat := encodedFloat32(expected.lat)
	if lon != wantLon || lat != wantLat {
		return "", fmt.Errorf("coordinates [%v %v], want catalog [%v %v]", lon, lat, wantLon, wantLat)
	}
	return id, nil
}

func encodedFloat32(value float32) float64 {
	encoded := strconv.FormatFloat(float64(value), 'g', -1, 32)
	parsed, err := strconv.ParseFloat(encoded, 64)
	if err != nil {
		panic(err)
	}
	return parsed
}

func decodeHTTPJSON(result api.ProtocolResult) (any, api.ValidationResult) {
	if !result.TransportSuccess {
		return nil, invalid(result.ErrorCategory, result.ErrorMessage)
	}
	response, ok := result.Payload.(api.HTTPResponse)
	if !ok {
		return nil, invalid("invalid_response", fmt.Sprintf("expected HTTP response, got %T", result.Payload))
	}
	if response.StatusCode != http.StatusOK {
		return nil, invalid("http_status", fmt.Sprintf("HTTP %d, want %d", response.StatusCode, http.StatusOK))
	}
	var value any
	if err := json.Unmarshal(response.Body, &value); err != nil {
		return nil, invalid("response_json", err.Error())
	}
	return value, api.ValidationResult{Success: true}
}

func sortedKeys(object map[string]any) []string {
	keys := make([]string, 0, len(object))
	for key := range object {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func invalid(category, message string) api.ValidationResult {
	return api.ValidationResult{ErrorCategory: category, ErrorMessage: message}
}
