package api

import (
	"context"
	"time"
)

const WorkloadVersion = 1

type Phase string

const (
	PhaseWarmup      Phase = "warmup"
	PhaseMeasurement Phase = "measurement"
)

type Load struct {
	Model               string  `toml:"model" json:"model"`
	Rate                float64 `toml:"rate" json:"rate"`
	WarmupSeconds       float64 `toml:"warmup_seconds" json:"warmup_seconds"`
	DurationSeconds     float64 `toml:"duration_seconds" json:"duration_seconds"`
	Concurrency         int     `toml:"concurrency" json:"concurrency"`
	TimeoutSeconds      float64 `toml:"timeout_seconds" json:"timeout_seconds"`
	Repetitions         int     `toml:"repetitions" json:"repetitions"`
	Seed                int64   `toml:"seed" json:"seed"`
	MinOfferedRateRatio float64 `toml:"min_offered_rate_ratio" json:"min_offered_rate_ratio"`
}

type ProfileOverride struct {
	Model               *string        `toml:"model"`
	Rate                *float64       `toml:"rate"`
	WarmupSeconds       *float64       `toml:"warmup_seconds"`
	DurationSeconds     *float64       `toml:"duration_seconds"`
	Concurrency         *int           `toml:"concurrency"`
	TimeoutSeconds      *float64       `toml:"timeout_seconds"`
	Repetitions         *int           `toml:"repetitions"`
	Seed                *int64         `toml:"seed"`
	MinOfferedRateRatio *float64       `toml:"min_offered_rate_ratio"`
	ApplicationConfig   map[string]any `toml:"application_config"`
}

func (o ProfileOverride) Apply(load *Load) {
	if o.Model != nil {
		load.Model = *o.Model
	}
	if o.Rate != nil {
		load.Rate = *o.Rate
	}
	if o.WarmupSeconds != nil {
		load.WarmupSeconds = *o.WarmupSeconds
	}
	if o.DurationSeconds != nil {
		load.DurationSeconds = *o.DurationSeconds
	}
	if o.Concurrency != nil {
		load.Concurrency = *o.Concurrency
	}
	if o.TimeoutSeconds != nil {
		load.TimeoutSeconds = *o.TimeoutSeconds
	}
	if o.Repetitions != nil {
		load.Repetitions = *o.Repetitions
	}
	if o.Seed != nil {
		load.Seed = *o.Seed
	}
	if o.MinOfferedRateRatio != nil {
		load.MinOfferedRateRatio = *o.MinOfferedRateRatio
	}
}

type Target struct {
	Name          string            `toml:"name" json:"name"`
	Protocol      string            `toml:"protocol" json:"protocol"`
	Address       string            `toml:"address" json:"address"`
	SessionPolicy string            `toml:"session_policy" json:"session_policy"`
	Settings      map[string]string `toml:"settings" json:"settings,omitempty"`
}

type HTTPRequestSpec struct {
	Method  string            `toml:"method" json:"method"`
	Path    string            `toml:"path" json:"path"`
	Query   map[string]string `toml:"query" json:"query,omitempty"`
	Headers map[string]string `toml:"headers" json:"headers,omitempty"`
	Form    map[string]string `toml:"form" json:"form,omitempty"`
	Body    string            `toml:"body" json:"body,omitempty"`
}

type HTTPResponse struct {
	StatusCode int
	Body       []byte
}

type Expectation struct {
	Statuses                       []int  `toml:"statuses" json:"statuses,omitempty"`
	JSON                           bool   `toml:"json" json:"json,omitempty"`
	TextContains                   string `toml:"text_contains" json:"text_contains,omitempty"`
	JSONStatusIfPresent            *int   `toml:"json_status_if_present" json:"json_status_if_present,omitempty"`
	JSONObjectRequiresStatusOrData bool   `toml:"json_object_requires_status_or_data" json:"json_object_requires_status_or_data,omitempty"`
}

type HeaderCapture struct {
	Name   string `toml:"name" json:"name"`
	Header string `toml:"header" json:"header"`
	Unit   string `toml:"unit" json:"unit"`
}

