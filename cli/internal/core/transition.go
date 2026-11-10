package core

import (
	"maps"
	"math"
	"slices"
	"strings"
)

// next lists the states a job in this state can move to, and is the one place the job state
// machine is written down:
//
//	queued  -> running | cancelled | failed
//	running -> succeeded | failed | cancelled | lost | queued
//
// A queued job fails without running when its environment cannot be reached, and is never lost,
// since queued jobs survive a restart. Only a running job is lost. A running job goes back to
// queued only when its attempt was cut off (see Transition.Cut and MaxAttempts). A final state has
// no way out.
func (s JobState) next() []JobState {
	switch s {
	case JobQueued:
		return []JobState{JobRunning, JobCancelled, JobFailed}
	case JobRunning:
		return []JobState{JobSucceeded, JobFailed, JobCancelled, JobLost, JobQueued}
	default:
		return nil
	}
}

// CanMoveTo reports whether a job may move from this state to another in one step.
func (s JobState) CanMoveTo(to JobState) bool {
	return slices.Contains(s.next(), to)
}

// Apply moves the job as the transition asks and returns it, leaving j as it was. It is the one
// rule for what a transition may do, so every Store applies the same one.
//
// It fails with a *Failure. FailureConflict means the job is no longer in the state the
// transition moves from, such as a cancel that lost a race with the job finishing. Everything
// else is FailureInvalidInput naming the field: a transition for another job, a move the state
// machine does not allow, a missing time, or an effect that does not belong to the move:
//
//   - the environment is pinned by the move to running and by nothing else, and so is a new key
//     for a check or measure job, which then runs under the key of the environment it pinned;
//   - a result is saved by the move to succeeded and by nothing else, and a check or measure job
//     cannot succeed without one: the result must be this job's, of this commit and key, taken in
//     the pinned environment at the current schema version, and of the job's own kind. A
//     measurement needs a metric with a direction, finite samples and a check pass to name, so a
//     measurement exists only for a check that passed;
//   - a cancel reason goes with cancelled only, and an exit code with succeeded or failed;
//   - a cut goes with the move from running back to queued and with nothing else, and only while
//     the job has had fewer than MaxAttempts attempts counting the one cut off. That move keeps the
//     attempt in Attempts and unpins the environment, which the next start pins again.
func (j Job) Apply(t Transition) (Job, error) {
	if t.Job != j.ID {
		return Job{}, failf(FailureInvalidInput, "transition: for job %q, applied to job %q", t.Job, j.ID)
	}
	if t.From != j.State {
		return Job{}, failf(FailureConflict, "job %s is %s, not %s", j.ID, j.State, t.From)
	}
	if !t.From.CanMoveTo(t.To) {
		return Job{}, failf(FailureInvalidInput, "transition: a job cannot move from %s to %s", t.From, t.To)
	}
	if t.At.IsZero() {
		return Job{}, failf(FailureInvalidInput, "transition: no time")
	}
	if j.Spec.Settings == nil {
		return Job{}, failf(FailureInvalidInput, "job %s: no settings", j.ID)
	}
	kind := j.Spec.Settings.Kind()
	if err := checkEffects(j, kind, t); err != nil {
		return Job{}, err
	}

	out := j
	out.State = t.To
	at := t.At
	switch t.To {
	case JobRunning:
		out.StartedAt = &at
		pinned := *t.Environment
		pinned.Facts = maps.Clone(pinned.Facts)
		out.Environment = &pinned
		if t.Key != "" {
			out.Key = t.Key
		}
	case JobQueued:
		past := PastAttempt{EndedAt: at, Cut: t.Cut}
		if j.StartedAt != nil {
			past.StartedAt = *j.StartedAt
		}
		out.Attempts = append(slices.Clone(j.Attempts), past)
		out.StartedAt, out.Environment = nil, nil
	default:
		out.FinishedAt = &at
	}
	if t.ExitCode != nil {
		code := *t.ExitCode
		out.ExitCode = &code
	}
	if t.To == JobFailed {
		out.FailedStage = t.FailedStage
	}
	if t.To == JobCancelled {
		out.CancelReason = t.CancelReason
	}
	if t.CheckJob != "" {
		out.CheckJob = t.CheckJob
	}
	if t.Result != nil {
		out.ResultJob = j.ID
		if measured, ok := t.Result.Outcome.(MeasureOutcome); ok {
			out.CheckJob = measured.CheckJob
		}
	}
	return out, nil
}

