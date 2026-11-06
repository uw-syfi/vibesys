package telemetry

import (
	"fmt"
	"sort"
	"strings"
	"time"
)

const TraceGraphSchemaVersion = 2

type LatencyDistribution struct {
	Count      int     `json:"count"`
	ErrorCount int     `json:"error_count"`
	MeanMS     float64 `json:"mean_ms"`
	P50MS      float64 `json:"p50_ms"`
	P95MS      float64 `json:"p95_ms"`
	P99MS      float64 `json:"p99_ms"`
	MaxMS      float64 `json:"max_ms"`
}

type TraceQuality struct {
	CapturedTraces           int            `json:"captured_traces"`
	EligibleTraces           int            `json:"eligible_traces"`
	ExcludedTraces           int            `json:"excluded_traces"`
	ExclusionReasons         map[string]int `json:"exclusion_reasons,omitempty"`
	MatchedClientServerPairs int            `json:"matched_client_server_pairs"`
	UnmatchedClientSpans     int            `json:"unmatched_client_spans"`
	UnmatchedServerSpans     int            `json:"unmatched_server_spans"`
	AsyncRelationships       int            `json:"async_relationships"`
}

type TraceTrialQuality struct {
	Trial          int `json:"trial"`
	CapturedTraces int `json:"captured_traces"`
	EligibleTraces int `json:"eligible_traces"`
	ExcludedTraces int `json:"excluded_traces"`
}

type TraceGraphNode struct {
	ID        string              `json:"id"`
	Path      string              `json:"path"`
	Service   string              `json:"service"`
	Operation string              `json:"operation"`
	Kind      string              `json:"kind"`
	Inclusive LatencyDistribution `json:"inclusive_latency_ms"`
	Exclusive LatencyDistribution `json:"exclusive_latency_ms"`
}

type TraceGraphEdge struct {
	From         string `json:"from"`
	To           string `json:"to"`
	Relationship string `json:"relationship"`
	Count        int    `json:"count"`
}

type WaterfallSpan struct {
	NodeID     string  `json:"node_id"`
	Service    string  `json:"service"`
	Operation  string  `json:"operation"`
	OffsetMS   float64 `json:"offset_ms"`
	DurationMS float64 `json:"duration_ms"`
}

type RepresentativeTrace struct {
	TraceID    string          `json:"trace_id"`
	DurationMS float64         `json:"duration_ms"`
	Spans      []WaterfallSpan `json:"spans"`
}

type CriticalPathNodeContribution struct {
	NodeID       string              `json:"node_id"`
	Path         string              `json:"path"`
	Service      string              `json:"service"`
	Operation    string              `json:"operation"`
	Contribution LatencyDistribution `json:"contribution_ms"`
}

type CriticalPathSegment struct {
	NodeID     string  `json:"node_id"`
	OffsetMS   float64 `json:"offset_ms"`
	DurationMS float64 `json:"duration_ms"`
}

type RepresentativeCriticalPath struct {
	TraceID    string                `json:"trace_id"`
	DurationMS float64               `json:"duration_ms"`
	Segments   []CriticalPathSegment `json:"segments"`
}

type CriticalPathSummary struct {
	Algorithm                  string                         `json:"algorithm"`
	Scope                      string                         `json:"scope"`
	TraceCount                 int                            `json:"trace_count"`
	AsyncRelationshipsExcluded int                            `json:"async_relationships_excluded"`
	Duration                   LatencyDistribution            `json:"duration_ms"`
	Nodes                      []CriticalPathNodeContribution `json:"nodes_by_contribution"`
	Representative             RepresentativeCriticalPath     `json:"representative"`
}

type TraceRootGraph struct {
	Service        string              `json:"service"`
	Operation      string              `json:"operation"`
	TraceCount     int                 `json:"trace_count"`
	ErrorCount     int                 `json:"error_count"`
	Latency        LatencyDistribution `json:"latency_ms"`
	Nodes          []TraceGraphNode    `json:"nodes"`
	Edges          []TraceGraphEdge    `json:"edges"`
	Representative RepresentativeTrace `json:"representative_trace"`
	CriticalPath   CriticalPathSummary `json:"critical_path"`
}

