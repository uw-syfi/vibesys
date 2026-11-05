package telemetry

import (
	"strings"
	"testing"
)

func TestRenderTraceGraphShowsArchitectureMetricsAndWaterfall(t *testing.T) {
	report := renderFixtureReport()

	first, err := RenderTraceGraph(report, TraceRenderOptions{MaxRoots: 5, MaxNodesPerRoot: 5, TimelineWidth: 20})
	if err != nil {
		t.Fatal(err)
	}
	second, err := RenderTraceGraph(report, TraceRenderOptions{MaxRoots: 5, MaxNodesPerRoot: 5, TimelineWidth: 20})
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatal("rendering is not deterministic")
	}
	for _, want := range []string{
		"TRACE GRAPH v2", "eligible=3/4", "frontend: GET /hotels", "mean=100.00ms",
		"CALL GRAPH", "┌", "┐", "└", "┘", "│ calls", "▼",
		"search: Search/Nearby", "exclusive: 35.00 ms",
		"CRITICAL PATH", "scope=synchronous_request", "async_excluded=0",
		"CONTRIBUTORS", "selected=3/3", "REPRESENTATIVE PATH", "│ then",
		"REPRESENTATIVE WATERFALL", "TIME →", "[", "]", "trace-a",
		"Legend: inclusive is wall time; exclusive subtracts the union of direct-child intervals; critical contribution attributes root wall time without overlap.",
	} {
		if !strings.Contains(first, want) {
			t.Fatalf("rendered graph missing %q:\n%s", want, first)
		}
	}
}

func TestRenderTraceGraphUsesBoxAndArrowLayout(t *testing.T) {
	rendered, err := RenderTraceGraph(renderFixtureReport(), TraceRenderOptions{MaxRoots: 5, MaxNodesPerRoot: 5, TimelineWidth: 20})
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range []string{
		"┌─────────────────────────────────────────────┐",
		"│ frontend: GET /hotels                       │",
		"│ inclusive: 100.00 ms   exclusive: 40.00 ms  │",
		"└─────────────────────────────────────────────┘",
		"                       │ calls",
		"                       ▼",
		"│ search: Search/Nearby                       │",
	} {
		if !strings.Contains(rendered, line) {
			t.Fatalf("rendered graph missing exact line %q:\n%s", line, rendered)
		}
	}
}

func TestRenderTraceGraphReportsOmittedRootsAndNodes(t *testing.T) {
	report := renderFixtureReport()
	report.Roots = append(report.Roots, report.Roots[0])
	report.Roots[0].Nodes = append(report.Roots[0].Nodes, report.Roots[0].Nodes[1])

	rendered, err := RenderTraceGraph(report, TraceRenderOptions{MaxRoots: 1, MaxNodesPerRoot: 2, TimelineWidth: 20})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(rendered, "1 node omitted") || !strings.Contains(rendered, "1 root group omitted") {
		t.Fatalf("omission counts missing:\n%s", rendered)
	}
}

func TestRenderTraceGraphSeparatesRootTraceGroups(t *testing.T) {
	report := renderFixtureReport()
	second := report.Roots[0]
	second.Operation = "GET /user"
	report.Roots = append(report.Roots, second)

	rendered, err := RenderTraceGraph(report, TraceRenderOptions{MaxRoots: 5, MaxNodesPerRoot: 5, TimelineWidth: 20})
	if err != nil {
		t.Fatal(err)
	}
	separator := strings.Repeat("═", 150)
	if strings.Count(rendered, separator) != 1 {
		t.Fatalf("trace groups do not have exactly one separator:\n%s", rendered)
	}
	first := strings.Index(rendered, "ROOT  frontend: GET /hotels")
	bar := strings.Index(rendered, separator)
	secondRoot := strings.Index(rendered, "ROOT  frontend: GET /user")
	if first < 0 || bar < first || secondRoot < bar {
		t.Fatalf("separator is not between trace groups:\n%s", rendered)
	}
}

