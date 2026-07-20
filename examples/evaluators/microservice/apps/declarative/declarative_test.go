package declarative

import (
	"testing"

	"vibesys/microservice-evaluator/api"
)

func TestValidateRejectsApplicationErrorEnvelope(t *testing.T) {
	status := 1
	application := &Application{}
	operation := api.Operation{Expect: api.Expectation{
		Statuses:            []int{200},
		JSON:                true,
		JSONStatusIfPresent: &status,
	}}
	validation := application.Validate(operation, api.ProtocolResult{
		TransportSuccess: true,
		Payload:          api.HTTPResponse{StatusCode: 200, Body: []byte(`{"status":0,"msg":"failed"}`)},
	})
	if validation.Success || validation.ErrorCategory != "application_status" {
		t.Fatalf("unexpected validation: %+v", validation)
	}
}

func TestBuildInvocationExpandsDeterministicVariables(t *testing.T) {
	application := &Application{}
	operation := api.Operation{
		Name: "compose", Target: "api",
		HTTP: &api.HTTPRequestSpec{Path: "/${counter}", Form: map[string]string{"value": "${random}"}},
	}
	invocation, err := application.BuildInvocation(operation, api.Sample{Counter: 7, Random: 11}, nil)
	if err != nil {
		t.Fatal(err)
	}
	request := invocation.Payload.(api.HTTPRequestSpec)
	if request.Path != "/7" || request.Form["value"] != "11" {
		t.Fatalf("variables were not expanded: %+v", request)
	}
}
