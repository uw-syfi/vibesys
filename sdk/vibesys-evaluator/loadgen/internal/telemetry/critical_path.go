package telemetry

import (
	"fmt"
	"math"
	"sort"
)

const (
	CriticalPathAlgorithm = "wall_clock_active_leaf_v1"
	criticalPathScope     = "synchronous_request"
)

type criticalSpan struct {
	span       *graphSpan
	id         string
	path       string
	start, end int64
	depth      int
	children   []*criticalSpan
}

type criticalTraceSegment struct {
	path       string
	start, end int64
	failed     bool
}

type traceCriticalPath struct {
	traceID       string
	rootStart     int64
	rootEnd       int64
	rootFailed    bool
	segments      []criticalTraceSegment
	asyncExcluded int
}

func analyzeCriticalPath(trace eligibleTrace) (traceCriticalPath, error) {
	visibleByID := make(map[string]*criticalSpan)
	parentByID := make(map[string]string)
	asyncExcluded := 0
	for _, span := range trace.spans {
		if trace.paired[span.spanID] {
			continue
		}
		start, end := span.start, span.end
		parentID := span.parentID
		if parent := findSpan(trace.spans, parentID); parent != nil && trace.paired[parent.spanID] {
			if trace.pairs[parent.spanID] == span.spanID {
				start, end = parent.start, parent.end
			}
			parentID = parent.parentID
		}
		for parent := findSpan(trace.spans, parentID); parent != nil && trace.paired[parent.spanID]; parent = findSpan(trace.spans, parentID) {
			parentID = parent.parentID
		}
		visibleByID[span.spanID] = &criticalSpan{span: span, id: span.spanID, path: trace.paths[span.spanID], start: start, end: end}
		parentByID[span.spanID] = parentID
	}

	var root *criticalSpan
	for spanID, span := range visibleByID {
		parent := visibleByID[parentByID[spanID]]
		if parent == nil {
			if root != nil {
				return traceCriticalPath{}, fmt.Errorf("trace %s critical path has multiple visible roots", trace.id)
			}
			root = span
			continue
		}
		if parent.span.kind == "producer" || span.span.kind == "consumer" {
			asyncExcluded++
			continue
		}
		parent.children = append(parent.children, span)
	}
	if root == nil {
		return traceCriticalPath{}, fmt.Errorf("trace %s critical path has no visible root", trace.id)
	}
	setCriticalDepths(root, 0)
	for _, span := range trace.spans {
		asyncExcluded += len(span.links)
	}

	segments := criticalSegments(root)
	segments = mergeCriticalSegments(segments)
	total := int64(0)
	for _, segment := range segments {
		total += segment.end - segment.start
	}
	if total != root.end-root.start {
		return traceCriticalPath{}, fmt.Errorf("trace %s critical path covers %d ns, want %d ns", trace.id, total, root.end-root.start)
	}
	return traceCriticalPath{traceID: trace.id, rootStart: root.start, rootEnd: root.end, rootFailed: root.span.failed, segments: segments, asyncExcluded: asyncExcluded}, nil
}

func setCriticalDepths(span *criticalSpan, depth int) {
	span.depth = depth
	for _, child := range span.children {
		setCriticalDepths(child, depth+1)
	}
}

func criticalSegments(root *criticalSpan) []criticalTraceSegment {
	spans := collectCriticalSpans(root)
	boundaries := []int64{root.start, root.end}
	for _, span := range spans {
		boundaries = append(boundaries, span.start, span.end)
	}
	sort.Slice(boundaries, func(i, j int) bool { return boundaries[i] < boundaries[j] })
	boundaries = compactInt64s(boundaries)
	segments := make([]criticalTraceSegment, 0, len(boundaries)-1)
	for index := 0; index+1 < len(boundaries); index++ {
		start, end := boundaries[index], boundaries[index+1]
		if end <= start {
			continue
		}
		active := make(map[*criticalSpan]bool)
		for _, span := range spans {
			if span.start <= start && span.end >= end {
				active[span] = true
			}
		}
		var selected *criticalSpan
		for span := range active {
			if hasActiveCriticalChild(span, active) {
				continue
			}
			if selected == nil || span.end > selected.end ||
				(span.end == selected.end && span.depth > selected.depth) ||
				(span.end == selected.end && span.depth == selected.depth && span.id < selected.id) {
				selected = span
			}
		}
		if selected != nil {
			segments = append(segments, criticalTraceSegment{path: selected.path, start: start, end: end, failed: selected.span.failed})
		}
	}
	return segments
}

func collectCriticalSpans(root *criticalSpan) []*criticalSpan {
	result := []*criticalSpan{root}
	for _, child := range root.children {
		result = append(result, collectCriticalSpans(child)...)
	}
	return result
}

func compactInt64s(values []int64) []int64 {
	result := values[:0]
	for _, value := range values {
		if len(result) == 0 || result[len(result)-1] != value {
			result = append(result, value)
		}
	}
	return result
}

func hasActiveCriticalChild(span *criticalSpan, active map[*criticalSpan]bool) bool {
	for _, child := range span.children {
		if active[child] {
			return true
		}
	}
	return false
}

func mergeCriticalSegments(segments []criticalTraceSegment) []criticalTraceSegment {
	merged := make([]criticalTraceSegment, 0, len(segments))
	for _, segment := range segments {
		if segment.end <= segment.start {
			continue
		}
		last := len(merged) - 1
		if last >= 0 && merged[last].path == segment.path && merged[last].end == segment.start && merged[last].failed == segment.failed {
			merged[last].end = segment.end
			continue
		}
		merged = append(merged, segment)
	}
	return merged
}

