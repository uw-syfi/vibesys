package telemetry

import (
	"fmt"
	"sort"
	"strings"
	"unicode/utf8"
)

const (
	minimumBoxInnerWidth = 45
	callGraphGap         = 2
	traceGroupBarWidth   = 150
)

type TraceRenderOptions struct {
	MaxRoots        int
	MaxNodesPerRoot int
	TimelineWidth   int
}

type callTree struct {
	node     TraceGraphNode
	children []*callTree
}

type textBlock struct {
	lines      []string
	width      int
	rootCenter int
}

// RenderTraceGraph produces deterministic, bounded text for human inspection.
func RenderTraceGraph(report TraceGraphReport, options TraceRenderOptions) (string, error) {
	if report.SchemaVersion != TraceGraphSchemaVersion {
		return "", fmt.Errorf("trace graph schema_version must be %d", TraceGraphSchemaVersion)
	}
	if options.MaxRoots <= 0 || options.MaxNodesPerRoot <= 0 || options.TimelineWidth < 10 {
		return "", fmt.Errorf("trace render limits must be positive and timeline width must be at least 10")
	}
	roots := append([]TraceRootGraph(nil), report.Roots...)
	sort.Slice(roots, func(i, j int) bool {
		if roots[i].Latency.P95MS == roots[j].Latency.P95MS {
			return roots[i].Service+roots[i].Operation < roots[j].Service+roots[j].Operation
		}
		return roots[i].Latency.P95MS > roots[j].Latency.P95MS
	})
	var output strings.Builder
	fmt.Fprintf(&output, "TRACE GRAPH v%d  workload=%s\n", report.SchemaVersion, report.WorkloadName)
	fmt.Fprintf(&output, "quality: eligible=%d/%d excluded=%d\n", report.Quality.EligibleTraces, report.Quality.CapturedTraces, report.Quality.ExcludedTraces)
	fmt.Fprintf(&output, "transport: paired=%d unmatched_client=%d unmatched_server=%d async=%d\n\n", report.Quality.MatchedClientServerPairs, report.Quality.UnmatchedClientSpans, report.Quality.UnmatchedServerSpans, report.Quality.AsyncRelationships)
	rootLimit := minInt(len(roots), options.MaxRoots)
	for rootIndex := 0; rootIndex < rootLimit; rootIndex++ {
		if rootIndex > 0 {
			output.WriteString(strings.Repeat("═", traceGroupBarWidth))
			output.WriteString("\n\n")
		}
		renderRoot(&output, roots[rootIndex], options)
	}
	if omitted := len(roots) - rootLimit; omitted > 0 {
		fmt.Fprintf(&output, "... %d %s omitted\n", omitted, plural(omitted, "root group", "root groups"))
	}
	output.WriteString("Legend: inclusive is wall time; exclusive subtracts the union of direct-child intervals; critical contribution attributes root wall time without overlap.\n")
	return output.String(), nil
}

func renderRoot(output *strings.Builder, root TraceRootGraph, options TraceRenderOptions) {
	fmt.Fprintf(output, "ROOT  %s: %s  traces=%d errors=%d\n", root.Service, root.Operation, root.TraceCount, root.ErrorCount)
	fmt.Fprintf(output, "latency mean=%.2fms p50=%.2fms p95=%.2fms p99=%.2fms max=%.2fms\n\n", root.Latency.MeanMS, root.Latency.P50MS, root.Latency.P95MS, root.Latency.P99MS, root.Latency.MaxMS)
	output.WriteString("CALL GRAPH\n\n")
	nodes := append([]TraceGraphNode(nil), root.Nodes...)
	sort.Slice(nodes, func(i, j int) bool { return nodes[i].Path < nodes[j].Path })
	nodeLimit := minInt(len(nodes), options.MaxNodesPerRoot)
	for _, tree := range buildCallTrees(nodes[:nodeLimit]) {
		block := renderCallTree(tree)
		for _, line := range block.lines {
			output.WriteString(strings.TrimRight(line, " "))
			output.WriteByte('\n')
		}
	}
	if omitted := len(nodes) - nodeLimit; omitted > 0 {
		fmt.Fprintf(output, "... %d %s omitted\n", omitted, plural(omitted, "node", "nodes"))
	}
	renderCriticalPath(output, root, options.MaxNodesPerRoot)
	fmt.Fprintf(output, "\nREPRESENTATIVE WATERFALL  trace=%s  duration=%.2fms\n", root.Representative.TraceID, root.Representative.DurationMS)
	renderWaterfall(output, root.Representative, options.TimelineWidth)
	output.WriteByte('\n')
}

