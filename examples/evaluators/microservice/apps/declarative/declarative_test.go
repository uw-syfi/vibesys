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
	validation := application.ValidateOperation(operation, api.OperationPlan{}, []api.ProtocolResult{{
		TransportSuccess: true,
		Payload:          api.HTTPResponse{StatusCode: 200, Body: []byte(`{"status":0,"msg":"failed"}`)},
	}})
	if validation.Success || validation.ErrorCategory != "application_status" {
		t.Fatalf("unexpected validation: %+v", validation)
	}
}

func TestBuildOperationExpandsDeterministicVariables(t *testing.T) {
	application := &Application{}
	operation := api.Operation{
		Name: "compose", Target: "api",
		HTTP: &api.HTTPRequestSpec{Path: "/${counter}", Form: map[string]string{"value": "${random}"}},
	}
	plan, err := application.BuildOperation(operation, api.Sample{Counter: 7, Random: 11}, nil)
	if err != nil {
		t.Fatal(err)
	}
	request := plan.Invocations[0].Payload.(api.HTTPRequestSpec)
	if request.Path != "/7" || request.Form["value"] != "11" {
		t.Fatalf("variables were not expanded: %+v", request)
	}
}
