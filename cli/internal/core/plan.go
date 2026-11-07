package core

import (
	"fmt"
	"math"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

// PlanStatus is where a plan stands. Agents choose it: no rule orders the statuses.
type PlanStatus string

// The plan statuses.
const (
	PlanOpen     PlanStatus = "open"
	PlanChosen   PlanStatus = "chosen"
	PlanWon      PlanStatus = "won"
	PlanParked   PlanStatus = "parked"
	PlanRejected PlanStatus = "rejected"
)

// PlanStatuses lists every plan status, in the order a listing names them.
func PlanStatuses() []PlanStatus {
	return []PlanStatus{PlanOpen, PlanChosen, PlanWon, PlanParked, PlanRejected}
}

// Valid says whether the status is one of the five.
func (s PlanStatus) Valid() bool {
	for _, known := range PlanStatuses() {
		if s == known {
			return true
		}
	}
	return false
}

// Closes says whether closing a plan may set this status: any status but open does. The rule
// goes no further, so no status has to follow another.
func (s PlanStatus) Closes() bool { return s.Valid() && s != PlanOpen }

// GainReference is what a gain bound is a share of.
type GainReference string

// The things a gain bound can be a share of.
const (
	// GainHeadline is the headline: the metric value of the task's current result, or the base's
	// value before a first promotion.
	GainHeadline GainReference = "headline"
)

// GainReferences lists every reference, in the order a listing names them.
func GainReferences() []GainReference { return []GainReference{GainHeadline} }

// Valid says whether the reference is one of those.
func (r GainReference) Valid() bool {
	for _, known := range GainReferences() {
		if r == known {
			return true
		}
	}
	return false
}

// GainBound is the most a plan could gain, as its planner states it: a share of a reference, 0.05
// for five percent of the headline.
type GainBound struct {
	// Share is at least 0 and finite. It may exceed 1.
	Share      float64
	RelativeTo GainReference
}

// CheckGainBound says whether a bound is a finite share of at least 0 of a reference there is. The
// error is a *Failure of kind FailureInvalidInput that names the field.
func CheckGainBound(field string, b GainBound) error {
	switch {
	case math.IsNaN(b.Share) || math.IsInf(b.Share, 0) || b.Share < 0:
		return failf(FailureInvalidInput, "%s: a share is a finite number of at least 0, got %v", field, b.Share)
	case !b.RelativeTo.Valid():
		return failf(FailureInvalidInput, "%s: %q is not something a bound is a share of", field, b.RelativeTo)
	}
	return nil
}

// PlanComment is a comment on a plan. Comments carry decisions and results.
type PlanComment struct {
	Text  string
	At    time.Time
	Agent string
}

// PlanTransition is one time a plan took a status, and who made it so.
type PlanTransition struct {
	Status PlanStatus
	// Origin is who set the status, and when: the call's run and agent, and the daemon's clock.
	Origin RecordOrigin
}

// Plan is stored the way an issue is: a number the store assigns, a free-form markdown body and
// comments.
type Plan struct {
	Number    PlanNumber
	Title     string
	Body      string
	Status    PlanStatus
	Comments  []PlanComment
	CreatedAt time.Time
	UpdatedAt time.Time
	// GainBound is the plan's bound on its possible gain, nil when it states none.
	GainBound *GainBound
	// Workstream is the workstream the plan belongs to, 0 for none.
	Workstream WorkstreamNumber
	// Origin is who made the plan and when, nil on a plan saved before store format 6.
	Origin *RecordOrigin
	// Transitions are the statuses the plan took, oldest first: its creation as open, then each
	// edit that set a status it did not have. Empty on a plan saved before store format 6, and then
	// they start with its first status change after that.
	Transitions []PlanTransition
}

// CheckHistory says whether the plan's origin and transitions are as a store keeps them: each
// origin has its time, each transition has a status and differs from the one before it, the last
// transition is the plan's status, and a plan with an origin starts open, as that origin made it.
// The error is a *Failure of kind FailureInvalidInput that names the field.
func (p Plan) CheckHistory() error {
	if p.Origin != nil {
		if err := p.Origin.Check("origin"); err != nil {
			return err
		}
	}
	for i, t := range p.Transitions {
		if !t.Status.Valid() {
			return failf(FailureInvalidInput, "transitions[%d].status: %q is not a plan status", i, t.Status)
		}
		if err := t.Origin.Check(fmt.Sprintf("transitions[%d].origin", i)); err != nil {
			return err
		}
		if i > 0 && t.Status == p.Transitions[i-1].Status {
			return failf(FailureInvalidInput, "transitions[%d].status: the plan already had status %q", i, t.Status)
		}
	}
	if n := len(p.Transitions); n > 0 && p.Transitions[n-1].Status != p.Status {
		return failf(FailureInvalidInput, "status: %q is not the status of the last transition, %q", p.Status, p.Transitions[n-1].Status)
	}
	if p.Origin != nil && (len(p.Transitions) == 0 || p.Transitions[0].Status != PlanOpen || !p.Transitions[0].Origin.Same(*p.Origin)) {
		return failf(FailureInvalidInput, "transitions[0]: a plan made at its origin starts open, set by that origin")
	}
	return nil
}

// NewPlan is a request to create a plan. The store assigns the number and the open status.
type NewPlan struct {
	Title     string
	Body      string
	GainBound *GainBound
	// Workstream is the workstream the plan joins, 0 for none. It is one the task has.
	Workstream WorkstreamNumber
	// Origin is who makes the plan and when, which is its creation time.
	Origin RecordOrigin
}

// PlanEdit changes a plan. A nil field is left as it is, and the whole edit is one change.
type PlanEdit struct {
	Title  *string
	Body   *string
	Status *PlanStatus
	// GainBound replaces the plan's bound. A bound is replaced and never cleared.
	GainBound *GainBound
	Comment   *PlanComment
	// Workstream moves the plan to a workstream the task has. A plan's workstream is moved and never
	// cleared.
	Workstream *WorkstreamNumber
	// Origin is who makes the edit and when, which is the plan's new update time, and the origin of
	// the transition when the edit sets a status the plan does not have.
	Origin RecordOrigin
}

// PlanFilter selects plans. Fields combine with AND and the zero value matches every plan.
type PlanFilter struct {
	Statuses []PlanStatus
	// Text matches a plan whose title or body contains it.
	Text string
}

// The limits on what a plan holds. They keep a runaway write from filling the store, and are far
// above what a plan or a decision takes.
const (
	MaxPlanTitleBytes   = 256
	MaxPlanBodyBytes    = 256 << 10
	MaxPlanCommentBytes = 64 << 10
)

// CheckPlanTitle says whether a title is one non-empty line of text within its limit. The error
// is a *Failure of kind FailureInvalidInput that names the field.
func CheckPlanTitle(field, title string) error {
	switch {
	case strings.TrimSpace(title) == "":
		return failf(FailureInvalidInput, "%s: a plan needs a title", field)
	case strings.ContainsAny(title, "\r\n"):
		return failf(FailureInvalidInput, "%s: a title is one line", field)
	case len(title) > MaxPlanTitleBytes:
		return failf(FailureInvalidInput, "%s: %d bytes is over the limit of %d", field, len(title), MaxPlanTitleBytes)
	case !utf8.ValidString(title):
		return failf(FailureInvalidInput, "%s: not valid UTF-8", field)
	}
	return nil
}

// CheckPlanBody says whether a body is text within its limit. A body may be empty: a title can be
// a whole plan.
func CheckPlanBody(field, body string) error {
	switch {
	case len(body) > MaxPlanBodyBytes:
		return failf(FailureInvalidInput, "%s: %d bytes is over the limit of %d", field, len(body), MaxPlanBodyBytes)
	case !utf8.ValidString(body):
		return failf(FailureInvalidInput, "%s: not valid UTF-8", field)
	}
	return nil
}

// CheckPlanComment says whether a comment is non-empty text within its limit.
func CheckPlanComment(field, text string) error {
	switch {
	case strings.TrimSpace(text) == "":
		return failf(FailureInvalidInput, "%s: a comment is not empty", field)
	case len(text) > MaxPlanCommentBytes:
		return failf(FailureInvalidInput, "%s: %d bytes is over the limit of %d", field, len(text), MaxPlanCommentBytes)
	case !utf8.ValidString(text):
		return failf(FailureInvalidInput, "%s: not valid UTF-8", field)
	}
	return nil
}

// PlanView is a plan with the jobs submitted against it, oldest first.
type PlanView struct {
	Plan Plan
	Jobs []Job
	// Verdicts is how each commit of a linked job compares with the result branch's head, for the
	// commits that could be compared. A commit with no entry has none to give: there is no head,
	// or a side is not measured, or the sides were measured in two environments.
	Verdicts map[Commit]Verdict
}

// LinkedTo returns the jobs that link to the plan, in the order given.
func LinkedTo(number PlanNumber, jobs []Job) []Job {
	var out []Job
	for _, j := range jobs {
		for _, p := range j.Plans {
			if p == number {
				out = append(out, j)
				break
			}
		}
	}
	return out
}

// GainCheck answers whether every plan that is still open or parked states a bound below a share.
type GainCheck struct {
	// Share is the share asked about.
	Share float64
	// Counted is how many plans are open or parked.
	Counted int
	// Blockers are the counted plans that state no bound, or one that is not below Share, by number.
	Blockers []Plan
}

// AllUnder says whether no counted plan blocks: true also when none is counted.
func (c GainCheck) AllUnder() bool { return len(c.Blockers) == 0 }

// CheckGain asks, of plans, whether each one that is open or parked states a bound below share of
// its reference. Plans in another status are not asked. A plan that states no bound is not under
// any share. Share is not checked here; a caller refuses one that is not a finite number.
func CheckGain(plans []Plan, share float64) GainCheck {
	check := GainCheck{Share: share}
	for _, p := range plans {
		if p.Status != PlanOpen && p.Status != PlanParked {
			continue
		}
		check.Counted++
		if p.GainBound == nil || !(p.GainBound.Share < share) {
			check.Blockers = append(check.Blockers, p)
		}
	}
	sort.Slice(check.Blockers, func(i, j int) bool { return check.Blockers[i].Number < check.Blockers[j].Number })
	return check
}