type TraceGraphReport struct {
	SchemaVersion int                 `json:"schema_version"`
	Source        string              `json:"source"`
	CollectedAt   time.Time           `json:"collected_at"`
	WorkloadName  string              `json:"workload_name"`
	WorkloadHash  string              `json:"workload_hash"`
	Windows       []MeasurementWindow `json:"measurement_windows"`
	Quality       TraceQuality        `json:"quality"`
	Trials        []TraceTrialQuality `json:"trials"`
	Roots         []TraceRootGraph    `json:"roots"`
}

func ValidateTraceGraph(report TraceGraphReport) error {
	if report.SchemaVersion != TraceGraphSchemaVersion {
		return fmt.Errorf("trace graph schema_version must be %d", TraceGraphSchemaVersion)
	}
	if report.Source == "" || report.CollectedAt.IsZero() {
		return fmt.Errorf("trace graph source and collected_at must not be empty")
	}
	if err := validateIdentityAndWindows(report.WorkloadName, report.WorkloadHash, report.Windows); err != nil {
		return fmt.Errorf("trace graph: %w", err)
	}
	if report.Quality.CapturedTraces <= 0 || report.Quality.EligibleTraces <= 0 || report.Quality.ExcludedTraces < 0 || report.Quality.EligibleTraces+report.Quality.ExcludedTraces != report.Quality.CapturedTraces {
		return fmt.Errorf("trace graph has invalid trace quality counts")
	}
	if len(report.Trials) != len(report.Windows) || len(report.Roots) == 0 {
		return fmt.Errorf("trace graph requires per-trial quality and root graphs")
	}
	for _, root := range report.Roots {
		if err := validateCriticalPath(root); err != nil {
			return err
		}
	}
	return nil
}

type graphSpan struct {
	traceID, spanID, parentID string
	service, operation, kind  string
	start, end                int64
	failed                    bool
	links                     []graphSpanLink
}

type graphSpanLink struct{ traceID, spanID string }

type graphSample struct {
	inclusive float64
	exclusive float64
	failed    bool
}

type graphNodeAggregate struct {
	path, service, operation, kind string
	samples                        []graphSample
}

type graphRootAggregate struct {
	service, operation string
	traces             []eligibleTrace
	criticalPaths      []traceCriticalPath
	nodes              map[string]*graphNodeAggregate
	edges              map[string]int
}

type eligibleTrace struct {
	id     string
	root   *graphSpan
	spans  []*graphSpan
	paths  map[string]string
	paired map[string]bool
	pairs  map[string]string
	window int
}

