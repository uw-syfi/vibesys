package engine

import "vibesys/microservice-evaluator/api"

const ResultSchemaVersion = 1

type Distribution struct {
	Count int      `json:"count"`
	Mean  *float64 `json:"mean,omitempty"`
	P50   *float64 `json:"p50,omitempty"`
	P90   *float64 `json:"p90,omitempty"`
	P95   *float64 `json:"p95,omitempty"`
	P99   *float64 `json:"p99,omitempty"`
	P999  *float64 `json:"p999,omitempty"`
	Max   *float64 `json:"max,omitempty"`
}

type OperationResult struct {
	Requests        int                     `json:"requests"`
	Successes       int                     `json:"successes"`
	Failures        int                     `json:"failures"`
	LatencyMS       Distribution            `json:"latency_ms"`
	QueueWaitMS     Distribution            `json:"queue_wait_ms"`
	ProtocolTimeMS  Distribution            `json:"protocol_time_ms"`
	CustomTimingsMS map[string]Distribution `json:"custom_timings_ms,omitempty"`
}

type GeneratorReport struct {
	TargetRate        float64      `json:"target_rate"`
	OfferedRate       float64      `json:"offered_rate"`
	MinOfferedRate    float64      `json:"min_offered_rate"`
	SubmittedRequests int          `json:"submitted_requests"`
	MaxQueueDepth     int          `json:"max_queue_depth"`
	SchedulerLagMS    Distribution `json:"scheduler_lag_ms"`
	Sustained         bool         `json:"sustained"`
}

type TrialResult struct {
	Index              int                        `json:"index"`
	Valid              bool                       `json:"valid"`
	InvalidReasons     []string                   `json:"invalid_reasons,omitempty"`
	PrimaryValue       *float64                   `json:"primary_value,omitempty"`
	ElapsedSeconds     float64                    `json:"elapsed_seconds"`
	TotalRequests      int                        `json:"total_requests"`
	SuccessfulRequests int                        `json:"successful_requests"`
	FailedRequests     int                        `json:"failed_requests"`
	SuccessRate        float64                    `json:"success_rate"`
	ErrorRate          float64                    `json:"error_rate"`
	LatencyMS          Distribution               `json:"latency_ms"`
	QueueWaitMS        Distribution               `json:"queue_wait_ms"`
	ProtocolTimeMS     Distribution               `json:"protocol_time_ms"`
	CustomTimingsMS    map[string]Distribution    `json:"custom_timings_ms,omitempty"`
	ByOperation        map[string]OperationResult `json:"by_operation"`
	ErrorsByCategory   map[string]int             `json:"errors_by_category,omitempty"`
	Generator          GeneratorReport            `json:"load_generator"`
}

type Aggregate struct {
	Trials int       `json:"trials"`
	Median *float64  `json:"median,omitempty"`
	MAD    *float64  `json:"mad,omitempty"`
	IQR    *float64  `json:"iqr,omitempty"`
	CI95   []float64 `json:"ci95,omitempty"`
}

type ConstraintResult struct {
	Passed         bool     `json:"passed"`
	Reasons        []string `json:"reasons,omitempty"`
	MinSuccessRate *float64 `json:"min_success_rate,omitempty"`
	MaxErrorRate   *float64 `json:"max_error_rate,omitempty"`
}

type Summary struct {
	SchemaVersion int              `json:"schema_version"`
	EngineVersion string           `json:"engine_version"`
	WorkloadName  string           `json:"workload_name"`
	WorkloadHash  string           `json:"workload_hash"`
	PrimaryValue  *float64         `json:"primary_value,omitempty"`
	PrimaryMetric api.Objective    `json:"primary_metric"`
	Valid         bool             `json:"valid"`
	Trials        []TrialResult    `json:"trials"`
	Aggregate     Aggregate        `json:"aggregate"`
	Constraints   ConstraintResult `json:"constraints"`
}

type RunResult struct {
	Summary      Summary
	Observations []api.Observation
}
