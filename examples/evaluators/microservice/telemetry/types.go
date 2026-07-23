// Package telemetry defines the evaluator-owned contract for internal latency evidence.
package telemetry

import (
	"fmt"
	"math"
	"time"
)

const (
	ReportSchemaVersion  = 1
	RequestSchemaVersion = 1
)

// MeasurementWindow bounds the network activity included in one measured trial.
type MeasurementWindow struct {
	Start time.Time `json:"start"`
	End   time.Time `json:"end"`
}

// CollectionRequest is passed to an external collector after benchmark execution.
type CollectionRequest struct {
	SchemaVersion int                 `json:"schema_version"`
	WorkloadName  string              `json:"workload_name"`
	WorkloadHash  string              `json:"workload_hash"`
	Windows       []MeasurementWindow `json:"measurement_windows"`
}

// LatencyRow is a normalized service, span, or datastore latency distribution.
type LatencyRow struct {
	Name       string   `json:"name"`
	Count      int      `json:"count"`
	ErrorCount int      `json:"error_count"`
	MeanMS     *float64 `json:"mean_ms,omitempty"`
	P50MS      *float64 `json:"p50_ms,omitempty"`
	P95MS      *float64 `json:"p95_ms,omitempty"`
	P99MS      *float64 `json:"p99_ms,omitempty"`
	MaxMS      *float64 `json:"max_ms,omitempty"`
}

// Report is the normalized OTel artifact consumed by VibeSys and profiler tools.
type Report struct {
	SchemaVersion int                 `json:"schema_version"`
	Source        string              `json:"source"`
	CollectedAt   time.Time           `json:"collected_at"`
	WorkloadName  string              `json:"workload_name"`
	WorkloadHash  string              `json:"workload_hash"`
	Windows       []MeasurementWindow `json:"measurement_windows"`
	SpanCount     int                 `json:"span_count"`
	ErrorCount    int                 `json:"error_count"`
	Services      []LatencyRow        `json:"services_by_p95"`
	Spans         []LatencyRow        `json:"spans_by_p95"`
	Datastores    []LatencyRow        `json:"datastores_by_p95,omitempty"`
}

// ValidateRequest rejects missing or nonsensical benchmark windows.
func ValidateRequest(request CollectionRequest) error {
	if request.SchemaVersion != RequestSchemaVersion {
		return fmt.Errorf("telemetry request schema_version must be %d", RequestSchemaVersion)
	}
	if err := validateIdentityAndWindows(request.WorkloadName, request.WorkloadHash, request.Windows); err != nil {
		return fmt.Errorf("telemetry request: %w", err)
	}
	return nil
}

// ValidateReport ensures configured telemetry fails closed on empty or malformed evidence.
func ValidateReport(report Report) error {
	if report.SchemaVersion != ReportSchemaVersion {
		return fmt.Errorf("telemetry report schema_version must be %d", ReportSchemaVersion)
	}
	if report.Source == "" {
		return fmt.Errorf("telemetry report source must not be empty")
	}
	if report.CollectedAt.IsZero() {
		return fmt.Errorf("telemetry report collected_at must not be empty")
	}
	if err := validateIdentityAndWindows(report.WorkloadName, report.WorkloadHash, report.Windows); err != nil {
		return fmt.Errorf("telemetry report: %w", err)
	}
	if report.SpanCount <= 0 {
		return fmt.Errorf("telemetry report contains no spans in the measurement windows")
	}
	if len(report.Services) == 0 || len(report.Spans) == 0 {
		return fmt.Errorf("telemetry report requires service and span latency rows")
	}
	groups := []struct {
		label string
		rows  []LatencyRow
	}{
		{label: "services_by_p95", rows: report.Services},
		{label: "spans_by_p95", rows: report.Spans},
		{label: "datastores_by_p95", rows: report.Datastores},
	}
	for _, group := range groups {
		label, rows := group.label, group.rows
		seen := make(map[string]struct{}, len(rows))
		for index, row := range rows {
			if err := validateLatencyRow(row); err != nil {
				return fmt.Errorf("telemetry %s[%d]: %w", label, index, err)
			}
			if _, exists := seen[row.Name]; exists {
				return fmt.Errorf("telemetry %s contains duplicate name %q", label, row.Name)
			}
			seen[row.Name] = struct{}{}
		}
	}
	return nil
}

func validateIdentityAndWindows(name, hash string, windows []MeasurementWindow) error {
	if name == "" || hash == "" {
		return fmt.Errorf("workload_name and workload_hash must not be empty")
	}
	if len(windows) == 0 {
		return fmt.Errorf("measurement_windows must not be empty")
	}
	for index, window := range windows {
		if window.Start.IsZero() || window.End.IsZero() || window.End.Before(window.Start) {
			return fmt.Errorf("measurement_windows[%d] is invalid", index)
		}
	}
	return nil
}

func validateLatencyRow(row LatencyRow) error {
	if row.Name == "" {
		return fmt.Errorf("name must not be empty")
	}
	if row.Count <= 0 || row.ErrorCount < 0 || row.ErrorCount > row.Count {
		return fmt.Errorf("invalid count or error_count")
	}
	values := []*float64{row.MeanMS, row.P50MS, row.P95MS, row.P99MS, row.MaxMS}
	for _, value := range values {
		if value == nil || math.IsNaN(*value) || math.IsInf(*value, 0) || *value < 0 {
			return fmt.Errorf("latency values must be finite non-negative numbers")
		}
	}
	if *row.P50MS > *row.P95MS || *row.P95MS > *row.P99MS || *row.P99MS > *row.MaxMS {
		return fmt.Errorf("latency percentiles must satisfy p50 <= p95 <= p99 <= max")
	}
	return nil
}