// BuildTraceGraph reconstructs complete workload-window traces into a stable graph.
func BuildTraceGraph(request CollectionRequest, paths []string) (TraceGraphReport, error) {
	if err := ValidateRequest(request); err != nil {
		return TraceGraphReport{}, err
	}
	if len(paths) == 0 {
		return TraceGraphReport{}, fmt.Errorf("at least one OTLP JSON input is required")
	}
	byTrace := make(map[string][]*graphSpan)
	for _, path := range paths {
		documents, err := readJSONDocuments(path)
		if err != nil {
			return TraceGraphReport{}, err
		}
		for _, document := range documents {
			visitResourceSpans(document, func(service string, raw map[string]any) {
				span, ok := parseGraphSpan(service, raw)
				if ok && overlapsMeasurementWindows(span.start, span.end, request.Windows) {
					byTrace[span.traceID] = append(byTrace[span.traceID], span)
				}
			})
		}
	}
	report := TraceGraphReport{
		SchemaVersion: TraceGraphSchemaVersion,
		Source:        "otlp-json",
		CollectedAt:   time.Now().UTC(),
		WorkloadName:  request.WorkloadName,
		WorkloadHash:  request.WorkloadHash,
		Windows:       append([]MeasurementWindow(nil), request.Windows...),
		Quality:       TraceQuality{CapturedTraces: len(byTrace), ExclusionReasons: map[string]int{}},
		Trials:        make([]TraceTrialQuality, len(request.Windows)),
	}
	for index := range report.Trials {
		report.Trials[index].Trial = index + 1
	}
	aggregates := make(map[string]*graphRootAggregate)
	traceIDs := sortedTraceIDs(byTrace)
	for _, traceID := range traceIDs {
		trace, reason := qualifyTrace(traceID, byTrace[traceID], request.Windows)
		trial := trace.window
		if trial < 0 {
			trial = firstOverlappingWindow(byTrace[traceID], request.Windows)
		}
		if trial >= 0 {
			report.Trials[trial].CapturedTraces++
		}
		if reason != "" {
			report.Quality.ExcludedTraces++
			report.Quality.ExclusionReasons[reason]++
			if trial >= 0 {
				report.Trials[trial].ExcludedTraces++
			}
			continue
		}
		report.Quality.EligibleTraces++
		report.Trials[trace.window].EligibleTraces++
		accountPairing(&report.Quality, trace)
		key := trace.root.service + "\x00" + trace.root.operation
		aggregate := aggregates[key]
		if aggregate == nil {
			aggregate = &graphRootAggregate{service: trace.root.service, operation: trace.root.operation, nodes: map[string]*graphNodeAggregate{}, edges: map[string]int{}}
			aggregates[key] = aggregate
		}
		if err := aggregate.addTrace(trace, &report.Quality); err != nil {
			return TraceGraphReport{}, err
		}
	}
	if report.Quality.EligibleTraces == 0 {
		return TraceGraphReport{}, fmt.Errorf("trace graph contains no eligible traces")
	}
	report.Roots = materializeRoots(aggregates)
	if err := ValidateTraceGraph(report); err != nil {
		return TraceGraphReport{}, err
	}
	return report, nil
}

func parseGraphSpan(service string, raw map[string]any) (*graphSpan, bool) {
	start, end, ok := spanTimes(raw)
	traceID, spanID := stringValue(raw["traceId"]), stringValue(raw["spanId"])
	if !ok || traceID == "" || spanID == "" {
		return nil, false
	}
	attributes := otlpAttributes(asSlice(raw["attributes"]))
	if service == "" {
		service = "unknown"
	}
	span := &graphSpan{
		traceID: traceID, spanID: spanID, parentID: stringValue(raw["parentSpanId"]),
		service: service, operation: semanticOperation(stringValue(raw["name"]), attributes),
		kind: normalizeSpanKind(stringValue(raw["kind"])), start: start, end: end,
		failed: spanFailed(raw, attributes),
	}
	for _, rawLink := range asSlice(raw["links"]) {
		link, ok := rawLink.(map[string]any)
		if !ok {
			continue
		}
		span.links = append(span.links, graphSpanLink{traceID: stringValue(link["traceId"]), spanID: stringValue(link["spanId"])})
	}
	return span, true
}

func semanticOperation(name string, attributes map[string]any) string {
	if route := stringValue(attributes["http.route"]); route != "" {
		if method := stringValue(attributes["http.request.method"]); method != "" {
			return method + " " + route
		}
		return route
	}
	if service, method := stringValue(attributes["rpc.service"]), stringValue(attributes["rpc.method"]); service != "" && method != "" {
		return service + "/" + method
	}
	if system := firstString(attributes, "db.system.name", "db.system"); system != "" {
		if operation := stringValue(attributes["db.operation.name"]); operation != "" {
			return system + ":" + operation
		}
	}
	if name == "" {
		return "unknown"
	}
	return stripQueryString(name)
}

func stripQueryString(value string) string {
	query := strings.IndexByte(value, '?')
	if query < 0 {
		return value
	}
	suffix := strings.IndexAny(value[query:], " \t")
	if suffix < 0 {
		return value[:query]
	}
	return value[:query] + value[query+suffix:]
}

func firstString(attributes map[string]any, keys ...string) string {
	for _, key := range keys {
		if value := stringValue(attributes[key]); value != "" {
			return value
		}
	}
	return ""
}

func normalizeSpanKind(kind string) string {
	kind = strings.TrimPrefix(strings.ToLower(kind), "span_kind_")
	if kind == "" || kind == "0" || kind == "unspecified" {
		return "internal"
	}
	return kind
}