func checkEffects(j Job, kind JobKind, t Transition) error {
	if t.To == JobRunning && t.Environment != nil {
		if err := t.Environment.validatePinned(); err != nil {
			return err
		}
	}
	switch {
	case t.To == JobRunning && t.Environment == nil:
		return failf(FailureInvalidInput, "transition: the move to running pins the environment, and none is set")
	case t.To != JobRunning && t.Environment != nil:
		return failf(FailureInvalidInput, "transition: the environment is set only by the move to running, not to %s", t.To)
	case t.Key != "" && t.To != JobRunning:
		return failf(FailureInvalidInput, "transition: a key is set only by the move to running, not to %s", t.To)
	case t.Key != "" && kind == JobCommand:
		return failf(FailureInvalidInput, "transition: a command job has no key")
	case t.CancelReason != "" && t.To != JobCancelled:
		return failf(FailureInvalidInput, "transition: a cancel reason goes with cancelled, not %s", t.To)
	case t.ExitCode != nil && t.To != JobSucceeded && t.To != JobFailed:
		return failf(FailureInvalidInput, "transition: an exit code goes with succeeded or failed, not %s", t.To)
	case t.FailedStage != "" && t.To != JobFailed:
		return failf(FailureInvalidInput, "transition: a failed stage goes with failed, not %s", t.To)
	case t.FailedStage != "" && kind == JobCommand:
		return failf(FailureInvalidInput, "transition: a command job has no stages")
	case t.FailedStage != "" && !t.FailedStage.Valid():
		return failf(FailureInvalidInput, "transition: failed stage %q is not one of the stages", t.FailedStage)
	case t.To == JobQueued && t.Cut == nil:
		return failf(FailureInvalidInput, "transition: the move back to queued says how the attempt was cut off, and none is set")
	case t.To != JobQueued && t.Cut != nil:
		return failf(FailureInvalidInput, "transition: a cut goes with the move back to queued, not %s", t.To)
	case t.Cut != nil && !t.Cut.Cause().Valid():
		return failf(FailureInvalidInput, "transition: cut cause %q is not one of the causes", t.Cut.Cause())
	case t.To == JobQueued && len(j.Attempts)+1 >= MaxAttempts:
		return failf(FailureInvalidInput, "transition: job %s had %d attempts, the most it gets, and is not queued again", j.ID, len(j.Attempts)+1)
	case t.CheckJob != "" && kind != JobMeasure:
		return failf(FailureInvalidInput, "transition: only a measure job links a check pass, not a %s job", kind)
	case t.Result != nil && t.To != JobSucceeded:
		return failf(FailureInvalidInput, "transition: a result is saved only by the move to succeeded, not %s", t.To)
	case t.To == JobSucceeded && kind != JobCommand && t.Result == nil:
		return failf(FailureInvalidInput, "transition: a %s job cannot succeed without a result", kind)
	case t.To == JobSucceeded && kind == JobCommand && t.Result != nil:
		return failf(FailureInvalidInput, "transition: a command job saves no result")
	}
	if t.Result == nil {
		return nil
	}
	if err := checkSavedResult(j, kind, *t.Result); err != nil {
		return err
	}
	if measured, ok := t.Result.Outcome.(MeasureOutcome); ok && t.CheckJob != "" && t.CheckJob != measured.CheckJob {
		return failf(FailureInvalidInput, "transition: check job %q disagrees with the measurement's check job %q", t.CheckJob, measured.CheckJob)
	}
	return nil
}

func checkSavedResult(j Job, kind JobKind, r Result) error {
	switch {
	case r.Job != j.ID:
		return failf(FailureInvalidInput, "result: of job %q, saved for job %q", r.Job, j.ID)
	case r.Commit != j.Spec.Commit:
		return failf(FailureInvalidInput, "result: of commit %q, saved for a job on %q", r.Commit, j.Spec.Commit)
	case r.Key != j.Key:
		return failf(FailureInvalidInput, "result: key %q is not the job's key %q", r.Key, j.Key)
	case r.SchemaVersion != SchemaVersion:
		return failf(FailureInvalidInput, "result: schema version %d, this build writes %d", r.SchemaVersion, SchemaVersion)
	case j.Environment == nil || !r.Environment.Equal(*j.Environment):
		return failf(FailureInvalidInput, "result: not taken in the environment the job pinned")
	}
	switch outcome := r.Outcome.(type) {
	case CheckOutcome:
		if kind != JobCheck {
			return failf(FailureInvalidInput, "result: a check outcome for a %s job", kind)
		}
	case MeasureOutcome:
		if kind != JobMeasure {
			return failf(FailureInvalidInput, "result: a measure outcome for a %s job", kind)
		}
		return checkMeasurement(outcome)
	case TraceOutcome:
		if kind != JobTrace {
			return failf(FailureInvalidInput, "result: a trace outcome for a %s job", kind)
		}
		return checkTrace(outcome)
	default:
		return failf(FailureInvalidInput, "result: outcome %T is not a check, measure or trace outcome", r.Outcome)
	}
	return nil
}

func checkTrace(t TraceOutcome) error {
	switch {
	case t.ArtifactsDir == "":
		return failf(FailureInvalidInput, "result: a trace names no artifacts directory")
	case len(t.Files) > MaxTraceFiles:
		return failf(FailureInvalidInput, "result: a trace lists %d files, more than %d", len(t.Files), MaxTraceFiles)
	case t.Duration < 0:
		return failf(FailureInvalidInput, "result: a trace ran for a negative time (%v)", t.Duration)
	}
	for i, name := range t.Files {
		if name == "" || name == "." || name == ".." || strings.Contains(name, "/") {
			return failf(FailureInvalidInput, "result: trace file %d (%q) is not a name in the trace directory", i, name)
		}
	}
	return nil
}

func checkMeasurement(m MeasureOutcome) error {
	switch {
	case m.Metric.Name == "":
		return failf(FailureInvalidInput, "result: the metric has no name")
	case m.Metric.Direction != Maximize && m.Metric.Direction != Minimize:
		return failf(FailureInvalidInput, "result: metric %q has direction %q, not %q or %q", m.Metric.Name, m.Metric.Direction, Maximize, Minimize)
	case len(m.Samples) == 0:
		return failf(FailureInvalidInput, "result: a measurement has no samples")
	case m.CheckJob == "":
		return failf(FailureInvalidInput, "result: a measurement names no check pass, and one is saved only for a check that passed")
	}
	for i, sample := range m.Samples {
		if math.IsNaN(sample) || math.IsInf(sample, 0) {
			return failf(FailureInvalidInput, "result: sample %d is not finite (%v)", i, sample)
		}
	}
	return nil
}
