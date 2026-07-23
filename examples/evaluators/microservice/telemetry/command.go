package telemetry

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

// CommandCollector invokes a trusted collector command using the normalized contract.
type CommandCollector struct {
	Command    []string
	OutputPath string
	Timeout    time.Duration
}

// Collect writes a request, invokes the collector, and validates its report.
func (collector CommandCollector) Collect(
	ctx context.Context,
	request CollectionRequest,
) (Report, error) {
	if err := ValidateRequest(request); err != nil {
		return Report{}, err
	}
	if len(collector.Command) == 0 {
		return Report{}, fmt.Errorf("telemetry collector command must not be empty")
	}
	if collector.OutputPath == "" {
		return Report{}, fmt.Errorf("telemetry collector output path must not be empty")
	}
	outputPath, err := filepath.Abs(collector.OutputPath)
	if err != nil {
		return Report{}, fmt.Errorf("resolve telemetry output path: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(outputPath), 0o755); err != nil {
		return Report{}, fmt.Errorf("create telemetry output directory: %w", err)
	}
	if err := os.Remove(outputPath); err != nil && !os.IsNotExist(err) {
		return Report{}, fmt.Errorf("remove stale telemetry report: %w", err)
	}
	requestFile, err := os.CreateTemp("", "servicebench-telemetry-request-*.json")
	if err != nil {
		return Report{}, fmt.Errorf("create telemetry request: %w", err)
	}
	requestPath := requestFile.Name()
	defer os.Remove(requestPath)
	encoder := json.NewEncoder(requestFile)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(request); err != nil {
		requestFile.Close()
		return Report{}, fmt.Errorf("write telemetry request: %w", err)
	}
	if err := requestFile.Close(); err != nil {
		return Report{}, fmt.Errorf("close telemetry request: %w", err)
	}

	commandContext := ctx
	var cancel context.CancelFunc
	if collector.Timeout > 0 {
		commandContext, cancel = context.WithTimeout(ctx, collector.Timeout)
		defer cancel()
	}
	arguments := append([]string(nil), collector.Command[1:]...)
	arguments = append(arguments, "--request-json", requestPath, "--output-json", outputPath)
	command := exec.CommandContext(commandContext, collector.Command[0], arguments...)
	output, err := command.CombinedOutput()
	if err != nil {
		if commandContext.Err() != nil {
			return Report{}, fmt.Errorf("telemetry collector timed out: %w", commandContext.Err())
		}
		return Report{}, fmt.Errorf("telemetry collector failed: %w: %s", err, string(output))
	}
	data, err := os.ReadFile(outputPath)
	if err != nil {
		return Report{}, fmt.Errorf("read telemetry report: %w", err)
	}
	var report Report
	if err := json.Unmarshal(data, &report); err != nil {
		return Report{}, fmt.Errorf("decode telemetry report: %w", err)
	}
	if err := ValidateReport(report); err != nil {
		return Report{}, err
	}
	if err := validateReportRequest(report, request); err != nil {
		return Report{}, err
	}
	return report, nil
}

func validateReportRequest(report Report, request CollectionRequest) error {
	if report.WorkloadName != request.WorkloadName || report.WorkloadHash != request.WorkloadHash {
		return fmt.Errorf("telemetry report workload identity does not match request")
	}
	if len(report.Windows) != len(request.Windows) {
		return fmt.Errorf("telemetry report measurement windows do not match request")
	}
	for index := range request.Windows {
		if !report.Windows[index].Start.Equal(request.Windows[index].Start) ||
			!report.Windows[index].End.Equal(request.Windows[index].End) {
			return fmt.Errorf("telemetry report measurement windows do not match request")
		}
	}
	return nil
}
