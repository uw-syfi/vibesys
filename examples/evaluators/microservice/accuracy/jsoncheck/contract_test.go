package jsoncheck

import (
	"testing"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
)

func testContract() Contract {
	return Contract{
		Name: "record",
		Fields: map[string]FieldValidator{
			"id": NonEmptyString,
			"n":  Integer,
		},
		Key: func(object Object) (string, error) { return object["id"].(string), nil },
	}
}

func decode(t *testing.T, raw string) any {
	t.Helper()
	value, err := httpcheck.DecodeJSON([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func TestIndexListRejectsDuplicateKeys(t *testing.T) {
	_, err := testContract().IndexList(
		decode(t, `[{"id":"same","n":1},{"id":"same","n":2}]`),
		"records",
	)
	if err == nil {
		t.Fatal("duplicate keys were accepted")
	}
}

func TestIndexListValidatesEveryRow(t *testing.T) {
	_, err := testContract().IndexList(
		decode(t, `[{"id":"selected","n":1},{"id":"other","n":"wrong"}]`),
		"records",
	)
	if err == nil {
		t.Fatal("corrupt unselected row was accepted")
	}
}

func TestContractRejectsMissingAndExtraFields(t *testing.T) {
	for _, raw := range []string{
		`{"id":"x"}`,
		`{"id":"x","n":1,"extra":true}`,
	} {
		if _, err := testContract().Validate(decode(t, raw), "record"); err == nil {
			t.Fatalf("accepted invalid object %s", raw)
		}
	}
}
