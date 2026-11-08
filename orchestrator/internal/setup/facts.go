// Package setup derives the state of the six checks a task passes before a run may optimize it,
// from the facts one read of the repository and the job store gives, and holds what the host does
// with those reads: the latest observation of each run and the cadence that decides when to read.
//
// Everything here is data in and data out. Derive reads no clock and no file, Cadence takes the
// time as an argument, and Store is a map under a mutex. The reads themselves belong to the daemon.
package setup

import (
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// Relation says how a job's commit stands to the base commit, as the reader found it in git.
type Relation uint8

// The relations. Unknown is a commit whose object is gone: it never counts.
const (
	RelationUnspecified Relation = iota
	RelationBase
	RelationDescendant
	RelationAncestor
	RelationUnrelated
	RelationUnknown
)

// JobKind is the kind of a job the checks read. Command jobs are not facts here.
type JobKind uint8

// The kinds.
const (
	KindUnspecified JobKind = iota
	KindCheck
	KindMeasure
)

// JobState is a job's lifecycle state.
type JobState uint8

// The states.
const (
	JobUnspecified JobState = iota
	JobQueued
	JobRunning
	JobSucceeded
	JobFailed
	JobCancelled
	JobLost
)

// Stage is the step of a job a failure happened in, as the CLI recorded it. StageUnrecorded covers a
// failure with no stage (checkout, image, bad output) and a job stored before stages were kept.
type Stage uint8

// The stages.
const (
	StageUnrecorded Stage = iota
	StageBuild
	StageCheck
	StageMeasure
)

// Base is the repository's HEAD at the read and the task's version as committed there ("" when the
// task is absent from it).
type Base struct{ Commit, Version string }

// Objective is one objective the task declares. NoiseFloor is its own relative noise, nil when it
// declares none.
type Objective struct {
	Name       string
	Unit       string
	NoiseFloor *float64
}

// Declarations is what the task's manifest declares.
type Declarations struct {
	Objectives        []Objective
	CheckDeclared     bool
	BenchmarkDeclared bool
}

// Job is a check or measure job.
type Job struct {
	ID          string
	Kind        JobKind
	State       JobState
	Commit      string
	Version     string
	ExitCode    *int32
	FailedStage Stage
	Relation    Relation
	// CodeChanged is true for a descendant of the base whose change touches a path outside the
	// task's directory.
	CodeChanged bool
	SubmittedAt time.Time
	FinishedAt  time.Time
}

// Series is a named list of readings.
type Series struct {
	Name   string
	Values []float64
}

// Measurement is a commit's pooled measurement. Samples are the scored metric's; Observations are
// every other objective's readings.
type Measurement struct {
	Commit       string
	Version      string
	Metric       string
	Unit         string
	Value        float64
	Samples      []float64
	Observations []Series
}

// Facts is everything Derive reads. Version is the working tree's task version, which every fact is
// held to.
type Facts struct {
	Task         string
	Version      string
	Card         core.TaskCard
	Decl         Declarations
	Base         Base
	Jobs         []Job
	Measurements []Measurement
}
