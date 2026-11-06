package telemetry

import (
	"testing"
	"time"
)

func TestBuildTraceGraphCriticalPathIncludesSequentialCalls(t *testing.T) {
	start := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rawGraphSpan("trace-a", "root", "", "root", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond),
			rawGraphSpan("trace-a", "first", "root", "first", "SPAN_KIND_INTERNAL", start.Add(110*time.Millisecond), 20*time.Millisecond),
			rawGraphSpan("trace-a", "second", "root", "second", "SPAN_KIND_INTERNAL", start.Add(140*time.Millisecond), 30*time.Millisecond),
		},
	})

	report, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
	if err != nil {
		t.Fatal(err)
	}
	critical := report.Roots[0].CriticalPath
	if critical.Algorithm != CriticalPathAlgorithm || critical.TraceCount != 1 {
		t.Fatalf("critical path metadata = %+v", critical)
	}
	if critical.Duration.MeanMS != 100 {
		t.Fatalf("critical path duration = %v, want 100", critical.Duration.MeanMS)
	}
	assertCriticalContribution(t, critical, "frontend", "root", 50, 1)
	assertCriticalContribution(t, critical, "frontend", "first", 20, 1)
	assertCriticalContribution(t, critical, "frontend", "second", 30, 1)
	assertCriticalSegmentsCoverRoot(t, critical.Representative)
}

func TestBuildTraceGraphCriticalPathAttributesStaggeredOverlap(t *testing.T) {
	start := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rawGraphSpan("trace-a", "root", "", "root", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond),
			rawGraphSpan("trace-a", "early", "root", "early", "SPAN_KIND_INTERNAL", start.Add(110*time.Millisecond), 60*time.Millisecond),
			rawGraphSpan("trace-a", "late", "root", "late", "SPAN_KIND_INTERNAL", start.Add(130*time.Millisecond), 60*time.Millisecond),
		},
	})

	report, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
	if err != nil {
		t.Fatal(err)
	}
	critical := report.Roots[0].CriticalPath
	assertCriticalContribution(t, critical, "frontend", "root", 20, 1)
	assertCriticalContribution(t, critical, "frontend", "early", 20, 1)
	assertCriticalContribution(t, critical, "frontend", "late", 60, 1)
	assertCriticalSegmentsCoverRoot(t, critical.Representative)
}

func TestBuildTraceGraphCriticalPathHandlesChainedStaggeredOverlap(t *testing.T) {
	start := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rawGraphSpan("trace-a", "root", "", "root", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond),
			rawGraphSpan("trace-a", "a", "root", "a", "SPAN_KIND_INTERNAL", start.Add(110*time.Millisecond), 70*time.Millisecond),
			rawGraphSpan("trace-a", "b", "root", "b", "SPAN_KIND_INTERNAL", start.Add(120*time.Millisecond), 70*time.Millisecond),
			rawGraphSpan("trace-a", "c", "root", "c", "SPAN_KIND_INTERNAL", start.Add(170*time.Millisecond), 30*time.Millisecond),
		},
	})

	report, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
	if err != nil {
		t.Fatal(err)
	}
	critical := report.Roots[0].CriticalPath
	assertCriticalContribution(t, critical, "frontend", "root", 10, 1)
	assertCriticalContribution(t, critical, "frontend", "a", 10, 1)
	assertCriticalContribution(t, critical, "frontend", "b", 50, 1)
	assertCriticalContribution(t, critical, "frontend", "c", 30, 1)
	assertCriticalSegmentsCoverRoot(t, critical.Representative)
}

func TestBuildTraceGraphCriticalPathPreservesRPCEnvelope(t *testing.T) {
	start := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rawGraphSpan("trace-a", "root", "", "root", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond),
			rawGraphSpan("trace-a", "search-client", "root", "Search/Nearby", "SPAN_KIND_CLIENT", start.Add(110*time.Millisecond), 60*time.Millisecond),
		},
		"search": {
			rawGraphSpan("trace-a", "search-server", "search-client", "Search/Nearby", "SPAN_KIND_SERVER", start.Add(112*time.Millisecond), 55*time.Millisecond),
			rawGraphSpan("trace-a", "database", "search-server", "mongo.find", "SPAN_KIND_CLIENT", start.Add(120*time.Millisecond), 20*time.Millisecond),
		},
	})

	report, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
	if err != nil {
		t.Fatal(err)
	}
	critical := report.Roots[0].CriticalPath
	assertCriticalContribution(t, critical, "frontend", "root", 40, 1)
	assertCriticalContribution(t, critical, "search", "Search/Nearby", 40, 1)
	assertCriticalContribution(t, critical, "search", "mongo.find", 20, 1)
	assertCriticalSegmentsCoverRoot(t, critical.Representative)
}

