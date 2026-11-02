package core

import (
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

// PlanComment is a comment on a plan. Comments carry decisions and results.
type PlanComment struct {
	Text  string
	At    time.Time
	Agent string
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
}

// NewPlan is a request to create a plan. The store assigns the number and the open status.
type NewPlan struct {
	Title string
	Body  string
	At    time.Time
}

// PlanEdit changes a plan. A nil field is left as it is, and the whole edit is one change.
type PlanEdit struct {
	Title   *string
	Body    *string
	Status  *PlanStatus
	Comment *PlanComment
	At      time.Time
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
