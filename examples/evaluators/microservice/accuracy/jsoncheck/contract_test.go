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

func TestContractRejectsIncompleteDefinition(t *testing.T) {
	for _, contract := range []Contract{
		{},
		{Name: "missing-fields", Key: func(Object) (string, error) { return "x", nil }},
		{Name: "missing-validator", Fields: map[string]FieldValidator{"id": nil}, Key: func(Object) (string, error) { return "x", nil }},
		{Name: "missing-key", Fields: map[string]FieldValidator{"id": String}},
	} {
		if err := contract.ValidateDefinition(); err == nil {
			t.Fatalf("accepted incomplete contract %+v", contract)
		}
	}
}

func TestExactListRejectsMissingUnexpectedStaleAndDuplicateExpectedRows(t *testing.T) {
	contract := Contract{
		Name: "entity",
		Fields: map[string]FieldValidator{
			"id": NonEmptyString, "value": String,
		},
		Key: func(object Object) (string, error) { return object["id"].(string), nil },
	}
	expected := []any{map[string]any{"id": "one", "value": "current"}}
	valid := []any{decode(t, `{"id":"one","value":"current"}`)}
	if _, err := contract.ExactList(valid, expected, "entities"); err != nil {
		t.Fatal(err)
	}
	mutants := []struct {
		name     string
		actual   []any
		expected []any
	}{
		{"missing", nil, expected},
		{"unexpected", append(valid, decode(t, `{"id":"two","value":"extra"}`)), expected},
		{"stale", []any{decode(t, `{"id":"one","value":"old"}`)}, expected},
		{"duplicate expected", valid, append(expected, expected[0])},
	}
	for _, mutant := range mutants {
		t.Run(mutant.name, func(t *testing.T) {
			if _, err := contract.ExactList(mutant.actual, mutant.expected, "entities"); err == nil {
				t.Fatal("mutant unexpectedly passed")
			}
		})
	}
}