func TestBuildTraceGraphCriticalPathCountsLinksOnCollapsedClient(t *testing.T) {
	start := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	client := rawGraphSpan("trace-a", "client", "root", "remote", "SPAN_KIND_CLIENT", start.Add(110*time.Millisecond), 60*time.Millisecond)
	client["links"] = []any{map[string]any{"traceId": "trace-b", "spanId": "producer"}}
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rawGraphSpan("trace-a", "root", "", "root", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond),
			client,
		},
		"search": {
			rawGraphSpan("trace-a", "server", "client", "remote", "SPAN_KIND_SERVER", start.Add(112*time.Millisecond), 55*time.Millisecond),
		},
	})

	report, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
	if err != nil {
		t.Fatal(err)
	}
	if report.Roots[0].CriticalPath.AsyncRelationshipsExcluded != 1 {
		t.Fatalf("excluded async relationships = %d, want 1", report.Roots[0].CriticalPath.AsyncRelationshipsExcluded)
	}
}

func TestBuildTraceGraphCriticalPathExcludesAsyncConsumers(t *testing.T) {
	start := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rawGraphSpan("trace-a", "root", "", "root", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond),
		},
		"worker": {
			rawGraphSpan("trace-a", "consumer", "root", "consume", "SPAN_KIND_CONSUMER", start.Add(120*time.Millisecond), 70*time.Millisecond),
		},
	})

	report, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
	if err != nil {
		t.Fatal(err)
	}
	critical := report.Roots[0].CriticalPath
	if critical.AsyncRelationshipsExcluded != 1 {
		t.Fatalf("excluded async relationships = %d, want 1", critical.AsyncRelationshipsExcluded)
	}
	assertCriticalContribution(t, critical, "frontend", "root", 100, 1)
	if contribution := findCriticalContribution(critical, "worker", "consume"); contribution != nil {
		t.Fatalf("async consumer contributed to synchronous path: %+v", contribution)
	}
	assertCriticalSegmentsCoverRoot(t, critical.Representative)
}

func TestBuildTraceGraphCriticalPathCountsCrossTraceLinks(t *testing.T) {
	start := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	rootA := rawGraphSpan("trace-a", "root-a", "", "root", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 50*time.Millisecond)
	rootA["links"] = []any{map[string]any{"traceId": "trace-b", "spanId": "root-b"}}
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rootA,
			rawGraphSpan("trace-b", "root-b", "", "root", "SPAN_KIND_SERVER", start.Add(200*time.Millisecond), 50*time.Millisecond),
		},
	})

	report, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
	if err != nil {
		t.Fatal(err)
	}
	if report.Roots[0].CriticalPath.AsyncRelationshipsExcluded != 1 {
		t.Fatalf("excluded async relationships = %d, want 1", report.Roots[0].CriticalPath.AsyncRelationshipsExcluded)
	}
}

func TestBuildTraceGraphCriticalPathUsesStableSpanIDTieBreak(t *testing.T) {
	start := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	failed := rawGraphSpan("trace-a", "z-failed", "root", "same", "SPAN_KIND_INTERNAL", start.Add(110*time.Millisecond), 40*time.Millisecond)
	failed["status"] = map[string]any{"code": "STATUS_CODE_ERROR"}
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rawGraphSpan("trace-a", "root", "", "root", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond),
			rawGraphSpan("trace-a", "a-ok", "root", "same", "SPAN_KIND_INTERNAL", start.Add(110*time.Millisecond), 40*time.Millisecond),
			failed,
		},
	})

	for iteration := 0; iteration < 20; iteration++ {
		report, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
		if err != nil {
			t.Fatal(err)
		}
		contribution := findCriticalContribution(report.Roots[0].CriticalPath, "frontend", "same")
		if contribution == nil || contribution.Contribution.ErrorCount != 0 {
			t.Fatalf("unstable tied-span selection: %+v", contribution)
		}
	}
}

func TestBuildTraceGraphCriticalPathPrefersDeeperLeafOnEqualEnd(t *testing.T) {
	start := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rawGraphSpan("trace-a", "root", "", "root", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond),
			rawGraphSpan("trace-a", "parent", "root", "parent", "SPAN_KIND_INTERNAL", start.Add(110*time.Millisecond), 80*time.Millisecond),
			rawGraphSpan("trace-a", "z-deep", "parent", "deep", "SPAN_KIND_INTERNAL", start.Add(120*time.Millisecond), 70*time.Millisecond),
			rawGraphSpan("trace-a", "a-shallow", "root", "shallow", "SPAN_KIND_INTERNAL", start.Add(130*time.Millisecond), 60*time.Millisecond),
		},
	})

	report, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
	if err != nil {
		t.Fatal(err)
	}
	critical := report.Roots[0].CriticalPath
	assertCriticalContribution(t, critical, "frontend", "deep", 70, 1)
	if contribution := findCriticalContribution(critical, "frontend", "shallow"); contribution != nil {
		t.Fatalf("shallower equal-ending leaf contributed: %+v", contribution)
	}
}

