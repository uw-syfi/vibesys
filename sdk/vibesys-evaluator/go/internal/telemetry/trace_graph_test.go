package telemetry

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestBuildTraceGraphAggregatesPathsAndExclusiveLatency(t *testing.T) {
	start := time.Date(2026, 8, 9, 12, 0, 0, 0, time.UTC)
	request := traceGraphRequest(start, 2*time.Second)
	root := rawGraphSpan("trace-a", "root", "", "GET /hotels", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond)
	root["attributes"] = []any{attribute("http.request.method", "GET"), attribute("http.route", "/hotels")}
	searchClient := rawGraphSpan("trace-a", "search-client", "root", "grpc.search", "SPAN_KIND_CLIENT", start.Add(110*time.Millisecond), 60*time.Millisecond)
	searchServer := rawGraphSpan("trace-a", "search-server", "search-client", "Search", "SPAN_KIND_SERVER", start.Add(112*time.Millisecond), 55*time.Millisecond)
	searchServer["attributes"] = []any{attribute("rpc.service", "search.SearchService"), attribute("rpc.method", "Nearby")}
	db := rawGraphSpan("trace-a", "db", "search-server", "mongo.find", "SPAN_KIND_CLIENT", start.Add(120*time.Millisecond), 20*time.Millisecond)
	db["attributes"] = []any{attribute("db.system.name", "mongodb"), attribute("db.operation.name", "find")}
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {root, searchClient},
		"search":   {searchServer, db},
	})

	report, err := BuildTraceGraph(request, []string{path})
	if err != nil {
		t.Fatal(err)
	}
	if report.SchemaVersion != TraceGraphSchemaVersion || report.Quality.EligibleTraces != 1 {
		t.Fatalf("report metadata = %+v", report)
	}
	if report.Quality.MatchedClientServerPairs != 1 || report.Quality.UnmatchedClientSpans != 1 {
		t.Fatalf("pair quality = %+v", report.Quality)
	}
	if len(report.Roots) != 1 || report.Roots[0].Operation != "GET /hotels" {
		t.Fatalf("roots = %+v", report.Roots)
	}
	rootNode := findGraphNode(t, report.Roots[0], "frontend", "GET /hotels")
	searchNode := findGraphNode(t, report.Roots[0], "search", "search.SearchService/Nearby")
	dbNode := findGraphNode(t, report.Roots[0], "search", "mongodb:find")
	if rootNode.Exclusive.MeanMS != 40 || searchNode.Exclusive.MeanMS != 35 || dbNode.Exclusive.MeanMS != 20 {
		t.Fatalf("exclusive means root=%v search=%v db=%v", rootNode.Exclusive.MeanMS, searchNode.Exclusive.MeanMS, dbNode.Exclusive.MeanMS)
	}
	if searchNode.Path == rootNode.Path || !strings.Contains(dbNode.Path, searchNode.Path) {
		t.Fatalf("paths root=%q search=%q db=%q", rootNode.Path, searchNode.Path, dbNode.Path)
	}
	if report.Roots[0].Representative.TraceID != "trace-a" || len(report.Roots[0].Representative.Spans) != 3 {
		t.Fatalf("representative = %+v", report.Roots[0].Representative)
	}
}

func TestBuildTraceGraphUnionsOverlappingChildren(t *testing.T) {
	start := time.Date(2026, 8, 9, 12, 0, 0, 0, time.UTC)
	request := traceGraphRequest(start, time.Second)
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rawGraphSpan("trace-a", "root", "", "root", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond),
			rawGraphSpan("trace-a", "a", "root", "a", "SPAN_KIND_INTERNAL", start.Add(110*time.Millisecond), 40*time.Millisecond),
			rawGraphSpan("trace-a", "b", "root", "b", "SPAN_KIND_INTERNAL", start.Add(130*time.Millisecond), 40*time.Millisecond),
		},
	})

	report, err := BuildTraceGraph(request, []string{path})
	if err != nil {
		t.Fatal(err)
	}
	root := findGraphNode(t, report.Roots[0], "frontend", "root")
	if root.Exclusive.MeanMS != 40 {
		t.Fatalf("exclusive mean = %v, want 40", root.Exclusive.MeanMS)
	}
}

