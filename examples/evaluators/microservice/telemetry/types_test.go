package telemetry

import (
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
