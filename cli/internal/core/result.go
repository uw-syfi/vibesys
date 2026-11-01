package core

import "time"

// Direction says which way a metric improves.
type Direction string

// The directions.
const (
	Maximize Direction = "maximize"
	Minimize Direction = "minimize"
)

// Metric is a scored quantity and which way it improves.
type Metric struct {
	Name      string
	Direction Direction
}

// Observation is a named series of auxiliary readings kept beside the scored metric. It is never
// part of a verdict.
type Observation struct {
	Name   string
	Values []float64
}

// Outcome is what a saved result recorded: exactly one of CheckOutcome and MeasureOutcome.
type Outcome interface {
	outcome()
}

// CheckOutcome is a passed correctness check. A failed check saves no result.
type CheckOutcome struct {
	Duration time.Duration
}

// MeasureOutcome is the official measurement of a commit.
type MeasureOutcome struct {
	Metric       Metric
	Samples      []float64
	Observations []Observation
	// CheckJob is the check pass the measurement relies on, run in the same job or reused.
	CheckJob JobID
}

func (CheckOutcome) outcome()   {}
func (MeasureOutcome) outcome() {}

// Result is one saved outcome of a job. It is immutable once saved.
type Result struct {
	// Job produced it and is its identity.
	Job           JobID
	Key           JobKey
	Task          string
	TaskVersion   string
	Commit        Commit
	SchemaVersion uint32
	Environment   Fingerprint
	Machine       MachineFacts
	Load          MachineLoad
	RecordedAt    time.Time
	Outcome       Outcome
}

// ResultFilter selects results. Fields combine with AND and the zero value matches every result.
type ResultFilter struct {
	Commit Commit
	Key    JobKey
}
