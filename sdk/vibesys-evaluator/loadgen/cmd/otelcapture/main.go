// Command otelcapture normalizes OTLP JSON spans into latency evidence for a measurement window:
// a report of service, span and datastore latencies, and optionally a trace graph. Moved here
// from the microservice evaluator; its request and report formats are unchanged.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/fsutil"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/telemetry"
)

type stringList []string

func (values *stringList) String() string { return fmt.Sprint([]string(*values)) }

func (values *stringList) Set(value string) error {
	if value == "" {
		return fmt.Errorf("input path must not be empty")
	}
	*values = append(*values, value)
	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "otelcapture:", err)
		os.Exit(1)
	}
}

func run() error {
	var inputs stringList
	var requestPath string
	var outputPath string
	var traceGraphPath string
	var traceTextPath string
	var top int
	var maxRoots int
	var maxNodes int
	var timelineWidth int
	var settleSeconds float64
	flag.Var(&inputs, "input-json", "OTLP JSON or NDJSON input path (repeatable)")
	flag.StringVar(&requestPath, "request-json", "", "servicebench telemetry request path")
	flag.StringVar(&outputPath, "output-json", "", "normalized telemetry report path")
	flag.StringVar(&traceGraphPath, "trace-graph-json", "", "optional versioned trace graph path")
	flag.StringVar(&traceTextPath, "trace-graph-text", "", "optional rendered trace graph path")
	flag.IntVar(&top, "top", 20, "maximum rows per latency category")
	flag.IntVar(&maxRoots, "trace-max-roots", 10, "maximum root groups in rendered trace text")
	flag.IntVar(&maxNodes, "trace-max-nodes", 30, "maximum nodes per root in rendered trace text")
	flag.IntVar(&timelineWidth, "trace-timeline-width", 48, "representative waterfall width")
	flag.Float64Var(&settleSeconds, "settle-seconds", 0,
		"delay before reading inputs so exporter buffers flush")
	flag.Parse()
	if requestPath == "" || outputPath == "" {
		return fmt.Errorf("--request-json and --output-json are required")
	}
	if traceTextPath != "" && traceGraphPath == "" {
		return fmt.Errorf("--trace-graph-text requires --trace-graph-json")
	}
	if settleSeconds < 0 {
		return fmt.Errorf("--settle-seconds must not be negative")
	}
	time.Sleep(time.Duration(settleSeconds * float64(time.Second)))
	requestData, err := os.ReadFile(requestPath)
	if err != nil {
		return fmt.Errorf("read request: %w", err)
	}
	var request telemetry.CollectionRequest
	if err := json.Unmarshal(requestData, &request); err != nil {
		return fmt.Errorf("decode request: %w", err)
	}
	report, err := telemetry.SummarizeOTLP(request, inputs, top)
	if err != nil {
		return err
	}
	encoded, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return fmt.Errorf("encode report: %w", err)
	}
	if err := fsutil.WriteFileAtomic(outputPath, append(encoded, '\n'), 0o644); err != nil {
		return fmt.Errorf("write report: %w", err)
	}
	if traceGraphPath != "" {
		if err := writeTraceGraphArtifacts(request, inputs, traceGraphPath, traceTextPath, telemetry.TraceRenderOptions{MaxRoots: maxRoots, MaxNodesPerRoot: maxNodes, TimelineWidth: timelineWidth}); err != nil {
			return err
		}
	}
	return nil
}

func writeTraceGraphArtifacts(
	request telemetry.CollectionRequest,
	inputs []string,
	graphPath string,
	textPath string,
	options telemetry.TraceRenderOptions,
) error {
	graph, err := telemetry.BuildTraceGraph(request, inputs)
	if err != nil {
		return err
	}
	encoded, err := json.MarshalIndent(graph, "", "  ")
	if err != nil {
		return fmt.Errorf("encode trace graph: %w", err)
	}
	if err := fsutil.WriteFileAtomic(graphPath, append(encoded, '\n'), 0o644); err != nil {
		return fmt.Errorf("write trace graph: %w", err)
	}
	if textPath == "" {
		return nil
	}
	rendered, err := telemetry.RenderTraceGraph(graph, options)
	if err != nil {
		return err
	}
	if err := fsutil.WriteFileAtomic(textPath, []byte(rendered), 0o644); err != nil {
		return fmt.Errorf("write rendered trace graph: %w", err)
	}
	return nil
}
