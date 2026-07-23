package telemetry

import (
	"math"
	"strconv"
	"testing"
	"time"
)

func TestValidateReportRejectsInvalidAggregateErrorCount(t *testing.T) {
	for _, errorCount := range []int{-1, 2} {
		t.Run("error_count="+strconv.Itoa(errorCount), func(t *testing.T) {
			report := validReport()
			report.ErrorCount = errorCount
			if err := ValidateReport(report); err == nil {
				t.Fatalf("accepted aggregate error_count=%d", errorCount)
			}
		})
	}
}

func TestValidateReportRejectsNonFiniteOrUnorderedLatency(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*LatencyRow)
	}{
		{"unordered p50 above p95", func(row *LatencyRow) { *row.P50MS = *row.P95MS + 1 }},
		{"unordered p99 above max", func(row *LatencyRow) { *row.P99MS = *row.MaxMS + 1 }},
		{"negative mean", func(row *LatencyRow) { *row.MeanMS = -1 }},
		{"nan p95", func(row *LatencyRow) { *row.P95MS = math.NaN() }},
		{"inf max", func(row *LatencyRow) { *row.MaxMS = math.Inf(1) }},
		{"missing p50", func(row *LatencyRow) { row.P50MS = nil }},
		{"invalid row count", func(row *LatencyRow) { row.Count = 0 }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			report := validReport()
			row := report.Services[0]
			mean, p50, p95, p99, maximum := *row.MeanMS, *row.P50MS, *row.P95MS, *row.P99MS, *row.MaxMS
			row.MeanMS, row.P50MS, row.P95MS = &mean, &p50, &p95
			row.P99MS, row.MaxMS = &p99, &maximum
			test.mutate(&row)
			report.Services = []LatencyRow{row}
			if err := ValidateReport(report); err == nil {
				t.Fatalf("accepted latency row: %s", test.name)
			}
		})
	}
}

func TestValidateReportRejectsDuplicateRowNames(t *testing.T) {
	report := validReport()
	report.Services = append(report.Services, report.Services[0])
	if err := ValidateReport(report); err == nil {
		t.Fatal("accepted duplicate service row names")
	}
}

func validReport() Report {
	mean := 1.0
	p50 := 1.0
	p95 := 1.0
	p99 := 1.0
	maximum := 1.0
	row := LatencyRow{
		Name: "frontend", Count: 1, ErrorCount: 0,
		MeanMS: &mean, P50MS: &p50, P95MS: &p95, P99MS: &p99, MaxMS: &maximum,
	}
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	return Report{
		SchemaVersion: ReportSchemaVersion,
		Source:        "test",
		CollectedAt:   start,
		WorkloadName:  "hotel",
		WorkloadHash:  "abc123",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(time.Second)}},
		SpanCount:     1,
		Services:      []LatencyRow{row},
		Spans:         []LatencyRow{row},
	}
}
