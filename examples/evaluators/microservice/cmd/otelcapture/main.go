// Command otelcapture normalizes OTLP JSON spans for a servicebench run.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"vibesys/microservice-evaluator/telemetry"
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
	var top int
	flag.Var(&inputs, "input-json", "OTLP JSON or NDJSON input path (repeatable)")
	flag.StringVar(&requestPath, "request-json", "", "servicebench telemetry request path")
	flag.StringVar(&outputPath, "output-json", "", "normalized telemetry report path")
	flag.IntVar(&top, "top", 20, "maximum rows per latency category")
	flag.Parse()
	if requestPath == "" || outputPath == "" {
		return fmt.Errorf("--request-json and --output-json are required")
	}
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
	if err := writeAtomic(outputPath, append(encoded, '\n')); err != nil {
		return fmt.Errorf("write report: %w", err)
	}
	return nil
}

func writeAtomic(path string, data []byte) error {
	temporary, err := os.CreateTemp(filepath.Dir(path), ".otel-report-*.json")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if _, err := temporary.Write(data); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Chmod(0o644); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporaryPath, path)
}