func renderCriticalPath(output *strings.Builder, root TraceRootGraph, limit int) {
	critical := root.CriticalPath
	fmt.Fprintf(output, "\nCRITICAL PATH  algorithm=%s  scope=%s  traces=%d  async_excluded=%d\n", critical.Algorithm, critical.Scope, critical.TraceCount, critical.AsyncRelationshipsExcluded)
	output.WriteString("CONTRIBUTORS\n")
	contributorLimit := minInt(len(critical.Nodes), limit)
	for index, node := range critical.Nodes[:contributorLimit] {
		fmt.Fprintf(output, "%2d. %s: %s  mean=%.2fms p95=%.2fms selected=%d/%d\n", index+1, node.Service, node.Operation, node.Contribution.MeanMS, node.Contribution.P95MS, node.Contribution.Count, critical.TraceCount)
	}
	if omitted := len(critical.Nodes) - contributorLimit; omitted > 0 {
		fmt.Fprintf(output, "... %d critical %s omitted\n", omitted, plural(omitted, "contributor", "contributors"))
	}
	fmt.Fprintf(output, "\nREPRESENTATIVE PATH  trace=%s  duration=%.2fms\n", critical.Representative.TraceID, critical.Representative.DurationMS)
	nodesByID := make(map[string]TraceGraphNode, len(root.Nodes))
	for _, node := range root.Nodes {
		nodesByID[node.ID] = node
	}
	segmentLimit := minInt(len(critical.Representative.Segments), limit)
	for index, segment := range critical.Representative.Segments[:segmentLimit] {
		node := nodesByID[segment.NodeID]
		box := renderCriticalSegmentBox(node, segment)
		for _, line := range box {
			output.WriteString(line)
			output.WriteByte('\n')
		}
		if index+1 < segmentLimit {
			center := runeWidth(box[0]) / 2
			fmt.Fprintf(output, "%s│ then\n%s▼\n", strings.Repeat(" ", center), strings.Repeat(" ", center))
		}
	}
	if omitted := len(critical.Representative.Segments) - segmentLimit; omitted > 0 {
		fmt.Fprintf(output, "... %d critical path %s omitted\n", omitted, plural(omitted, "segment", "segments"))
	}
}

func renderCriticalSegmentBox(node TraceGraphNode, segment CriticalPathSegment) []string {
	label := node.Service + ": " + node.Operation
	metrics := fmt.Sprintf("offset: %.2f ms   duration: %.2f ms", segment.OffsetMS, segment.DurationMS)
	innerWidth := maxInt(minimumBoxInnerWidth, maxInt(runeWidth(label), runeWidth(metrics))+2)
	contentWidth := innerWidth - 2
	return []string{
		"┌" + strings.Repeat("─", innerWidth) + "┐",
		"│ " + padRight(label, contentWidth) + " │",
		"│ " + padRight(metrics, contentWidth) + " │",
		"└" + strings.Repeat("─", innerWidth) + "┘",
	}
}

func buildCallTrees(nodes []TraceGraphNode) []*callTree {
	byPath := make(map[string]*callTree, len(nodes))
	for _, node := range nodes {
		copy := node
		byPath[node.Path] = &callTree{node: copy}
	}
	var roots []*callTree
	for _, tree := range byPath {
		parentPath := pathParent(tree.node.Path)
		parent := byPath[parentPath]
		if parent == nil {
			roots = append(roots, tree)
			continue
		}
		parent.children = append(parent.children, tree)
	}
	var sortTree func(*callTree)
	sortTree = func(tree *callTree) {
		sort.Slice(tree.children, func(i, j int) bool {
			return tree.children[i].node.Service+tree.children[i].node.Operation <
				tree.children[j].node.Service+tree.children[j].node.Operation
		})
		for _, child := range tree.children {
			sortTree(child)
		}
	}
	sort.Slice(roots, func(i, j int) bool { return roots[i].node.Path < roots[j].node.Path })
	for _, root := range roots {
		sortTree(root)
	}
	return roots
}

func pathParent(path string) string {
	index := strings.LastIndex(path, " > ")
	if index < 0 {
		return ""
	}
	return path[:index]
}

func renderCallTree(tree *callTree) textBlock {
	box := renderNodeBox(tree.node)
	if len(tree.children) == 0 {
		return textBlock{lines: box, width: runeWidth(box[0]), rootCenter: runeWidth(box[0]) / 2}
	}
	children := make([]textBlock, 0, len(tree.children))
	childrenWidth := 0
	for _, child := range tree.children {
		block := renderCallTree(child)
		children = append(children, block)
		childrenWidth += block.width
	}
	childrenWidth += callGraphGap * (len(children) - 1)
	boxWidth := runeWidth(box[0])
	width := maxInt(boxWidth, childrenWidth)
	rootCenter := width / 2
	lines := indentLines(box, rootCenter-boxWidth/2, width)
	childOffset := (width - childrenWidth) / 2
	childCenters := make([]int, len(children))
	offset := childOffset
	for index, child := range children {
		childCenters[index] = offset + child.rootCenter
		offset += child.width + callGraphGap
	}
	lines = append(lines, connectorLines(width, rootCenter, childCenters)...)
	maxHeight := 0
	for _, child := range children {
		maxHeight = maxInt(maxHeight, len(child.lines))
	}
	for row := 0; row < maxHeight; row++ {
		canvas := blankCanvas(width)
		offset = childOffset
		for _, child := range children {
			if row < len(child.lines) {
				placeRunes(canvas, offset, child.lines[row])
			}
			offset += child.width + callGraphGap
		}
		lines = append(lines, string(canvas))
	}
	return textBlock{lines: lines, width: width, rootCenter: rootCenter}
}