func qualifyTrace(id string, spans []*graphSpan, windows []MeasurementWindow) (eligibleTrace, string) {
	trace := eligibleTrace{id: id, spans: spans, paths: map[string]string{}, paired: map[string]bool{}, pairs: map[string]string{}, window: -1}
	byID := make(map[string]*graphSpan, len(spans))
	for _, span := range spans {
		if _, exists := byID[span.spanID]; exists {
			return trace, "duplicate_span_id"
		}
		byID[span.spanID] = span
		if span.parentID == "" {
			if trace.root != nil {
				return trace, "multiple_roots"
			}
			trace.root = span
		}
	}
	if trace.root == nil {
		return trace, "missing_parent"
	}
	for index, window := range windows {
		if inMeasurementWindows(trace.root.start, trace.root.end, []MeasurementWindow{window}) {
			trace.window = index
			break
		}
	}
	if trace.window < 0 {
		return trace, "outside_measurement_window"
	}
	window := windows[trace.window]
	for _, span := range spans {
		if !inMeasurementWindows(span.start, span.end, []MeasurementWindow{window}) {
			return trace, "outside_measurement_window"
		}
		if span.parentID == "" {
			continue
		}
		parent := byID[span.parentID]
		if parent == nil {
			return trace, "missing_parent"
		}
		if span.start < parent.start || span.end > parent.end {
			return trace, "clock_inconsistency"
		}
	}
	children := childrenByParent(spans)
	for _, span := range spans {
		if span.kind == "client" {
			var matched *graphSpan
			for _, child := range children[span.spanID] {
				if child.kind == "server" && child.service != span.service {
					if matched != nil {
						matched = nil
						break
					}
					matched = child
				}
			}
			if matched != nil {
				trace.paired[span.spanID] = true
				trace.pairs[span.spanID] = matched.spanID
			}
		}
	}
	if !tracePaths(&trace, byID) {
		return trace, "cycle"
	}
	return trace, ""
}

func tracePaths(trace *eligibleTrace, byID map[string]*graphSpan) bool {
	visiting := map[string]bool{}
	var resolve func(*graphSpan) (string, bool)
	resolve = func(span *graphSpan) (string, bool) {
		if path, ok := trace.paths[span.spanID]; ok {
			return path, true
		}
		if visiting[span.spanID] {
			return "", false
		}
		visiting[span.spanID] = true
		segment := span.service + ":" + span.operation
		if span.parentID == "" {
			trace.paths[span.spanID] = segment
		} else {
			parent := byID[span.parentID]
			for parent != nil && trace.paired[parent.spanID] {
				parent = byID[parent.parentID]
			}
			if parent == nil {
				return "", false
			}
			parentPath, ok := resolve(parent)
			if !ok {
				return "", false
			}
			trace.paths[span.spanID] = parentPath + " > " + segment
		}
		visiting[span.spanID] = false
		return trace.paths[span.spanID], true
	}
	for _, span := range trace.spans {
		if _, ok := resolve(span); !ok {
			return false
		}
	}
	return true
}

