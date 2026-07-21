package httpcheck

import (
	"testing"

	"vibesys/microservice-evaluator/api"
)

func response(body string) api.ProtocolResult {
	return api.ProtocolResult{
		TransportSuccess: true,
		Payload:          api.HTTPResponse{StatusCode: 200, Body: []byte(body)},
	}
}

func TestExactEnvelopeRejectsSchemaAndTypeSubstitution(t *testing.T) {
	tests := []string{
		`{"status":1,"msg":"ok"}`,
		`{"status":1,"msg":"ok","data":{},"extra":true}`,
		`{"status":"1","msg":"ok","data":{}}`,
		`{"status":1.0,"msg":"ok","data":{}}`,
		`{"status":1,"msg":"ok","data":{}} {}`,
	}
	for _, body := range tests {
		if _, err := ExactEnvelope(response(body), 200, 1); err == nil {
			t.Fatalf("accepted invalid envelope %s", body)
		}
	}
}

func TestExactEnvelopePreservesNumberTypes(t *testing.T) {
	envelope, err := ExactEnvelope(
		response(`{"status":1,"msg":"ok","data":{"integer":3,"number":3.5}}`),
		200,
		1,
	)
	if err != nil {
		t.Fatal(err)
	}
	data := envelope.Data.(map[string]any)
	if integer, err := Integer(data["integer"]); err != nil || integer != 3 {
		t.Fatalf("integer=%d err=%v", integer, err)
	}
	if _, err := Integer(data["number"]); err == nil {
		t.Fatal("fractional number accepted as integer")
	}
}