type Operation struct {
	Name           string           `toml:"name" json:"name"`
	Target         string           `toml:"target" json:"target"`
	Weight         int              `toml:"weight" json:"weight"`
	Tags           []string         `toml:"tags" json:"tags,omitempty"`
	HTTP           *HTTPRequestSpec `toml:"http" json:"http,omitempty"`
	Expect         Expectation      `toml:"expect" json:"expect"`
	CaptureHeaders []HeaderCapture  `toml:"capture_headers" json:"capture_headers,omitempty"`
}

func (o Operation) HasTag(want string) bool {
	for _, tag := range o.Tags {
		if tag == want {
			return true
		}
	}
	return false
}

type Objective struct {
	Name      string   `toml:"name" json:"name"`
	Metric    string   `toml:"metric" json:"metric"`
	Direction string   `toml:"direction" json:"direction"`
	Unit      string   `toml:"unit" json:"unit"`
	Tags      []string `toml:"tags" json:"tags,omitempty"`
}

type Constraints struct {
	MinSuccessRate       *float64 `toml:"min_success_rate" json:"min_success_rate,omitempty"`
	MaxErrorRate         *float64 `toml:"max_error_rate" json:"max_error_rate,omitempty"`
	MinOperationsPerType int      `toml:"min_operations_per_type" json:"min_operations_per_type,omitempty"`
}

type Workload struct {
	Version           int                        `toml:"version" json:"version"`
	Name              string                     `toml:"name" json:"name"`
	Application       string                     `toml:"application" json:"application"`
	Load              Load                       `toml:"load" json:"load"`
	Profiles          map[string]ProfileOverride `toml:"profiles" json:"-"`
	Targets           []Target                   `toml:"targets" json:"targets"`
	Operations        []Operation                `toml:"operations" json:"operations"`
	Objective         Objective                  `toml:"objective" json:"objective"`
	Constraints       Constraints                `toml:"constraints" json:"constraints"`
	ApplicationConfig map[string]any             `toml:"application_config" json:"application_config,omitempty"`
}

type Sample struct {
	Counter int64
	Random  uint64
}

type Invocation struct {
	Target    string
	Operation string
	Payload   any
}

// OperationPlan describes every protocol invocation in one scheduled logical
// operation. State is opaque application-owned data used to validate the
// collected results. The engine, rather than the application, always executes
// and accounts for every invocation.
type OperationPlan struct {
	Invocations []Invocation
	State       any
}

type ProtocolResult struct {
	TransportSuccess bool
	NativeStatus     string
	RequestBytes     int64
	ResponseBytes    int64
	Metadata         map[string][]string
	Payload          any
	ErrorCategory    string
	ErrorMessage     string
	ConnectionKnown  bool
	ConnectionReused bool
}

type ValidationResult struct {
	Success       bool
	ErrorCategory string
	ErrorMessage  string
	CustomTimings map[string]time.Duration
}

type Driver interface {
	Protocol() string
	Open(context.Context, Target) (Client, error)
}

type Client interface {
	Invoke(context.Context, Invocation) ProtocolResult
	Close() error
}

type Runtime interface {
	Invoke(context.Context, Invocation) ProtocolResult
}

type TrialContext struct {
	Index int
	Seed  int64
}

type Application interface {
	Name() string
	Prepare(context.Context, Runtime, TrialContext) (any, error)
	Reset(context.Context, Runtime, TrialContext) error
	BuildOperation(Operation, Sample, any) (OperationPlan, error)
	ValidateOperation(Operation, OperationPlan, []ProtocolResult) ValidationResult
	FinishOperation(OperationPlan)
}

// AccuracyProperty declares one independently reported correctness property.
// Required properties must be explicitly passed before an accuracy run can be
// valid. Optional properties remain false when their prerequisite (for
// example, a candidate lifecycle hook) is unavailable.
type AccuracyProperty struct {
	Name     string
	Required bool
}

// ReadinessProbe is an application-owned request that the shared accuracy
// runner uses to prove that every required endpoint is ready, and that none of
// them remains reachable before a managed candidate is restarted.
type ReadinessProbe struct {
	Name       string
	Invocation Invocation
	Validate   func(ProtocolResult) error
}

