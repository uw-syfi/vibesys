package main

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/telemetry"
)

func TestWriteTraceGraphArtifactsWritesJSONAndText(t *testing.T) {
	start := time.Date(2026, 8, 9, 12, 0, 0, 0, time.UTC)
	input := filepath.Join(t.TempDir(), "spans.json")
	data := `{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"frontend"}}]},"scopeSpans":[{"spans":[{"traceId":"trace-a","spanId":"root","name":"GET /","kind":"SPAN_KIND_SERVER","startTimeUnixNano":"` +
		formatNano(start.Add(100*time.Millisecond).UnixNano()) + `","endTimeUnixNano":"` + formatNano(start.Add(120*time.Millisecond).UnixNano()) + `","status":{"code":"STATUS_CODE_OK"}}]}]}]}`
	if err := os.WriteFile(input, []byte(data), 0o644); err != nil {
		t.Fatal(err)
	}
	request := telemetry.CollectionRequest{SchemaVersion: telemetry.RequestSchemaVersion, WorkloadName: "test", WorkloadHash: "abc", Windows: []telemetry.MeasurementWindow{{Start: start, End: start.Add(time.Second)}}}
	graphPath := filepath.Join(t.TempDir(), "graph.json")
	textPath := filepath.Join(t.TempDir(), "graph.txt")

	if err := writeTraceGraphArtifacts(request, []string{input}, graphPath, textPath, telemetry.TraceRenderOptions{MaxRoots: 5, MaxNodesPerRoot: 10, TimelineWidth: 40}); err != nil {
		t.Fatal(err)
	}
	graph, err := os.ReadFile(graphPath)
	if err != nil || !strings.Contains(string(graph), `"schema_version": 2`) || !strings.Contains(string(graph), `"critical_path"`) {
		t.Fatalf("graph JSON = %q, %v", graph, err)
	}
	rendered, err := os.ReadFile(textPath)
	if err != nil || !strings.Contains(string(rendered), "CRITICAL PATH") || !strings.Contains(string(rendered), "REPRESENTATIVE WATERFALL") {
		t.Fatalf("graph text = %q, %v", rendered, err)
	}
}

func formatNano(value int64) string { return strconv.FormatInt(value, 10) }