func TestRenderTraceGraphPlacesSiblingCallsSideBySide(t *testing.T) {
	report := renderFixtureReport()
	report.Roots[0].Nodes = append(report.Roots[0].Nodes, TraceGraphNode{
		ID: "node-003", Path: "frontend:GET /hotels > rate:Rate/GetRates",
		Service: "rate", Operation: "Rate/GetRates",
		Inclusive: LatencyDistribution{Count: 3, MeanMS: 20},
		Exclusive: LatencyDistribution{Count: 3, MeanMS: 5},
	})

	rendered, err := RenderTraceGraph(report, TraceRenderOptions{MaxRoots: 5, MaxNodesPerRoot: 5, TimelineWidth: 20})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(rendered, "┴") || !strings.Contains(rendered, "rate: Rate/GetRates") {
		t.Fatalf("branch layout missing:\n%s", rendered)
	}
	foundSiblingBoxes := false
	for _, line := range strings.Split(rendered, "\n") {
		if strings.Count(line, "┌") == 2 {
			foundSiblingBoxes = true
		}
	}
	if !foundSiblingBoxes {
		t.Fatalf("sibling boxes were not placed side by side:\n%s", rendered)
	}
}

func renderFixtureReport() TraceGraphReport {
	distribution := LatencyDistribution{Count: 3, MeanMS: 100, P50MS: 90, P95MS: 120, P99MS: 124, MaxMS: 125}
	rootContribution := LatencyDistribution{Count: 3, MeanMS: 45, P50MS: 40, P95MS: 55, P99MS: 59, MaxMS: 60}
	searchContribution := LatencyDistribution{Count: 3, MeanMS: 55, P50MS: 50, P95MS: 65, P99MS: 69, MaxMS: 70}
	return TraceGraphReport{
		SchemaVersion: TraceGraphSchemaVersion,
		WorkloadName:  "hotel",
		WorkloadHash:  "abc",
		Quality:       TraceQuality{CapturedTraces: 4, EligibleTraces: 3, ExcludedTraces: 1},
		Roots: []TraceRootGraph{{
			Service: "frontend", Operation: "GET /hotels", TraceCount: 3, Latency: distribution,
			Nodes: []TraceGraphNode{
				{ID: "node-001", Path: "frontend:GET /hotels", Service: "frontend", Operation: "GET /hotels", Inclusive: distribution, Exclusive: LatencyDistribution{Count: 3, MeanMS: 40}},
				{ID: "node-002", Path: "frontend:GET /hotels > search:Search/Nearby", Service: "search", Operation: "Search/Nearby", Inclusive: LatencyDistribution{Count: 3, MeanMS: 55}, Exclusive: LatencyDistribution{Count: 3, MeanMS: 35}},
			},
			Representative: RepresentativeTrace{TraceID: "trace-a", DurationMS: 100, Spans: []WaterfallSpan{
				{NodeID: "node-001", Service: "frontend", Operation: "GET /hotels", OffsetMS: 0, DurationMS: 100},
				{NodeID: "node-002", Service: "search", Operation: "Search/Nearby", OffsetMS: 10, DurationMS: 55},
			}},
			CriticalPath: CriticalPathSummary{
				Algorithm: CriticalPathAlgorithm, Scope: criticalPathScope, TraceCount: 3, Duration: distribution,
				Nodes: []CriticalPathNodeContribution{
					{NodeID: "node-002", Path: "frontend:GET /hotels > search:Search/Nearby", Service: "search", Operation: "Search/Nearby", Contribution: searchContribution},
					{NodeID: "node-001", Path: "frontend:GET /hotels", Service: "frontend", Operation: "GET /hotels", Contribution: rootContribution},
				},
				Representative: RepresentativeCriticalPath{TraceID: "trace-a", DurationMS: 100, Segments: []CriticalPathSegment{
					{NodeID: "node-001", OffsetMS: 0, DurationMS: 10},
					{NodeID: "node-002", OffsetMS: 10, DurationMS: 55},
					{NodeID: "node-001", OffsetMS: 65, DurationMS: 35},
				}},
			},
		}},
	}
}