func TestValidateTraceGraphRejectsVersionOneWithoutCriticalPath(t *testing.T) {
	report := renderFixtureReport()
	report.SchemaVersion = 1
	if err := ValidateTraceGraph(report); err == nil {
		t.Fatal("accepted stale trace graph schema")
	}
}

func TestBuildTraceGraphAggregatesCriticalContributionsAcrossTraces(t *testing.T) {
	start := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	path := writeTraceDocument(t, map[string][]map[string]any{
		"frontend": {
			rawGraphSpan("trace-a", "root-a", "", "root", "SPAN_KIND_SERVER", start.Add(100*time.Millisecond), 100*time.Millisecond),
			rawGraphSpan("trace-a", "search-a", "root-a", "search", "SPAN_KIND_INTERNAL", start.Add(110*time.Millisecond), 40*time.Millisecond),
			rawGraphSpan("trace-b", "root-b", "", "root", "SPAN_KIND_SERVER", start.Add(300*time.Millisecond), 100*time.Millisecond),
			rawGraphSpan("trace-b", "search-b", "root-b", "search", "SPAN_KIND_INTERNAL", start.Add(310*time.Millisecond), 20*time.Millisecond),
			rawGraphSpan("trace-b", "rate-b", "root-b", "rate", "SPAN_KIND_INTERNAL", start.Add(340*time.Millisecond), 30*time.Millisecond),
		},
	})

	report, err := BuildTraceGraph(traceGraphRequest(start, time.Second), []string{path})
	if err != nil {
		t.Fatal(err)
	}
	critical := report.Roots[0].CriticalPath
	if critical.TraceCount != 2 || critical.Duration.Count != 2 || critical.Duration.MeanMS != 100 {
		t.Fatalf("critical aggregate = %+v", critical)
	}
	assertCriticalContribution(t, critical, "frontend", "search", 30, 2)
	assertCriticalContribution(t, critical, "frontend", "rate", 30, 1)
	assertCriticalContribution(t, critical, "frontend", "root", 55, 2)
	if critical.Nodes[0].Operation != "root" || critical.Nodes[1].Operation != "rate" || critical.Nodes[2].Operation != "search" {
		t.Fatalf("contributions are not deterministically sorted: %+v", critical.Nodes)
	}
}

func TestValidateTraceGraphRejectsCriticalPathThatDoesNotCoverRoot(t *testing.T) {
	report := renderFixtureReport()
	report.Roots[0].CriticalPath.Representative.Segments[0].DurationMS--
	if err := ValidateTraceGraph(report); err == nil {
		t.Fatal("accepted a critical path that does not cover the representative root")
	}
}

func TestValidateTraceGraphRejectsOverlappingCriticalSegments(t *testing.T) {
	report := renderFixtureReport()
	report.Roots[0].CriticalPath.Representative.Segments[1].OffsetMS = 0
	if err := ValidateTraceGraph(report); err == nil {
		t.Fatal("accepted overlapping critical path segments")
	}
}

func TestValidateTraceGraphRejectsUnknownCriticalContributor(t *testing.T) {
	report := renderFixtureReport()
	report.Roots[0].CriticalPath.Nodes[0].NodeID = "missing"
	if err := ValidateTraceGraph(report); err == nil {
		t.Fatal("accepted critical contribution for an unknown node")
	}
}

func TestValidateTraceGraphRequiresContributorForEveryRepresentativeSegment(t *testing.T) {
	report := renderFixtureReport()
	report.Roots[0].CriticalPath.Nodes = report.Roots[0].CriticalPath.Nodes[:1]
	if err := ValidateTraceGraph(report); err == nil {
		t.Fatal("accepted representative segment without aggregate contribution")
	}
}

func assertCriticalContribution(t *testing.T, critical CriticalPathSummary, service, operation string, mean float64, count int) {
	t.Helper()
	contribution := findCriticalContribution(critical, service, operation)
	if contribution == nil {
		t.Fatalf("missing critical contribution %s:%s in %+v", service, operation, critical.Nodes)
	}
	if contribution.Contribution.MeanMS != mean || contribution.Contribution.Count != count {
		t.Fatalf("critical contribution %s:%s = %+v, want mean=%v count=%d", service, operation, contribution.Contribution, mean, count)
	}
}

func findCriticalContribution(critical CriticalPathSummary, service, operation string) *CriticalPathNodeContribution {
	for index := range critical.Nodes {
		if critical.Nodes[index].Service == service && critical.Nodes[index].Operation == operation {
			return &critical.Nodes[index]
		}
	}
	return nil
}

func assertCriticalSegmentsCoverRoot(t *testing.T, representative RepresentativeCriticalPath) {
	t.Helper()
	total := 0.0
	for _, segment := range representative.Segments {
		total += segment.DurationMS
	}
	if total != representative.DurationMS {
		t.Fatalf("critical segments total %v, want root duration %v: %+v", total, representative.DurationMS, representative.Segments)
	}
}