func materializeCriticalPath(aggregate *graphRootAggregate, ids map[string]string, representativeID string) CriticalPathSummary {
	samplesByPath := make(map[string][]spanSample)
	durationSamples := make([]spanSample, 0, len(aggregate.criticalPaths))
	asyncExcluded := 0
	var representative traceCriticalPath
	for _, critical := range aggregate.criticalPaths {
		durationSamples = append(durationSamples, spanSample{durationMS: durationMS(critical.rootStart, critical.rootEnd), failed: critical.rootFailed})
		asyncExcluded += critical.asyncExcluded
		perTrace := make(map[string]spanSample)
		for _, segment := range critical.segments {
			sample := perTrace[segment.path]
			sample.durationMS += durationMS(segment.start, segment.end)
			sample.failed = sample.failed || segment.failed
			perTrace[segment.path] = sample
		}
		for path, sample := range perTrace {
			samplesByPath[path] = append(samplesByPath[path], sample)
		}
		if critical.traceID == representativeID {
			representative = critical
		}
	}
	nodes := make([]CriticalPathNodeContribution, 0, len(samplesByPath))
	for path, samples := range samplesByPath {
		node := aggregate.nodes[path]
		nodes = append(nodes, CriticalPathNodeContribution{
			NodeID: ids[path], Path: path, Service: node.service, Operation: node.operation,
			Contribution: distribution(samples),
		})
	}
	sort.Slice(nodes, func(i, j int) bool {
		if nodes[i].Contribution.MeanMS == nodes[j].Contribution.MeanMS {
			return nodes[i].Path < nodes[j].Path
		}
		return nodes[i].Contribution.MeanMS > nodes[j].Contribution.MeanMS
	})
	result := CriticalPathSummary{
		Algorithm: CriticalPathAlgorithm, Scope: criticalPathScope, TraceCount: len(aggregate.criticalPaths),
		AsyncRelationshipsExcluded: asyncExcluded, Duration: distribution(durationSamples), Nodes: nodes,
		Representative: RepresentativeCriticalPath{TraceID: representative.traceID, DurationMS: durationMS(representative.rootStart, representative.rootEnd)},
	}
	for _, segment := range representative.segments {
		result.Representative.Segments = append(result.Representative.Segments, CriticalPathSegment{
			NodeID: ids[segment.path], OffsetMS: durationMS(representative.rootStart, segment.start), DurationMS: durationMS(segment.start, segment.end),
		})
	}
	return result
}

func validateCriticalPath(root TraceRootGraph) error {
	critical := root.CriticalPath
	if critical.Algorithm != CriticalPathAlgorithm || critical.Scope != criticalPathScope {
		return fmt.Errorf("trace graph root %s:%s has unsupported critical path", root.Service, root.Operation)
	}
	if critical.TraceCount != root.TraceCount || critical.Duration.Count != root.TraceCount || len(critical.Nodes) == 0 {
		return fmt.Errorf("trace graph root %s:%s has invalid critical path counts", root.Service, root.Operation)
	}
	if critical.Representative.TraceID != root.Representative.TraceID || critical.Representative.DurationMS != root.Representative.DurationMS {
		return fmt.Errorf("trace graph root %s:%s has mismatched representative critical path", root.Service, root.Operation)
	}
	validNodes := make(map[string]bool, len(root.Nodes))
	for _, node := range root.Nodes {
		validNodes[node.ID] = true
	}
	nodesByID := make(map[string]TraceGraphNode, len(root.Nodes))
	for _, node := range root.Nodes {
		nodesByID[node.ID] = node
	}
	seenContributors := make(map[string]bool, len(critical.Nodes))
	for _, node := range critical.Nodes {
		graphNode, exists := nodesByID[node.NodeID]
		if !exists || seenContributors[node.NodeID] || node.Path != graphNode.Path || node.Service != graphNode.Service || node.Operation != graphNode.Operation || node.Contribution.Count <= 0 || node.Contribution.Count > critical.TraceCount || !validLatencyDistribution(node.Contribution) {
			return fmt.Errorf("trace graph root %s:%s has invalid critical path contributor", root.Service, root.Operation)
		}
		seenContributors[node.NodeID] = true
	}
	if !validLatencyDistribution(critical.Duration) {
		return fmt.Errorf("trace graph root %s:%s has invalid critical path duration", root.Service, root.Operation)
	}
	cursor := 0.0
	for _, segment := range critical.Representative.Segments {
		if !validNodes[segment.NodeID] || !seenContributors[segment.NodeID] || segment.DurationMS <= 0 || math.IsNaN(segment.OffsetMS) || math.IsInf(segment.OffsetMS, 0) || math.IsNaN(segment.DurationMS) || math.IsInf(segment.DurationMS, 0) || abs(segment.OffsetMS-cursor) > 1e-9 {
			return fmt.Errorf("trace graph root %s:%s has invalid critical path segment", root.Service, root.Operation)
		}
		cursor += segment.DurationMS
	}
	if abs(cursor-critical.Representative.DurationMS) > 1e-9 {
		return fmt.Errorf("trace graph root %s:%s critical path does not cover representative duration", root.Service, root.Operation)
	}
	return nil
}

func validLatencyDistribution(value LatencyDistribution) bool {
	values := []float64{value.MeanMS, value.P50MS, value.P95MS, value.P99MS, value.MaxMS}
	for _, current := range values {
		if current < 0 || math.IsNaN(current) || math.IsInf(current, 0) {
			return false
		}
	}
	return value.Count > 0 && value.ErrorCount >= 0 && value.ErrorCount <= value.Count &&
		value.MeanMS <= value.MaxMS && value.P50MS <= value.P95MS && value.P95MS <= value.P99MS && value.P99MS <= value.MaxMS
}
