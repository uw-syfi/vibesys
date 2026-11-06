package core

import (
	"fmt"
	"strings"
	"time"
	"unicode/utf8"
)

// Steering a queue: what an operator or a lead agent says about which waiting job goes next, on
// top of what the scheduler decides from the resources. It never stops a job that runs.
//
// A job is held (JobHold) or not; a daemon drains (Drain) or not. Who asks is the caller's own word
// (CallContext.Agent): nothing here checks who may steer, and the skills carry the convention.

// JobHold says who held a queued job, when and why. A held job keeps its place in the queue, holds
// back no other job, and is not started until it is released. A job that starts is not held.
type JobHold struct {
	Agent  string
	Reason string
	At     time.Time
}

// WaitDetail is why a held job waits, in words, for its wait reason.
func (h JobHold) WaitDetail() string {
	return "held" + byWhom(h.Agent, h.Reason) + "; `vibesys-cli release` lets it start in its turn"
}

// Drain says the daemon starts no queued job: a job that runs goes on, and the queued jobs keep
// their places until the daemon is told to resume. A daemon keeps it across a restart.
type Drain struct {
	Agent  string
	Reason string
	Since  time.Time
}

// WaitDetail is why a queued job waits while the daemon drains, in words, for its wait reason.
func (d Drain) WaitDetail() string {
	return "the daemon is draining" + byWhom(d.Agent, d.Reason) + "; `vibesys-cli daemon resume` starts jobs again"
}

// byWhom is " by agent: reason", leaving out what is empty.
func byWhom(agent, reason string) string {
	var b strings.Builder
	if agent != "" {
		b.WriteString(" by " + agent)
	}
	if reason != "" {
		b.WriteString(": " + reason)
	}
	return b.String()
}

// MaxReasonBytes bounds the reason given for a hold, a drain or a priority change.
const MaxReasonBytes = 4096

// CheckReason says whether a reason is one line of valid text within MaxReasonBytes. An empty
// reason is valid where none is required. The error is a *Failure of kind FailureInvalidInput that
// names the field.
func CheckReason(field, reason string) error {
	switch {
	case len(reason) > MaxReasonBytes:
		return failf(FailureInvalidInput, "%s: %d bytes is over the limit of %d", field, len(reason), MaxReasonBytes)
	case !utf8.ValidString(reason):
		return failf(FailureInvalidInput, "%s: not valid UTF-8", field)
	case strings.ContainsAny(reason, "\r\n"):
		return failf(FailureInvalidInput, "%s: a reason is one line", field)
	}
	return nil
}

// JobControl is a change to how a job waits in the queue. The set is closed.
type JobControl interface {
	jobControl()
}

// HoldJob holds a queued job.
type HoldJob struct{ Hold JobHold }

// ReleaseJob lets a held job start in its turn. Agent and At say who released it and when, for the
// note it leaves.
type ReleaseJob struct {
	Agent string
	At    time.Time
}

func (HoldJob) jobControl()    {}
func (ReleaseJob) jobControl() {}

// Control applies a change to how a job waits, and returns the job, leaving j as it was. Each
// change that changes something leaves a note on the job, with who asked and when, so the job's
// notes are the history of how it was steered.
//
// It fails with a *Failure: FailureConflict for a hold of a job that is not queued, since only a
// waiting job can be held, and FailureInvalidInput for a change with no time or a reason that is
// not one line within MaxReasonBytes. Releasing a job that is not held changes nothing and is not
// an error, so a release can be repeated.
func (j Job) Control(c JobControl) (Job, error) {
	out := j
	switch c := c.(type) {
	case HoldJob:
		if j.State != JobQueued {
			return Job{}, failf(FailureConflict, "job %s is %s: only a queued job can be held", j.ID, j.State)
		}
		if c.Hold.At.IsZero() {
			return Job{}, failf(FailureInvalidInput, "hold: no time")
		}
		if err := CheckReason("reason", c.Hold.Reason); err != nil {
			return Job{}, err
		}
		hold := c.Hold
		out.Hold = &hold
		out.Notes = append(cloneNotes(j.Notes), Note{Text: "held" + byWhom("", c.Hold.Reason), At: c.Hold.At, Agent: c.Hold.Agent})
	case ReleaseJob:
		if j.Hold == nil {
			return j, nil
		}
		if c.At.IsZero() {
			return Job{}, failf(FailureInvalidInput, "release: no time")
		}
		out.Hold = nil
		out.Notes = append(cloneNotes(j.Notes), Note{Text: fmt.Sprintf("released (held since %s)", j.Hold.At.UTC().Format(time.RFC3339)), At: c.At, Agent: c.Agent})
	default:
		return Job{}, failf(FailureInvalidInput, "control: %T is not a change to how a job waits", c)
	}
	return out, nil
}

func cloneNotes(notes []Note) []Note {
	return append([]Note(nil), notes...)
}