type AccuracyContext struct {
	Seed    int64
	Cases   int
	Restart func(context.Context) error
}

type AccuracyRecorder interface {
	AddChecks(int)
	Pass(...string) error
}

// AccuracyApplication owns application-specific fixtures, scenarios, and
// semantic assertions. Transport sessions, readiness polling, lifecycle
// transitions, result reporting, and required-property enforcement remain in
// the shared accuracy framework.
type AccuracyApplication interface {
	Name() string
	Properties() []AccuracyProperty
	ReadinessProbes() []ReadinessProbe
	Check(context.Context, Runtime, AccuracyContext, AccuracyRecorder) error
}

type Observation struct {
	Trial              int                      `json:"trial"`
	Phase              Phase                    `json:"phase"`
	Operation          string                   `json:"operation"`
	Target             string                   `json:"target"`
	Protocol           string                   `json:"protocol"`
	Tags               []string                 `json:"tags,omitempty"`
	ScheduledAt        time.Time                `json:"scheduled_at"`
	DispatchedAt       time.Time                `json:"dispatched_at"`
	SentAt             time.Time                `json:"sent_at"`
	CompletedAt        time.Time                `json:"completed_at"`
	ValidatedAt        time.Time                `json:"validated_at"`
	QueueWait          time.Duration            `json:"-"`
	ClientPrepareTime  time.Duration            `json:"-"`
	ProtocolTime       time.Duration            `json:"-"`
	TotalLatency       time.Duration            `json:"-"`
	ValidationTime     time.Duration            `json:"-"`
	QueueWaitMS        float64                  `json:"queue_wait_ms"`
	ClientPrepareMS    float64                  `json:"client_prepare_ms"`
	ProtocolTimeMS     float64                  `json:"protocol_time_ms"`
	TotalLatencyMS     float64                  `json:"total_latency_ms"`
	ValidationTimeMS   float64                  `json:"validation_time_ms"`
	TransportSuccess   bool                     `json:"transport_success"`
	ApplicationSuccess bool                     `json:"application_success"`
	NativeStatus       string                   `json:"native_status"`
	ErrorCategory      string                   `json:"error_category,omitempty"`
	ErrorMessage       string                   `json:"error_message,omitempty"`
	InvocationCount    int                      `json:"invocation_count"`
	NativeStatuses     []string                 `json:"native_statuses,omitempty"`
	RequestBytes       int64                    `json:"request_bytes"`
	ResponseBytes      int64                    `json:"response_bytes"`
	CustomTimings      map[string]time.Duration `json:"-"`
	CustomTimingsMS    map[string]float64       `json:"custom_timings_ms,omitempty"`
}

func (o *Observation) PopulateDurations() {
	if o.ValidatedAt.IsZero() {
		o.ValidatedAt = o.CompletedAt
	}
	o.QueueWait = o.DispatchedAt.Sub(o.ScheduledAt)
	o.ClientPrepareTime = o.SentAt.Sub(o.DispatchedAt)
	o.ProtocolTime = o.CompletedAt.Sub(o.SentAt)
	o.TotalLatency = o.CompletedAt.Sub(o.ScheduledAt)
	o.ValidationTime = o.ValidatedAt.Sub(o.CompletedAt)
	o.QueueWaitMS = durationMS(o.QueueWait)
	o.ClientPrepareMS = durationMS(o.ClientPrepareTime)
	o.ProtocolTimeMS = durationMS(o.ProtocolTime)
	o.TotalLatencyMS = durationMS(o.TotalLatency)
	o.ValidationTimeMS = durationMS(o.ValidationTime)
	if len(o.CustomTimings) > 0 {
		o.CustomTimingsMS = make(map[string]float64, len(o.CustomTimings))
		for name, value := range o.CustomTimings {
			o.CustomTimingsMS[name] = durationMS(value)
		}
	}
}

func durationMS(value time.Duration) float64 {
	return float64(value) / float64(time.Millisecond)
}
