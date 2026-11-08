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
	// Unit is what the metric is counted in, as the task declares it: a label for people that
	// nothing converts. Empty is unknown (the task declares none, or the result predates units),
	// and an unknown unit is no match for any other.
	Unit string
}

// Observation is a named series of auxiliary readings kept beside the scored metric. It is never
// part of a verdict.
type Observation struct {
	Name   string
	Values []float64
	// Unit is what the values are counted in, as Metric.Unit says it. Empty is unknown.
	Unit string
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
	// Objectives is what a measurement of a multi-objective task is judged on, and the zero value
	// for any other task.
	Objectives Objectives
	// Services is the server each sample ran on, for a task with a job-scoped server: nil, or one
	// per sample, in the order of Samples.
	Services []SampleService
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
	// CandidateImage is the exact digest of the candidate's image the result was taken against, for
	// a task of two images, and empty otherwise. It is recorded and takes no part in the key: a
	// dependency the candidate changes is part of the candidate, as its code is.
	CandidateImage string
}

// ResultFilter selects results. Fields combine with AND and the zero value matches every result.
type ResultFilter struct {
	Commit Commit
	Key    JobKey
}

// PassKey is the key of the check whose pass the result holds, and false for a result that holds
// none. A check result holds the pass its own key names. A measurement that ran its check in its own
// job holds the pass of the check job on the same commit, task version, environment and schema
// version, so a later check is answered by it. A measurement that linked a pass saved by another
// job holds none of its own: that pass is the other job's.
func (r Result) PassKey() (JobKey, bool) {
	switch o := r.Outcome.(type) {
	case CheckOutcome:
		return r.Key, true
	case MeasureOutcome:
		if o.CheckJob != r.Job {
			return "", false
		}
		key, err := NewJobKey(KeyInputs{
			Kind: JobCheck, Commit: r.Commit, TaskVersion: r.TaskVersion,
			Environment: r.Environment, Settings: CheckSettings{}, SchemaVersion: r.SchemaVersion,
		})
		return key, err == nil
	default:
		return "", false
	}
}