func renderNodeBox(node TraceGraphNode) []string {
	label := node.Service + ": " + node.Operation
	metrics := fmt.Sprintf("inclusive: %.2f ms   exclusive: %.2f ms", node.Inclusive.MeanMS, node.Exclusive.MeanMS)
	innerWidth := maxInt(minimumBoxInnerWidth, maxInt(runeWidth(label), runeWidth(metrics))+2)
	contentWidth := innerWidth - 2
	return []string{
		"┌" + strings.Repeat("─", innerWidth) + "┐",
		"│ " + padRight(label, contentWidth) + " │",
		"│ " + padRight(metrics, contentWidth) + " │",
		"└" + strings.Repeat("─", innerWidth) + "┘",
	}
}

func connectorLines(width, rootCenter int, childCenters []int) []string {
	if len(childCenters) == 1 {
		calls := blankCanvas(width)
		placeRunes(calls, rootCenter, "│ calls")
		arrow := blankCanvas(width)
		placeRunes(arrow, childCenters[0], "▼")
		return []string{string(calls), string(arrow)}
	}
	vertical := blankCanvas(width)
	placeRunes(vertical, rootCenter, "│")
	branch := blankCanvas(width)
	first, last := childCenters[0], childCenters[len(childCenters)-1]
	for index := first + 1; index < last; index++ {
		branch[index] = '─'
	}
	branch[first], branch[last], branch[rootCenter] = '┌', '┐', '┴'
	calls := blankCanvas(width)
	arrows := blankCanvas(width)
	for _, center := range childCenters {
		placeRunes(calls, center, "│ calls")
		placeRunes(arrows, center, "▼")
	}
	return []string{string(vertical), string(branch), string(calls), string(arrows)}
}

func renderWaterfall(output *strings.Builder, trace RepresentativeTrace, width int) {
	labelWidth := 0
	for _, span := range trace.Spans {
		labelWidth = maxInt(labelWidth, runeWidth(span.Service+" "+span.Operation))
	}
	fmt.Fprintf(output, "TIME →  0 ms%s%.2f ms\n\n", strings.Repeat(" ", maxInt(1, labelWidth+width-8)), trace.DurationMS)
	for _, span := range trace.Spans {
		label := span.Service + " " + span.Operation
		fmt.Fprintf(output, "%-*s [%s] %.2f ms\n", labelWidth, label, timeline(span.OffsetMS, span.DurationMS, trace.DurationMS, width), span.DurationMS)
	}
}

func timeline(offset, duration, total float64, width int) string {
	if total <= 0 {
		return strings.Repeat(" ", width)
	}
	start := int(offset / total * float64(width))
	end := int((offset + duration) / total * float64(width))
	if start < 0 {
		start = 0
	}
	if end <= start {
		end = start + 1
	}
	if end > width {
		end = width
	}
	if start >= width {
		start = width - 1
	}
	return strings.Repeat(" ", start) + strings.Repeat("=", end-start) + strings.Repeat(" ", width-end)
}

func indentLines(lines []string, offset, width int) []string {
	result := make([]string, 0, len(lines))
	for _, line := range lines {
		canvas := blankCanvas(width)
		placeRunes(canvas, offset, line)
		result = append(result, string(canvas))
	}
	return result
}

func blankCanvas(width int) []rune { return []rune(strings.Repeat(" ", width)) }

func placeRunes(canvas []rune, offset int, value string) {
	for index, character := range []rune(value) {
		position := offset + index
		if position >= 0 && position < len(canvas) {
			canvas[position] = character
		}
	}
}

func padRight(value string, width int) string {
	return value + strings.Repeat(" ", maxInt(0, width-runeWidth(value)))
}

func runeWidth(value string) int { return utf8.RuneCountInString(value) }

func minInt(left, right int) int {
	if left < right {
		return left
	}
	return right
}

func maxInt(left, right int) int {
	if left > right {
		return left
	}
	return right
}

func plural(count int, singular, plural string) string {
	if count == 1 {
		return singular
	}
	return plural
}
