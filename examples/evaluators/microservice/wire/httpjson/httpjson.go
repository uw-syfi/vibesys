// Package httpjson constructs mode-neutral JSON-over-HTTP requests.
package httpjson

import (
	"bytes"
	"encoding/json"
	"fmt"

	"vibesys/microservice-evaluator/api"
)

// Request constructs an HTTP request with the evaluator's canonical JSON
// headers and encoding. Canonicalizing through an untyped JSON value makes map
// and struct inputs produce the same observable key ordering.
func Request(method, path string, body any, authorization string) (api.HTTPRequestSpec, error) {
	spec := api.HTTPRequestSpec{
		Method:  method,
		Path:    path,
		Headers: map[string]string{"Accept": "application/json"},
	}
	if authorization != "" {
		spec.Headers["Authorization"] = authorization
	}
	if body == nil {
		return spec, nil
	}
	encoded, err := Marshal(body)
	if err != nil {
		return api.HTTPRequestSpec{}, err
	}
	spec.Body = string(encoded)
	spec.Headers["Content-Type"] = "application/json"
	return spec, nil
}

// MustRequest is for evaluator-owned values whose failure to encode is a
// programming error.
func MustRequest(method, path string, body any, authorization string) api.HTTPRequestSpec {
	spec, err := Request(method, path, body, authorization)
	if err != nil {
		panic(err)
	}
	return spec
}

// Marshal returns a stable JSON representation regardless of whether callers
// provide structs or maps. Numbers retain their original JSON representation.
func Marshal(value any) ([]byte, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return nil, fmt.Errorf("marshal HTTP JSON body: %w", err)
	}
	decoder := json.NewDecoder(bytes.NewReader(encoded))
	decoder.UseNumber()
	var normalized any
	if err := decoder.Decode(&normalized); err != nil {
		return nil, fmt.Errorf("normalize HTTP JSON body: %w", err)
	}
	canonical, err := json.Marshal(normalized)
	if err != nil {
		return nil, fmt.Errorf("encode canonical HTTP JSON body: %w", err)
	}
	return canonical, nil
}
