package telemetry

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestSummarizeOTLPFiltersMeasurementWindowsAndRanksRows(t *testing.T) {
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	request := CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "hotel",
		WorkloadHash:  "abc123",
		Windows: []MeasurementWindow{{
			Start: start,
			End:   start.Add(10 * time.Second),
		}},
	}
	document := map[string]any{
		"resourceSpans": []any{
			resourceSpans("frontend", []map[string]any{
				span("GET /hotels", start.Add(time.Second), 20*time.Millisecond, false, false),
				span("GET /hotels", start.Add(2*time.Second), 40*time.Millisecond, true, false),
				span("warmup", start.Add(-time.Second), 500*time.Millisecond, false, false),
				span("crosses-window-end", start.Add(9900*time.Millisecond), 200*time.Millisecond, false, false),
			}),
			resourceSpans("rate", []map[string]any{
				span("mongo.find", start.Add(3*time.Second), 100*time.Millisecond, false, true),
			}),
		},
	}
	path := filepath.Join(t.TempDir(), "spans.json")
	writeJSON(t, path, document)

	report, err := SummarizeOTLP(request, []string{path}, 10)
	if err != nil {
		t.Fatal(err)
	}
	if report.SpanCount != 3 || report.ErrorCount != 1 {
		t.Fatalf("counts = spans %d errors %d", report.SpanCount, report.ErrorCount)
	}
	if report.Services[0].Name != "rate" || *report.Services[0].P95MS != 100 {
		t.Fatalf("services = %+v", report.Services)
	}
	if len(report.Datastores) != 1 || report.Datastores[0].Name != "rate:mongo.find" {
		t.Fatalf("datastores = %+v", report.Datastores)
	}
	if report.Services[1].Count != 2 || report.Services[1].ErrorCount != 1 {
		t.Fatalf("frontend row = %+v", report.Services[1])
	}
}

func TestSummarizeOTLPRejectsMissingMeasurementSpans(t *testing.T) {
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	request := CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "hotel",
		WorkloadHash:  "abc123",
		Windows: []MeasurementWindow{{
			Start: start,
			End:   start.Add(time.Second),
		}},
	}
	path := filepath.Join(t.TempDir(), "spans.json")
	writeJSON(t, path, map[string]any{
		"resourceSpans": []any{
			resourceSpans("frontend", []map[string]any{
				span("warmup", start.Add(-time.Second), time.Millisecond, false, false),
			}),
		},
	})

	if _, err := SummarizeOTLP(request, []string{path}, 10); err == nil {
		t.Fatal("accepted telemetry without measurement-window spans")
	}
}

func resourceSpans(service string, spans []map[string]any) map[string]any {
	rawSpans := make([]any, 0, len(spans))
	for _, item := range spans {
		rawSpans = append(rawSpans, item)
	}
	return map[string]any{
		"resource": map[string]any{
			"attributes": []any{map[string]any{
				"key":   "service.name",
				"value": map[string]any{"stringValue": service},
			}},
		},
		"scopeSpans": []any{map[string]any{"spans": rawSpans}},
	}
}

func span(
	name string,
	start time.Time,
	duration time.Duration,
	failed bool,
	datastore bool,
) map[string]any {
	attributes := []any{}
	if datastore {
		attributes = append(attributes, map[string]any{
			"key":   "db.system",
			"value": map[string]any{"stringValue": "mongodb"},
		})
	}
	status := map[string]any{"code": "STATUS_CODE_OK"}
	if failed {
		status["code"] = "STATUS_CODE_ERROR"
	}
	return map[string]any{
		"name":              name,
		"startTimeUnixNano": start.UnixNano(),
		"endTimeUnixNano":   start.Add(duration).UnixNano(),
		"attributes":        attributes,
		"status":            status,
	}
}

func writeJSON(t *testing.T, path string, value any) {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
}