func (aggregate *graphRootAggregate) addTrace(trace eligibleTrace, quality *TraceQuality) error {
	aggregate.traces = append(aggregate.traces, trace)
	critical, err := analyzeCriticalPath(trace)
	if err != nil {
		return err
	}
	aggregate.criticalPaths = append(aggregate.criticalPaths, critical)
	children := childrenByParent(trace.spans)
	visibleByPath := make(map[string]*graphSpan)
	for _, span := range trace.spans {
		if trace.paired[span.spanID] {
			continue
		}
		path := trace.paths[span.spanID]
		node := aggregate.nodes[path]
		if node == nil {
			node = &graphNodeAggregate{path: path, service: span.service, operation: span.operation, kind: span.kind}
			aggregate.nodes[path] = node
		}
		node.samples = append(node.samples, graphSample{
			inclusive: durationMS(span.start, span.end),
			exclusive: exclusiveDurationMS(span, children[span.spanID]),
			failed:    span.failed,
		})
		visibleByPath[path] = span
		if span.kind == "client" {
			quality.UnmatchedClientSpans++
		}
		if span.kind == "server" && span.parentID != "" {
			parent := findSpan(trace.spans, span.parentID)
			if parent == nil || !trace.paired[parent.spanID] {
				quality.UnmatchedServerSpans++
			}
		}
	}
	for path, span := range visibleByPath {
		if span.parentID == "" {
			continue
		}
		parent := findSpan(trace.spans, span.parentID)
		for parent != nil && trace.paired[parent.spanID] {
			parent = findSpan(trace.spans, parent.parentID)
		}
		if parent == nil {
			continue
		}
		relationship := "sync"
		if parent.kind == "producer" || span.kind == "consumer" {
			relationship = "async"
			quality.AsyncRelationships++
		}
		aggregate.edges[trace.paths[parent.spanID]+"\x00"+path+"\x00"+relationship]++
		for _, link := range span.links {
			if link.traceID != trace.id {
				continue
			}
			linked := findSpan(trace.spans, link.spanID)
			if linked == nil || trace.paired[linked.spanID] {
				continue
			}
			aggregate.edges[trace.paths[linked.spanID]+"\x00"+path+"\x00link"]++
			quality.AsyncRelationships++
		}
	}
	return nil
}

func accountPairing(quality *TraceQuality, trace eligibleTrace) {
	quality.MatchedClientServerPairs += len(trace.paired)
}

