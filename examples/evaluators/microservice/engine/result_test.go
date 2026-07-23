package engine

import (
	"encoding/json"
	"testing"
	"time"

	"vibesys/microservice-evaluator/telemetry"
)

func TestResultSchemaVersionCoversTelemetryFields(t *testing.T) {
	if ResultSchemaVersion != 4 {
		t.Fatalf("ResultSchemaVersion = %d, want 4", ResultSchemaVersion)
	}

	window := telemetry.MeasurementWindow{
		Start: time.Date(2026, time.July, 23, 12, 0, 0, 0, time.UTC),
		End:   time.Date(2026, time.July, 23, 12, 0, 1, 0, time.UTC),
	}
	payload, err := json.Marshal(Summary{
		SchemaVersion: ResultSchemaVersion,
		Trials: []TrialResult{{
			MeasurementWindow: window,
		}},
		Telemetry: &telemetry.Report{Windows: []telemetry.MeasurementWindow{window}},
	})
	if err != nil {
		t.Fatal(err)
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal(payload, &fields); err != nil {
		t.Fatal(err)
	}
	var version int
	if err := json.Unmarshal(fields["schema_version"], &version); err != nil {
		t.Fatal(err)
	}
	if version != ResultSchemaVersion {
		t.Fatalf("serialized schema_version = %d, want %d", version, ResultSchemaVersion)
	}
	if _, ok := fields["telemetry"]; !ok {
		t.Fatal("serialized summary omitted telemetry")
	}
	if !json.Valid(fields["trials"]) {
		t.Fatal("serialized summary omitted trials")
	}
}
