package telemetry

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
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

func TestSummarizeOTLPReadsAllNDJSONDocuments(t *testing.T) {
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
	first, err := json.Marshal(map[string]any{
		"resourceSpans": []any{resourceSpans("frontend", []map[string]any{
			span("warmup", start.Add(-time.Second), time.Millisecond, false, false),
		})},
	})
	if err != nil {
		t.Fatal(err)
	}
	second, err := json.Marshal(map[string]any{
		"resourceSpans": []any{resourceSpans("frontend", []map[string]any{
			span("GET /hotels", start.Add(100*time.Millisecond), 20*time.Millisecond, false, false),
		})},
	})
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "spans.ndjson")
	if err := os.WriteFile(path, []byte(strings.Join([]string{string(first), string(second)}, "\n")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	report, err := SummarizeOTLP(request, []string{path}, 10)
	if err != nil {
		t.Fatal(err)
	}
	if report.SpanCount != 1 || report.Spans[0].Name != "frontend:GET /hotels" {
		t.Fatalf("report = %+v", report)
	}
}

func TestSummarizeOTLPReadsBOMPrefixedDocument(t *testing.T) {
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
	document, err := json.Marshal(map[string]any{
		"resourceSpans": []any{resourceSpans("frontend", []map[string]any{
			span("GET /hotels", start.Add(100*time.Millisecond), 20*time.Millisecond, false, false),
		})},
	})
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "spans.json")
	// A leading UTF-8 BOM must not prevent the document from being read.
	if err := os.WriteFile(path, append([]byte("\xef\xbb\xbf"), document...), 0o644); err != nil {
		t.Fatal(err)
	}

	report, err := SummarizeOTLP(request, []string{path}, 10)
	if err != nil {
		t.Fatal(err)
	}
	if report.SpanCount != 1 || report.Spans[0].Name != "frontend:GET /hotels" {
		t.Fatalf("report = %+v", report)
	}
}

func TestSummarizeOTLPReadsLargeNDJSONLines(t *testing.T) {
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
	// A single ExportTraceServiceRequest can exceed the previous 16MB NDJSON
	// line cap. Use two documents so a whole-file decode cannot apply and the
	// reader must handle each line, and pad the first with a large ignored
	// attribute so its line is well over that limit. A line-capped scanner would
	// reject the big line; confirm both in-window spans still read.
	padding := strings.Repeat("x", 20*1024*1024)
	big := span("GET /hotels", start.Add(100*time.Millisecond), 20*time.Millisecond, false, false)
	big["attributes"] = []any{map[string]any{
		"key":   "padding",
		"value": map[string]any{"stringValue": padding},
	}}
	first, err := json.Marshal(map[string]any{
		"resourceSpans": []any{resourceSpans("frontend", []map[string]any{big})},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(first) <= 16*1024*1024 {
		t.Fatalf("test document is only %d bytes; expected > 16MB", len(first))
	}
	second, err := json.Marshal(map[string]any{
		"resourceSpans": []any{resourceSpans("rate", []map[string]any{
			span("GET /rates", start.Add(200*time.Millisecond), 10*time.Millisecond, false, false),
		})},
	})
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "spans.ndjson")
	contents := append(append(first, '\n'), append(second, '\n')...)
	if err := os.WriteFile(path, contents, 0o644); err != nil {
		t.Fatal(err)
	}

	report, err := SummarizeOTLP(request, []string{path}, 10)
	if err != nil {
		t.Fatal(err)
	}
	if report.SpanCount != 2 {
		t.Fatalf("span count = %d, want 2 (report = %+v)", report.SpanCount, report)
	}
}

func TestSummarizeOTLPAcceptsEqualDurationSpans(t *testing.T) {
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	request := CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "hotel",
		WorkloadHash:  "abc123",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(time.Hour)}},
	}
	// Every in-window span has an identical duration. The percentile invariant
	// p50<=p95<=p99<=max must still hold, so the report must not be rejected by
	// floating-point drift in the percentile interpolation.
	spans := make([]map[string]any, 0, 7)
	for index := 0; index < 7; index++ {
		spans = append(spans, span(
			"GET /x",
			start.Add(time.Duration(index)*time.Second),
			123456*time.Microsecond,
			false,
			false,
		))
	}
	path := filepath.Join(t.TempDir(), "spans.json")
	writeJSON(t, path, map[string]any{"resourceSpans": []any{resourceSpans("svc", spans)}})

	report, err := SummarizeOTLP(request, []string{path}, 10)
	if err != nil {
		t.Fatalf("rejected equal-duration telemetry: %v", err)
	}
	row := report.Services[0]
	if *row.P50MS != *row.P95MS || *row.P95MS != *row.P99MS || *row.P99MS != *row.MaxMS {
		t.Fatalf("equal durations produced unequal percentiles: %+v", row)
	}
}

func TestSummarizeOTLPRejectsInvalidArguments(t *testing.T) {
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	request := CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "hotel",
		WorkloadHash:  "abc123",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(time.Second)}},
	}
	valid := filepath.Join(t.TempDir(), "spans.json")
	writeJSON(t, valid, map[string]any{
		"resourceSpans": []any{resourceSpans("frontend", []map[string]any{
			span("GET /hotels", start.Add(100*time.Millisecond), 20*time.Millisecond, false, false),
		})},
	})

	if _, err := SummarizeOTLP(request, nil, 10); err == nil {
		t.Fatal("accepted a request with no OTLP inputs")
	}
	if _, err := SummarizeOTLP(request, []string{valid}, 0); err == nil {
		t.Fatal("accepted a non-positive top")
	}
}

func TestSummarizeOTLPRejectsMalformedJSON(t *testing.T) {
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	request := CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "hotel",
		WorkloadHash:  "abc123",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(time.Second)}},
	}
	path := filepath.Join(t.TempDir(), "spans.json")
	if err := os.WriteFile(path, []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := SummarizeOTLP(request, []string{path}, 10)
	if err == nil {
		t.Fatal("accepted malformed OTLP JSON")
	}
	if !strings.Contains(err.Error(), "parse OTLP JSON") {
		t.Fatalf("error = %v", err)
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
