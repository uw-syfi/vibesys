package core

import (
	"fmt"
	"slices"
	"time"
)

// Priority orders the queued jobs that may start: a job of a higher priority is considered before
// one of a lower, and among jobs of one priority the scheduler's policy orders them. It never stops
// a job that runs, and never lets a job past what the resources allow: it orders only the jobs that
// may start. The set is closed; the zero value is no priority of its own (a job inherits its plans',
// a plan is normal).
type Priority string

// The priorities, lowest first.
const (
	PriorityLow    Priority = "low"
	PriorityNormal Priority = "normal"
	PriorityHigh   Priority = "high"
)

// Priorities lists every priority, lowest first.
func Priorities() []Priority { return []Priority{PriorityLow, PriorityNormal, PriorityHigh} }

// Valid says whether the priority is one of the three.
func (p Priority) Valid() bool { return slices.Contains(Priorities(), p) }

// Level is the priority's place, 0 for low to 2 for high. No priority of its own is normal.
func (p Priority) Level() int {
	if i := slices.Index(Priorities(), p); i >= 0 {
		return i
	}
	return 1
}

// OrNormal is the priority, or normal when it has none.
func (p Priority) OrNormal() Priority {
	if p.Valid() {
		return p
	}
	return PriorityNormal
}

// PriorityAging is how long a queued job waits before its priority counts one level higher, and
// again for each further level, up to high. A low job counts as normal after one, and as high after
// two; a normal job as high after one. So among the jobs that may start on a resource, a job that has
// waited twice this long is considered before every job submitted after it whatever their priority,
// under a policy that keeps arrival order within a level: no job is starved by a stream of jobs of
// a higher priority.
const PriorityAging = time.Hour

// AgedPriority is the priority a queued job counts as after it has waited this long.
func AgedPriority(p Priority, waited time.Duration) Priority {
	level := p.Level()
	if waited > 0 {
		level += int(waited / PriorityAging)
	}
	ps := Priorities()
	return ps[min(level, len(ps)-1)]
}

// EffectivePriority is the priority a job waits with before aging: its own, when it has one, and
// otherwise the highest of the plans it is linked to, of those given, and normal with none.
func EffectivePriority(job Job, plans []Plan) Priority {
	if job.Priority.Valid() {
		return job.Priority
	}
	out := Priority("")
	for _, p := range plans {
		if !slices.Contains(job.Plans, p.Number) {
			continue
		}
		if out == "" || p.Priority.Level() > out.Level() {
			out = p.Priority.OrNormal()
		}
	}
	return out.OrNormal()
}

// SetJobPriority sets a queued job's own priority, or with no priority lets it inherit its plans'
// again. A reason is required: Agent, At and Reason are kept in a note on the job.
type SetJobPriority struct {
	Priority Priority
	Reason   string
	Agent    string
	At       time.Time
}

func (SetJobPriority) jobControl() {}

// PriorityName is a priority in words, with the zero value as "inherited".
func PriorityName(p Priority) string {
	if p == "" {
		return "inherited"
	}
	return string(p)
}

// PriorityChange is the text that records a change of priority, from one to another, and why.
func PriorityChange(from, to Priority, reason string) string {
	return fmt.Sprintf("priority %s -> %s: %s", PriorityName(from), PriorityName(to), reason)
}

// CheckPriorityReason says whether a reason for a change of priority is given and valid.
func CheckPriorityReason(field, reason string) error {
	if reason == "" {
		return failf(FailureInvalidInput, "%s: a change of priority needs a reason", field)
	}
	return CheckReason(field, reason)
}

func (j Job) setPriority(c SetJobPriority) (Job, error) {
	if j.State != JobQueued {
		return Job{}, failf(FailureConflict, "job %s is %s: only a queued job's priority can change", j.ID, j.State)
	}
	if c.Priority != "" && !c.Priority.Valid() {
		return Job{}, failf(FailureInvalidInput, "priority: %q is not a priority (low, normal or high)", c.Priority)
	}
	if c.At.IsZero() {
		return Job{}, failf(FailureInvalidInput, "priority: no time")
	}
	if err := CheckPriorityReason("reason", c.Reason); err != nil {
		return Job{}, err
	}
	out := j
	out.Priority = c.Priority
	out.Notes = append(cloneNotes(j.Notes), Note{Text: PriorityChange(j.Priority, c.Priority, c.Reason), At: c.At, Agent: c.Agent})
	return out, nil
}
