package httpcheck

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"strconv"

	"vibesys/microservice-evaluator/api"
)

type Envelope struct {
	Status int
	Msg    any
	Data   any
}

func Response(result api.ProtocolResult, expectedStatus int) (api.HTTPResponse, error) {
	if !result.TransportSuccess {
		return api.HTTPResponse{}, fmt.Errorf(
			"transport failed (%s): %s",
			result.ErrorCategory,
			result.ErrorMessage,
		)
	}
	response, ok := result.Payload.(api.HTTPResponse)
	if !ok {
		return api.HTTPResponse{}, fmt.Errorf("expected HTTP response, got %T", result.Payload)
	}
	if response.StatusCode != expectedStatus {
		return api.HTTPResponse{}, fmt.Errorf(
			"HTTP %d, expected %d; body=%q",
			response.StatusCode,
			expectedStatus,
			truncate(response.Body, 300),
		)
	}
	return response, nil
}

func ExactText(result api.ProtocolResult, expectedStatus int, expected string) error {
	response, err := Response(result, expectedStatus)
	if err != nil {
		return err
	}
	if string(response.Body) != expected {
		return fmt.Errorf("body %q, expected %q", truncate(response.Body, 300), expected)
	}
	return nil
}

func ExactEnvelope(
	result api.ProtocolResult,
	expectedHTTPStatus int,
	expectedApplicationStatus int,
) (Envelope, error) {
	response, err := Response(result, expectedHTTPStatus)
	if err != nil {
		return Envelope{}, err
	}
	value, err := DecodeJSON(response.Body)
	if err != nil {
		return Envelope{}, err
	}
	object, ok := value.(map[string]any)
	if !ok {
		return Envelope{}, fmt.Errorf("response envelope must be an object, got %T", value)
	}
	if err := ExactFields(object, "data", "msg", "status"); err != nil {
		return Envelope{}, fmt.Errorf("response envelope: %w", err)
	}
	status, err := Integer(object["status"])
	if err != nil {
		return Envelope{}, fmt.Errorf("response envelope status: %w", err)
	}
	if status != int64(expectedApplicationStatus) {
		return Envelope{}, fmt.Errorf(
			"application status %d, expected %d",
			status,
			expectedApplicationStatus,
		)
	}
	return Envelope{Status: int(status), Msg: object["msg"], Data: object["data"]}, nil
}

func DecodeJSON(raw []byte) (any, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, fmt.Errorf("decode JSON: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return nil, fmt.Errorf("decode JSON: multiple values")
		}
		return nil, fmt.Errorf("decode trailing JSON: %w", err)
	}
	return value, nil
}

func ExactFields(object map[string]any, fields ...string) error {
	if len(object) != len(fields) {
		return fmt.Errorf("fields differ: got %v, expected %v", sortedKeys(object), fields)
	}
	wanted := make(map[string]struct{}, len(fields))
	for _, field := range fields {
		wanted[field] = struct{}{}
	}
	for field := range object {
		if _, ok := wanted[field]; !ok {
			return fmt.Errorf("unexpected field %q; got %v, expected %v", field, sortedKeys(object), fields)
		}
	}
	return nil
}

func Integer(value any) (int64, error) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, fmt.Errorf("expected integer, got %T (%v)", value, value)
	}
	integer, err := strconv.ParseInt(string(number), 10, 64)
	if err != nil {
		return 0, fmt.Errorf("expected integer, got %q", number)
	}
	return integer, nil
}

func Number(value any) (float64, error) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, fmt.Errorf("expected number, got %T (%v)", value, value)
	}
	parsed, err := strconv.ParseFloat(string(number), 64)
	if err != nil {
		return 0, fmt.Errorf("expected finite number, got %q", number)
	}
	if parsed != parsed || parsed > 1.7976931348623157e308 || parsed < -1.7976931348623157e308 {
		return 0, fmt.Errorf("expected finite number, got %q", number)
	}
	return parsed, nil
}

func Header(result api.ProtocolResult, name string) string {
	for key, values := range result.Metadata {
		if equalFoldASCII(key, name) && len(values) > 0 {
			return values[0]
		}
	}
	return ""
}

func truncate(raw []byte, limit int) string {
	if len(raw) <= limit {
		return string(raw)
	}
	return string(raw[:limit]) + "..."
}

func sortedKeys(object map[string]any) []string {
	keys := make([]string, 0, len(object))
	for key := range object {
		keys = append(keys, key)
	}
	for index := 1; index < len(keys); index++ {
		for position := index; position > 0 && keys[position] < keys[position-1]; position-- {
			keys[position], keys[position-1] = keys[position-1], keys[position]
		}
	}
	return keys
}

func equalFoldASCII(left, right string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		a, b := left[index], right[index]
		if a >= 'A' && a <= 'Z' {
			a += 'a' - 'A'
		}
		if b >= 'A' && b <= 'Z' {
			b += 'a' - 'A'
		}
		if a != b {
			return false
		}
	}
	return true
}