func materializeRoots(aggregates map[string]*graphRootAggregate) []TraceRootGraph {
	keys := make([]string, 0, len(aggregates))
	for key := range aggregates {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	roots := make([]TraceRootGraph, 0, len(keys))
	for _, key := range keys {
		aggregate := aggregates[key]
		paths := make([]string, 0, len(aggregate.nodes))
		for path := range aggregate.nodes {
			paths = append(paths, path)
		}
		sort.Strings(paths)
		ids := make(map[string]string, len(paths))
		nodes := make([]TraceGraphNode, 0, len(paths))
		for index, path := range paths {
			ids[path] = fmt.Sprintf("node-%03d", index+1)
			node := aggregate.nodes[path]
			inclusive, exclusive := make([]spanSample, 0, len(node.samples)), make([]spanSample, 0, len(node.samples))
			for _, sample := range node.samples {
				inclusive = append(inclusive, spanSample{durationMS: sample.inclusive, failed: sample.failed})
				exclusive = append(exclusive, spanSample{durationMS: sample.exclusive, failed: sample.failed})
			}
			nodes = append(nodes, TraceGraphNode{ID: ids[path], Path: path, Service: node.service, Operation: node.operation, Kind: node.kind, Inclusive: distribution(inclusive), Exclusive: distribution(exclusive)})
		}
		edgeKeys := make([]string, 0, len(aggregate.edges))
		for edge := range aggregate.edges {
			edgeKeys = append(edgeKeys, edge)
		}
		sort.Strings(edgeKeys)
		edges := make([]TraceGraphEdge, 0, len(edgeKeys))
		for _, edgeKey := range edgeKeys {
			parts := strings.Split(edgeKey, "\x00")
			edges = append(edges, TraceGraphEdge{From: ids[parts[0]], To: ids[parts[1]], Relationship: parts[2], Count: aggregate.edges[edgeKey]})
		}
		rootSamples := make([]spanSample, 0, len(aggregate.traces))
		errorCount := 0
		for _, trace := range aggregate.traces {
			rootSamples = append(rootSamples, spanSample{durationMS: durationMS(trace.root.start, trace.root.end), failed: trace.root.failed})
			if trace.root.failed {
				errorCount++
			}
		}
		root := TraceRootGraph{Service: aggregate.service, Operation: aggregate.operation, TraceCount: len(aggregate.traces), ErrorCount: errorCount, Latency: distribution(rootSamples), Nodes: nodes, Edges: edges}
		root.Representative = representativeTrace(aggregate.traces, ids, root.Latency.P95MS)
		root.CriticalPath = materializeCriticalPath(aggregate, ids, root.Representative.TraceID)
		roots = append(roots, root)
	}
	return roots
}

func representativeTrace(traces []eligibleTrace, ids map[string]string, target float64) RepresentativeTrace {
	var candidates []eligibleTrace
	for _, trace := range traces {
		if !trace.root.failed {
			candidates = append(candidates, trace)
		}
	}
	if len(candidates) == 0 {
		candidates = traces
	}
	sort.Slice(candidates, func(i, j int) bool {
		di := abs(durationMS(candidates[i].root.start, candidates[i].root.end) - target)
		dj := abs(durationMS(candidates[j].root.start, candidates[j].root.end) - target)
		if di == dj {
			return candidates[i].id < candidates[j].id
		}
		return di < dj
	})
	selected := candidates[0]
	result := RepresentativeTrace{TraceID: selected.id, DurationMS: durationMS(selected.root.start, selected.root.end)}
	for _, span := range selected.spans {
		if selected.paired[span.spanID] {
			continue
		}
		result.Spans = append(result.Spans, WaterfallSpan{NodeID: ids[selected.paths[span.spanID]], Service: span.service, Operation: span.operation, OffsetMS: durationMS(selected.root.start, span.start), DurationMS: durationMS(span.start, span.end)})
	}
	sort.Slice(result.Spans, func(i, j int) bool {
		if result.Spans[i].OffsetMS == result.Spans[j].OffsetMS {
			return result.Spans[i].NodeID < result.Spans[j].NodeID
		}
		return result.Spans[i].OffsetMS < result.Spans[j].OffsetMS
	})
	return result
}

func distribution(samples []spanSample) LatencyDistribution {
	values := make([]float64, 0, len(samples))
	errors := 0
	for _, sample := range samples {
		values = append(values, sample.durationMS)
		if sample.failed {
			errors++
		}
	}
	sort.Float64s(values)
	mean := 0.0
	for _, value := range values {
		mean += value
	}
	mean /= float64(len(values))
	return LatencyDistribution{Count: len(values), ErrorCount: errors, MeanMS: mean, P50MS: percentile(values, 50), P95MS: percentile(values, 95), P99MS: percentile(values, 99), MaxMS: values[len(values)-1]}
}

func exclusiveDurationMS(parent *graphSpan, children []*graphSpan) float64 {
	intervals := make([][2]int64, 0, len(children))
	for _, child := range children {
		intervals = append(intervals, [2]int64{child.start, child.end})
	}
	sort.Slice(intervals, func(i, j int) bool { return intervals[i][0] < intervals[j][0] })
	covered := int64(0)
	for index := 0; index < len(intervals); {
		start, end := intervals[index][0], intervals[index][1]
		index++
		for index < len(intervals) && intervals[index][0] <= end {
			if intervals[index][1] > end {
				end = intervals[index][1]
			}
			index++
		}
		covered += end - start
	}
	return float64(parent.end-parent.start-covered) / 1e6
}

func childrenByParent(spans []*graphSpan) map[string][]*graphSpan {
	children := make(map[string][]*graphSpan)
	for _, span := range spans {
		children[span.parentID] = append(children[span.parentID], span)
	}
	return children
}

func overlapsMeasurementWindows(start, end int64, windows []MeasurementWindow) bool {
	for _, window := range windows {
		if time.Unix(0, end).After(window.Start) && time.Unix(0, start).Before(window.End) {
			return true
		}
	}
	return false
}

func firstOverlappingWindow(spans []*graphSpan, windows []MeasurementWindow) int {
	for index, window := range windows {
		for _, span := range spans {
			if overlapsMeasurementWindows(span.start, span.end, []MeasurementWindow{window}) {
				return index
			}
		}
	}
	return -1
}

func sortedTraceIDs(traces map[string][]*graphSpan) []string {
	ids := make([]string, 0, len(traces))
	for id := range traces {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

func findSpan(spans []*graphSpan, id string) *graphSpan {
	for _, span := range spans {
		if span.spanID == id {
			return span
		}
	}
	return nil
}

func durationMS(start, end int64) float64 { return float64(end-start) / 1e6 }
func abs(value float64) float64 {
	if value < 0 {
		return -value
	}
	return value
}