func TestBuildTraceGraphAccountsForExcludedTraces(t *testing.T) {
	start := time.Date(2026, 8, 9, 12, 0, 0, 0, time.UTC)
	request := traceGraphRequest(start, time.Second)
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rawGraphSpan("good", "root-good", "", "good", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 50*time.Millisecond),
			rawGraphSpan("orphan", "child", "missing", "orphan", "SPAN_KIND_INTERNAL", start.Add(200*time.Millisecond), 20*time.Millisecond),
			rawGraphSpan("crossing", "root-cross", "", "crossing", "SPAN_KIND_SERVER", start.Add(990*time.Millisecond), 20*time.Millisecond),
		},
	})

	report, err := BuildTraceGraph(request, []string{path})
	if err != nil {
		t.Fatal(err)
	}
	if report.Quality.CapturedTraces != 3 || report.Quality.EligibleTraces != 1 || report.Quality.ExcludedTraces != 2 {
		t.Fatalf("quality = %+v", report.Quality)
	}
	if report.Quality.ExclusionReasons["missing_parent"] != 1 || report.Quality.ExclusionReasons["outside_measurement_window"] != 1 {
		t.Fatalf("exclusions = %+v", report.Quality.ExclusionReasons)
	}
	if len(report.Trials) != 1 || report.Trials[0].EligibleTraces != 1 {
		t.Fatalf("trials = %+v", report.Trials)
	}
}

func TestBuildTraceGraphRejectsNoEligibleTraces(t *testing.T) {
	start := time.Date(2026, 8, 9, 12, 0, 0, 0, time.UTC)
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {rawGraphSpan("orphan", "child", "missing", "orphan", "SPAN_KIND_INTERNAL", start.Add(100*time.Millisecond), 20*time.Millisecond)},
	})
	_, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
	if err == nil || !strings.Contains(err.Error(), "no eligible traces") {
		t.Fatalf("error = %v", err)
	}
}

func TestBuildTraceGraphPreservesLinkedAsyncRelationships(t *testing.T) {
	start := time.Date(2026, 8, 9, 12, 0, 0, 0, time.UTC)
	producer := rawGraphSpan("trace-a", "producer", "root", "publish", "SPAN_KIND_PRODUCER", start.Add(110*time.Millisecond), 10*time.Millisecond)
	consumer := rawGraphSpan("trace-a", "consumer", "root", "consume", "SPAN_KIND_CONSUMER", start.Add(130*time.Millisecond), 20*time.Millisecond)
	consumer["links"] = []any{map[string]any{"traceId": "trace-a", "spanId": "producer"}}
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {rawGraphSpan("trace-a", "root", "", "request", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond)},
		"queue":    {producer, consumer},
	})

	report, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
	if err != nil {
		t.Fatal(err)
	}
	if report.Quality.AsyncRelationships < 1 {
		t.Fatalf("quality = %+v", report.Quality)
	}
	foundLink := false
	for _, edge := range report.Roots[0].Edges {
		foundLink = foundLink || edge.Relationship == "link"
	}
	if !foundLink {
		t.Fatalf("edges = %+v", report.Roots[0].Edges)
	}
}

func TestSemanticOperationDoesNotRetainRawQueryStrings(t *testing.T) {
	operation := semanticOperation("HTTP GET /hotels?api_key=secret", map[string]any{})
	if operation != "HTTP GET /hotels" {
		t.Fatalf("operation = %q", operation)
	}
}

func traceGraphRequest(start time.Time, duration time.Duration) CollectionRequest {
	return CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "hotel",
		WorkloadHash:  "abc123",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(duration)}},
	}
}

func rawGraphSpan(traceID, spanID, parentID, name, kind string, start time.Time, duration time.Duration) map[string]any {
	return map[string]any{
		"traceId": traceID, "spanId": spanID, "parentSpanId": parentID,
		"name": name, "kind": kind,
		"startTimeUnixNano": start.UnixNano(), "endTimeUnixNano": start.Add(duration).UnixNano(),
		"status": map[string]any{"code": "STATUS_CODE_OK"},
	}
}

func attribute(key, value string) map[string]any {
	return map[string]any{"key": key, "value": map[string]any{"stringValue": value}}
}

func writeTraceDocument(t *testing.T, services map[string][]map[string]any) string {
	t.Helper()
	resources := make([]any, 0, len(services))
	for service, spans := range services {
		resources = append(resources, resourceSpans(service, spans))
	}
	data, err := json.Marshal(map[string]any{"resourceSpans": resources})
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "traces.json")
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func findGraphNode(t *testing.T, root TraceRootGraph, service, operation string) TraceGraphNode {
	t.Helper()
	for _, node := range root.Nodes {
		if node.Service == service && node.Operation == operation {
			return node
		}
	}
	t.Fatalf("missing node %s:%s in %+v", service, operation, root.Nodes)
	return TraceGraphNode{}
}
